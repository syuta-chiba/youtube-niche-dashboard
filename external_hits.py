"""外部HITコーパス (time_series/external_hits.jsonl) の読み書き。

「類似HIT横断分析」の照合範囲を priority ウォッチ内から YouTube 全体へ広げるための蓄積層。
market_validate.py (急伸時の市場検証) と discovery_loop.py (日次発掘) が既に quota を払って
取得した監視枠外チャンネルの HIT 動画を、使い捨てずにここへ永続化する (追加 quota ゼロ)。
priority_channels_check.py (Slack 通知の類似HIT) と build_pages_data.py (ダッシュボード) が読む。

1 行 1 動画の JSONL。video_id で dedup し、内容が変わった時だけ書き換える (git 差分ノイズ防止)。
広告ブースト疑いのチャンネル由来のデータは呼び出し側で除外してから渡すこと
(discovery_loop は boost_suspect / likes_hidden を弾く。boosted 枠と同方針)。
"""

import json
from datetime import datetime, timezone
from pathlib import Path

# 照合用 (Slack 類似HIT・ダッシュボード) に読む本体ファイルの上限。超えたら last_seen が
# 古い順に本体から外すが、捨てずに月別アーカイブへ移す (2026-10-27〜。それ以前は捨てていた)。
MAX_ENTRIES = 5000
# 取り込み条件。score (views/subs) は HIT の定義 (鉱脈しきい値 2.0)、
# views 床は極小チャンネルの偶然 (30v/10subs = score 3.0) を弾くノイズ除け。
MIN_SCORE = 2.0
MIN_VIEWS = 500
# 2026-08-11 追加: 偽HIT (登録者数が実態と合わないミラー/転載系) を入口で弾く。
# score は H-90 で主指標から廃止済みだが、この取り込み条件だけ旧基準のまま残っていて
# subs 1,560 の ch に 6億再生 (score 40万) といった行を大量に通していた。
# 軸内/軸外のラベリングは corpus_filter.py が別途行う (決定論 + AI)。
MAX_SCORE = 500
MAX_VIEWS = 3_000_000

CORPUS_FILE = "external_hits.jsonl"  # time_series/ 直下に置く
# 本体から外れた行の置き場 (time_series/ 直下)。first_seen の年月ごとに 1 ファイル・追記のみ。
ARCHIVE_DIR = "external_hits_archive"

# dedup 比較でみる中身 (これが同じなら再観測でも書き換えない)
_CORE_KEYS = ("title", "channel", "views", "subs", "score", "published_at")


def load_external_hits(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def merge_external_hits(path: Path, rows: list[dict], source: str,
                        exclude_channel_ids: set[str] | None = None) -> int:
    """rows をコーパスへ dedup 追記し、追加/更新件数を返す。

    rows の想定キー: video_id, title, channel, channel_id, views, subs, score,
    published_at, url。しきい値未満・監視枠内 ch は黙ってスキップする。
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = {r["video_id"]: r for r in load_external_hits(path)}
    changed = 0
    for r in rows:
        # rel_hit=True はチャンネル相対 (普段中央値の5倍) で HIT 済みの行。
        # score (views/subs) が 2.0 未満でも「普段が低いのに跳ねた=企画の需要」なので通す。
        if not r.get("rel_hit") and (r.get("score") or 0) < MIN_SCORE:
            continue
        if (r.get("views") or 0) < MIN_VIEWS:
            continue
        if (r.get("score") or 0) > MAX_SCORE or (r.get("views") or 0) > MAX_VIEWS:
            continue          # 偽HIT / 層が違いすぎる
        if exclude_channel_ids and r.get("channel_id") in exclude_channel_ids:
            continue
        vid = r.get("video_id")
        if not vid:
            continue
        prev = existing.get(vid)
        entry = {
            "video_id": vid,
            "title": r.get("title", ""),
            "channel": r.get("channel", ""),
            "channel_id": r.get("channel_id", ""),
            "views": r.get("views", 0),
            "subs": r.get("subs"),
            "score": r.get("score"),
            "published_at": r.get("published_at", ""),
            "url": r.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "source": (prev or {}).get("source") or source,
            "first_seen": (prev or {}).get("first_seen") or now,
            "last_seen": now,
        }
        if prev and all(prev.get(k) == entry[k] for k in _CORE_KEYS):
            continue  # 中身が同じなら last_seen も更新しない (無駄 commit 防止)
        existing[vid] = entry
        changed += 1
    if not changed:
        return 0
    entries = sorted(existing.values(), key=lambda e: e.get("last_seen", ""), reverse=True)
    archive_external_hits(path.parent / ARCHIVE_DIR, entries[MAX_ENTRIES:])
    entries = sorted(entries[:MAX_ENTRIES], key=lambda e: (e.get("first_seen", ""), e["video_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")
    return changed


def archive_external_hits(archive_dir: Path, rows: list[dict]) -> int:
    """本体から外れた行を月別アーカイブへ追記する。既にある video_id は書かない。追記件数を返す。"""
    by_month: dict[str, list[dict]] = {}
    for r in rows:
        by_month.setdefault((r.get("first_seen") or "unknown")[:7], []).append(r)
    added = 0
    for month, rs in by_month.items():
        f = archive_dir / f"{month}.jsonl"
        have = {e["video_id"] for e in load_external_hits(f)}
        new = [r for r in rs if r["video_id"] not in have]
        if not new:
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in new)
        added += len(new)
    return added


def load_all_external_hits(base: Path) -> list[dict]:
    """本体 + アーカイブの全件 (分析用)。同じ video_id は本体側を優先する。"""
    out = {e["video_id"]: e for e in load_external_hits(base / CORPUS_FILE)}
    arch = base / ARCHIVE_DIR
    if arch.is_dir():
        for f in sorted(arch.glob("*.jsonl")):
            for e in load_external_hits(f):
                out.setdefault(e["video_id"], e)
    return list(out.values())


def corpus_age_days(published: str) -> int:
    """published_at (ISO) → 経過日数。パース不能は 9999 (表示側で握り潰さないための番兵)。"""
    try:
        pub = datetime.fromisoformat(published.replace("Z", "+00:00"))
        return max((datetime.now(timezone.utc) - pub).days, 0)
    except (ValueError, AttributeError, TypeError):
        return 9999
