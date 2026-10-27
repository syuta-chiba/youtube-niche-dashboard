# youtube-niche-dashboard

YouTube の AI・開発系チャンネルの**公開データ**を GitHub Actions で定点観測し、ダッシュボードとして公開するリポジトリ。

- **ダッシュボード**: https://syuta-chiba.github.io/youtube-niche-dashboard/
- **仕組みの全体図**: https://syuta-chiba.github.io/youtube-niche-dashboard/system.html

分析・計画・自チャンネルの非公開データは private repo `youtube-niche-research` 側にあり、ここには置かない。
データ収集は 2026-10-27 に private repo からここへ移した（public repo は Actions が無料）。

## 何が動いているか（JST）

| 時刻 | workflow | 中身 |
|---|---|---|
| 3・9・15・21時 | `priority-channels.yml` → `pages.yml` | 注目chの定点観測 → Slack 通知（🚨新作 / ⚡初速 / 🚀急伸 / 🎯本命）→ ダッシュボード生成・Pages 公開 |
| 04:23 | `discovery.yml` | キーワード検索で新しいchを発見（現在 cron 停止中・手動実行のみ） |
| 05:03 | `rss-patrol.yml` | 発見済みch全員を RSS で巡回（quota ほぼ 0） |
| 05:33 | `competitor-db.yml` | 全動画DB（`corpus/`）に新着を追記。初回の全履歴取得は手動実行 |
| 14:20 | `quota-drain.yml` | 余った quota で試験語検索（検索部分は現在停止中） |
| 失敗時 | `notify-failure.yml` | 上記が落ちたら Slack に通知 |

## 構成

- `dashboard/` … サイトの静的ファイル。**Pages に公開されるのはこのフォルダだけ**（`pages.yml` が `dashboard.json` を生成して artifact として deploy。Settings → Pages の Source は「GitHub Actions」）
- `time_series/` `rss_pool.json` `corpus/` など … 観測データ（リポジトリには入るがサイトには出ない）
- `*.py` … 収集スクリプト。**観測対象リスト（`priority_channels.txt` 等）と収集スクリプトの正本はこのリポジトリ**。private repo には日次で同期される

## 必要な secrets

| 名前 | 用途 |
|---|---|
| `YOUTUBE_API_KEY` | YouTube Data API v3 |
| `SLACK_WEBHOOK_URL` | 通知 |
| `PRIVATE_READ_TOKEN` | private repo の読み取り（ダッシュボードのメモ・分析・日次タブと、AI判定の取り込みに使う。無ければその部分だけ空になる） |

## 注意

- **このリポジトリは public**。データ・コード・Actions のログは誰でも見られる。扱うのは YouTube の公開データだけにする。
- private repo の入力（`docs/` `own_channel/` `vein_map.json` `shelf_checks.json`）は実行時に取り込むだけで、`.gitignore` 済み。commit しない。
- `pull_request_target` などの外部トリガーは使わない（secrets を外部から触らせない）。
