"""priority_channels.txt の最重要チャンネルを毎セッション初動でチェック。

各チャンネルについて:
- 現在の登録者数 + 全動画 views を取得（min-age-days 0、Shorts 除外）
- time_series/<channel_id>/<YYYY-MM-DD>.csv にスナップショット保存
- time_series/<channel_id>/snapshots.jsonl に登録者推移を追記
- ★直近 HIT (age ≤ 14d かつ views ≥ 1,000) を抽出して標準出力
- 🚀 急伸検知: 直近動画が初めて HIT 閾値 (views ≥ 1,000 または score ≥ 2.0) を超えた瞬間に
  HIT_DETECTED| マーカーを 1 度だけ出力 (workflow が Slack 通知)。重複防止 state は
  alerted_hits.json。初回は既存 HIT を silent seed して誤発火を防ぐ
- 前日比で登録者が動いたチャンネルを警告

quota: 1 ch あたり ~10 unit。5 ch でも 50 unit / セッション。

使い方:
    python priority_channels_check.py
    python priority_channels_check.py --hit-threshold 500 --recent-days 7
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import quota_meter
from external_hits import CORPUS_FILE, corpus_age_days, load_external_hits


DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

# --- 通知エンリッチ用 (LLM 不要・履歴から決定的に計算) ---
# 英単語(3字+) / カタカナ語(3字+) / 漢字熟語(2字+) を抽出。#claude 等の頻出ノイズは除外。
KW_RE = re.compile(r"[A-Za-z][A-Za-z0-9.+#_-]{2,}|[ァ-ヶー]{3,}|[一-龥]{2,}")
KW_STOP = {"claude", "claudecode", "code", "with", "your", "the", "for", "and", "ai"}


def extract_keywords(title: str) -> set[str]:
    toks = {t.lower() for t in KW_RE.findall(title or "")}
    return {t for t in toks if t not in KW_STOP and len(t) >= 3}


def read_video_history(path: Path, video_id: str) -> list[dict]:
    """videos.jsonl から特定動画の観測点を時系列(古→新)で返す。"""
    if not path.exists():
        return []
    pts = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("video_id") == video_id:
                pts.append(o)
    pts.sort(key=lambda x: x.get("ts", ""))
    return pts


def compute_velocity(history: list[dict]) -> float | None:
    """直近2観測点から views/日 を概算。観測点が1点以下なら None。"""
    if len(history) < 2:
        return None
    a, b = history[-2], history[-1]
    try:
        ta = datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    days = (tb - ta).total_seconds() / 86400
    if days <= 0:
        return None
    return round((b["views"] - a["views"]) / days, 1)


def timing_label(age_days: int, velocity: float | None) -> str:
    """参入タイミング判定。初速ゾーンが最良、ピークアウト気味なら急ぐべき。"""
    if age_days <= 3:
        return "🟢 初速ゾーン（参入の最良タイミング）"
    if velocity is not None and velocity >= 80:
        return "🟡 まだ伸びている（参入間に合う）"
    if velocity is not None and velocity < 20:
        return "🔴 ピークアウト気味（真似るなら急ぐ）"
    return "🟡 観測中"


# HIT 定義（チャンネル相対）。build_pages_data.py / dashboard js/main.js の HIT 定数と対で管理。
HIT_STRONG_MULT = 5
HIT_SEMI_MULT = 3
HIT_REL_FLOOR = 500
HIT_ABS_FALLBACK = 1000
HIT_MIN_SAMPLE = 5

# 本命検知の広告フィルタ: HIT 動画の like数が「非HIT動画への回帰から求めた期待like数」の
# この倍率未満なら views 購入疑いとして除外 (0.5 = 期待値の半分までは許容。
# build_pages_data.py の AD_LIKE_RATIO_MIN と対で管理。docs/ad_check_methodology.md 参照)
PRIME_LIKE_RATIO_MIN = 0.5


def likes_baseline(points: list[tuple[int, int]]) -> dict | None:
    """非HIT動画の (views, likes) から like数の基準線を Theil-Sen 回帰で推定。

        like数 ≒ fan_likes（固定ファン分・切片） + slope × views（一見視聴者のlike率・傾き）

    「like率の中央値/平均」と違い、少数の熱心なファンが全動画に like する系統バイアスを
    切片に吸収できる（率だと低再生動画ほどファン分が効いて基準線が上振れする）。
    Theil-Sen（全ペア傾きの中央値）なので「偶然 like が多く付いた1本」にも頑健。
    戻り値: {"fan_likes", "slope", "n"} / 本数 < HIT_MIN_SAMPLE なら None。
    """
    pts = [(v, l) for v, l in points if v > 0]
    if len(pts) < HIT_MIN_SAMPLE:
        return None
    slopes = []
    for i in range(len(pts)):
        v1, l1 = pts[i]
        for j in range(i + 1, len(pts)):
            v2, l2 = pts[j]
            if v1 != v2:
                slopes.append((l2 - l1) / (v2 - v1))
    if not slopes:
        return None
    slope = max(0.0, statistics.median(slopes))
    fan = max(0.0, statistics.median(l - slope * v for v, l in pts))
    return {"fan_likes": round(fan, 1), "slope": slope, "n": len(pts)}


def expected_likes(baseline: dict | None, views: int) -> float | None:
    """基準線から views 時の期待 like数。基準線なし・期待値 1 未満（判定精度なし）は None。"""
    if not baseline:
        return None
    exp = baseline["fan_likes"] + baseline["slope"] * views
    return exp if exp >= 1 else None


def channel_baselines(all_rows: list[dict]) -> dict[str, int | None]:
    """channel_id → 全動画 views の中央値 (=そのchの「普段」)。本数 < HIT_MIN_SAMPLE は None。"""
    by_ch: dict[str, list[int]] = {}
    for r in all_rows:
        by_ch.setdefault(r["channel_id"], []).append(r["views"])
    out: dict[str, int | None] = {}
    for cid, views in by_ch.items():
        if len(views) < HIT_MIN_SAMPLE:
            out[cid] = None
            continue
        views.sort()
        mid = len(views) // 2
        out[cid] = views[mid] if len(views) % 2 else (views[mid - 1] + views[mid]) // 2
    return out


def channel_hit_thresholds(all_rows: list[dict]) -> dict[str, int]:
    """channel_id → 相対 HIT しきい値 (views 中央値の HIT_STRONG_MULT 倍、下限 HIT_REL_FLOOR)。
    動画本数 < HIT_MIN_SAMPLE の ch は HIT_ABS_FALLBACK。"""
    out = {}
    for cid, med in channel_baselines(all_rows).items():
        if med is None:
            out[cid] = HIT_ABS_FALLBACK
        else:
            out[cid] = max(HIT_REL_FLOOR, HIT_STRONG_MULT * med)
    return out


def load_corpus_rows(base: Path, watched_ids: set[str]) -> list[dict]:
    """外部HITコーパス (market_validate / discovery_loop が蓄積) を類似HIT検索用の行に変換。
    監視枠内 ch (二重照合防止) と「違う確定」ch (rejected_channels.txt) は除外。
    external=True マークで通知に 🌍 表示される。"""
    skip = set(watched_ids)
    rejected_path = Path("rejected_channels.txt")
    if rejected_path.exists():
        skip |= set(load_channel_ids(rejected_path))
    out = []
    for e in load_external_hits(base / CORPUS_FILE):
        if e.get("channel_id") in skip:
            continue
        out.append({
            "channel": e.get("channel", "?"),
            "channel_id": e.get("channel_id", ""),
            "video_id": e["video_id"],
            "title": e.get("title", ""),
            "views": e.get("views", 0),
            "score": e.get("score") or 0,
            "age_days": corpus_age_days(e.get("published_at", "")),
            "_kw": extract_keywords(e.get("title", "")),
            "external": True,
        })
    return out


def find_similar_hits(kw: set[str], all_rows: list[dict], exclude_id: str,
                      hit_thresholds: dict[str, int], limit: int = 3,
                      source_title: str = "") -> list[dict]:
    """全 priority ch の履歴 + 外部HITコーパスから、キーワードが重なる過去 HIT を共有数→views 順で返す。
    他 ch の HIT も対象なので「同テーマが横展開で当たっている」証拠が自然に出る。
    HIT 判定はチャンネル相対 (channel_hit_thresholds) のみ — score (views/subs) は 2026-07-21 に
    判定から廃止 (普段比で見ないと大手優遇/登録者数の歪みが入る)。
    外部コーパス行 (external=True) は取り込み時点で相対HIT検証済みなのでしきい値を再適用しない。

    **キーワード重なりは粗い一次フィルタでしかない** — 語が重なっていても別テーマの本が
    混ざる。**横断分析は必ず LLM を通す方針** (2026-09-08 ユーザー指定 /
    docs/hit_rate_methodology.md §2d) なので、source_title を渡した場合は
    候補を広めに集めてから claude -p で「同じ企画か」を判定し直す。
    LLM が使えない時は語ベースのまま返すが、各行に llm_checked=False を残して可視化する。
    """
    scored = []
    for r in all_rows:
        if r["video_id"] == exclude_id:
            continue
        hit_views = hit_thresholds.get(r["channel_id"], HIT_ABS_FALLBACK)
        if not (r.get("external") or r["views"] >= hit_views):
            continue
        shared = kw & r.get("_kw", set())
        if not shared:
            continue
        scored.append((len(shared), r["views"], r))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    if not source_title:
        out = [r for _, _, r in scored[:limit]]
        for r in out:
            r["llm_checked"] = False
        return out

    # LLM に渡す候補は limit より広く取る (語で上位に来た本が棚外のことがあるため)
    cand = [r for _, _, r in scored[:max(limit * 4, 12)]]
    try:
        import llm_judge
        shelf = f"「{source_title}」と同じ企画・同じテーマを扱った動画"
        targets = [{"video_id": r["video_id"], "title": r.get("title", "")} for r in cand]
        llm_judge.judge(shelf, targets)
        verdict = {t["video_id"]: t.get("llm_on_shelf") for t in targets}
        if any(v is not None for v in verdict.values()):
            kept = []
            for r in cand:
                v = verdict.get(r["video_id"])
                r["llm_checked"] = v is not None
                # 判定できなかった本 (None) は語ベースのまま残す
                if v is not False:
                    kept.append(r)
            return kept[:limit]
    except Exception as e:
        print(f"[warn] 類似HITのLLM判定に失敗、語ベースへ降格: {e}", file=sys.stderr)

    out = cand[:limit]
    for r in out:
        r["llm_checked"] = False
    return out


# ⚡ 初速検知 (H-33): 「当たる/当たらない」の day1/day7 分水嶺の間を速めに拾う早期警戒トラック。
# 根拠 = docs/analysis_initial_velocity_2026-07-31.md (6ch 219本):
#   day1>=500 → 94%が最終1,000+ (当たりの79%を初日に検知) / day7>=700 → 96%。
# 急伸検知 (views>=1000 or score>=2.0) の手前で鳴る「1,000到達コース予告」。
# dedup state = ch_dir/alerted_early.json (初回 silent seed)。急伸で既知の動画は鳴らさない。
EARLY_TIERS = ((2, 500), (7, 700))  # (age_days がこれ以下, views がこれ以上)


def detect_prime(per_ch: dict[str, dict], all_rows: list[dict], hit_alert_score: float,
                 recent_days: int, ts: str, sim_rows: list[dict] | None = None) -> list[dict]:
    """🎯 本命検知: 「相対HIT × 横展開実証 × like率健全」を全て満たした瞬間に 1 度だけ通知。

    急伸検知 (views≥1000 or score≥2.0 突破) とは独立の検知器。急伸検知は一度鳴ると
    dedup されるため、その後に相対HITライン (chによっては1,000より上) を超えても
    鳴らない。真似元候補として最も確度の高い瞬間を取りこぼさないための専用トラック。

    条件 (すべて必須):
      1. age ≤ recent_days かつ views ≥ ch相対HITしきい値 (普段の5倍・下限500)
      2. 広告疑いなし: like率 ≥ 0.4% かつ like数が「非HIT動画への Theil-Sen 回帰
         (like数 ≒ 固定ファン分 + 一見視聴者率 × views) から求めた期待like数」の
         PRIME_LIKE_RATIO_MIN 倍以上。オーガニック視聴だけの非HIT動画をベースラインに
         することで「HIT だけ like が極端に薄い = views を買って伸びて見えているだけ」を弾く。
         (views≥500 平均だと HIT 自身が母集団になり自己参照 / 率の中央値だと少数の
          熱心なファンが全動画に like するチャンネルで基準線が上振れする — 回帰の切片が
          ファン分を吸収する。詳細 docs/ad_check_methodology.md)
      3. 類似HITが他チャンネルに存在 (横展開の実証あり)
    dedup state: ch_dir/alerted_prime.json (初回は silent seed して誤発火防止)。
    """
    hit_thresholds = channel_hit_thresholds(all_rows)
    baselines = channel_baselines(all_rows)
    out = []
    for cid, chd in per_ch.items():
        rows = chd["rows"]
        ch_dir = chd["ch_dir"]
        th = hit_thresholds.get(cid, HIT_ABS_FALLBACK)
        med = baselines.get(cid)

        # 「普段の当たってない動画」= 準HITライン (普段の3倍) 未満の動画から
        # like数の基準線 (固定ファン分 + 一見視聴者率 × views) を回帰で推定。
        # 本数 < HIT_MIN_SAMPLE なら基準線なし → 絶対 0.4% のみで判定。
        semi_cutoff = max(HIT_REL_FLOOR, HIT_SEMI_MULT * med) if med else HIT_ABS_FALLBACK
        baseline = likes_baseline([
            (r["views"], r["likes"])
            for r in rows
            if r["views"] < semi_cutoff and r.get("likes") is not None
        ])

        candidates = []
        for r in rows:
            if r["age_days"] > recent_days or r["views"] < th:
                continue
            likes = r.get("likes") or 0
            like_pct = round(100.0 * likes / r["views"], 2) if r["views"] else 0.0
            if like_pct < 0.4:
                continue
            # 普段比チェック: 期待like数 (ファン分 + 一見率×views) の半分未満なら購入views疑いで除外
            exp = expected_likes(baseline, r["views"])
            if exp and likes < PRIME_LIKE_RATIO_MIN * exp:
                continue
            kw = extract_keywords(r["title"])
            sims = find_similar_hits(kw, sim_rows or all_rows, r["video_id"], hit_thresholds,
                                     source_title=r.get("title", ""))
            cross_sims = [s for s in sims if s["channel_id"] != cid]
            if not cross_sims:
                continue
            candidates.append((r, like_pct, exp, cross_sims))

        state_path = ch_dir / "alerted_prime.json"
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
        except (json.JSONDecodeError, OSError):
            state = None
        if state is None:
            state = {r["video_id"]: {"ts": ts, "views": r["views"], "seeded": True}
                     for r, _, _, _ in candidates}
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  (本命検知[{chd['title']}]: state 初期化、{len(state)} 件を silent seed)")
            continue

        fresh = [(r, lp, ex, cs) for r, lp, ex, cs in candidates if r["video_id"] not in state]
        for r, like_pct, exp, cross_sims in fresh:
            url = f"https://www.youtube.com/watch?v={r['video_id']}"
            mult = round(r["views"] / med, 1) if med else None
            history = read_video_history(ch_dir / "videos.jsonl", r["video_id"])
            velocity = compute_velocity(history)
            tlabel = timing_label(r["age_days"], velocity)
            vel_txt = f"~{velocity:.0f} views/日" if velocity is not None else "速度データ不足"
            lines = [
                f"✅ 相対HIT: {r['views']:,}v = 普段{med:,}vの{mult}倍 (しきい {th:,}v)" if med
                else f"✅ HIT: {r['views']:,}v (しきい {th:,}v)",
                (f"✅ like {r.get('likes') or 0}個 健全（非HIT基準線の期待 {exp:.0f}個の {round(100 * (r.get('likes') or 0) / exp)}%・like率 {like_pct}%）"
                 if exp else f"✅ like率 {like_pct}% 健全（基準線データ不足・絶対0.4%のみで判定）"),
                f"✅ 横展開実証: 他chの類似HIT {len(cross_sims)}件"
                + ("（🌍 監視枠外含む＝世の中の需要実証）"
                   if any(s.get("external") for s in cross_sims) else ""),
            ]
            for s in cross_sims[:2]:
                mark = "🌍" if s.get("external") else "🌐"
                lines.append(f"　{mark} [{s['channel']}] {s['title'][:45]} — {s['views']:,}v")
            lines.append(f"⏱ {tlabel}（公開{r['age_days']}日前 / {vel_txt}）")
            safe_title = r["title"].replace("|", "/")
            print(f"PRIME_DETECTED|{chd['title']}|{r['video_id']}|{r['views']}|{safe_title}|{url}")
            state[r["video_id"]] = {"ts": ts, "views": r["views"]}
            out.append({
                "channel": chd["title"],
                "channel_id": cid,
                "video_id": r["video_id"],
                "title": r["title"],
                "url": url,
                "views": r["views"],
                "score": r["score"],
                "age_days": r["age_days"],
                "published_at": r.get("published_at", ""),
                "like_pct": like_pct,
                "expected_likes": round(exp, 1) if exp else None,
                "likes_vs_expected_pct": round(100 * (r.get("likes") or 0) / exp) if exp else None,
                "baseline_fan_likes": baseline["fan_likes"] if baseline else None,
                "baseline_slope_pct": round(100 * baseline["slope"], 2) if baseline else None,
                "baseline_mult": mult,
                "hit_threshold": th,
                "velocity_per_day": velocity,
                "timing_label": tlabel,
                "similar_hits": [
                    {"channel": s["channel"], "title": s["title"], "views": s["views"],
                     "url": f"https://www.youtube.com/watch?v={s['video_id']}", "cross_channel": True,
                     "external": bool(s.get("external"))}
                    for s in cross_sims
                ],
                "analysis": "\n".join(lines),
            })
        if fresh:
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def enrich_and_write(all_hit_alerts: list[dict], all_new_videos: list[dict],
                     all_rows: list[dict], base: Path,
                     hit_threshold: int, hit_alert_score: float,
                     sim_rows: list[dict] | None = None) -> tuple[int, int]:
    """急伸/新作アラートに類似HIT・タイミング分析を添えて JSON ファイルに書き出す。
    workflow がこの JSON を読んで Discord に送る。戻り値は (急伸件数, 新作件数)。"""
    hit_thresholds = channel_hit_thresholds(all_rows)
    enriched = []
    for h in all_hit_alerts:
        cid = h["channel_id"]
        kw = extract_keywords(h["title"])
        history = read_video_history(base / cid / "videos.jsonl", h["video_id"])
        velocity = compute_velocity(history)
        sims = find_similar_hits(kw, sim_rows or all_rows, h["video_id"], hit_thresholds,
                                 source_title=h.get("title", ""))
        tlabel = timing_label(h["age_days"], velocity)

        vel_txt = f"~{velocity:.0f} views/日" if velocity is not None else "速度データ不足"
        lines = [f"⏱ タイミング: {tlabel}（公開{h['age_days']}日前 / {vel_txt}）"]
        if sims:
            cross = any(s["channel_id"] != cid for s in sims)
            has_ext = any(s.get("external") for s in sims)
            lines.append(f"🔁 類似HIT {len(sims)}件:")
            for s in sims:
                mark = "🌍" if s.get("external") else ("🌐" if s["channel_id"] != cid else "・")
                lines.append(f"{mark} [{s['channel']}] {s['title'][:48]} — {s['views']:,}v / {s['age_days']}d前")
            if has_ext:
                lines.append("→ 監視枠外のchでも同テーマがHIT＝世の中の需要実証（最強シグナル）")
            elif cross:
                lines.append("→ 同テーマが他chでもHIT＝横展開の実証あり（強シグナル）")
        else:
            lines.append("🔁 類似HIT: 過去履歴・外部コーパスに該当なし（新規テーマの可能性）")

        enriched.append({
            "channel": h["channel"],
            "video_id": h["video_id"],
            "title": h["title"],
            "url": f"https://www.youtube.com/watch?v={h['video_id']}",
            "views": h["views"],
            "delta": h.get("delta"),
            "score": h["score"],
            "age_days": h["age_days"],
            "velocity_per_day": velocity,
            "timing_label": tlabel,
            "similar_hits": [
                {
                    "channel": s["channel"],
                    "title": s["title"],
                    "views": s["views"],
                    "age_days": s["age_days"],
                    "url": f"https://www.youtube.com/watch?v={s['video_id']}",
                    "cross_channel": s["channel_id"] != cid,
                    "external": bool(s.get("external")),
                }
                for s in sims
            ],
            "analysis": "\n".join(lines),
        })

    new_alerts = [
        {
            "channel": n["channel"],
            "video_id": n["video_id"],
            "title": n["title"],
            "url": f"https://www.youtube.com/watch?v={n['video_id']}",
            "views": n["views"],
            "published_at": n["published_at"],
        }
        for n in all_new_videos
    ]

    Path("hit_alerts.json").write_text(
        json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    Path("new_video_alerts.json").write_text(
        json.dumps(new_alerts, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(enriched), len(new_alerts)


def load_channel_ids(path: Path) -> list[str]:
    ids = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def parse_iso8601(s: str) -> int:
    m = DURATION_RE.fullmatch(s or "")
    if not m:
        return 0
    h, mn, sec = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mn * 60 + sec


def get_channel_info(yt, ids):
    info = {}
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        resp = (
            yt.channels()
            .list(part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50)
            .execute()
        )
        quota_meter.add(1)
        for item in resp.get("items", []):
            info[item["id"]] = {
                "title": item["snippet"]["title"],
                "subs": int(item["statistics"].get("subscriberCount", 0)),
                "uploads": item["contentDetails"]["relatedPlaylists"]["uploads"],
            }
    return info


def list_video_ids(yt, playlist_id, limit=300):
    ids = []
    token = None
    while len(ids) < limit:
        resp = (
            yt.playlistItems()
            .list(
                part="contentDetails",
                playlistId=playlist_id,
                maxResults=min(50, limit - len(ids)),
                pageToken=token,
            )
            .execute()
        )
        quota_meter.add(1)
        for item in resp.get("items", []):
            ids.append(item["contentDetails"]["videoId"])
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


def fetch_video_details(yt, video_ids):
    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = (
            yt.videos()
            .list(part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50)
            .execute()
        )
        quota_meter.add(1)
        for item in resp.get("items", []):
            stats = item["statistics"]
            videos.append(
                {
                    "id": item["id"],
                    "title": item["snippet"]["title"],
                    "published_at": item["snippet"]["publishedAt"],
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "duration_sec": parse_iso8601(item["contentDetails"].get("duration", "")),
                }
            )
    return videos


def read_prev_snapshots(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--channels", default="priority_channels.txt")
    p.add_argument("--out-dir", default="time_series", help="スナップショット出力先 (boosted枠は time_series_boosted)")
    p.add_argument("--no-alert-files", action="store_true",
                   help="hit_alerts.json / new_video_alerts.json を書かない (boosted枠用: 通知させない)")
    # 全動画を取り切る前提の上限 (quota 暴走ガード)。50本ごとに +2u/回 (列挙+統計)。
    # 現最大は戒くん 357本。1000 なら量産型 ch でも当面全量カバー。
    p.add_argument("--per-channel", type=int, default=1000)
    p.add_argument("--include-shorts", action="store_true", help="Shorts も含める（default: 除外）")
    p.add_argument("--hit-threshold", type=int, default=1000, help="直近 HIT の views 閾値 (default: 1000)")
    p.add_argument("--recent-days", type=int, default=14, help="直近とみなす日数 (default: 14)")
    p.add_argument("--hit-alert-score", type=float, default=2.0, help="急伸検知のスコア閾値 views/subs (default: 2.0 = 鉱脈)")
    p.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"))
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: YOUTUBE_API_KEY 未設定", file=sys.stderr)
        return 1

    path = Path(args.channels)
    if not path.exists():
        print(f"ERROR: {path} が見つかりません", file=sys.stderr)
        return 1

    ids = load_channel_ids(path)
    if not ids:
        print("ERROR: チャンネル ID が読めません", file=sys.stderr)
        return 1

    yt = build("youtube", "v3", developerKey=args.api_key, cache_discovery=False)

    try:
        info = get_channel_info(yt, ids)
    except HttpError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)
    base = Path(args.out_dir)
    all_recent_hits = []
    all_new_videos = []  # 24h 以内の新作 (前回 snapshot に無かった video_id)
    all_hit_alerts = []  # 初めて HIT 閾値を超えた動画 (急伸検知)
    all_early_alerts = []  # ⚡ 初速検知 (H-33 分水嶺・1,000到達前の予告)
    all_rows = []  # 全 ch の全動画 (類似HIT 検索用コーパス。キーワード _kw 付き)
    per_ch: dict[str, dict] = {}  # 本命検知 (detect_prime) 用: cid → rows / ch_dir / title

    print(f"=== priority_channels 初動チェック ({today} UTC) ===\n")

    for cid in ids:
        ch = info.get(cid)
        if not ch:
            print(f"[warn] {cid} が解決できません", file=sys.stderr)
            continue

        try:
            vid_ids = list_video_ids(yt, ch["uploads"], args.per_channel)
            videos = fetch_video_details(yt, vid_ids)
        except HttpError as e:
            print(f"[warn] {ch['title']} 取得失敗: {e}", file=sys.stderr)
            continue

        rows = []
        shorts_rows = []
        for v in videos:
            # Shorts は **捨てずに別建てで残す** (2026-08-12 変更・ユーザー指示)。
            #
            # 以前は 180 秒以下を dump ごと落としていたため、
            #   ① データが残らない
            #   ② 除外基準を変えた日に「大量取り下げ」に見える不連続が出る
            #      (2026-07-17 に 60→180 秒へ変更 → 3ch で計183本が消えたように見えた)
            # の2つの害があった。判定・中央値・通知からは外すが、観測は続ける。
            # YouTube Shorts は 2024-10 から最大 3 分 (180 秒)。
            is_short = 0 < v["duration_sec"] <= 180
            try:
                pub = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
                age = max((now - pub).days, 0)
            except Exception:
                age = 9999
            score = v["views"] / ch["subs"] if ch["subs"] > 0 else 0.0
            row = {
                "video_id": v["id"],
                "title": v["title"],
                "views": v["views"],
                "likes": v["likes"],
                "comments": v["comments"],
                "score": round(score, 3),
                "age_days": age,
                "published_at": v["published_at"][:10],
                "duration_sec": v["duration_sec"],
                "is_short": 1 if is_short else 0,
            }
            # 判定・中央値・通知に使うのは長尺だけ。ショートは混ぜずに別建てで持つ。
            (shorts_rows if (is_short and not args.include_shorts) else rows).append(row)

        ch_dir = base / cid
        ch_dir.mkdir(parents=True, exist_ok=True)
        snapshot_csv = ch_dir / f"{today}.csv"
        if rows:
            with snapshot_csv.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)

        # ショートは別ファイルに落とす (混ぜない・捨てない)
        if shorts_rows:
            with (ch_dir / f"{today}_shorts.csv").open("w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(shorts_rows[0].keys()))
                w.writeheader()
                w.writerows(shorts_rows)

        # 観測ポイントごとの動画 views を append (videos.jsonl)
        # 日付単位 CSV は上書きされて intra-day 観測点が失われるので、別ファイルに残す
        ts_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if shorts_rows:
            with (ch_dir / "videos_shorts.jsonl").open("a", encoding="utf-8") as f:
                for r in shorts_rows:
                    f.write(json.dumps({
                        "ts": ts_iso, "video_id": r["video_id"], "title": r["title"],
                        "views": r["views"], "age_days": r["age_days"],
                        "published_at": r["published_at"],
                    }, ensure_ascii=False) + "\n")
        if rows:
            videos_jsonl = ch_dir / "videos.jsonl"
            with videos_jsonl.open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({
                        "ts": ts_iso,
                        "video_id": r["video_id"],
                        "title": r["title"],
                        "views": r["views"],
                        "age_days": r["age_days"],
                        "published_at": r["published_at"],
                    }, ensure_ascii=False) + "\n")

        # 類似HIT 検索用コーパスに全動画を蓄積 (キーワードは一度だけ計算)
        for r in rows:
            all_rows.append({
                "channel": ch["title"],
                "channel_id": cid,
                "video_id": r["video_id"],
                "title": r["title"],
                "views": r["views"],
                "score": r["score"],
                "age_days": r["age_days"],
                "_kw": extract_keywords(r["title"]),
            })
        per_ch[cid] = {"title": ch["title"], "rows": rows, "ch_dir": ch_dir}

        snapshot_meta = {
            "date": today,
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel_id": cid,
            "channel_title": ch["title"],
            "subscriber_count": ch["subs"],
            "total_videos_in_window": len(rows),
            "total_views_in_window": sum(r["views"] for r in rows),
            "shorts_videos": len(shorts_rows),
            "shorts_views": sum(r["views"] for r in shorts_rows),
            "videos_over_1000_views": sum(1 for r in rows if r["views"] >= 1000),
        }

        snapshots_path = ch_dir / "snapshots.jsonl"
        prev_snapshots = read_prev_snapshots(snapshots_path)

        # 新作検知: 前回 snapshot CSV になく & age_days ≤ 1 の動画
        prev_video_ids: set[str] = set()
        prev_views: dict[str, int] = {}
        if prev_snapshots:
            last_date = prev_snapshots[-1].get("date")
            if last_date and last_date != today:
                prev_csv = ch_dir / f"{last_date}.csv"
                if prev_csv.exists():
                    with prev_csv.open(encoding="utf-8-sig") as f:
                        for r in csv.DictReader(f):
                            prev_video_ids.add(r["video_id"])
                            try:
                                prev_views[r["video_id"]] = int(r["views"])
                            except (KeyError, ValueError):
                                pass
        new_videos_this_ch = []
        if prev_video_ids:  # 初回 or 同日再実行は誤検知防止のためスキップ
            for r in rows:
                if r["video_id"] in prev_video_ids:
                    continue
                if r["age_days"] > 1:  # 24h 以内に絞る
                    continue
                new_videos_this_ch.append(r)

        with snapshots_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot_meta, ensure_ascii=False) + "\n")

        # サマリ出力
        print(f"--- {ch['title']} ({ch['subs']:,} subs / 取得 {len(rows)} 動画) ---")
        if prev_snapshots:
            last = prev_snapshots[-1]
            delta = ch["subs"] - last.get("subscriber_count", ch["subs"])
            sign = "+" if delta >= 0 else ""
            last_label = last.get("timestamp") or last.get("date")
            print(
                f"  前回 ({last_label}): {last['subscriber_count']:,} subs → 今回 {ch['subs']:,} ({sign}{delta})"
            )

        recent_hits = [
            r for r in rows if r["age_days"] <= args.recent_days and r["views"] >= args.hit_threshold
        ]
        if recent_hits:
            recent_hits.sort(key=lambda x: x["views"], reverse=True)
            print(f"  ★ 直近 {args.recent_days} 日 HIT (views ≥ {args.hit_threshold}): {len(recent_hits)} 件")
            for r in recent_hits:
                print(
                    f"    views={r['views']:>5,} score={r['score']:>5.2f} age={r['age_days']:>2}d  {r['title'][:55]}"
                )
                print(f"      https://www.youtube.com/watch?v={r['video_id']}")
                all_recent_hits.append({"channel": ch["title"], **r})
        else:
            print(f"  直近 {args.recent_days} 日に {args.hit_threshold}+ views の HIT なし")

        # 🚀 急伸検知 (HIT アラート): 直近動画が初めて HIT 閾値を超えた瞬間に 1 度だけ通知。
        #   - 条件: age ≤ recent_days かつ (views ≥ hit_threshold または score ≥ hit_alert_score)
        #   - dedup: ch_dir/alerted_hits.json に通知済 video_id を記録 (commit されて永続)
        #   - 初回 (state 未作成) は既存 HIT を silent seed して誤発火を防ぐ
        alert_path = ch_dir / "alerted_hits.json"
        try:
            alerted = json.loads(alert_path.read_text(encoding="utf-8")) if alert_path.exists() else None
        except (json.JSONDecodeError, OSError):
            alerted = None

        alert_candidates = [
            r
            for r in rows
            if r["age_days"] <= args.recent_days
            and (r["views"] >= args.hit_threshold or (ch["subs"] > 0 and r["score"] >= args.hit_alert_score))
        ]

        if alerted is None:
            alerted = {
                r["video_id"]: {
                    "ts": snapshot_meta["timestamp"],
                    "views": r["views"],
                    "score": r["score"],
                    "seeded": True,
                }
                for r in alert_candidates
            }
            alert_path.write_text(json.dumps(alerted, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  (急伸検知: state 初期化、{len(alerted)} 件を既知 HIT として silent seed)")
        else:
            new_hits = [r for r in alert_candidates if r["video_id"] not in alerted]
            if new_hits:
                new_hits.sort(key=lambda x: x["views"], reverse=True)
                print(f"  🚀 急伸検知 (初めて HIT 閾値超え): {len(new_hits)} 件")
                for r in new_hits:
                    url = f"https://www.youtube.com/watch?v={r['video_id']}"
                    prev_v = prev_views.get(r["video_id"])
                    delta = r["views"] - prev_v if prev_v is not None else None
                    delta_txt = delta if delta is not None else ""
                    print(
                        f"    views={r['views']:,} score={r['score']:.2f} age={r['age_days']}d Δ={delta_txt}  {r['title'][:55]}"
                    )
                    print(f"      {url}")
                    safe_title = r["title"].replace("|", "/")
                    print(
                        f"HIT_DETECTED|{ch['title']}|{r['video_id']}|{r['published_at']}|{r['views']}|{r['score']}|{delta_txt}|{r['age_days']}|{safe_title}|{url}"
                    )
                    alerted[r["video_id"]] = {
                        "ts": snapshot_meta["timestamp"],
                        "views": r["views"],
                        "score": r["score"],
                    }
                    all_hit_alerts.append({"channel": ch["title"], "channel_id": cid, "delta": delta, **r})
                alert_path.write_text(json.dumps(alerted, ensure_ascii=False, indent=2), encoding="utf-8")

        # ⚡ 初速検知 (H-33 早期警戒): 急伸しきい (1,000 / score) の手前で「当たりの初速」を1度だけ通知。
        #   条件: EARLY_TIERS のいずれか (day1-2で500 / day7で700) を超え、かつ views < hit_threshold
        #   (1,000 以上は急伸検知トラックに任せる)。急伸 state 既知の動画 (score 経由で通知済み含む) は除外。
        early_path = ch_dir / "alerted_early.json"
        try:
            early_state = json.loads(early_path.read_text(encoding="utf-8")) if early_path.exists() else None
        except (json.JSONDecodeError, OSError):
            early_state = None
        early_candidates = [
            r
            for r in rows
            if any(r["age_days"] <= a and r["views"] >= v for a, v in EARLY_TIERS)
            and r["views"] < args.hit_threshold
            and r["video_id"] not in alerted
        ]
        if early_state is None:
            early_state = {
                r["video_id"]: {"ts": snapshot_meta["timestamp"], "views": r["views"], "seeded": True}
                for r in early_candidates
            }
            early_path.write_text(json.dumps(early_state, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  (初速検知: state 初期化、{len(early_state)} 件を silent seed)")
        else:
            new_early = [r for r in early_candidates if r["video_id"] not in early_state]
            if new_early:
                new_early.sort(key=lambda x: x["views"], reverse=True)
                print(f"  ⚡ 初速検知 (H-33 分水嶺超え・1,000到達前の予告): {len(new_early)} 件")
                for r in new_early:
                    url = f"https://www.youtube.com/watch?v={r['video_id']}"
                    print(f"    views={r['views']:,} age={r['age_days']}d  {r['title'][:55]}")
                    print(f"      {url}")
                    early_state[r["video_id"]] = {"ts": snapshot_meta["timestamp"], "views": r["views"]}
                    all_early_alerts.append(
                        {"channel": ch["title"], "channel_id": cid, "url": url, **r})
                early_path.write_text(json.dumps(early_state, ensure_ascii=False, indent=2), encoding="utf-8")

        # 新作アラート (24h 以内)
        if new_videos_this_ch:
            print(f"  🆕 新作 (24h 以内、前回 snapshot に未存在): {len(new_videos_this_ch)} 件")
            for nv in new_videos_this_ch:
                url = f"https://www.youtube.com/watch?v={nv['video_id']}"
                print(f"    {nv['title'][:55]}  views={nv['views']}  age={nv['age_days']}d")
                print(f"      {url}")
                # workflow が grep する機械可読マーカー
                safe_title = nv["title"].replace("|", "/")
                print(
                    f"NEW_VIDEO_DETECTED|{ch['title']}|{nv['video_id']}|{nv['published_at']}|{nv['views']}|{safe_title}|{url}"
                )
                all_new_videos.append({"channel": ch["title"], "channel_id": cid, **nv})
        print()

    # 急伸/新作アラートに類似HIT・タイミング分析を添えて JSON 出力 (workflow が Discord に送る)
    if args.no_alert_files:
        hit_n, new_n, prime_n, early_n = 0, 0, 0, 0
        print("(--no-alert-files: 通知ファイルはスキップ)")
    else:
        Path("early_alerts.json").write_text(
            json.dumps(all_early_alerts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        early_n = len(all_early_alerts)
        # 類似HIT検索コーパス = 内輪の全動画履歴 + 外部HITコーパス (market_validate /
        # discovery_loop が蓄積した監視枠外の HIT)。しきい値・like基準線の母集団には混ぜない
        # — 「判定は内輪、照合は YouTube 全体」の分離。
        ext_rows = load_corpus_rows(base, set(per_ch))
        if ext_rows:
            print(f"(外部HITコーパス: {len(ext_rows)} 件を類似HIT照合に合流)")
        sim_rows = all_rows + ext_rows
        hit_n, new_n = enrich_and_write(
            all_hit_alerts, all_new_videos, all_rows, base, args.hit_threshold,
            args.hit_alert_score, sim_rows=sim_rows,
        )
        # 🎯 本命検知: 相対HIT × 横展開実証 × like率健全 (急伸検知とは独立トラック)
        prime_alerts = detect_prime(
            per_ch, all_rows, args.hit_alert_score, args.recent_days,
            now.strftime("%Y-%m-%dT%H:%M:%SZ"), sim_rows=sim_rows,
        )
        Path("prime_alerts.json").write_text(
            json.dumps(prime_alerts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        prime_n = len(prime_alerts)

    print(f"=== 全 priority_channels の ★直近 HIT 集約: {len(all_recent_hits)} 件 ===")
    print(f"=== 全 priority_channels の 🆕 24h 新作: {new_n} 件 (→ new_video_alerts.json) ===")
    print(f"=== 全 priority_channels の 🚀 急伸検知 (初 HIT 突破): {hit_n} 件 (→ hit_alerts.json) ===")
    print(f"=== 全 priority_channels の ⚡ 初速検知 (H-33 分水嶺・1,000前の予告): {early_n} 件 (→ early_alerts.json) ===")
    print(f"=== 全 priority_channels の 🎯 本命検知 (相対HIT×横展開×like健全): {prime_n} 件 (→ prime_alerts.json) ===")
    # quota 自己計上 (ログは boosted 実行でも time_series/ 側に集約)
    spent = quota_meter.flush(
        Path("time_series"), "priority" if args.out_dir == "time_series" else "boosted")
    print(f"=== YouTube API 推定消費: {spent}u (→ time_series/{quota_meter.LOG_FILE}) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
