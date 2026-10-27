"""PT 日次リセット前に、余った quota を試験語検索に使い切るドレイン。

設計 (2026-07-24 / hypotheses H-14「検索KWは消耗品」の運用化):
  - quota_log.jsonl から今日 (PT 日付) の消費を合算し、残り = 上限 - 予約 - 消費 を算出
  - 残りが1語分に満たない / 在庫 (discovery_keywords_trial.txt) が空なら何もしない
  - 在庫の先頭から予算内の語数だけ取り、一時ファイル経由で discovery_loop.py を実行
    (--no-promote 固定: 収穫は queue/コーパス/rss_pool 行き。priority 追加は人間確認制のまま)
  - 消費した語は discovery_keywords_tried.txt へ日付付きで移動 (同じ語は二度と検索しない)

quota 見積りの注意:
  - quota_log は cron 系 (priority/discovery/rss/market) の消費しか知らない。ローカルの
    手動 probe は計上されないため RESERVE を厚め (既定 2,000u) に取る
  - 読み違えて quotaExceeded になっても discovery_loop は「そこまでで集計」に落ちるだけで安全

実行: GitHub Actions (.github/workflows/quota-drain.yml) から毎日 UTC 05:20 (JST 14:20)。
PT リセット (夏 UTC07:00 / 冬 UTC08:00) の前、かつ後続は JST15時の priority cron (~60u) のみ
という時間帯を選んでいる。
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DAILY_LIMIT = 10_000
RESERVE = 2_000          # 後続 cron + ローカル手動消費 (quota_log に載らない) の安全マージン
PER_WORD_BUDGET = 160    # search 100u + 深掘り分のバッファ (実測: 新語1語あたり数十u)
TRIAL_FILE = "discovery_keywords_trial.txt"
TRIED_FILE = "discovery_keywords_tried.txt"
BATCH_FILE = "_trial_batch.txt"  # gitignore 対象の一時ファイル
QUOTA_LOG = Path("time_series") / "quota_log.jsonl"


def spent_today_pt() -> int:
    if not QUOTA_LOG.exists():
        return 0
    today_pt = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    total = 0
    for line in QUOTA_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("date_pt") == today_pt:
            total += r.get("units", 0)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reserve", type=int, default=int(os.environ.get("DRAIN_RESERVE", RESERVE)))
    ap.add_argument("--dry-run", action="store_true", help="語の選定まで表示して実行しない")
    args = ap.parse_args()

    spent = spent_today_pt()
    budget = DAILY_LIMIT - args.reserve - spent
    print(f"[info] 本日(PT)消費 {spent}u / 予約 {args.reserve}u / ドレイン予算 {budget}u", file=sys.stderr)
    if budget < PER_WORD_BUDGET:
        print("[info] 予算不足。何もしない。")
        print("DRAIN_SUMMARY|words=0|reason=no_budget")
        return 0

    trial_path = Path(TRIAL_FILE)
    if not trial_path.exists():
        print("DRAIN_SUMMARY|words=0|reason=no_backlog_file")
        return 0
    lines = trial_path.read_text(encoding="utf-8").splitlines()
    words = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if not words:
        print("[info] 在庫なし。何もしない (無駄撃ちしない)。")
        print("DRAIN_SUMMARY|words=0|reason=empty_backlog")
        return 0

    n = min(len(words), budget // PER_WORD_BUDGET)
    batch = words[:n]
    print(f"[info] 在庫 {len(words)} 語から {n} 語を試験: {' | '.join(batch)}", file=sys.stderr)
    if args.dry_run:
        print(f"DRAIN_SUMMARY|words={n}|reason=dry_run")
        return 0

    Path(BATCH_FILE).write_text(
        "".join(w + "\n" for w in batch), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "discovery_loop.py", "--no-promote", "--keywords-file", BATCH_FILE],
    )
    # 部分成功 (quotaExceeded で途中終了) でも語は消費済み扱いにする — 再検索の無駄を防ぐ
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    with Path(TRIED_FILE).open("a", encoding="utf-8") as f:
        for w in batch:
            f.write(f"{today}  {w}\n")
    remaining = [ln for ln in lines
                 if ln.strip().startswith("#") or not ln.strip() or ln.strip() not in set(batch)]
    trial_path.write_text("".join(ln + "\n" for ln in remaining), encoding="utf-8")
    Path(BATCH_FILE).unlink(missing_ok=True)

    print(f"DRAIN_SUMMARY|words={n}|exit={r.returncode}")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
