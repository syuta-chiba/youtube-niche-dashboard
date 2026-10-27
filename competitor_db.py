# -*- coding: utf-8 -*-
"""competitor_db.py — 競合全chの全動画を貯める永続DB (横断分析の「分母」)

なぜ要るか:
  external_hits.jsonl は **HIT だけ** のコーパスなので flop の分母が無く、打率が出せない
  (docs/hit_system.md §5b)。rss_pool.json の ~2,000ch は「中央値」しか残していない。
  ここで **全ch × 全動画 (1本1スナップショット)** を貯め、hit_rate.py / theme_scan.py の
  分母にする。これで「このテーマ、Nch中X%がヒット」を flop 込みで言える。

置き場所 (別リポジトリ推奨):
  既定 = env CORPUS_DIR、無ければ ../youtube-niche-corpus。--corpus-dir で上書き。
    channels/<channel_id>/videos.jsonl   ch別・追記/更新。schema は time_series の
                                         videos.jsonl と同一 (hit_rate.load_population 互換)
    manifest.json                        ch → {n_videos, last_backfill, last_refresh, shard}
  行 schema:  {ts, video_id, title, views, likes, published_at, duration_sec}
    - 1動画1スナップショット (時系列は持たない = git 差分が膨らまない)
    - ch別ファイル = 100MB/ファイル上限に当たらない・変わった ch のファイルだけ差分に出る

2経路でデータを作る (RSS は新着専用という制約への対処):
  backfill … 全アップロード履歴を playlistItems で列挙 → videos.list で stats → 追記。
             過去の深さ(=分母)はこれが作る。--budget で quota 上限、超えたら中断(レジューム可)。
  refresh  … RSS フィード(quota 0)で新着だけ検知 → 追記。backfill 後を安く最新に保つ係。
             RSS は views/likes も返す (media:statistics) ので新着は追加 quota ほぼ 0。

quota: playlistItems 1u/50本・videos.list 1u/50本。1ch ~200本 ≒ 8u。~2,000ch ≒ 16k u。
  → GitHub Actions で 1日 --budget 数千 に絞り、数日かけて初回バックフィル。以後は refresh 主体。
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import quota_meter
from rss_patrol import (
    DURATION_RE,
    OWN_CHANNEL_ID,
    POOL_FILE,
    build_pool,
    fetch_feed,
    load_json,
)

API = "https://www.googleapis.com/youtube/v3"
ROOT = Path(__file__).resolve().parent
MANIFEST = "manifest.json"
PER_CHANNEL_CAP = 1000   # 1ch あたり列挙する最大本数 (RECENT_VIDEOS と同値)
BATCH = 50               # playlistItems / videos.list の 1 リクエスト上限


def corpus_root(cli: str | None) -> Path:
    """DB の置き場所。--corpus-dir > env CORPUS_DIR > ../youtube-niche-corpus。"""
    d = cli or os.environ.get("CORPUS_DIR") or str(ROOT.parent / "youtube-niche-corpus")
    p = Path(d)
    (p / "channels").mkdir(parents=True, exist_ok=True)
    return p


def get(path: str, params: dict) -> dict:
    """YouTube Data API 1 コール。quota_meter に記録する (drain の予算計算と整合)。"""
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        quota_meter.add(100 if path == "search" else 1)
        return json.load(r)


def parse_duration(iso: str) -> int:
    """ISO8601 (PT#H#M#S) → 秒。パース不能は 0 (=不明、Shorts 判定は保守的に長尺扱い)。"""
    m = DURATION_RE.fullmatch(iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def uploads_playlist(channel_id: str) -> str:
    """UCxxxx → UUxxxx (uploads playlist)。"""
    return "UU" + channel_id[2:]


def channel_file(root: Path, cid: str) -> Path:
    return root / "channels" / cid / "videos.jsonl"


def load_existing_ids(path: Path) -> set[str]:
    """その ch の jsonl に既にある video_id 集合 (重複追記の回避)。"""
    ids: set[str] = set()
    if not path.exists():
        return ids
    for line in io.open(path, encoding="utf-8"):
        try:
            vid = json.loads(line).get("video_id")
        except Exception:
            continue
        if vid:
            ids.add(vid)
    return ids


def list_upload_ids(cid: str, cap: int) -> list[str]:
    """ch の全アップロード動画 ID を新しい順に列挙 (最大 cap 本)。
    削除/非公開 ch の playlist は 404 → 空。"""
    ids: list[str] = []
    token = None
    while len(ids) < cap:
        params = {"part": "contentDetails", "maxResults": BATCH,
                  "playlistId": uploads_playlist(cid)}
        if token:
            params["pageToken"] = token
        try:
            data = get("playlistItems", params)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            raise
        for it in data.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            if vid:
                ids.append(vid)
        token = data.get("nextPageToken")
        if not token:
            break
    return ids[:cap]


def fetch_stats(video_ids: list[str]) -> list[dict]:
    """video_ids を 50件バッチで videos.list → 行 schema の dict 群。"""
    out: list[dict] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(0, len(video_ids), BATCH):
        chunk = video_ids[i:i + BATCH]
        data = get("videos", {"part": "snippet,statistics,contentDetails",
                              "id": ",".join(chunk), "maxResults": BATCH})
        for it in data.get("items", []):
            st = it.get("statistics", {})
            sn = it.get("snippet", {})
            out.append({
                "ts": ts,
                "video_id": it["id"],
                "title": sn.get("title", ""),
                "views": int(st.get("viewCount", 0)) if st.get("viewCount") is not None else 0,
                # likeCount 非公開 ch は欠落 → None (0 と区別: 広告チェックの誤判定回避)
                "likes": int(st["likeCount"]) if st.get("likeCount") is not None else None,
                "published_at": sn.get("publishedAt", ""),
                "duration_sec": parse_duration(it.get("contentDetails", {}).get("duration", "")),
            })
    return out


def append_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with io.open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def load_manifest(root: Path) -> dict:
    return load_json(root / MANIFEST, {"channels": {}, "shard": os.path.basename(str(root))})


def save_manifest(root: Path, man: dict) -> None:
    (root / MANIFEST).write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")


def pool_channel_ids() -> list[str]:
    """観測対象プール = rss_pool ∪ discovery_seen ∪ 外部HITコーパス (priority/boosted/自ch除外)。
    build_pool が既にその和集合を作っているので流用する。"""
    pool = build_pool(ROOT)
    return [cid for cid in pool if cid and cid != OWN_CHANNEL_ID]


def backfill(root: Path, budget: int, per_channel_cap: int, dry: bool) -> int:
    """全アップロード履歴を列挙 → 未取得分の stats を追記。budget(u) 超過で中断(レジューム可)。"""
    man = load_manifest(root)
    chans = man["channels"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ids = pool_channel_ids()
    # 未バックフィル → last_backfill が古い順。同テーマの偏りを避けるため決定的順序。
    ids.sort(key=lambda c: (chans.get(c, {}).get("last_backfill") or "", c))
    print(f"[info] プール {len(ids)}ch / 予算 {budget}u / 1chキャップ {per_channel_cap}本", file=sys.stderr)

    done = added_videos = 0
    for cid in ids:
        if quota_meter.total() >= budget:
            print(f"[info] 予算到達 ({quota_meter.total()}u)。{done}ch 完了で中断。次回レジューム", file=sys.stderr)
            break
        path = channel_file(root, cid)
        have = load_existing_ids(path)
        try:
            all_ids = list_upload_ids(cid, per_channel_cap)
        except urllib.error.HTTPError as e:
            print(f"[warn] {cid} playlistItems 失敗 {e.code}", file=sys.stderr)
            continue
        missing = [v for v in all_ids if v not in have]
        if dry:
            print(f"[dry] {cid}: 全{len(all_ids)}本 / 未取得{len(missing)}本", file=sys.stderr)
            done += 1
            continue
        rows = fetch_stats(missing) if missing else []
        append_rows(path, rows)
        added_videos += len(rows)
        chans[cid] = {**chans.get(cid, {}), "last_backfill": today,
                      "n_videos": len(have) + len(rows)}
        done += 1
        if done % 25 == 0:
            save_manifest(root, man)
            print(f"[info] {done}ch 完了 / 追加 {added_videos}本 / 消費 {quota_meter.total()}u", file=sys.stderr)
    if not dry:
        save_manifest(root, man)
    print(f"BACKFILL_SUMMARY|channels={done}|videos={added_videos}|quota={quota_meter.total()}")
    return 0


def refresh(root: Path, dry: bool) -> int:
    """RSS フィード(quota 0)で新着だけ追記。backfill 済み ch を安く最新に保つ。"""
    man = load_manifest(root)
    chans = man["channels"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ids = pool_channel_ids()
    print(f"[info] refresh 対象 {len(ids)}ch (RSS・quota 0)", file=sys.stderr)

    touched = added = 0
    for cid in ids:
        entries = fetch_feed(cid)
        if not entries:
            continue
        path = channel_file(root, cid)
        have = load_existing_ids(path)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        for e in entries:
            if e["video_id"] in have:
                continue
            rows.append({
                "ts": ts, "video_id": e["video_id"], "title": e["title"],
                "views": e["views"], "likes": e.get("likes"),
                "published_at": e["published_at"],
                "duration_sec": 0,   # RSS は尺を返さない → 0(不明)。backfill 側で確定する
            })
        if dry:
            if rows:
                print(f"[dry] {cid}: 新着 {len(rows)}本", file=sys.stderr)
            continue
        if rows:
            append_rows(path, rows)
            added += len(rows)
            touched += 1
            chans[cid] = {**chans.get(cid, {}), "last_refresh": today,
                          "n_videos": len(have) + len(rows)}
    if not dry:
        save_manifest(root, man)
    print(f"REFRESH_SUMMARY|channels_touched={touched}|videos={added}")
    return 0


def stats(root: Path) -> int:
    files = list((root / "channels").glob("*/videos.jsonl"))
    total_v = total_b = 0
    for p in files:
        total_b += p.stat().st_size
        total_v += sum(1 for _ in io.open(p, encoding="utf-8"))
    print(f"DB: {len(files)}ch / {total_v:,}本 / {total_b / 1e6:.1f}MB @ {root}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="競合全動画DB (横断分析の分母)")
    ap.add_argument("cmd", choices=["backfill", "refresh", "stats"])
    ap.add_argument("--corpus-dir", default=None, help="DB の置き場所 (既定 env CORPUS_DIR or ../youtube-niche-corpus)")
    ap.add_argument("--budget", type=int, default=int(os.environ.get("CDB_BUDGET", 4000)),
                    help="backfill の quota 上限u。超えたら中断(次回レジューム)")
    ap.add_argument("--per-channel-cap", type=int, default=PER_CHANNEL_CAP)
    ap.add_argument("--dry-run", action="store_true", help="API 取得のみ・書き込みなし確認")
    args = ap.parse_args()

    root = corpus_root(args.corpus_dir)
    if args.cmd == "backfill":
        return backfill(root, args.budget, args.per_channel_cap, args.dry_run)
    if args.cmd == "refresh":
        return refresh(root, args.dry_run)
    return stats(root)


if __name__ == "__main__":
    raise SystemExit(main())
