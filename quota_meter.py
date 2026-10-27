"""YouTube Data API quota の自己計上 (推定)。

Google 側に「残り quota を返す API」は無く、正確な値は Cloud Console でしか確認できない
(https://console.cloud.google.com/apis/api/youtube.googleapis.com/quotas)。
ただし課金表は決定的 (search.list=100u / その他の list=1u/リクエスト) なので、
各スクリプトが自分の API 呼び出しを数えて実行末尾に time_series/quota_log.jsonl へ
1 行 append すれば、ダッシュボードに「今日の推定消費 / 10,000」を出せる。

- 記録形式: {"ts", "date_pt", "source", "units"} の JSONL
- date_pt は太平洋時間の日付。YouTube API の quota は PT 深夜リセットのため、
  JST でも UTC でもなく PT で日を束ねる
- 計上箇所: priority_channels_check / discover_channels (discovery_loop 経由) /
  market_validate / daily_report。手動ローカル実行分も同様に記録される
- エラー中断時は flush されず記録漏れするので、あくまで推定値として読む
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_FILE = "quota_log.jsonl"
DAILY_LIMIT = 10_000

_units = 0


def add(units: int) -> None:
    """API リクエスト 1 回分を計上 (search.list=100 / その他の list=1)。"""
    global _units
    _units += units


def total() -> int:
    return _units


def pt_date(now: datetime | None = None) -> str:
    """quota リセット境界 = 太平洋時間の日付。zoneinfo が使えない環境は UTC-8 固定で近似。"""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    except Exception:
        return (now - timedelta(hours=8)).strftime("%Y-%m-%d")


def flush(base: Path, source: str) -> int:
    """溜めた計上を base/quota_log.jsonl に 1 行で書き出してリセット。書いた units を返す。"""
    global _units
    if _units <= 0:
        return 0
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date_pt": pt_date(now),
        "source": source,
        "units": _units,
    }
    base.mkdir(parents=True, exist_ok=True)
    with (base / LOG_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    n, _units = _units, 0
    return n


def load_log(base: Path) -> list[dict]:
    path = base / LOG_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
