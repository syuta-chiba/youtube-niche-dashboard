"""競合候補チャンネルの RSS 巡回 (quota ゼロの広域監視層)。

discovery loop (search.list) が「見つける」係なのに対し、こちらは「見つけた ch を
全員・永久に監視し続ける」係。gate で落ちた ch も一度でも視界に入った ch はプールに
貯め続け、後日跳ねたら再浮上させる — 「一度落ちたら二度と見ない」を無くすための層。

仕組み:
  1. プール構築: rss_pool.json + discovery_seen.json + external_hits.jsonl の
     チャンネルを合流 (priority 観測中 / boosted / 自チャンネルは除外)
  2. 各 ch の RSS フィード (youtube.com/feeds/videos.xml?channel_id=...) を取得。
     **YouTube Data API と別系統なので quota 消費ゼロ**。最新15本の views が取れる
  3. 相対スパイク判定: フィード内の views 中央値 (公開7日超の動画のみ、5本未満なら全部)
     の RSS_SPIKE_MULT 倍 (下限 RSS_SPIKE_FLOOR) を超えた未通知動画を候補に
  4. API 検証 (跳ねた動画だけ・数 unit): videos.list で 長尺(>180s)+likes、
     channels.list で subs → like率 0.4% 未満 (広告疑い) を除外
  5. 生き残りを rss_alerts.json (Slack 用) と外部HITコーパスに書き出し。
     dedup は rss_state.json (video_id 単位・commit 永続)

使い方:
    python rss_patrol.py                # 巡回 + 検証 + 出力
    python rss_patrol.py --max-alerts 5 # Slack に流す件数を絞る (state には全件記録)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import quota_meter
from external_hits import CORPUS_FILE, merge_external_hits

DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def load_channel_ids(path: Path) -> list[str]:
    """priority_channels_check.load_channel_ids と同等 (googleapiclient 依存を避けるため複製。
    本スクリプトは stdlib のみで動く — workflow で pip install 不要)。"""
    ids = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids

NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

API = "https://www.googleapis.com/youtube/v3"

POOL_FILE = "rss_pool.json"     # channel_id → {title, subs, source, added}
STATE_FILE = "rss_state.json"   # channel_id → {alerted: [video_id], fail: n}
ALERTS_FILE = "rss_alerts.json"  # 実行毎再生成 (workflow が Slack へ)
REPORTS_DIR = "rss_reports"
SERIES_FILE = "time_series/rss_video_series.jsonl"  # 動画ごとの日次 views (形で広告を見分ける・view_shape.py)

OWN_CHANNEL_ID = "UCw-ytk4y6y4MntCyeiSa_-w"  # 自チャンネル (皇帝ペンギンラボ) は監視対象外

# 相対スパイク判定 (priority の相対HITと同思想。母数が RSS の最新15本しかないので
# 中央値の信頼度が低め → 倍率は同じ 5 倍、下限 500 も同じ)
RSS_SPIKE_MULT = 5
RSS_SPIKE_FLOOR = 500
MEDIAN_MIN_AGE_DAYS = 7   # 公開直後の動画は views が育ちきっておらず中央値を歪めるので除外
MEDIAN_MIN_SAMPLE = 5
FAIL_DROP = 5             # RSS 取得にこの回数連続失敗した ch はプールから外す (削除ch等)
LIKE_RATE_FLOOR = 0.004   # 検証時の広告疑い絶対床 (discovery_loop と同値)


def _get_api(path: str, params: dict) -> dict:
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        quota_meter.add(100 if path == "search" else 1)
        return json.load(r)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def fetch_feed(channel_id: str, retries: int = 3) -> list[dict] | None:
    """RSS フィード → [{video_id, title, views, published_at, age_days}] (最新15本)。
    YouTube の RSS は一時的な 500/404 を返すことがある (2026-07-21 実測: リトライで通る)
    ので短い backoff 付きで再試行。全滅なら None。quota 消費ゼロ。"""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    root = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                root = ET.fromstring(r.read())
            break
        except urllib.error.HTTPError as e:
            # 404 は「そのチャンネルのフィードが無い」(削除 ch / YouTube 側の一時障害) — 再試行しても変わらないので即諦める。
            # 2026-10-21: YouTube RSS が広範に 404/500 を返した日に 3,323 ch × (3 回 + 待ち 4 秒) で run が 4 時間級になった
            if e.code == 404:
                return None
            if attempt < retries - 1:
                import time
                time.sleep(2)
        except (urllib.error.URLError, ET.ParseError, TimeoutError, OSError):
            if attempt < retries - 1:
                import time
                time.sleep(2)
    if root is None:
        return None
    now = datetime.now(timezone.utc)
    out = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", namespaces=NS)
        title = e.findtext("a:title", namespaces=NS) or ""
        pub = e.findtext("a:published", namespaces=NS) or ""
        stats = e.find(".//media:statistics", NS)
        views = int(stats.get("views", 0)) if stats is not None else 0
        # media:starRating count ≒ like数 (広告チェックの普段like率算出に使う)
        rating = e.find(".//media:starRating", NS)
        likes = int(rating.get("count", 0)) if rating is not None else 0
        try:
            age = max((now - datetime.fromisoformat(pub)).days, 0)
        except ValueError:
            age = 9999
        out.append({"video_id": vid, "title": title, "views": views, "likes": likes,
                    "published_at": pub, "age_days": age})
    return out


def feed_like_rate(entries: list[dict]) -> float | None:
    """フィードの「普段 like率」(合算率: 総likes/総views、公開7日超のみ)。
    広告チェックのベースライン用。views が少なすぎる場合は None。"""
    pool = [e for e in entries if e["age_days"] > MEDIAN_MIN_AGE_DAYS]
    if len(pool) < MEDIAN_MIN_SAMPLE:
        pool = entries
    tv = sum(e["views"] for e in pool)
    tl = sum(e.get("likes", 0) for e in pool)
    if tv < 300:
        return None
    return tl / tv


def feed_median(entries: list[dict]) -> int:
    """フィードの「普段」。公開 MEDIAN_MIN_AGE_DAYS 日超の動画の views 中央値。"""
    seasoned = sorted(e["views"] for e in entries if e["age_days"] > MEDIAN_MIN_AGE_DAYS)
    pool = seasoned if len(seasoned) >= MEDIAN_MIN_SAMPLE else sorted(e["views"] for e in entries)
    if not pool:
        return 0
    mid = len(pool) // 2
    return pool[mid] if len(pool) % 2 else (pool[mid - 1] + pool[mid]) // 2


def build_pool(root: Path) -> dict[str, dict]:
    """プール = rss_pool.json ∪ discovery_seen ∪ 外部HITコーパスの ch。
    priority / boosted 観測中と自チャンネルは除外 (そちらは API で深く見ている)。"""
    pool: dict[str, dict] = load_json(root / POOL_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for cid, e in load_json(root / "discovery_seen.json", {}).items():
        pool.setdefault(cid, {"title": e.get("title", cid), "source": "discovery_seen",
                              "added": e.get("date", today)})
    corpus_path = root / "time_series" / CORPUS_FILE
    if corpus_path.exists():
        for line in corpus_path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = e.get("channel_id")
            if cid:
                pool.setdefault(cid, {"title": e.get("channel", cid), "source": e.get("source", "corpus"),
                                      "added": (e.get("first_seen") or today)[:10]})

    watched = set()
    # rejected_channels.txt = 人間の「違う確定」— 巡回対象から恒久的に外す (行削除で復帰)
    for f in ("priority_channels.txt", "boosted_channels.txt", "rejected_channels.txt"):
        p = root / f
        if p.exists():
            watched |= set(load_channel_ids(p))
    watched.add(OWN_CHANNEL_ID)
    return {cid: meta for cid, meta in pool.items() if cid not in watched}


def verify_spikes(cands: list[dict]) -> list[dict]:
    """跳ねた動画を API で検証: 長尺か / like率 / subs。通過分に検証値を添えて返す。
    quota: videos 1u/50本 + channels 1u/50ch — スパイクがあった時だけ数 unit。"""
    if not cands:
        return []
    vids = [c["video_id"] for c in cands]
    vinfo = {}
    for i in range(0, len(vids), 50):
        data = _get_api("videos", {"part": "snippet,statistics,contentDetails",
                                   "id": ",".join(vids[i:i + 50])})
        for it in data.get("items", []):
            m = DURATION_RE.fullmatch(it["contentDetails"].get("duration", "") or "")
            h, mn, s = ((int(x) if x else 0) for x in (m.groups() if m else (0, 0, 0)))
            vinfo[it["id"]] = {
                "views": int(it["statistics"].get("viewCount", 0)),
                "likes": int(it["statistics"].get("likeCount", 0)),
                "duration_sec": h * 3600 + mn * 60 + s,
            }
    ch_ids = sorted({c["channel_id"] for c in cands})
    subs_map = {}
    for i in range(0, len(ch_ids), 50):
        data = _get_api("channels", {"part": "statistics", "id": ",".join(ch_ids[i:i + 50])})
        for it in data.get("items", []):
            subs_map[it["id"]] = int(it["statistics"].get("subscriberCount", 0))

    out = []
    for c in cands:
        v = vinfo.get(c["video_id"])
        if not v:
            continue
        if 0 < v["duration_sec"] <= 180:
            c["drop"] = "Shorts"
            continue
        like_pct = 100.0 * v["likes"] / v["views"] if v["views"] else 0.0
        if like_pct < LIKE_RATE_FLOOR * 100:
            c["drop"] = f"広告疑い (like率 {like_pct:.2f}%)"
            continue
        subs = subs_map.get(c["channel_id"], 0)
        out.append({**c, "views": v["views"], "likes": v["likes"],
                    "like_pct": round(like_pct, 2), "subs": subs,
                    "score": round(v["views"] / subs, 2) if subs else None})
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-alerts", type=int, default=15, help="Slack に流す上限 (state には全件記録)")
    p.add_argument("--dry-run", action="store_true", help="state/pool を書き換えない")
    args = p.parse_args()

    root = Path(".")
    pool = build_pool(root)
    state: dict = load_json(root / STATE_FILE, {})
    print(f"[info] RSS 巡回プール: {len(pool)} ch (quota 消費ゼロ)", file=sys.stderr)

    # 2026-10-21: フィードの views を捨てずに動画ごとの日次系列として残す (quota ゼロ)。
    # 広告は「台地→崖→尾なし」、おすすめは「初日ピーク→減衰→尾」と形が違うので、
    # 観測 ch でなくても伸び方で見分けられるようにする。判定は view_shape.py。
    import view_shape
    series = view_shape.load_rss_series()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    series_n = 0

    candidates = []
    checked = 0
    for cid, meta in sorted(pool.items()):
        st = state.setdefault(cid, {"alerted": [], "fail": 0})
        entries = fetch_feed(cid)
        if entries is None:
            st["fail"] = st.get("fail", 0) + 1
            if st["fail"] >= FAIL_DROP:
                meta["dead"] = True
            continue
        st["fail"] = 0
        checked += 1
        series_n += view_shape.update_rss_series(series, cid, entries, today)
        med = feed_median(entries)
        # ダッシュボード「発見済みch」タブ用に普段値を維持
        meta["median_views"] = med
        meta["latest_video_at"] = max((e["published_at"] for e in entries), default="")[:10]
        # 言語判定 (ja/en/other) 用のサンプル。ch名だけでは英語名の日本語chを誤るので最新3本の題名を残す (2026-10-24)
        meta["sample_titles"] = [e["title"] for e in entries[:3] if e.get("title")]
        # 直近フィードの like 率 (quota 0)。ダッシュボードの ⚠️盛られ疑い 表示用 (2026-10-26)。
        # likeCount 非公開/全0 は「盛られ」でなく「不明」として None に倒す (誤判定回避)。
        lr = feed_like_rate(entries)
        if lr == 0 and all(e.get("likes", 0) == 0 for e in entries):
            lr = None
        meta["like_rate"] = round(lr, 4) if lr is not None else None
        th = max(RSS_SPIKE_FLOOR, RSS_SPIKE_MULT * med)
        for e in entries:
            if e["views"] >= th and e["video_id"] not in st["alerted"]:
                candidates.append({
                    **e, "channel_id": cid, "channel": meta.get("title", cid),
                    "feed_median": med, "spike_threshold": th,
                    "mult": round(e["views"] / med, 1) if med else None,
                })

    print(f"[info] {checked} ch 巡回 / スパイク候補 {len(candidates)} 件", file=sys.stderr)

    # プール全chの登録者数を更新 (1u/50ch — 500ch でも 10u)。前月比の伸び計算用に
    # subs_log へ日次追記し、35日より古いエントリは月初分だけ残して間引く。
    if os.environ.get("YOUTUBE_API_KEY") and pool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ids = sorted(pool)
        for i in range(0, len(ids), 50):
            try:
                data = _get_api("channels", {"part": "snippet,statistics",
                                             "id": ",".join(ids[i:i + 50])})
            except (urllib.error.URLError, OSError) as e:
                print(f"[warn] subs 更新失敗: {e}", file=sys.stderr)
                break
            for it in data.get("items", []):
                meta = pool.get(it["id"])
                if not meta:
                    continue
                meta["title"] = it["snippet"]["title"]
                meta["subs"] = int(it["statistics"].get("subscriberCount", 0))
                log = meta.setdefault("subs_log", {})
                log[today] = meta["subs"]
                cutoff = (datetime.now(timezone.utc)).strftime("%Y-%m-%d")
                for d in sorted(log):
                    age = (datetime.fromisoformat(cutoff) - datetime.fromisoformat(d)).days
                    if age > 35 and not d.endswith("-01"):
                        del log[d]

    verified = verify_spikes(candidates) if os.environ.get("YOUTUBE_API_KEY") else []
    if candidates and not os.environ.get("YOUTUBE_API_KEY"):
        print("[warn] YOUTUBE_API_KEY 未設定のため検証スキップ (候補は次回に持ち越し)", file=sys.stderr)
        candidates = []

    # dedup 記録 (検証で落ちた分も記録して再通知しない)
    for c in candidates:
        st = state[c["channel_id"]]
        if c["video_id"] not in st["alerted"]:
            st["alerted"].append(c["video_id"])

    verified.sort(key=lambda c: (c["mult"] or 0), reverse=True)
    alerts = []
    for c in verified:
        alerts.append({
            "channel": c["channel"],
            "channel_id": c["channel_id"],
            "video_id": c["video_id"],
            "title": c["title"],
            "url": f"https://www.youtube.com/watch?v={c['video_id']}",
            "views": c["views"],
            "subs": c["subs"],
            "score": c["score"],
            "like_pct": c["like_pct"],
            "age_days": c["age_days"],
            "published_at": c["published_at"],
            "feed_median": c["feed_median"],
            "mult": c["mult"],
            "analysis": (
                f"📡 RSS巡回検知: 普段(直近15本中央値) {c['feed_median']:,}v の"
                + (f"{c['mult']}倍" if c["mult"] else "突出")
                + f" / like率 {c['like_pct']}% 健全 / {c['subs']:,} subs (score {c['score']})"
                + f"\n⏱ 公開{c['age_days']}日前 — gate落ち/未評価チャンネルの再浮上シグナル。"
                + "真似元候補として目視確認を"
            ),
        })

    Path(ALERTS_FILE).write_text(
        json.dumps(alerts[:args.max_alerts], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[info] 検証通過 {len(verified)} 件 → {ALERTS_FILE} (Slack 送信 {min(len(alerts), args.max_alerts)} 件)",
          file=sys.stderr)

    # 検証通過分は外部HITコーパスへ (相対スパイク = rel_hit 扱いで score 2.0 未満も通す)
    if verified and not args.dry_run:
        merge_external_hits(
            root / "time_series" / CORPUS_FILE,
            [{**c, "rel_hit": True} for c in verified], "rss_patrol")

    if not args.dry_run:
        dropped = view_shape.prune_rss_series(series, today)
        view_shape.save_rss_series(series)
        print(f"[info] 動画日次系列: 今日 {series_n} 本記録 / 保持 {len(series)} 本 / 打ち切り {dropped} 本 → {SERIES_FILE}", file=sys.stderr)
        pool_out = load_json(root / POOL_FILE, {})
        pool_out.update({cid: m for cid, m in pool.items()})
        (root / POOL_FILE).write_text(json.dumps(pool_out, ensure_ascii=False, indent=2), encoding="utf-8")
        (root / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        if alerts:
            Path(REPORTS_DIR).mkdir(exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            lines = [f"# RSS 巡回レポート {today}", "",
                     f"- プール {len(pool)} ch / 巡回成功 {checked} / スパイク候補 {len(candidates)} / 検証通過 {len(verified)}", ""]
            for a in alerts:
                lines.append(f"- [{a['channel']}] **{a['title']}** — {a['views']:,}v "
                             f"(普段{a['feed_median']:,}vの{a['mult']}倍 / {a['subs']:,} subs / like {a['like_pct']}%)")
                lines.append(f"  - {a['url']}")
            Path(REPORTS_DIR, f"{today}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    spent = quota_meter.flush(root / "time_series", "rss_patrol")
    print(f"RSS_SUMMARY|pool={len(pool)}|checked={checked}|spikes={len(candidates)}|verified={len(verified)}|api={spent}u")
    return 0


if __name__ == "__main__":
    sys.exit(main())
