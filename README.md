# YouTube Niche Dashboard

AI/業務自動化ニッチで観測対象としている YouTube チャンネルの推移を可視化するダッシュボード。

- **Live**: https://syuta-chiba.github.io/youtube-niche-dashboard/
- **更新頻度**: 6 時間ごと（GitHub Actions による自動更新）
- **データ**: YouTube Data API v3 から取得した公開情報

## 表示項目

- 各チャンネルの登録者数 / 累計 views の時系列
- ★直近 HIT / 準HIT 動画一覧（age ≤ 14 日・チャンネル相対判定: 普段=全動画 views 中央値の 5 倍で 🎯HIT / 3 倍で 🚀準HIT）
- 動画別 views 推移（直近投稿 + 全 HIT 動画）

## 観測中チャンネル

- 戒くんのIT力高めるチャンネル
- AI時代のテック速報【ずんだもん解説】

## スタック

- Static HTML + [Chart.js](https://www.chartjs.org/)
- データ JSON はバックエンド側のスクリプトで生成し、本リポジトリに push
