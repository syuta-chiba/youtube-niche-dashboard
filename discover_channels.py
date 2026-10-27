"""ニッチ内の競合チャンネル候補を炙り出す discovery CLI。

複数キーワードで動画検索 → 出現チャンネルを集約 → 複数キーワードに
登場したチャンネルほど「ニッチの中心」にいるという仮説でランキング。

「AIすごい系」(単一キーワードヒット) と「業務具体事例系」(議事録・Excel・
経営など複数業務軸でヒット) を min-keywords で分離するのが要点。

quota: search.list は 1 回 100 unit。デフォルト 12 キーワードで ~1200 unit
(1 日無料枠 10,000 のうち 12%)。1 日 1 回の発掘想定。
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import quota_meter


# AI活用 × 業務自動化 × 中小企業向け を狙う既定キーワード集。
# ツール軸 + 業務領域軸 + 経営軸を混ぜて、AIすごい系を弾けるようにしてある。
DEFAULT_KEYWORDS = [
    "Claude Code 業務",
    "Claude 自動化 業務",
    "ChatGPT 業務効率化",
    "AI 業務自動化 中小企業",
    "GAS 自動化 業務",
    "n8n 業務自動化",
    "Make.com 業務効率化",
    "AI 議事録 自動化",
    "AI Excel 自動化",
    "AI メール 自動化",
    "AI エージェント 業務",
    "業務効率化 AI 事例",
]


def search_videos(youtube, query: str, limit: int, region: str):
    items = []
    token = None
    while len(items) < limit:
        resp = (
            youtube.search()
            .list(
                part="snippet",
                q=query,
                type="video",
                maxResults=min(50, limit - len(items)),
                relevanceLanguage="ja",
                regionCode=region,
                order="relevance",
                pageToken=token,
            )
            .execute()
        )
        quota_meter.add(100)  # search.list は 1 リクエスト 100u
        items.extend(resp.get("items", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return items


def fetch_channel_stats(youtube, channel_ids):
    out = {}
    ids = list(channel_ids)
    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        resp = (
            youtube.channels()
            .list(part="snippet,statistics", id=",".join(batch), maxResults=50)
            .execute()
        )
        quota_meter.add(1)
        for item in resp.get("items", []):
            out[item["id"]] = {
                "title": item["snippet"]["title"],
                "subs": int(item["statistics"].get("subscriberCount", 0)),
                "videos": int(item["statistics"].get("videoCount", 0)),
                "hidden_subs": item["statistics"].get("hiddenSubscriberCount", False),
            }
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--keywords-file",
        help="改行区切りキーワードファイル (# でコメント可、省略時は内蔵デフォルト)",
    )
    p.add_argument(
        "--print-keywords", action="store_true",
        help="使用キーワードを表示して終了 (実行前の確認用)",
    )
    p.add_argument("--per-query", type=int, default=25, help="各キーワードで取得する動画数 (max 50)")
    p.add_argument("--min-subs", type=int, default=10000, help="登録者数下限 (default 1万)")
    p.add_argument("--max-subs", type=int, default=100000, help="登録者数上限 (default 10万)")
    p.add_argument(
        "--min-keywords", type=int, default=2,
        help="このキーワード数未満ヒットのチャンネルは除外 (default 2)",
    )
    p.add_argument("--region", default="JP")
    p.add_argument("-o", "--output", default="discovered.csv")
    p.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"))
    args = p.parse_args()

    if args.keywords_file:
        keywords = [
            ln.strip()
            for ln in Path(args.keywords_file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    else:
        keywords = DEFAULT_KEYWORDS

    if args.print_keywords:
        for kw in keywords:
            print(kw)
        return 0

    if not args.api_key:
        print("ERROR: YOUTUBE_API_KEY が未設定", file=sys.stderr)
        return 1

    print(
        f"[info] {len(keywords)} キーワード × {args.per_query} 件 / "
        f"消費見込 ~{len(keywords) * 100} unit",
        file=sys.stderr,
    )

    youtube = build("youtube", "v3", developerKey=args.api_key, cache_discovery=False)

    channel_hits: dict[str, dict] = defaultdict(
        lambda: {"keywords": set(), "sample_titles": []}
    )

    for kw in keywords:
        print(f"[info] '{kw}' 検索中…", file=sys.stderr)
        try:
            items = search_videos(youtube, kw, args.per_query, args.region)
        except HttpError as e:
            msg = str(e)
            if "quotaExceeded" in msg:
                print("[error] クォータ超過、ここまでの結果で集計", file=sys.stderr)
                break
            print(f"[warn] '{kw}' 検索失敗: {e}", file=sys.stderr)
            continue
        for item in items:
            cid = item["snippet"]["channelId"]
            channel_hits[cid]["keywords"].add(kw)
            if len(channel_hits[cid]["sample_titles"]) < 3:
                channel_hits[cid]["sample_titles"].append(item["snippet"]["title"])

    candidate_ids = [
        cid for cid, hit in channel_hits.items()
        if len(hit["keywords"]) >= args.min_keywords
    ]
    print(
        f"[info] {len(channel_hits)} チャンネル中 "
        f"{len(candidate_ids)} 件が {args.min_keywords}+ キーワードでヒット",
        file=sys.stderr,
    )

    if not candidate_ids:
        print(
            "候補なし。--min-keywords を下げるかキーワードを追加してください。",
            file=sys.stderr,
        )
        quota_meter.flush(Path("time_series"), "discover_manual")
        return 0

    try:
        stats = fetch_channel_stats(youtube, candidate_ids)
    except HttpError as e:
        print(f"ERROR: 統計取得失敗: {e}", file=sys.stderr)
        return 2

    rows = []
    for cid, info in stats.items():
        if info["hidden_subs"]:
            continue
        if info["subs"] < args.min_subs or info["subs"] > args.max_subs:
            continue
        hit = channel_hits[cid]
        rows.append(
            {
                "channel_id": cid,
                "title": info["title"],
                "subs": info["subs"],
                "videos": info["videos"],
                "kw_hits": len(hit["keywords"]),
                "keywords": " | ".join(sorted(hit["keywords"])),
                "sample_titles": " // ".join(hit["sample_titles"]),
            }
        )

    # ランキング: キーワードヒット数優先、同点はサブ数で並べる
    rows.sort(key=lambda r: (r["kw_hits"], r["subs"]), reverse=True)

    fields = ["channel_id", "title", "subs", "videos", "kw_hits", "keywords", "sample_titles"]
    with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n=== Top {min(20, len(rows))} 候補 ===")
    print(f"{'channel_id':<26} {'subs':>8} {'kw':>3}  title")
    print("-" * 80)
    for r in rows[:20]:
        print(f"{r['channel_id']:<26} {r['subs']:>8,} {r['kw_hits']:>3}  {r['title']}")

    print(f"\n[done] 全 {len(rows)} 件を {args.output} に出力", file=sys.stderr)
    quota_meter.flush(Path("time_series"), "discover_manual")
    return 0


if __name__ == "__main__":
    sys.exit(main())
