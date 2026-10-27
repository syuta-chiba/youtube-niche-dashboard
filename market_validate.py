"""急伸検知した動画の企画テーマを「YouTube 全体」で需要検証する。

監視 ch 内の横断分析 (find_similar_hits) は priority ch の内輪に閉じている。
このスクリプトは search.list で監視枠の外まで検索し、
「同じ企画が世の中の他チャンネルでも当たっているか」を測定する
(HIT 判定はチャンネル相対: 各外部chの「普段」を RSS フィードから quota ゼロで取り、
 普段の5倍以上を HIT とする。score=views/subs 方式は 2026-07-21 に廃止)。

トリガー: priority_channels_check.py が生成する hit_alerts.json（急伸検知の瞬間）。
結果:
  - time_series/market_checks.json に video_id キーでキャッシュ（commit されて永続。
    build_pages_data.py が読んでダッシュボードの横断分析パネルに表示する）
  - --update-alerts 指定時は hit_alerts.json の analysis 欄に 🌍 市場検証行を追記
    （workflow の Slack 通知にそのまま乗る）

quota: search.list = 100 units/動画。--max-videos (default 3) で上限を絞る。
1 日 4 回の cron で急伸検知は稀なので実消費はごく小さい。

Usage:
    python market_validate.py --from-alerts hit_alerts.json --update-alerts
    python market_validate.py --title "Orca 完全版 AI開発IDE" --video-id abc123  # 手動検証
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import quota_meter
from external_hits import CORPUS_FILE, merge_external_hits
from rss_patrol import DURATION_RE, fetch_feed, feed_like_rate, feed_median

# タイトル/ch名に「かな」が含まれるか (日本語圏と海外の分離用)。
# 漢字だけで判定すると中国語動画を日本語圏に誤分類する (2026-07-21 実測) ため、かな必須。
JA_RE = re.compile(r"[ぁ-んァ-ヶ]")

API = "https://www.googleapis.com/youtube/v3"

# priority_channels_check.py の extract_keywords と同じ字種パターン。
# ただしクエリは語順が意味を持つのでタイトル出現順を保持する (set にしない)。
KW_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#_-]{2,}|[ァ-ヶー]{3,}|[一-龥]{2,}")
KW_STOP = {"claude", "claudecode", "code", "with", "your", "the", "for", "and", "ai"}

# HIT 判定はチャンネル相対 (2026-07-21 に score=views/subs 方式から刷新)。
# 外部chの「普段」= RSS フィード (最新15本・公開7日超) の views 中央値 — quota ゼロで取れる。
# 倍率/下限は priority / discovery と同じ値で対管理 (HIT_STRONG_MULT / HIT_REL_FLOOR)。
REL_HIT_MULT = 5     # 普段の5倍以上 = HIT
REL_HIT_FLOOR = 500  # 下限 views
WAVE_DAYS = 7        # 「波」= この日数以内の公開。イベント起点テーマを古い残骸と区別する
# score (views/subs) は表示参考値としてのみ残す (判定には使わない)


def build_query(title: str, max_kw: int = 3) -> str:
    """タイトルから検索クエリを作る（出現順の上位キーワードを空白連結）。"""
    seen = []
    for tok in KW_RE.findall(title or ""):
        t = tok.lower()
        if t in KW_STOP or len(t) < 3:
            continue
        if t not in seen:
            seen.append(t)
        if len(seen) >= max_kw:
            break
    return " ".join(seen)


def get(path: str, params: dict) -> dict:
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        quota_meter.add(100 if path == "search" else 1)
        return json.load(r)


def age_days(published: str) -> int:
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return 9999
    return (datetime.now(timezone.utc) - dt).days


def load_priority_channel_ids(root: Path) -> set[str]:
    ids = set()
    for name in ("priority_channels.txt", "boosted_channels.txt"):
        p = root / name
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8-sig").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                ids.add(line)
    return ids


def validate_market(query: str, own_channel_ids: set[str], exclude_video_id: str = "",
                    per: int = 25, event_date: str | None = None,
                    llm_shelf: str | None = None) -> dict:
    """1 クエリで市場検証。search.list (100 units) + videos/channels batch (~2 units)。

    event_date (YYYY-MM-DD) を渡すと **その日以降に公開された動画だけ** を検索する
    (search.list の publishedAfter)。発表・改定などイベント起点のテーマでは、それ以前の
    動画は物理的に「発表を受けた話」ではあり得ないため、検索段階から除外して
    25枠全部をイベント後の反応の観測に使う。"""
    params = {
        "part": "snippet", "q": query, "type": "video",
        "order": "relevance", "maxResults": per, "relevanceLanguage": "ja",
    }
    if event_date:
        params["publishedAfter"] = f"{event_date}T00:00:00Z"
    data = get("search", params)
    vids = [it["id"]["videoId"] for it in data.get("items", [])
            if it["id"]["videoId"] != exclude_video_id]
    if not vids:
        return {"query": query, "n": 0}

    vdata = get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(vids[:50])})
    items = vdata.get("items", [])
    # Shorts (≤180秒) はリポジトリ全体の方針通りハード除外 (別の流通・別の力学)
    def _dur(it):
        m = DURATION_RE.fullmatch(it.get("contentDetails", {}).get("duration", "") or "")
        h, mn, s = ((int(x) if x else 0) for x in (m.groups() if m else (0, 0, 0)))
        return h * 3600 + mn * 60 + s
    n_shorts = sum(1 for it in items if 0 < _dur(it) <= 180)
    items = [it for it in items if not (0 < _dur(it) <= 180)]
    ch_ids = {it["snippet"]["channelId"] for it in items}
    subs_map = {}
    cid_list = list(ch_ids)
    for i in range(0, len(cid_list), 50):
        cdata = get("channels", {"part": "statistics", "id": ",".join(cid_list[i:i + 50])})
        for it in cdata.get("items", []):
            subs_map[it["id"]] = int(it["statistics"].get("subscriberCount", 0))

    rows = []
    for it in items:
        cid = it["snippet"]["channelId"]
        subs = subs_map.get(cid, 0)
        views = int(it["statistics"].get("viewCount", 0))
        if subs <= 0:
            continue  # subs 非公開は score が定義できないので除外
        rows.append({
            "video_id": it["id"],
            "title": it["snippet"]["title"],
            "channel": it["snippet"]["channelTitle"],
            "channel_id": cid,
            "views": views,
            "likes": int(it["statistics"].get("likeCount", 0)),
            "subs": subs,
            "score": round(views / subs, 2),  # 表示参考値 (判定には使わない)
            "age_days": age_days(it["snippet"].get("publishedAt", "")),
            "published_at": it["snippet"].get("publishedAt", ""),
            "lang": "ja" if JA_RE.search(it["snippet"]["title"] + it["snippet"]["channelTitle"]) else "other",
            "external": cid not in own_channel_ids,
            "url": f"https://www.youtube.com/watch?v={it['id']}",
        })

    # 関連性フィルタ: 棚に本当に居る本だけを統計母数に残す。
    #
    # **横断分析は必ず LLM を通す**（2026-09-08 ユーザー指定・docs/hit_rate_methodology.md §2d）。
    # 語ベースの判定は両方向に外す — 実測で `ローカルLLM RAG` の直近90日の当たり率が
    # 語で絞ると 0.0%、意味で絞ると 8.3% になった（非RAG本を入れ、実HITを落としていた）。
    #
    # LLM が使えない時は語ベースへ落ちるが、**その事実を戻り値に載せて可視化する**。
    # 黙って決定論経路へ落ちる事故は ci_claude_auth.md に前例がある（6日間 401 に気づかず）。
    toks = [t.lower() for t in query.split() if t]
    llm_ok = False
    if llm_shelf:
        try:
            import llm_judge
            targets = [{"video_id": r["video_id"], "title": r["title"]}
                       for r in rows if r["external"]]
            if targets:
                llm_judge.judge(llm_shelf, targets)
                verdict = {t["video_id"]: t.get("llm_on_shelf") for t in targets}
                if any(v is not None for v in verdict.values()):
                    llm_ok = True
                    for r in rows:
                        r["on_shelf"] = verdict.get(r["video_id"]) if r["external"] else True
        except Exception as e:
            print(f"[warn] LLM 判定に失敗、語ベースへ降格: {e}", file=sys.stderr)

    if llm_ok:
        # 判定できなかった本 (None) は語ベースで救済する
        def keep(r):
            if not r["external"]:
                return True
            if r.get("on_shelf") is not None:
                return r["on_shelf"]
            return not toks or any(t in r["title"].lower() for t in toks)
    else:
        def keep(r):
            return (not r["external"] or not toks
                    or any(t in r["title"].lower() for t in toks))

    n_offtopic = sum(1 for r in rows if not keep(r))
    rows = [r for r in rows if keep(r)]

    ext = [r for r in rows if r["external"]]

    # 各外部chの「普段」を RSS フィードから取得 (quota ゼロ)。
    # HIT = 普段の REL_HIT_MULT 倍以上かつ views ≥ REL_HIT_FLOOR / FLOP = 普段未満。
    # フィードが取れない ch (削除等) は判定不能として mult=None (HIT/FLOP どちらにも数えない)。
    med_cache: dict[str, tuple[int | None, float | None]] = {}
    for r in ext:
        cid = r["channel_id"]
        if cid not in med_cache:
            entries = fetch_feed(cid, retries=2)
            med_cache[cid] = (feed_median(entries), feed_like_rate(entries)) if entries else (None, None)
        med, base_lr = med_cache[cid]
        r["ch_median"] = med
        r["mult"] = round(r["views"] / med, 1) if med else None
        # 広告チェック (detect_prime と同じ2段構え):
        #   絶対床 like率<0.4%、または そのchの普段like率 (RSSフィード合算) の半分未満 → 盛られ疑い
        lp = 100.0 * r["likes"] / r["views"] if r["views"] else 0.0
        r["like_pct"] = round(lp, 2)
        r["ch_like_rate_pct"] = round(base_lr * 100, 2) if base_lr else None
        r["ad_suspect"] = bool(
            r["views"] >= 500
            and (lp < 0.4 or (base_lr and lp < 0.5 * base_lr * 100)))
        # HIT は広告疑いを除外してから判定 (盛られた views を実証に数えない)
        r["rel_hit"] = (not r["ad_suspect"]) and bool(med) and r["views"] >= max(
            REL_HIT_FLOOR, REL_HIT_MULT * med)

    # 統計は3レンズで分ける (2026-07-21):
    #   🇯🇵 日本語圏 = 自分が戦う市場の実証 (verdict はここで判定)
    #   🌐 海外     = 先行指標 (英語圏の波は日本語圏に数日先行する。混ぜると数字が歪む)
    #   🌊 波(直近 WAVE_DAYS 日) = イベント起点の速報テーマで「古い同名テーマの残骸」と区別する
    n = len(ext)
    ja = [r for r in ext if r["lang"] == "ja"]
    other = [r for r in ext if r["lang"] != "ja"]
    wave = [r for r in ext if r["age_days"] <= WAVE_DAYS]
    ja_hits = [r for r in ja if r["rel_hit"]]
    other_hits = [r for r in other if r["rel_hit"]]
    wave_hits = [r for r in wave if r["rel_hit"]]
    flops = [r for r in ja if r["ch_median"] and r["views"] < r["ch_median"]]
    views_sorted = sorted(r["views"] for r in ext)
    median_views = views_sorted[len(views_sorted) // 2] if views_sorted else 0

    ja_rate = round(len(ja_hits) / len(ja), 2) if ja else 0.0
    if len(ja) < 5:
        verdict = "⚪ サンプル不足（日本語圏の検索結果が少なく判定不能）"
    elif ja_rate >= 0.2:
        verdict = "🟢 市場実証あり（日本語圏の外部チャンネルでも広く当たっている企画）"
    elif ja_rate >= 0.08:
        verdict = "🟡 部分的に実証（当たる例はあるが打率は低め — 演出/切り口勝負）"
    else:
        verdict = "🔴 日本語圏の実証弱い（当たれば先行者だが需要リスクあり）"
    if other_hits and len(ja_hits) == 0:
        verdict += " / 🌐 海外は当たっている＝先行余地の可能性"

    # 検知元 (検証のきっかけ動画) 自身の状況。検索対象からは除外している (自己参照防止) が、
    # 文脈の核なので views/普段/倍率を取ってカード先頭に表示する (統計には数えない)。1u + RSS。
    source = None
    if exclude_video_id and not exclude_video_id.startswith("manual:"):
        try:
            sv = get("videos", {"part": "snippet,statistics", "id": exclude_video_id})
            s_items = sv.get("items", [])
            if s_items:
                it = s_items[0]
                s_cid = it["snippet"]["channelId"]
                s_views = int(it["statistics"].get("viewCount", 0))
                s_entries = fetch_feed(s_cid, retries=2)
                s_med = feed_median(s_entries) if s_entries else None
                source = {
                    "video_id": exclude_video_id,
                    "title": it["snippet"]["title"],
                    "channel": it["snippet"]["channelTitle"],
                    "channel_id": s_cid,
                    "views": s_views,
                    "ch_median": s_med,
                    "mult": round(s_views / s_med, 1) if s_med else None,
                    "published_at": it["snippet"].get("publishedAt", ""),
                    "url": f"https://www.youtube.com/watch?v={exclude_video_id}",
                }
        except (urllib.error.URLError, OSError, KeyError, ValueError):
            source = None

    examples = sorted(ja_hits or other_hits or ext, key=lambda r: (r["mult"] or 0), reverse=True)[:3]
    return {
        "source": source,
        # 外部HITコーパス蓄積用の生データ (キャッシュ保存前に main() が pop する)
        "_ext_rows": ext,
        "query": query,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": n,
        "event_date": event_date,
        "n_shorts_excluded": n_shorts,
        "n_offtopic_excluded": n_offtopic,
        # 横断分析を LLM で絞れたか。False のまま数字を読むと当たり率が歪む (§2d)
        "llm_filtered": llm_ok,
        "hit_rate": ja_rate,  # 見出しの HIT率 = 日本語圏 (自分の市場)
        "ja_n": len(ja), "ja_hits": len(ja_hits),
        "other_n": len(other), "other_hits": len(other_hits),
        "other_hit_rate": round(len(other_hits) / len(other), 2) if other else 0.0,
        "wave_n": len(wave), "wave_hits": len(wave_hits), "wave_days": WAVE_DAYS,
        "flop_rate": round(len(flops) / len(ja), 2) if ja else 0.0,
        "median_views": median_views,
        "verdict": verdict,
        "examples": [
            {k: r.get(k) for k in ("title", "channel", "views", "subs", "score",
                                   "ch_median", "mult", "age_days", "url")}
            for r in examples
        ],
        # 全明細 (ダッシュボード 🌍 外部需要タブが「このクエリでこのchが出てきてこうだった」を表示)
        "videos": [
            {k: r.get(k) for k in ("video_id", "title", "channel", "channel_id", "views",
                                   "subs", "ch_median", "mult", "rel_hit", "age_days", "lang",
                                   "like_pct", "ch_like_rate_pct", "ad_suspect", "url")}
            for r in sorted(ext, key=lambda r: (r["mult"] or 0), reverse=True)
        ],
    }


def market_lines(m: dict) -> list[str]:
    """Slack / analysis 欄用のテキスト行。

    LLM で絞れていない時は必ずその旨を出す。黙って語ベースへ落ちると
    当たり率が歪んだまま通知に乗る (ci_claude_auth.md の 6日間無自覚401 と同じ事故)。
    """
    if m.get("n", 0) == 0:
        return [f"🌍 市場検証: 「{m['query']}」で外部動画が見つからず（新規テーマ濃厚）"]
    lines = [
        f"🌍 市場検証「{m['query']}」: {m['verdict']}",
        f"　🇯🇵 日本語圏 {m.get('ja_n', m['n'])}本中 HIT率(普段の{REL_HIT_MULT}倍) {int(m['hit_rate'] * 100)}%"
        f" / 🌊 波(直近{m.get('wave_days', WAVE_DAYS)}日) {m.get('wave_hits', '?')}/{m.get('wave_n', '?')}本HIT"
        + (f" / Shorts除外 {m['n_shorts_excluded']}本" if m.get("n_shorts_excluded") else ""),
        f"　🌐 海外 {m.get('other_n', 0)}本中 {int(m.get('other_hit_rate', 0) * 100)}% — 先行指標 (日本語圏に数日先行)",
    ]
    if not m.get("llm_filtered"):
        lines.append("　⚠️ **LLM 判定を通せていない**（語ベースの絞り込み）。棚外の本が母数に"
                     "混ざるので HIT率は歪んでいる。数字を鵜呑みにしないこと")
    for e in m["examples"][:2]:
        rel = f"普段{e['ch_median']:,}vの{e['mult']}倍" if e.get("mult") else f"score {e.get('score')}"
        lines.append(f"　例: [{e['channel']}] {e['title'][:40]} — {e['views']:,}v ({rel})")
    return lines


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from-alerts", action="append", default=[],
                   help="アラート JSON (hit_alerts.json / prime_alerts.json)。複数指定可")
    p.add_argument("--update-alerts", action="store_true",
                   help="--from-alerts の analysis 欄に市場検証行を追記して書き戻す (Slack 通知に乗る)")
    p.add_argument("--title", help="手動検証: 動画タイトル (キーワード抽出してクエリ化)")
    p.add_argument("--video-id", default="", help="手動検証: キャッシュキー用 video_id")
    p.add_argument("--event-date", default=None,
                   help="YYYY-MM-DD。発表/改定などイベント起点テーマ用 — この日以降の公開動画のみ検索")
    p.add_argument("--max-videos", type=int, default=3, help="1 実行あたりの検証本数上限 (search 100 units/本)")
    p.add_argument("--refresh-days", type=int, default=14, help="キャッシュがこの日数より新しければスキップ")
    p.add_argument("--cache", default="time_series/market_checks.json")
    p.add_argument("--shelf", default=None,
                   help="LLM に渡す棚の定義 (自然文)。省略時は元動画のタイトルから作る")
    p.add_argument("--no-llm", action="store_true",
                   help="LLM 判定を省く (非推奨)。横断分析は必ず LLM を通す方針 "
                        "(docs/hit_rate_methodology.md §2d)。語ベースへ落ちる")
    args = p.parse_args()

    if not os.environ.get("YOUTUBE_API_KEY"):
        print("[warn] YOUTUBE_API_KEY 未設定のため市場検証をスキップ")
        return 0

    root = Path(".")
    own_ids = load_priority_channel_ids(root)
    cache_path = root / args.cache
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}

    targets = []  # (video_id, title)
    if args.title:
        targets.append((args.video_id or f"manual:{build_query(args.title)}", args.title))
    alert_files: dict[str, list] = {}  # path → parsed alerts
    for fpath in args.from_alerts:
        alerts_path = Path(fpath)
        if not alerts_path.exists():
            continue
        try:
            alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not alerts:
            continue
        alert_files[fpath] = alerts
        for h in alerts:
            targets.append((h["video_id"], h["title"]))

    if not targets:
        print("検証対象なし (急伸検知 0 件)")
        return 0

    now = datetime.now(timezone.utc)
    checked = 0
    for vid, title in targets:
        if checked >= args.max_videos:
            print(f"[skip] {vid}: --max-videos {args.max_videos} 到達")
            continue
        cached = cache.get(vid)
        if cached and cached.get("checked_at"):
            try:
                age = (now - datetime.fromisoformat(cached["checked_at"].replace("Z", "+00:00"))).days
                if age < args.refresh_days:
                    print(f"[cache] {vid}: {age}日前に検証済み")
                    continue
            except ValueError:
                pass
        query = build_query(title)
        if not query:
            print(f"[skip] {vid}: キーワード抽出できず")
            continue
        try:
            # 棚の定義には元動画のタイトルをそのまま使う。クエリ語(3語)より
            # 意味が保たれるので、LLM が「同じ企画か」を判定しやすい。
            shelf = args.shelf or f"「{title}」と同じ企画・同じテーマを扱った動画"
            result = validate_market(query, own_ids, exclude_video_id=vid,
                                     event_date=args.event_date,
                                     llm_shelf=None if args.no_llm else shelf)
        except Exception as e:
            print(f"[warn] {vid}: 検証失敗 {e}")
            continue
        # 検索で見つけた外部HITは使い捨てずコーパスへ (類似HIT横断分析が全YouTubeを見られるように)
        ext_rows = result.pop("_ext_rows", [])
        try:
            n_corpus = merge_external_hits(
                root / "time_series" / CORPUS_FILE, ext_rows, "market_validate", own_ids)
            if n_corpus:
                print(f"  外部HITコーパスへ {n_corpus} 件追加/更新")
        except OSError as e:
            print(f"[warn] 外部HITコーパス更新失敗: {e}")
        result["source_title"] = title
        cache[vid] = result
        checked += 1
        print(f"[ok] {vid}: 「{query}」 n={result.get('n')} hit_rate={result.get('hit_rate')}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"cache -> {cache_path} ({len(cache)} 件)")

    # Slack 通知 (workflow の jq が読む analysis 欄) に市場検証を追記
    if args.update_alerts:
        for fpath, alerts in alert_files.items():
            changed = False
            for h in alerts:
                m = cache.get(h["video_id"])
                if not m:
                    continue
                h["market"] = m
                extra = "\n".join(market_lines(m))
                if extra and extra not in (h.get("analysis") or ""):
                    h["analysis"] = (h.get("analysis") or "") + "\n" + extra
                    changed = True
            if changed:
                Path(fpath).write_text(
                    json.dumps(alerts, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"alerts 更新 -> {fpath}")

    spent = quota_meter.flush(root / "time_series", "market_validate")
    if spent:
        print(f"YouTube API 推定消費: {spent}u")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
