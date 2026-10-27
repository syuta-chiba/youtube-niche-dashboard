# -*- coding: utf-8 -*-
"""伸び方の「形」で広告 (プロモート) とおすすめを見分ける (docs/ad_check_methodology.md 補助判定)。

形の定義 (2026-10-21・自ch PHP入門の広告16日間と AIの歩き方 10月 HIT 8本から):
  おすすめ : 公開 1〜2 日目がピーク → 日ごとに減衰 → 崖が無く尾 (数週間 +数十/日) が続く
  広告     : 初日がピークにならず予算ペースの台地が続く → 予算終了日に崖 (翌日からほぼゼロ) → 尾が無い

データ源 (どちらも日次の累計 views):
  1. time_series/<channel_id>/videos.jsonl   観測 ch (1 日 4 点) — 日ごとの最後の観測を使う
  2. time_series/rss_video_series.jsonl      RSS 巡回プール全 ch の最新 15 本 (1 日 1 点・rss_patrol.py が追記)
     1 行 = 1 動画 {"v": video_id, "c": channel_id, "p": 公開日, "f": 記録開始日, "s": [累計views...], "l": [likes...]}
     s[i] は f から i 日後の値 (欠測は null)。公開 SERIES_MAX_AGE 日で打ち切り。

使い方:
  python view_shape.py <video_id> [...]            動画の形を表示
  python view_shape.py --channel <channel_id>      その ch で系列がある動画を全部
  python view_shape.py --selftest                  PHP入門 (広告) と AIの歩き方 (おすすめ) の実測で自己検証
"""
from __future__ import annotations
import json, sys, io, argparse, collections
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERIES_FILE = ROOT / "time_series" / "rss_video_series.jsonl"
SERIES_MAX_AGE = 60      # 公開からこの日数を過ぎた動画は系列を打ち切る (形は最初の 2〜3 週で決まる)
MIN_POINTS = 5           # 判定に必要な日数

# ---------------------------------------------------------------- 系列の読み込み

def load_rss_series() -> dict[str, dict]:
    out = {}
    if SERIES_FILE.exists():
        for line in SERIES_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line); out[r["v"]] = r
    return out


def save_rss_series(d: dict[str, dict]) -> None:
    SERIES_FILE.parent.mkdir(exist_ok=True)
    lines = [json.dumps(d[k], ensure_ascii=False, separators=(",", ":")) for k in sorted(d)]
    SERIES_FILE.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def update_rss_series(d: dict[str, dict], channel_id: str, entries: list[dict], today: str) -> int:
    """rss_patrol の fetch_feed 結果 (video_id/views/likes/published_at/age_days) を今日の点として記録。
    返り値 = 記録した動画数。公開 SERIES_MAX_AGE 日超は記録しない。"""
    n = 0
    for e in entries:
        if e.get("age_days", 9999) > SERIES_MAX_AGE or not e.get("video_id"):
            continue
        vid = e["video_id"]
        rec = d.get(vid)
        if rec is None:
            rec = d[vid] = {"v": vid, "c": channel_id, "p": (e.get("published_at") or "")[:10], "f": today, "s": [], "l": []}
        idx = (date.fromisoformat(today) - date.fromisoformat(rec["f"])).days
        if idx < 0:
            continue
        for key in ("s", "l"):
            arr = rec[key]
            while len(arr) <= idx:
                arr.append(None)
        rec["s"][idx] = int(e.get("views") or 0)
        rec["l"][idx] = int(e.get("likes") or 0)
        n += 1
    return n


def prune_rss_series(d: dict[str, dict], today: str) -> int:
    """公開 SERIES_MAX_AGE 日を過ぎた動画を落とす。返り値 = 落とした数。"""
    t = date.fromisoformat(today); drop = []
    for vid, rec in d.items():
        try:
            if (t - date.fromisoformat(rec["p"])).days > SERIES_MAX_AGE:
                drop.append(vid)
        except ValueError:
            drop.append(vid)
    for vid in drop:
        del d[vid]
    return len(drop)


def series_from_videos_jsonl(channel_id: str, video_id: str) -> tuple[list[int | None], str | None, str | None]:
    """観測 ch の videos.jsonl から日次の累計 views (日ごとの最後の観測)。返り値 (系列, 公開日, 観測開始日)。"""
    f = ROOT / "time_series" / channel_id / "videos.jsonl"
    if not f.exists():
        return [], None, None
    byday: dict[str, int] = {}; pub = None
    for line in f.read_text(encoding="utf-8").splitlines():
        if video_id not in line:
            continue
        o = json.loads(line)
        if o.get("video_id") != video_id:
            continue
        byday[o["ts"][:10]] = o["views"]; pub = pub or (o.get("published_at") or "")[:10]
    if not byday:
        return [], None, None
    days = sorted(byday); d0 = date.fromisoformat(days[0]); last = date.fromisoformat(days[-1])
    out: list[int | None] = [None] * ((last - d0).days + 1)
    for k, v in byday.items():
        out[(date.fromisoformat(k) - d0).days] = v
    return out, pub, days[0]


def series_for(video_id: str, channel_id: str | None = None) -> tuple[list[int | None], dict]:
    """どちらかの源から系列を返す。meta に source / published / first を入れる。"""
    rss = load_rss_series().get(video_id)
    if channel_id:
        s, pub, first = series_from_videos_jsonl(channel_id, video_id)
        if len([x for x in s if x is not None]) >= MIN_POINTS:
            return s, {"source": "videos.jsonl", "published": pub, "first": first}
    if rss:
        return rss["s"], {"source": "rss", "published": rss.get("p"), "first": rss.get("f"), "channel": rss.get("c")}
    if channel_id:
        s, pub, first = series_from_videos_jsonl(channel_id, video_id)
        return s, {"source": "videos.jsonl", "published": pub, "first": first}
    return [], {}

# ---------------------------------------------------------------- 形の判定

def daily_deltas(cum: list[int | None]) -> list[int | None]:
    """累計 → 日次増分。欠測は None (前後の差を日数で割って埋めない: 形を鈍らせるため)。"""
    out: list[int | None] = []
    prev = None; prev_i = None
    for i, v in enumerate(cum):
        if v is None:
            out.append(None); continue
        if prev is None:
            out.append(None)
        else:
            gap = i - prev_i
            out.append(max(0, (v - prev) // gap) if gap == 1 else None)
        prev, prev_i = v, i
    return out


def classify(cum: list[int | None], published: str | None = None, first: str | None = None) -> dict:
    """形を判定する。返り値 {shape, label, peak_day, plateau_days, cliff_day, tail_ratio, decay_ratio, n}。
    shape: "organic" (減衰+尾) / "ad_like" (台地+崖+尾なし) / "unclear" / "insufficient"。
    系列の 0 日目が公開日と離れていると「初日ピーク」が見えないので、first と published の差 (offset) を考慮する。"""
    d = daily_deltas(cum)
    pts = [(i, x) for i, x in enumerate(d) if x is not None]
    if len(pts) < MIN_POINTS:
        return {"shape": "insufficient", "label": f"日次が {len(pts)} 日分 (要 {MIN_POINTS})", "n": len(pts)}
    offset = 0
    if published and first:
        try:
            offset = max(0, (date.fromisoformat(first) - date.fromisoformat(published)).days)
        except ValueError:
            pass
    vals = [x for _, x in pts]
    mx = max(vals)
    if mx < 20:
        return {"shape": "insufficient", "label": "動きが小さすぎる (最大 +%d/日)" % mx, "n": len(pts)}
    # 波の始まり = 初めて最大の 15% 以上が出た日。観測開始が公開から離れていて、しかも最初から波の最中なら
    # 立ち上がりが見えていない (古い動画の尾を台地と誤認しないため「後期」扱い)。
    wave_k = next(k for k, (_, x) in enumerate(pts) if x >= 0.15 * mx)
    if offset > 5 and wave_k == 0:
        avg = sum(vals[-7:]) / len(vals[-7:])
        return {"shape": "late", "label": f"公開 {offset} 日目からの観測 (立ち上がり未観測)・直近 +{avg:.0f}/日", "n": len(pts), "offset": offset}
    peak_k = next(k for k, (_, x) in enumerate(pts) if x == mx)
    peak_i = pts[peak_k][0]
    peak_day = peak_i + offset                       # 公開日からの日数
    peak_rel = peak_k - wave_k                        # 波の始まりからの日数 (古い動画に後から来た波にも使う)
    # 台地: 最大の 40% 以上が出た日数 (連続でなくてよい)
    plateau_days = sum(1 for x in vals if x >= 0.4 * mx)
    # 崖: 「直前 3 日平均の 5% 未満」に落ちて、その後ずっと最大の 3% 未満の日
    cliff_day = None
    for k in range(3, len(pts) - 2):
        i, x = pts[k]
        prev3 = [y for _, y in pts[k - 3:k]]; next3 = [y for _, y in pts[k:k + 3]]
        pm = sum(prev3) / 3; nm = sum(next3) / 3
        # 直前 3 日平均が最大の 25% 以上 → 直後 3 日平均がその 8% 未満 → 以降ずっと最大の 3% 未満
        if pm >= 0.25 * mx and nm < 0.08 * pm and all(y < 0.03 * mx for _, y in pts[k + 1:]):
            cliff_day = i + offset; break
    # 尾: 最後 5 日の平均 ÷ 最大 (崖があれば崖の後)
    last5 = [x for _, x in pts[-5:]]
    tail_ratio = (sum(last5) / len(last5)) / mx
    # 減衰: ピーク後 3〜9 日の中央値 ÷ ピーク
    post = [x for i, x in pts if peak_i + 3 <= i <= peak_i + 9]
    decay_ratio = (sorted(post)[len(post) // 2] / mx) if post else None
    res = {"peak_day": peak_day, "plateau_days": plateau_days, "cliff_day": cliff_day,
           "tail_ratio": round(tail_ratio, 4), "decay_ratio": None if decay_ratio is None else round(decay_ratio, 3),
           "n": len(pts), "max_delta": mx}
    if plateau_days >= 4 and cliff_day is not None and tail_ratio < 0.01:
        res.update(shape="ad_like", label=f"台地 {plateau_days} 日 → 公開 {cliff_day} 日目に崖 → 尾なし (広告の形)")
    elif peak_rel <= 2 and decay_ratio is not None and decay_ratio <= 0.35 and tail_ratio >= 0.003:
        when = f"公開 {peak_day} 日目" if offset <= 5 else f"波の {peak_rel} 日目 (公開 {peak_day} 日目)"
        res.update(shape="organic", label=f"{when}ピーク → 減衰 (×{decay_ratio:.2f}) → 尾あり (おすすめの形)")
    elif plateau_days >= 5 and peak_rel > 2:
        res.update(shape="unclear", label=f"台地 {plateau_days} 日・ピークが波の {peak_rel} 日目 (広告寄り・崖は未観測)")
    else:
        res.update(shape="unclear", label=f"ピーク {peak_day} 日目 / 減衰 ×{decay_ratio if decay_ratio is not None else '-'} / 尾 {tail_ratio:.3f}")
    return res


def shape_mark(res: dict) -> str:
    return {"organic": "📈自然", "ad_like": "📢広告型", "unclear": "❔不明", "late": "⏩後期", "insufficient": "・系列なし"}.get(res.get("shape"), "")

# ---------------------------------------------------------------- CLI

def _selftest() -> int:
    # 自ch PHP入門 (EIabeQcBmYM) の Analytics 日次 (2025-12-26〜2026-01-20): 広告 16 日 → 崖
    php = [0, 109, 661, 540, 851, 616, 217, 328, 344, 564, 124, 160, 524, 399, 314, 339, 55, 0, 2, 0, 1, 0, 0, 1, 2, 0]
    cum = [126]
    for x in php[1:]:
        cum.append(cum[-1] + x)
    r = classify(cum, published="2025-05-16", first="2025-12-26")
    print("PHP入門 (広告97%):", r["shape"], r["label"])
    ok = r["shape"] == "ad_like"
    # AIの歩き方 10/09 国産ローカルAI (videos.jsonl の日次) : おすすめ
    ai = [883, 3411, 5670, 7077, 7291, 7443, 7481, 7568, 7621, 7647, 7664, 7678, 7678]
    r = classify(ai, published="2026-10-09", first="2026-10-09")
    print("AIの歩き方 10/09:", r["shape"], r["label"])
    ok &= r["shape"] == "organic"
    ai2 = [2366, 4383, 6320, 8707, 8956, 9083, 9107]
    r = classify(ai2, published="2026-10-15", first="2026-10-15")
    print("AIの歩き方 10/15:", r["shape"], r["label"])
    ok &= r["shape"] in ("organic", "unclear")
    print("selftest:", "OK" if ok else "NG")
    return 0 if ok else 1


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_ids", nargs="*")
    ap.add_argument("--channel", help="channel_id (videos.jsonl / RSS 系列から全動画)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    targets: list[tuple[str, str | None]] = [(v, a.channel) for v in a.video_ids]
    if a.channel and not a.video_ids:
        rss = load_rss_series()
        targets += [(v, a.channel) for v, r in rss.items() if r.get("c") == a.channel]
        f = ROOT / "time_series" / a.channel / "videos.jsonl"
        if f.exists():
            seen = set()
            for line in f.read_text(encoding="utf-8").splitlines():
                o = json.loads(line); seen.add(o["video_id"])
            targets += [(v, a.channel) for v in seen if v not in {t[0] for t in targets}]
    if not targets:
        ap.print_help(); return 2
    for vid, cid in targets:
        s, meta = series_for(vid, cid)
        r = classify(s, meta.get("published"), meta.get("first"))
        d = daily_deltas(s)
        tail = " ".join("-" if x is None else f"+{x}" for x in d[1:15])
        print(f"{shape_mark(r):6s} {vid} [{meta.get('source', '-')}] 公開 {meta.get('published') or '?'} | {r['label']}\n        {tail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
