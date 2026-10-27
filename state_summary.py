"""いまの観測体制の全体像を1枚にまとめる — Slack 通知用。

「何chをウォッチしていて、何ch発見していて、今日いくつ増えたか」を
追加 quota ゼロ（commit 済みファイルを読むだけ）で出す。

Usage:
    python state_summary.py                 # 人が読む形（標準出力）
    python state_summary.py --slack         # Slack 用のテキスト1本
    python state_summary.py --slack --today 2026-08-10
"""
import argparse
import collections
import datetime as dt
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_json(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return {}


def _corpus_axis(name):
    """外部HITコーパスを axis ラベル別に数える（corpus_filter.py が付ける）。"""
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return 0, collections.Counter()
    n = 0
    c = collections.Counter()
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        n += 1
        try:
            c[json.loads(line).get("axis", "unlabeled")] += 1
        except Exception:
            c["unlabeled"] += 1
    return n, c


def _count_lines(name, key=None):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return 0, set()
    n, ids = 0, set()
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        n += 1
        if key:
            try:
                ids.add(json.loads(line).get(key))
            except Exception:
                pass
    return n, ids


def _channels(name):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return []
    out = []
    for line in io.open(p, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line.startswith("UC"):
            out.append(line.split()[0])
    return out


def collect(today):
    seen = _load_json("discovery_seen.json")
    pool = _load_json("rss_pool.json")
    watch = _channels("priority_channels.txt")
    boosted = _channels("boosted_channels.txt")
    hits_n, hit_ids = _count_lines(os.path.join("time_series", "external_hits.jsonl"), "video_id")
    _, axis = _corpus_axis(os.path.join("time_series", "external_hits.jsonl"))

    by_date = collections.Counter(v.get("date") for v in seen.values() if v.get("date"))
    pool_by_date = collections.Counter(v.get("added") for v in pool.values() if v.get("added"))
    decision = collections.Counter(v.get("decision") for v in seen.values())

    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(7)]
    week = sum(by_date.get(d, 0) for d in days)

    return {
        "today": today.isoformat(),
        "new_today": by_date.get(today.isoformat(), 0),
        "new_pool_today": pool_by_date.get(today.isoformat(), 0),
        "new_week": week,
        "seen_total": len(seen),
        "pool_total": len(pool),
        "watch": len(watch),
        "boosted": len(boosted),
        "hits_total": len(hit_ids) or hits_n,
        "hits_in": axis.get("in", 0),
        "hits_unknown": axis.get("unknown", 0) + axis.get("unlabeled", 0),
        "decision": decision,
        "recent": [(d, by_date.get(d, 0)) for d in reversed(days)],
    }


def render_human(s):
    L = []
    L.append(f"=== 観測体制サマリ ({s['today']}) ===")
    L.append("")
    L.append(f"  定点観測ch (priority)     : {s['watch']:>6}  ← 4時間おきに全動画を追跡")
    L.append(f"  広告混ざり枠 (boosted)    : {s['boosted']:>6}")
    L.append(f"  RSS巡回プール             : {s['pool_total']:>6}  ← 毎朝 quota 0 で全員巡回")
    L.append(f"  発見して評価済みch (累計) : {s['seen_total']:>6}")
    L.append(f"  外部HITコーパス (動画)    : {s['hits_total']:>6}  （技術軸 {s['hits_in']} / 未判定 {s['hits_unknown']}）")
    L.append("")
    L.append(f"  今日の新規発見            : {s['new_today']:>6} ch")
    L.append(f"  直近7日の新規発見         : {s['new_week']:>6} ch")
    L.append("")
    L.append("  日別の新規発見（直近7日）")
    for d, n in s["recent"]:
        bar = "█" * min(n, 40)
        L.append(f"    {d}  {n:>3}  {bar}")
    L.append("")
    L.append("  評価の内訳: " + " / ".join(f"{k}={v}" for k, v in s["decision"].most_common()))
    return "\n".join(L)


def render_slack(s):
    spark = " ".join(f"{n}" for _, n in s["recent"])
    lines = [
        f":satellite: *観測体制サマリ* ({s['today']})",
        f"• 今日の新規発見: *{s['new_today']} ch*　（直近7日 {s['new_week']} ch / 日別 {spark}）",
        f"• 定点観測: *{s['watch']} ch*　RSS巡回プール: *{s['pool_total']} ch*　評価済み累計: *{s['seen_total']} ch*",
        f"• 外部HITコーパス: *{s['hits_total']} 本*（うち技術軸 *{s['hits_in']} 本* / 未判定 {s['hits_unknown']}）",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slack", action="store_true", help="Slack 用テキストを1本で出す")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD（テスト用）")
    ap.add_argument("--count", choices=["seen", "pool", "watch", "hits"],
                    help="その数字だけを出す（workflow から差分を取る用）")
    ap.add_argument("--header", default=None, help="Slack 本文の先頭に置く見出し")
    ap.add_argument("--line", action="append", default=[], help="見出しの下に足す行（複数可）")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
    s = collect(today)
    if args.count:
        print(s[{"seen": "seen_total", "pool": "pool_total",
                 "watch": "watch", "hits": "hits_total"}[args.count]])
        return
    if not args.slack:
        print(render_human(s))
        return
    head = []
    if args.header:
        head.append(args.header)
    head += [f"• {x}" for x in args.line]
    body = render_slack(s)
    print(("\n".join(head) + "\n\n" + body) if head else body)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
