"""当たり企画の真似元チャンネルを自動発掘する 1 日 1 周のループ。

ユーザーが手でやっていた 5 工程をまとめて回す:

  ① チャンネル発掘   discovery_keywords_tech.txt の技術軸 KW で search.list
  ② 動画発掘        各候補の uploads を videos.list でダンプ (views/likes)
  ③ 広告/ブースト判定  like 率を見て emperor 基準 (1.25-1.80%) 比で極端に低ければ
                     「views が実需要を反映してない」= boost 疑いで除外
                     (docs/channel_strategy_2026-06.md の判定法を機械化)
  ④ 時間軸判定      直近 90 日の HIT 比率 (methodology §1.2 軸④) で
                     立ち上がり中 / ピーク終了を判定
  ⑤ ウォッチ追加     ゲートを通った候補を priority_channels.txt の AUTO セクションに
                     追記 → 既存の priority-channels Actions が次回から定点観測を開始

GitHub Actions (.github/workflows/discovery.yml) から 1 日 1 回呼ぶ前提。
ローカル手動実行は quota を二重消費するので原則しない (CLAUDE.md: 取得は Actions 専任)。

quota 見込み: discover 20KW × 100u = ~2,000u + 深掘り (subs 該当の新規全件 × 2〜8u) / 日。
深掘りの件数上限 (旧 EVAL_CAP=30) は 2026-07-24 に撤廃 — 1ch 2〜8u と安価で、
KW追加直後などの流入増でも数百u に収まるため。暴走時の最終防壁は quota 自体の日次上限。
priority 既存 Actions と合算しても 1 日無料枠 10,000u に十分収まる。

判定の境界事例 (boost 疑いギリギリ等) は AUTO 追加せず discovery_queue.md に積み、
次のセッションで人間 / Claude が最終判断する (methodology Phase 2 の手動ゲートを温存)。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 既存スクリプトのヘルパを再利用 (重複実装を避ける)
import quota_meter
import channel_filter as chf
from discover_channels import search_videos, fetch_channel_stats
from external_hits import CORPUS_FILE, merge_external_hits
from priority_channels_check import (
    HIT_REL_FLOOR,
    HIT_SEMI_MULT,
    HIT_STRONG_MULT,
    expected_likes,
    likes_baseline,
    list_video_ids,
    fetch_video_details,
    load_channel_ids,
)

# ───────────────────────── 調整パラメータ ─────────────────────────
# しきい値は docs/channel_strategy_2026-06.md / methodology.md の実測値に基づく。
# 緩めたい / 厳しくしたいときはここだけ触れば良い (環境変数でも上書き可)。
KEYWORDS_FILE = "discovery_keywords_tech.txt"
MIN_SUBS = 50          # 50 未満 = hiddenSubscriberCount 等のノイズ
MAX_SUBS = 2000        # 0〜2000 が真似元 sweet spot (channel_strategy §watch)
MIN_KEYWORDS = 1       # 深掘り評価する最小 KW ヒット数。2 だと専門特化ch (例: Ollama 専門で
                       # 1語しか引っかからない) を見逃す — 2026-07-21 網羅性優先で 1 に緩和。
                       # 深掘りは新規 subs 該当の全件 (上限撤廃 2026-07-24)。
MIN_KW_PROMOTE = 3     # AUTO 昇格に必要な KW ヒット数。methodology Phase 1 判断基準:
                       # 「3+ kw かつターゲットに合ってる → 採用」。2 のみは関連度が怪しく queue 止まり。
PER_QUERY = 50         # 1 KW あたり取得動画数。search.list は 1 ページ (≤50件) = 100u 固定なので
                       # 50 まで無料で網が広がる (25→50: 2026-07-24、初期値25は旧実装の踏襲で根拠なしと判明)。
                       # 51+ は 2 ページ目で +100u。
# EVAL_CAP (1日の深掘り上限・旧30) は 2026-07-24 撤廃: 実費 2〜8u/ch と安価で防壁の実態がなく、
# 未評価候補はコーパス蓄積に入らない機会損失の方が大きい。subs 該当の新規は全件深掘りする。
RECENT_VIDEOS = 1000   # 候補 1 ch あたり分析する長尺本数の上限 (UULF 列挙なので Shorts は数に入らない)。
                       # subs 0-2000 帯の ch で長尺 1000 本超はまず無いため実質「全長尺を見て検討」。
                       # 消費は本数比例 (50本ごとに列挙+統計で 2u)、動画が尽きた時点で止まる。
MIN_VIDEOS = 8         # 本編がこの本数未満 = 実績不足 (HIT率/median が無意味になる)
# HIT 判定はチャンネル相対 (普段中央値の HIT_STRONG_MULT 倍・下限 HIT_REL_FLOOR)。
# 絶対 1,000 だと「普段100v の ch の 900v HIT (9倍=企画の需要の証拠)」を見逃す。
# 定数は priority_channels_check から import して対管理を単一化。
LIKE_RATE_FLOOR = 0.004  # 0.4%。emperor 実測 1.25-1.80% に対し極端に低い = boost 疑い
LIKE_SAMPLE_MIN_VIEWS = 300  # ch単位 like 率はこの views 以上の動画でのみ測る (分母安定化)
MIN_HITS = 2           # AUTO 昇格に必要な HIT 本数 (hard floor)。単発 HIT は生存バイアス (§1.1)
MIN_HIT_RATE = 0.10    # 参考: HIT 率 (gate には使わず report 表示用)
TIMING_RECENT_DAYS = 90  # 軸④: この日数以内の HIT を「直近」とみなす
TIMING_RECENT_SHARE = 0.20  # 直近 HIT 比率がこれ以上なら立ち上がり/安定とみなす
# 時代混在ch対策 (2026-07-21 マイケル事例): 昔別ジャンルで当てた ch は全履歴中央値が
# 過去に支配され、ピボット後の「今の普段」に対する跳ねが見えない。直近N本の中央値を併用し、
# 「直近 MOMENTUM_DAYS 日以内 × 今の普段の5倍」をモメンタムHITとして拾う。
RECENT_MEDIAN_N = 30     # 「今の普段」= 直近この本数の長尺 views 中央値
MOMENTUM_DAYS = 180      # モメンタムHIT の対象となる動画の最大日齢
ACTIVE_DAYS = 120      # 最新投稿がこの日数以内 = 活動中
DAILY_PROMOTE_CAP = 2  # 1 日に AUTO 追加する上限
GLOBAL_AUTO_CAP = 12   # AUTO セクションの総数上限 (priority 取得 quota を抑える)

AUTO_HEADER = "# === AUTO-DISCOVERED (discovery_loop.py 自動追加 / レビューして良ければ手動セクションへ昇格・不要なら行削除) ==="

PRIORITY_FILE = "priority_channels.txt"
SEEN_FILE = "discovery_seen.json"
QUEUE_FILE = "discovery_queue.md"
REPORTS_DIR = "discovery_reports"


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v and v.strip() else default


def load_seen(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def uploads_playlist(channel_id: str) -> str:
    """UCxxxx -> UUxxxx (uploads playlist は接頭辞を UU に替えるだけ)。"""
    return "UU" + channel_id[2:]


def longform_playlist(channel_id: str) -> str:
    """UCxxxx -> UULFxxxx (長尺のみの自動プレイリスト。LF = Long Form)。
    API 側で Shorts を除外できるので窓 RECENT_VIDEOS がまるごと長尺に使える。
    非公式仕様 (ドキュメント外) のため、無い ch では 404 → uploads_playlist にフォールバック。
    2026-07-21 実測: ずんだもん解説ch UU=104本 / UULF=62本 で Shorts 42本を正しく除外。"""
    return "UULF" + channel_id[2:]


def count_auto_entries(priority_text: str) -> int:
    """priority_channels.txt の AUTO セクションに既にある ch 数を数える。"""
    in_auto = False
    n = 0
    for raw in priority_text.splitlines():
        if raw.strip() == AUTO_HEADER:
            in_auto = True
            continue
        if not in_auto:
            continue
        if raw.split("#", 1)[0].strip():  # ID 行
            n += 1
    return n


def analyze_channel(yt, cid: str, subs: int, keywords: list[str] | None = None) -> dict | None:
    """候補 1 ch の動画を取得して HIT / like 率 / 時間軸の指標を算出する。
    keywords を渡すと「タイトル群にマッチする検索KW数 (title_kw)」も算出 —
    検索ヒット数 (露出度) と別に、テーマ適合を直接測るため。"""
    # まず UULF (長尺のみ) で列挙し、Shorts に窓を食われないようにする。
    # UULF が無い ch (長尺ゼロ等) は 404 になるので UU (全アップロード) へフォールバック。
    try:
        vid_ids = list_video_ids(yt, longform_playlist(cid), RECENT_VIDEOS)
    except HttpError:
        try:
            vid_ids = list_video_ids(yt, uploads_playlist(cid), RECENT_VIDEOS)
        except HttpError:
            return None
    try:
        videos = fetch_video_details(yt, vid_ids)
    except HttpError:
        return None
    if not videos:
        return None

    now = datetime.now(timezone.utc)
    longform = []  # Shorts (<=180s。YouTube Shorts は 2024-10 から最大3分) を除外した本編
    for v in videos:
        if 0 < v["duration_sec"] <= 180:
            continue
        try:
            pub = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00"))
            age = max((now - pub).days, 0)
        except ValueError:
            age = 9999
        longform.append({**v, "age_days": age})
    if not longform:
        return None

    # ── ③ 広告/ブースト検査を **先に全動画へ** ──
    # 普段 (中央値) の算出前に盛られた動画を弾く。過去時代の views が購入だった場合、
    # 中央値としきい値ごと汚染されるため (2026-07-21 設計変更)。
    # likeCount 非公開 ch は likes=0 になるので「全部 0」は不明扱いにする。
    likes_hidden = bool(longform) and all(
        v["likes"] == 0 for v in longform if v["views"] >= LIKE_SAMPLE_MIN_VIEWS
    ) and any(v["views"] >= LIKE_SAMPLE_MIN_VIEWS for v in longform)

    # 動画単位: 回帰基準線 (like数 ≒ ファン分 + 一見率×views、docs/ad_check_methodology.md)
    # を全長尺に当て、views ≥ 500 なのに like が「絶対 0.4% 未満」または「期待値の半分未満」
    # の動画は盛られた疑いとして分析母集団から丸ごと外す。Theil-Sen は少数の盛られた動画に
    # 頑健なので基準線は全動画で引ける。絶対床は極小chで回帰の傾きが0に潰れる時の最後の砦。
    baseline = None
    if not likes_hidden:
        baseline = likes_baseline([(v["views"], v["likes"]) for v in longform])
    clean, fake_hits = [], []
    for v in longform:
        if not likes_hidden and v["views"] >= HIT_REL_FLOOR:
            exp = expected_likes(baseline, v["views"])
            abs_low = v["likes"] / v["views"] < LIKE_RATE_FLOOR
            if abs_low or (exp and v["likes"] < 0.5 * exp):
                fake_hits.append(v)
                continue
        clean.append(v)
    big_n = sum(1 for v in longform if v["views"] >= HIT_REL_FLOOR)
    fake_share = len(fake_hits) / big_n if big_n else 0.0
    if not clean:
        clean = longform  # 全滅 = ch ぐるみのブースト疑い (fake_share=1.0 が gate で効く)

    # ch 単位の like率は **盛られ動画を除いたクリーン分だけ** で測る。
    # 混在ch (過去だけブースト・現在オーガニック) を丸ごと殺さないため。
    # ch ぐるみの多用は fake_share (500v以上の動画に占める盛られ比率) で gate が弾く。
    sample = [v for v in clean if v["views"] >= LIKE_SAMPLE_MIN_VIEWS]
    like_rate = None
    if sample and not likes_hidden:
        rates = [v["likes"] / v["views"] for v in sample if v["views"] > 0]
        like_rate = sum(rates) / len(rates) if rates else None
    boost_suspect = like_rate is not None and like_rate < LIKE_RATE_FLOOR

    # HIT はチャンネル相対: 「普段 (中央値) の何倍か」。普段が低い ch の突出 =
    # チャンネル力でなく企画の需要で当たった証拠 (priority 監視と同一基準)。
    views = [v["views"] for v in clean]
    med = int(median(views))
    rel_th = max(HIT_REL_FLOOR, HIT_STRONG_MULT * med)
    # 「今の普段」= 直近 RECENT_MEDIAN_N 本の中央値。時代混在ch (昔別ジャンルで当てて
    # 現在ピボット済み) では全履歴中央値が過去に支配されるため、直近基準を併用する。
    recent_n = sorted(clean, key=lambda v: v["age_days"])[:RECENT_MEDIAN_N]
    med_recent = int(median(v["views"] for v in recent_n)) if len(recent_n) >= MIN_VIDEOS else med
    momentum_th = max(HIT_REL_FLOOR, HIT_STRONG_MULT * med_recent)

    hits = []
    for v in clean:
        legacy = v["views"] >= rel_th
        momentum = v["age_days"] <= MOMENTUM_DAYS and v["views"] >= momentum_th
        if legacy or momentum:
            hits.append({**v, "momentum": momentum and not legacy})
    momentum_hits = [v for v in hits if v.get("momentum")]
    recent_hits = [v for v in hits if v["age_days"] <= TIMING_RECENT_DAYS]
    latest_age = min(v["age_days"] for v in longform)

    # タイトル群への検索KWマッチ (= テーマ適合の直接測定。検索ヒット数は露出度でしかない)
    title_kw = 0
    if keywords:
        lower_titles = [v["title"].lower() for v in longform]
        for kw in keywords:
            toks = kw.lower().split()
            if any(all(t in ti for t in toks) for ti in lower_titles):
                title_kw += 1

    # ── ④ 時間軸判定 ──
    hit_rate = len(hits) / len(clean)
    recent_share = (len(recent_hits) / len(hits)) if hits else 0.0
    mom_recent = [v for v in momentum_hits if v["age_days"] <= TIMING_RECENT_DAYS]
    if not hits:
        timing = "様子見(HITなし)"
    elif recent_share >= TIMING_RECENT_SHARE:
        timing = "★立ち上がり/安定"
    elif len(mom_recent) >= 2:
        # 過去HITが多い時代混在chでも、直近90日にモメンタムHITが複数あればピボット後の立ち上がり
        timing = "★立ち上がり(モメンタム)"
    elif latest_age <= ACTIVE_DAYS:
        timing = "△HIT古いが活動中"
    else:
        timing = "✗ピーク終了/停止疑い"

    top = max(longform, key=lambda v: v["views"])
    return {
        "channel_id": cid,
        # 外部HITコーパス蓄積用: 相対HIT 動画の生データ (上位10本、偽HIT除外済み)
        "hit_videos": [
            {"video_id": v["id"], "title": v["title"], "views": v["views"],
             "published_at": v.get("published_at", ""), "rel_hit": True}
            for v in sorted(hits, key=lambda v: v["views"], reverse=True)[:10]
        ],
        "subs": subs,
        "n_videos": len(longform),
        "median_views": med,
        "recent_median_views": med_recent,
        "rel_hit_th": rel_th,
        "momentum_th": momentum_th,
        "momentum_hits": len(momentum_hits),
        "momentum_recent": len(mom_recent),
        "title_kw": title_kw,
        "fake_hits": len(fake_hits),
        "max_views": top["views"],
        "max_title": top["title"],
        "hits": len(hits),
        "hit_rate": round(hit_rate, 3),
        "recent_hits": len(recent_hits),
        "recent_share": round(recent_share, 3),
        "latest_age": latest_age,
        "like_rate": round(like_rate, 4) if like_rate is not None else None,
        "likes_hidden": likes_hidden,
        "boost_suspect": boost_suspect,
        "fake_share": round(fake_share, 2),
        "timing": timing,
    }


def promote_score(a: dict) -> float:
    """AUTO 昇格候補の並び順。HIT 実績と直近性を重視。"""
    return (
        a["hits"] * 2
        + a["hit_rate"] * 10
        + a["recent_share"] * 5
        + a["max_views"] / 10000
    )


def gate(a: dict) -> tuple[bool, str]:
    """AUTO 昇格して良いか。False の場合は queue 行きの理由を返す。"""
    if a["boost_suspect"]:
        return False, f"広告/boost疑い (クリーン分 like率 {a['like_rate']:.2%} < {LIKE_RATE_FLOOR:.1%})"
    if a.get("fake_share", 0) >= 0.5:
        return False, f"盛られ動画が過半 ({a['fake_share']:.0%} — 500v以上の動画の半数以上が広告疑い)"
    if a["n_videos"] < MIN_VIDEOS:
        return False, f"実績不足 (本編 {a['n_videos']}本 < {MIN_VIDEOS}、track record 不十分)"
    if a["latest_age"] > ACTIVE_DAYS:
        return False, f"活動停止疑い (最新投稿 {a['latest_age']}d 前)"
    if a["hits"] < MIN_HITS:
        fake = f"、偽HIT {a['fake_hits']}本除外済み" if a.get("fake_hits") else ""
        return False, f"相対HIT不足 ({a['hits']}本 < {MIN_HITS}、単発は生存バイアス §1.1{fake})"
    # 関連度 gate は 2026-07-21 に廃止: 確認制で人間が最終判断するため機械では弾かない。
    # 検索KW/題名KW は queue・レポートに表示され、人間のレビュー材料としてのみ使う。
    if a["recent_share"] < TIMING_RECENT_SHARE and a.get("momentum_recent", 0) < 2:
        return False, f"時間軸×: {a['timing']} (直近HIT比 {a['recent_share']:.0%}・直近モメンタム {a.get('momentum_recent', 0)}本)"
    return True, "ok"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--api-key", default=os.environ.get("YOUTUBE_API_KEY"))
    p.add_argument("--dry-run", action="store_true", help="分析・レポートは出すが priority/ledger は書き換えない")
    p.add_argument("--no-promote", action="store_true", help="AUTO 追加せず全候補を queue に回す (ledger は更新)")
    p.add_argument("--keywords-file", default=KEYWORDS_FILE,
                   help="検索キーワードファイル (quota_drain.py が試験語バッチを渡す用)")
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: YOUTUBE_API_KEY 未設定", file=sys.stderr)
        return 1

    daily_cap = env_int("DISCOVERY_DAILY_CAP", DAILY_PROMOTE_CAP)
    global_cap = env_int("DISCOVERY_GLOBAL_CAP", GLOBAL_AUTO_CAP)

    keywords = [
        ln.strip()
        for ln in Path(args.keywords_file).read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")

    priority_path = Path(PRIORITY_FILE)
    priority_text = priority_path.read_text(encoding="utf-8-sig") if priority_path.exists() else ""
    existing_ids = set(load_channel_ids(priority_path)) if priority_path.exists() else set()
    seen = load_seen(Path(SEEN_FILE))

    yt = build("youtube", "v3", developerKey=args.api_key, cache_discovery=False)

    # ── ① 発掘 ──
    print(f"[info] {len(keywords)} KW で発掘 (~{len(keywords) * 100}u)", file=sys.stderr)
    channel_hits: dict[str, dict] = {}
    for kw in keywords:
        try:
            items = search_videos(yt, kw, PER_QUERY, "JP")
        except HttpError as e:
            if "quotaExceeded" in str(e):
                print("[error] quota 超過。ここまでで集計", file=sys.stderr)
                break
            print(f"[warn] '{kw}' 検索失敗: {e}", file=sys.stderr)
            continue
        for item in items:
            cid = item["snippet"]["channelId"]
            h = channel_hits.setdefault(cid, {"keywords": set(), "titles": []})
            h["keywords"].add(kw)
            if len(h["titles"]) < 3:
                h["titles"].append(item["snippet"]["title"])

    cand_ids = [cid for cid, h in channel_hits.items() if len(h["keywords"]) >= MIN_KEYWORDS]
    # 既存 priority / 既評価済み / 違う確定 (rejected_channels.txt) は除外
    rejected_path = Path("rejected_channels.txt")
    rejected = set(load_channel_ids(rejected_path)) if rejected_path.exists() else set()
    new_ids = [cid for cid in cand_ids
               if cid not in existing_ids and cid not in seen and cid not in rejected]
    print(f"[info] {len(channel_hits)}ch 中 {len(cand_ids)} が {MIN_KEYWORDS}+KW / うち新規 {len(new_ids)}", file=sys.stderr)

    if not new_ids:
        print("[info] 新規候補なし。終了。", file=sys.stderr)
        # 空でもレポートは残す
        Path(REPORTS_DIR).mkdir(exist_ok=True)
        Path(REPORTS_DIR, f"{today}.md").write_text(
            f"# discovery {today}\n\n新規候補なし (既存 priority / 評価済みのみ)。\n", encoding="utf-8"
        )
        print("DISCOVERY_SUMMARY|promoted=0|queued=0|evaluated=0")
        quota_meter.flush(Path("time_series"), "discovery")
        return 0

    try:
        stats = fetch_channel_stats(yt, new_ids)
    except HttpError as e:
        print(f"ERROR: 統計取得失敗: {e}", file=sys.stderr)
        return 2

    in_range = [
        (cid, stats[cid])
        for cid in new_ids
        if cid in stats and not stats[cid]["hidden_subs"]
        and MIN_SUBS <= stats[cid]["subs"] <= MAX_SUBS
    ]
    # RSS 巡回プールへ subs 該当の全候補を記録。
    # 「一度視界に入った競合候補は quota ゼロの RSS 層 (rss_patrol.py) で監視し続ける」ため、
    # gate 落ち・評価漏れがあっても後日跳ねれば再浮上する。
    if in_range and not args.dry_run:
        pool_path = Path("rss_pool.json")
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8")) if pool_path.exists() else {}
        except json.JSONDecodeError:
            pool = {}
        for cid, st in in_range:
            pool.setdefault(cid, {"title": st["title"], "subs": st["subs"],
                                  "source": "discovery_search", "added": today})
        pool_path.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    # KW ヒット数 → subs 順 (レポートの並びを関連度順に保つ)。深掘りは全件 (上限撤廃 2026-07-24)。
    in_range.sort(key=lambda t: (len(channel_hits[t[0]]["keywords"]), -t[1]["subs"]), reverse=True)
    to_eval = in_range
    print(f"[info] subs {MIN_SUBS}-{MAX_SUBS} 該当 {len(in_range)} / 深掘り {len(to_eval)}", file=sys.stderr)

    # ── ②③④ 深掘り分析 ──
    analyzed = []
    for cid, st in to_eval:
        a = analyze_channel(yt, cid, st["subs"], keywords)
        if a is None:
            seen[cid] = {"date": today, "decision": "skip", "reason": "動画取得不可", "title": st["title"]}
            continue
        a["title"] = st["title"]
        a["kw_hits"] = len(channel_hits[cid]["keywords"])
        a["keywords"] = " | ".join(sorted(channel_hits[cid]["keywords"]))
        a["sample_titles"] = " // ".join(channel_hits[cid]["titles"])
        ok, reason = gate(a)
        a["pass"] = ok
        a["reason"] = reason
        analyzed.append(a)

    # ── 外部HITコーパスへ蓄積 (類似HIT横断分析が監視枠の外も照合できるように) ──
    # boost 疑い / like 非公開の ch は views が実需要を反映しない可能性があるためコーパス外
    # (boosted_channels.txt と同方針)。昇格の合否は問わない — HIT の実在自体が証拠。
    corpus_rows = []
    for a in analyzed:
        if a["boost_suspect"] or a["likes_hidden"] or not a["subs"]:
            continue
        for v in a.get("hit_videos", []):
            corpus_rows.append({
                **v,
                "channel": a["title"],
                "channel_id": a["channel_id"],
                "subs": a["subs"],
                "score": round(v["views"] / a["subs"], 2),
            })
    if corpus_rows and not args.dry_run:
        try:
            n_corpus = merge_external_hits(
                Path("time_series") / CORPUS_FILE, corpus_rows, "discovery_loop",
                set(load_channel_ids(priority_path)))
            if n_corpus:
                print(f"[info] 外部HITコーパスへ {n_corpus} 件追加/更新", file=sys.stderr)
        except OSError as e:
            print(f"[warn] 外部HITコーパス更新失敗: {e}", file=sys.stderr)

    passers = sorted([a for a in analyzed if a["pass"]], key=promote_score, reverse=True)
    queued = [a for a in analyzed if not a["pass"]]

    # AI関連度フィルタ (2026-10-20): 数値ゲートを通っても「ITと全く無関係」と判定済みの ch は
    # 確認待ちから外す。ラベルは channel_filter.py / ai_judge Routine が付ける。
    # 未判定 (ラベル無し) は従来どおり通す = 確認制を保つ。channel_labels.json 無しなら全て no-op。
    ch_labels = chf.load_labels()
    offtarget = [a for a in passers if chf.is_offtarget(a["channel_id"], ch_labels)]
    if offtarget:
        off_ids = {a["channel_id"] for a in offtarget}
        passers = [a for a in passers if a["channel_id"] not in off_ids]
        for a in offtarget:
            a["reason"] = "🚫 AI関連度: ITと無関係 — 確認待ちから除外 (channel_filter)"
        queued = queued + offtarget
        print(f"[info] AI関連度で確認待ちから除外: {len(offtarget)} ch", file=sys.stderr)

    # ── ⑤ AUTO 昇格 (cap 適用) ──
    auto_now = count_auto_entries(priority_text)
    room = max(0, global_cap - auto_now)
    promote_n = 0 if (args.no_promote or args.dry_run) else min(daily_cap, room, len(passers))
    promoted = passers[:promote_n]
    spilled = passers[promote_n:]  # cap で溢れた合格者 → queue へ

    # priority_channels.txt 追記
    if promoted and not args.dry_run:
        lines = priority_text.rstrip("\n").split("\n") if priority_text.strip() else []
        if AUTO_HEADER not in priority_text:
            lines.append("")
            lines.append(AUTO_HEADER)
        for a in promoted:
            note = (
                f"[auto {today}] {a['title']} ({a['subs']} subs / "
                f"HIT {a['hits']}本 {a['hit_rate']:.0%} / like率 "
                + (f"{a['like_rate']:.2%}" if a['like_rate'] is not None else "n/a")
                + f" / {a['timing']})"
            )
            lines.append(f"{a['channel_id']}  # {note}")
        priority_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    # ledger 更新
    if not args.dry_run:
        for a in promoted:
            seen[a["channel_id"]] = {"date": today, "decision": "promoted", "title": a["title"],
                                     **gate_passer_metrics(a)}
        for a in spilled:
            # 全条件クリア (確認待ち) は台帳に指標ごと残す。レポートは同日2回目の実行 (quota_drain)
            # で上書きされ得るため、ダッシュボード「✅ 全条件クリア」一覧はこちらを正本にする (2026-10-13)
            seen[a["channel_id"]] = {"date": today, "decision": "queued(cap)", "title": a["title"],
                                     **gate_passer_metrics(a)}
        for a in queued:
            seen[a["channel_id"]] = {"date": today, "decision": "queued", "title": a["title"], "reason": a["reason"]}
        Path(SEEN_FILE).write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")

    # queue.md (人間レビュー用) — 合格 (確認制/cap溢れ) + 不合格を積む
    for a in spilled:
        a["reason"] = "✅ 全条件クリア — 確認待ち (良ければ priority_channels.txt の手動セクションへ)"
    queue_items = spilled + queued
    write_queue(today, promoted, queue_items, args.dry_run)
    write_report(today, now_iso, analyzed, promoted, spilled, queued, auto_now, global_cap, args)

    print(
        f"DISCOVERY_SUMMARY|promoted={len(promoted)}|queued={len(queue_items)}|evaluated={len(analyzed)}"
    )
    for a in promoted:
        safe = a["title"].replace("|", "/")
        print(f"PROMOTED|{a['channel_id']}|{safe}|{a['subs']}|HIT{a['hits']}|{a['timing']}")
    spent = quota_meter.flush(Path("time_series"), "discovery")
    print(f"[info] YouTube API 推定消費: {spent}u", file=sys.stderr)
    return 0


def write_queue(today, promoted, queue_items, dry):
    lines = [f"# discovery_queue (最終更新 {today} UTC{' / DRY-RUN' if dry else ''})", ""]
    lines.append("priority に自動追加された ch と、レビュー待ちの候補。")
    lines.append("良さそうなら priority_channels.txt の手動セクションへ。不要なら無視 (再提案されない)。")
    lines.append("")
    if promoted:
        lines.append(f"## ✅ 今回 AUTO 追加 ({len(promoted)})")
        for a in promoted:
            lines.append(
                f"- `{a['channel_id']}` **{a['title']}** ({a['subs']} subs) — "
                f"HIT {a['hits']}本 {a['hit_rate']:.0%} / 最大 {a['max_views']:,}v / {a['timing']}"
            )
        lines.append("")
    if queue_items:
        lines.append(f"## 🔎 レビュー待ち ({len(queue_items)})")
        for a in queue_items:
            lr = f"{a['like_rate']:.2%}" if a["like_rate"] is not None else ("非公開" if a["likes_hidden"] else "n/a")
            lines.append(
                f"- `{a['channel_id']}` **{a['title']}** ({a['subs']} subs) — "
                f"HIT {a['hits']}本 / like率 {lr} / {a['timing']} → **{a['reason']}**"
            )
            lines.append(f"  - 最大: {a['max_views']:,}v 「{a['max_title'][:50]}」")
        lines.append("")
    Path(QUEUE_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")


def gate_passer_metrics(a: dict) -> dict:
    """全条件クリア ch の評価指標を台帳 (discovery_seen.json) に残す分だけ抜き出す。"""
    keys = ("subs", "n_videos", "kw_hits", "title_kw", "keywords", "median_views",
            "recent_median_views", "hits", "momentum_hits", "fake_hits", "hit_rate",
            "max_views", "max_title", "like_rate", "timing")
    return {k: a.get(k) for k in keys}


def write_report(today, now_iso, analyzed, promoted, spilled, queued, auto_now, global_cap, args):
    Path(REPORTS_DIR).mkdir(exist_ok=True)
    # 同じ日に 2 回走る (朝 discovery.yml + 夜 quota-drain.yml) と同名ファイルを上書きして
    # 朝の評価結果が消えていた (2026-07-24〜10-12 で 65 日分・全条件クリア 200 ch の指標を喪失)。
    # 2 回目以降は末尾に追記する (2026-10-13)
    report_path = Path(REPORTS_DIR, f"{today}.md")
    prior = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    lines = [
        f"# discovery レポート {today}" + (" (同日 2 回目以降の実行・追記)" if prior else ""),
        "",
        f"- 実行: {now_iso}" + ("  **DRY-RUN**" if args.dry_run else (" (no-promote)" if args.no_promote else "")),
        f"- 深掘り評価: {len(analyzed)} ch / AUTO 追加: {len(promoted)} / queue: {len(spilled) + len(queued)}",
        f"- AUTO セクション: 追加前 {auto_now} → 上限 {global_cap}",
        "",
        "| ch | subs | 本編 | KW(検索/題名) | 普段(全期→直近30) | 相対HIT(内モメンタム) | 偽HIT | HIT率 | 最大views | like率 | timing | 判定 |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|",
    ]
    ordered = sorted(analyzed, key=promote_score, reverse=True)
    for a in ordered:
        lr = f"{a['like_rate']:.2%}" if a["like_rate"] is not None else ("非公開" if a["likes_hidden"] else "—")
        verdict = "✅AUTO" if a in promoted else ("⏳cap溢れ" if a in spilled else "🔎queue")
        lines.append(
            f"| {a['title'][:22]} | {a['subs']} | {a['n_videos']} | {a['kw_hits']}/{a['title_kw']} | "
            f"{a['median_views']:,}→{a['recent_median_views']:,} | "
            f"{a['hits']} ({a['momentum_hits']}) | {a['fake_hits']} | "
            f"{a['hit_rate']:.0%} | {a['max_views']:,} | {lr} | {a['timing']} | {verdict} ({a['reason']}) |"
        )
    # 関連度の目視用: マッチ KW と最大 HIT タイトル
    lines += ["", "## 詳細 (関連度チェック用)", ""]
    for a in ordered:
        lines.append(f"### {a['title']} (`{a['channel_id']}`)")
        lines.append(f"- マッチ KW ({a['kw_hits']}): {a['keywords']}")
        lines.append(f"- 最大 HIT: {a['max_views']:,}v 「{a['max_title']}」")
        lines.append(f"- 検索ヒット例: {a['sample_titles']}")
        lines.append("")
    body = "\n".join(lines) + "\n"
    report_path.write_text((prior.rstrip("\n") + "\n\n---\n\n" + body) if prior else body, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
