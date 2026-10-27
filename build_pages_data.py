"""GitHub Pages 用に time_series/ から公開 JSON を生成。

priority_channels.txt のチャンネル群について:
- snapshots.jsonl から subs / total_views 推移
- CSV スナップショット間 join で動画別 views 推移
- 直近 HIT 動画リスト

出力: dashboard/data/dashboard.json
"""

import csv
import json
import os
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import quota_meter
from external_hits import CORPUS_FILE, load_external_hits


def load_channel_ids(path: Path) -> list[str]:
    ids = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def load_snapshots(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def snap_ts(s: dict) -> str:
    return s.get("timestamp") or s.get("date")


def load_csv_snapshots(ch_dir: Path) -> dict[str, list[dict]]:
    """各 CSV (YYYY-MM-DD.csv) を読んで {date: [rows]}.

    Shorts (尺 180 秒以下) は除外する。取得側 (priority_channels_check.py) も
    180 秒で除外するが、2026-07-17 以前の CSV には 61〜180 秒のニュース Shorts が
    混入しているため、ここでも防御的にフィルタする。duration_sec 列が無い古い
    CSV の行はそのまま通す。
    """
    out: dict[str, list[dict]] = {}
    for csv_path in sorted(ch_dir.glob("????-??-??.csv")):
        date = csv_path.stem
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = []
            for r in csv.DictReader(f):
                try:
                    dur = int(r.get("duration_sec") or 0)
                except ValueError:
                    dur = 0
                if 0 < dur <= 180:
                    continue
                rows.append(r)
            out[date] = rows
    return out


# === HIT 定義（チャンネル相対・二段階） ===
# 絶対 1,000 再生ではなく「そのチャンネルの普段（最新 CSV 全動画 views の中央値）の何倍か」で判定。
# フロント (youtube-niche-dashboard js/main.js の HIT 定数) と対で管理。変える時は両方直すこと。
#   🎯 hit  = 中央値の HIT_STRONG_MULT 倍以上（本物の突出）
#   🚀 semi = 中央値の HIT_SEMI_MULT 倍以上（軽い突出・伸びかけ）
HIT_STRONG_MULT = 5
HIT_SEMI_MULT = 3
HIT_REL_FLOOR = 500      # 相対しきいの絶対下限（小チャンネルのノイズ除け）
HIT_ABS_FALLBACK = 1000  # 動画本数 < HIT_MIN_SAMPLE で中央値が不安定な ch の絶対値
HIT_MIN_SAMPLE = 5


def channel_baseline(rows: list[dict]) -> int | None:
    """最新 CSV 全動画 (Shorts 除外済) の views 中央値 = そのチャンネルの「普段」。"""
    views = []
    for r in rows:
        try:
            views.append(int(r["views"]))
        except (KeyError, ValueError):
            continue
    if len(views) < HIT_MIN_SAMPLE:
        return None
    views.sort()
    mid = len(views) // 2
    return views[mid] if len(views) % 2 else (views[mid - 1] + views[mid]) // 2


def relative_thresholds(rows: list[dict]) -> tuple[int | None, int, int]:
    """(baseline_median, hit_threshold, semi_threshold) を返す。"""
    base = channel_baseline(rows)
    if base is None:
        return None, HIT_ABS_FALLBACK, HIT_ABS_FALLBACK
    hit_th = max(HIT_REL_FLOOR, HIT_STRONG_MULT * base)
    semi_th = max(HIT_REL_FLOOR, HIT_SEMI_MULT * base)
    return base, hit_th, semi_th


# 広告判定: like数が基準線の期待値のこの倍率未満なら購入views疑い
# (priority_channels_check.py の PRIME_LIKE_RATIO_MIN と対で管理。docs/ad_check_methodology.md 参照)
AD_LIKE_RATIO_MIN = 0.5


def likes_baseline(points: list[tuple[int, int]]) -> dict | None:
    """非HIT動画の (views, likes) から like数の基準線を Theil-Sen 回帰で推定。

        like数 ≒ fan_likes（固定ファン分・切片） + slope × views（一見視聴者のlike率・傾き）

    「like率の中央値/平均」と違い、少数の熱心なファンが全動画に like する系統バイアスを
    切片に吸収できる（率だと低再生動画ほどファン分が効いて基準線が上振れする）。
    Theil-Sen（全ペア傾きの中央値）なので「偶然 like が多く付いた1本」にも頑健。
    戻り値: {"fan_likes", "slope", "n"} / 本数 < HIT_MIN_SAMPLE なら None。
    """
    pts = [(v, l) for v, l in points if v > 0]
    if len(pts) < HIT_MIN_SAMPLE:
        return None
    slopes = []
    for i in range(len(pts)):
        v1, l1 = pts[i]
        for j in range(i + 1, len(pts)):
            v2, l2 = pts[j]
            if v1 != v2:
                slopes.append((l2 - l1) / (v2 - v1))
    if not slopes:
        return None
    slope = max(0.0, statistics.median(slopes))
    fan = max(0.0, statistics.median(l - slope * v for v, l in pts))
    return {"fan_likes": round(fan, 1), "slope": slope, "n": len(pts)}


def expected_likes(baseline: dict | None, views: int) -> float | None:
    """基準線から views 時の期待 like数。基準線なし・期待値 1 未満（判定精度なし）は None。"""
    if not baseline:
        return None
    exp = baseline["fan_likes"] + baseline["slope"] * views
    return exp if exp >= 1 else None


# === 🌱 検索資産 (slow-burn) タブ ===
# 公開90日超なのに views が伸び続ける動画 = 検索需要キーワードの証拠 (hypotheses H-23/H-24)。
# ロジックは slow_burn_probe.py と対管理 (docs/longtail_keywords_2026-07-28.md が初回実走の正本)。
# 窓比 = 直近30日Δ / その前30日Δ。レコメンド尻尾は減衰 (中央値~0.6)、検索流入は安定〜加速 (>=0.8)。
SB_MIN_AGE_DAYS = 90
SB_MIN_MOVE = 5          # 両30日窓がこれ以上動いていれば表に載せる (完全停止は載せない)
SB_CANDIDATE_DELTA = 15  # 両窓これ以上 = slow-burn 候補 (search / tail の判定対象)
SB_SEARCH_RATIO = 0.8    # 窓比これ以上 = 🔎 持続流入 (旧称: 検索駆動疑い。H-23 精密化 2026-07-28)


def build_slow_burn(cids: list[str], base: Path, ch_meta: dict[str, dict]) -> list[dict]:
    """priority 各 ch の videos.jsonl から 90日超×伸び続けている動画を抽出。

    基準日 anchor = 全観測の最新日 (データから決まる = 再生成が決定的)。
    """
    series: dict[str, dict[str, dict]] = {}
    anchor: str = ""
    for cid in cids:
        path = base / cid / "videos.jsonl"
        if not path.exists():
            continue
        vids: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = r.get("video_id")
            ts = r.get("ts")
            views = r.get("views")
            if not (vid and ts and views is not None):
                continue
            v = vids.setdefault(vid, {"title": "", "published": "", "obs": {}})
            v["obs"][ts[:10]] = int(views)  # 同日複数観測は後勝ち (ts 昇順で読まれる前提はないが日粒度で十分)
            if r.get("title"):
                v["title"] = r["title"]
            if r.get("published_at"):
                v["published"] = r["published_at"][:10]
        series[cid] = vids
        for v in vids.values():
            if v["obs"]:
                anchor = max(anchor, max(v["obs"]))
    if not anchor:
        return []

    def views_at(obs: dict[str, int], target: str) -> tuple[str, int] | None:
        best = None
        for d, views in obs.items():
            if d <= target and (best is None or d > best[0]):
                best = (d, views)
        return best

    anchor_d = datetime.fromisoformat(anchor)
    out = []
    for cid, vids in series.items():
        meta = ch_meta.get(cid, {})
        baseline = meta.get("baseline_median")
        for vid, v in vids.items():
            if not v["published"]:
                continue
            try:
                age = (anchor_d - datetime.fromisoformat(v["published"])).days
            except ValueError:
                continue
            if age < SB_MIN_AGE_DAYS:
                continue
            latest_d = max(v["obs"])
            if (anchor_d - datetime.fromisoformat(latest_d)).days > 4:
                continue  # 観測が止まっている (削除等)
            p7 = views_at(v["obs"], (anchor_d - timedelta(days=7)).strftime("%Y-%m-%d"))
            p30 = views_at(v["obs"], (anchor_d - timedelta(days=30)).strftime("%Y-%m-%d"))
            p60 = views_at(v["obs"], (anchor_d - timedelta(days=60)).strftime("%Y-%m-%d"))
            if not (p30 and p60):
                continue
            views = v["obs"][latest_d]
            d30 = views - p30[1]
            d30_prev = p30[1] - p60[1]
            if d30 < SB_MIN_MOVE or d30_prev < SB_MIN_MOVE:
                continue
            ratio = round(d30 / d30_prev, 2) if d30_prev > 0 else None
            if d30 >= SB_CANDIDATE_DELTA and d30_prev >= SB_CANDIDATE_DELTA:
                klass = "search" if (ratio or 0) >= SB_SEARCH_RATIO else "tail"
            else:
                klass = "weak"
            out.append({
                "channel_id": cid,
                "channel": meta.get("title", cid),
                "video_id": vid,
                "title": v["title"],
                "url": f"https://www.youtube.com/watch?v={vid}",
                "published_at": v["published"],
                "age_days": age,
                "views": views,
                "baseline_mult": round(views / baseline, 1) if baseline else None,
                "d7": (views - p7[1]) if p7 else None,
                "d30": d30,
                "d30_prev": d30_prev,
                "ratio": ratio,
                "klass": klass,
            })
    out.sort(key=lambda x: -x["d30"])
    return out


# 動画別分析表 (videos.html): 公開 N 日後時点の views を復元する観測ポイント
MILESTONE_DAYS = (1, 3, 7, 14, 28)

# 軌跡分類 (videos.html 初速分析): 公開30日以降の直近28日Δで資産化/停止を判定。
# しきい値は 🌱検索資産タブ (SB_*) と対管理 — 2026-07-31 初速×資産化分析で導入
# (自ch旧作Ollama: 1,000到達day6→その後+15〜25/日で資産化、が発想元。docs/analysis_initial_velocity_2026-07-31.md)
TRAJ_MIN_AGE = 30      # これ未満は「進行中」(判定保留)
TRAJ_ASSET_D28 = 15    # 直近28日Δがこれ以上 = 伸長中 (SB_CANDIDATE_DELTA と同水準)
TRAJ_STALL_D28 = 5     # これ未満 = 停止 (0再生連発)


def build_video_table(csv_snaps: dict, engagement: dict) -> list[dict]:
    """チャンネル専用分析ページ用: 全動画 × (views / like率 / 広告判定 / 公開N日後の views)。

    N日後の値は日次 CSV スナップショットから復元。「公開N日後〜+1日」の観測が
    存在するときだけ埋める — 観測開始前に公開された動画は None (捏造しない)。"""
    if not csv_snaps:
        return []
    latest_date = max(csv_snaps)
    obs: dict[str, dict[str, int]] = {}
    for date, rows in csv_snaps.items():
        for r in rows:
            try:
                obs.setdefault(r["video_id"], {})[date] = int(r["views"])
            except (KeyError, ValueError):
                continue
    out = []
    for r in csv_snaps[latest_date]:
        try:
            views = int(r["views"])
        except (KeyError, ValueError):
            continue
        vid = r["video_id"]
        pub = (r.get("published_at") or "")[:10]
        marks: dict[str, int | None] = {f"d{nd}": None for nd in MILESTONE_DAYS}
        if pub and vid in obs:
            try:
                pub_d = datetime.fromisoformat(pub)
                dates = sorted(obs[vid])
                for nd in MILESTONE_DAYS:
                    target = pub_d + timedelta(days=nd)
                    for d in dates:
                        dd = datetime.fromisoformat(d)
                        if target <= dd <= target + timedelta(days=1):
                            marks[f"d{nd}"] = obs[vid][d]
                            break
                        if dd > target + timedelta(days=1):
                            break
            except ValueError:
                pass
        # 逆引きモード用: 直近 N 日での伸び = 現在 views − (最新観測日 − N日) 時点の views。
        # 目標日 ±1日の観測が無ければ None (初速モードと同じ「捏造しない」方針)。
        recents: dict[str, int | None] = {f"r{nd}": None for nd in MILESTONE_DAYS}
        if vid in obs:
            try:
                latest_d = datetime.fromisoformat(latest_date)
                pub_dt = datetime.fromisoformat(pub) if pub else None
                dates = sorted(obs[vid])
                for nd in MILESTONE_DAYS:
                    target = latest_d - timedelta(days=nd)
                    if pub_dt and pub_dt > target:
                        # 窓の途中で公開された動画: 全 views が窓内の伸び
                        recents[f"r{nd}"] = views
                        continue
                    best = None
                    for d in dates:
                        dd = datetime.fromisoformat(d)
                        if abs((dd - target).days) <= 1:
                            best = obs[vid][d]
                        elif dd > target + timedelta(days=1):
                            break
                    if best is not None:
                        recents[f"r{nd}"] = max(0, views - best)
            except ValueError:
                pass
        # 1,000到達日数: 日次観測が「跨ぎ」を目撃した場合のみ復元 (観測開始前の到達は捏造しない)。
        # 初回観測が公開3日以内で既に1,000超えの場合のみ「≤N日」(上限値) として記録。
        to1000 = None
        to1000_lte = False
        if views >= 1000 and pub and vid in obs:
            try:
                pub_d = datetime.fromisoformat(pub)
                prev = None
                for d in sorted(obs[vid]):
                    v_ = obs[vid][d]
                    dd = datetime.fromisoformat(d)
                    if v_ >= 1000:
                        if prev is None:
                            first_age = (dd - pub_d).days
                            if first_age <= 3:
                                to1000 = max(first_age, 1)
                                to1000_lte = True
                        else:
                            pd_, pv = prev
                            span = (dd - pd_).total_seconds()
                            frac = (1000 - pv) / (v_ - pv) if v_ > pv else 0.0
                            cross = pd_ + timedelta(seconds=frac * span)
                            to1000 = round((cross - pub_d).total_seconds() / 86400, 1)
                        break
                    prev = (dd, v_)
            except ValueError:
                pass
        # 軌跡分類: 30日以降も伸びているか (直近28日Δ = r28 で判定)
        age_days = None
        traj = None
        if pub:
            try:
                age_days = (datetime.fromisoformat(latest_date) - datetime.fromisoformat(pub)).days
            except ValueError:
                pass
        if age_days is not None:
            r28_ = recents.get("r28")
            if age_days < TRAJ_MIN_AGE:
                traj = "fresh"
            elif r28_ is None:
                traj = None
            elif r28_ >= TRAJ_ASSET_D28:
                traj = "asset"
            elif r28_ < TRAJ_STALL_D28:
                traj = "stalled"
            else:
                traj = "tail"
        e = engagement.get(vid, {})
        out.append({
            "video_id": vid,
            "title": r.get("title", ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published_at": pub,
            "views": views,
            "age_days": age_days,
            "to1000": to1000,
            "to1000_lte": to1000_lte,
            "traj": traj,
            "like_pct": e.get("like_pct"),
            "likes": e.get("likes"),
            "ad_check": e.get("ad_check"),
            "likes_vs_exp_pct": e.get("likes_vs_exp_pct"),
            **marks,
            **recents,
        })
    out.sort(key=lambda x: x["published_at"], reverse=True)
    return out


def build_channel_data(cid: str, base: Path, recent_days: int = 14) -> dict:
    ch_dir = base / cid
    snapshots = load_snapshots(ch_dir / "snapshots.jsonl")
    csv_snaps = load_csv_snapshots(ch_dir)

    if not snapshots:
        return {"id": cid, "title": cid, "snapshots": [], "recent_hits": [], "video_history": {}}

    # 最新スナップショットから title 取得
    title = snapshots[-1].get("channel_title", cid)

    # subs / total_views 推移
    subs_history = []
    views_history = []
    for s in snapshots:
        ts = snap_ts(s)
        subs_history.append({"timestamp": ts, "subs": s.get("subscriber_count", 0)})
        views_history.append(
            {
                "timestamp": ts,
                "total_views": s.get("total_views_in_window", 0),
                "videos": s.get("total_videos_in_window", 0),
            }
        )

    # チャンネル相対の HIT しきい値（最新 CSV 全動画から算出）
    latest_rows_all = csv_snaps[max(csv_snaps.keys())] if csv_snaps else []
    baseline_median, hit_threshold, semi_threshold = relative_thresholds(latest_rows_all)

    # 広告判定 (最新 CSV の like ベース。priority_channels_check.detect_prime と対で管理。
    #  仕組みの解説は docs/ad_check_methodology.md)
    #   基準線: 非HIT動画 (views < semi_threshold) への Theil-Sen 回帰
    #           like数 ≒ fan_likes(固定ファン分) + slope×views(一見視聴者のlike率)
    #   suspect: views>=500 かつ (like率 < 0.4% または like数 < 期待値の AD_LIKE_RATIO_MIN 倍)
    #   ok:      views>=500 で上記に該当せず
    #   na:      views < 500 (サンプル不足で判定対象外)
    #   unknown: チャンネルが likeCount 非公開 (全動画 like=0)
    engagement: dict[str, dict] = {}
    like_baseline = None
    if csv_snaps:
        latest_rows_e = csv_snaps[max(csv_snaps.keys())]
        likes_all_zero = True
        parsed_e = []
        for r in latest_rows_e:
            try:
                parsed_e.append((r, int(r["views"]), int(r.get("likes") or 0)))
            except (KeyError, ValueError):
                continue
        likes_all_zero = not any(likes > 0 for _, _, likes in parsed_e)
        if not likes_all_zero:
            like_baseline = likes_baseline(
                [(views, likes) for _, views, likes in parsed_e if views < semi_threshold])
        for r, views, likes in parsed_e:
            like_pct = round(100.0 * likes / views, 2) if views else 0.0
            exp = expected_likes(like_baseline, views)
            ratio_pct = round(100.0 * likes / exp) if exp else None
            if likes_all_zero:
                verdict = "unknown"
            elif views < 500:
                verdict = "na"
            elif like_pct < 0.4 or (exp and likes < AD_LIKE_RATIO_MIN * exp):
                verdict = "suspect"
            else:
                verdict = "ok"
            engagement[r["video_id"]] = {
                "likes": likes, "like_pct": like_pct, "ad_check": verdict,
                "exp_likes": round(exp, 1) if exp else None,
                "likes_vs_exp_pct": ratio_pct,
            }

    # 最新 CSV から ★直近 HIT（相対判定。semi 以上を載せ、tier で区別）
    recent_hits = []
    if csv_snaps:
        latest_date = max(csv_snaps.keys())
        latest_rows = csv_snaps[latest_date]
        for r in latest_rows:
            try:
                age = int(r["age_days"])
                views = int(r["views"])
            except (KeyError, ValueError):
                continue
            if age <= recent_days and views >= semi_threshold:
                recent_hits.append(
                    {
                        "video_id": r["video_id"],
                        "title": r["title"],
                        "views": views,
                        "score": float(r.get("score", 0)),
                        "age_days": age,
                        "published_at": r.get("published_at", ""),
                        "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                        "tier": "hit" if views >= hit_threshold else "semi",
                        "baseline_mult": round(views / baseline_median, 1) if baseline_median else None,
                        **engagement.get(r["video_id"], {}),
                    }
                )
        recent_hits.sort(key=lambda x: x["views"], reverse=True)

    # 動画別 views 推移（直近 30 日投稿 + 全 HIT/準HIT 動画）
    # 旧絶対基準 1,000+ も残す: 相対しきいが高い ch で追跡済み動画の履歴が途切れないようにする
    target_video_ids = set()
    if csv_snaps:
        latest_date = max(csv_snaps.keys())
        for r in csv_snaps[latest_date]:
            try:
                age = int(r["age_days"])
                views = int(r["views"])
            except (KeyError, ValueError):
                continue
            if age <= 30 or views >= HIT_ABS_FALLBACK or views >= semi_threshold:
                target_video_ids.add(r["video_id"])

    # 日付 → その日の最新観測タイムスタンプ
    date_to_latest_ts: dict[str, str] = {}
    for s in snapshots:
        ts = s.get("timestamp") or s.get("date")
        if not ts:
            continue
        d = ts[:10]
        prev = date_to_latest_ts.get(d, "")
        if ts > prev:
            date_to_latest_ts[d] = ts if "T" in ts else (ts + "T23:59:59Z")

    # video_id -> {ts: views}（dedupe 用）
    obs_by_vid: dict[str, dict[str, int]] = {}
    titles: dict[str, str] = {}
    pubs: dict[str, str] = {}

    # 1) 日付単位 CSV: 1 日 1 観測（intra-day 上書きで失われた過去データ）
    for date in sorted(csv_snaps.keys()):
        ts = date_to_latest_ts.get(date, date + "T23:59:59Z")
        for r in csv_snaps[date]:
            vid = r["video_id"]
            if vid not in target_video_ids:
                continue
            try:
                views = int(r["views"])
            except (KeyError, ValueError):
                continue
            obs_by_vid.setdefault(vid, {})[ts] = views
            titles[vid] = r["title"]
            pubs[vid] = r.get("published_at", "")

    # 2) videos.jsonl: 観測ポイント単位（5/13 以降から蓄積）
    videos_jsonl = ch_dir / "videos.jsonl"
    if videos_jsonl.exists():
        for line in videos_jsonl.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = r.get("video_id")
            ts = r.get("ts")
            views = r.get("views")
            if not (vid and ts and views is not None):
                continue
            if vid not in target_video_ids:
                continue
            obs_by_vid.setdefault(vid, {})[ts] = int(views)
            if r.get("title"):
                titles[vid] = r["title"]
            if r.get("published_at"):
                pubs[vid] = r["published_at"]

    video_history: dict[str, dict] = {}
    for vid, ts_views in obs_by_vid.items():
        sorted_history = [
            {"date": ts, "views": v}
            for ts, v in sorted(ts_views.items())
        ]
        video_history[vid] = {
            "title": titles.get(vid, vid),
            "published_at": pubs.get(vid, ""),
            "url": f"https://www.youtube.com/watch?v={vid}",
            "history": sorted_history,
            **engagement.get(vid, {}),
        }

    # 投稿日別の本数（最新 CSV から、直近 60 日分）
    daily_posts: dict[str, int] = {}
    if csv_snaps:
        latest_date = max(csv_snaps.keys())
        for r in csv_snaps[latest_date]:
            try:
                age = int(r["age_days"])
            except (KeyError, ValueError):
                continue
            if age > 60:
                continue
            d = r.get("published_at", "")[:10]
            if d:
                daily_posts[d] = daily_posts.get(d, 0) + 1
    daily_posts_list = [{"date": d, "count": c} for d, c in sorted(daily_posts.items())]

    return {
        "id": cid,
        "title": title,
        "like_baseline": (
            {"fan_likes": like_baseline["fan_likes"],
             "slope_pct": round(100 * like_baseline["slope"], 2),
             "n": like_baseline["n"]}
            if like_baseline else None),
        # フロントはこの値を優先して使う (video_history から再計算すると追跡対象のみの偏ったサンプルになるため)
        "baseline_median": baseline_median,
        "hit_threshold": hit_threshold,
        "semi_threshold": semi_threshold,
        "subs_history": subs_history,
        "views_history": views_history,
        "daily_posts": daily_posts_list,
        "recent_hits": recent_hits,
        "video_history": video_history,
        # チャンネル専用分析ページ (videos.html?ch=<id>) 用の全動画表
        "video_table": build_video_table(csv_snaps, engagement),
    }


def load_discovery_data(root: Path) -> dict:
    """discovery loop の状態をダッシュボード「🧭 新規発見」タブ用に整形。

    - keywords: discovery_keywords_tech.txt の検索キーワード + コメント欄の根拠
      (「#   <KW> … <根拠>」形式の行から対応付け)
    - evaluated: discovery_seen.json の評価済み ch 台帳 (新しい順)
    """
    kw_path = root / "discovery_keywords_tech.txt"
    header = ""
    group = ""
    kws: list[tuple[str, str]] = []  # (keyword, group)
    notes: dict[str, str] = {}
    if kw_path.exists():
        for raw in kw_path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("##"):
                # 「## グループ名」= トピック分類の見出し (discovery_loop からはコメント扱い)
                group = line.lstrip("#").strip()
                continue
            if line.startswith("#"):
                body = line.lstrip("#").strip()
                if not header and body:
                    header = body
                if "…" in body:
                    k, _, note = body.partition("…")
                    if k.strip():
                        notes[k.strip()] = note.strip()
                continue
            kws.append((line, group))

    # 規模感 (subs / 再生中央値) は rss_pool.json から突合する
    # (discovery_seen.json 自体には保存されていない。pool は RSS 巡回が日々更新)
    pool: dict = {}
    pool_path = root / "rss_pool.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] rss_pool.json が壊れているため規模感の突合をスキップ")

    # 「違う確定」ch は評価済み台帳から外す — 専用の rejected 台帳 (load_rejected_entries) 側に移動
    rejected = load_rejected(root)
    # 言語 (ja / en / other) — ダッシュボードで 🇯🇵 日本語 / 🌐 海外 に分けるため (2026-10-24)。
    # ch名だけでは英語名の日本語ch (GDG Tokyo 等) を誤るので、外部HITコーパスのサンプル題名・
    # 全条件クリア時の最大HIT題名・RSS巡回が残す最新題名 (sample_titles) も併せて見る
    from pipeline_board import lang_of
    try:
        from channel_filter import _channel_samples
        samples = _channel_samples()
    except Exception:
        samples = {}
    evaluated = []
    seen_path = root / "discovery_seen.json"
    if seen_path.exists():
        try:
            seen = json.loads(seen_path.read_text(encoding="utf-8"))
            for cid, e in seen.items():
                if cid in rejected:
                    continue
                p = pool.get(cid, {})
                lang_text = " ".join(
                    list(samples.get(cid, [])) + list(p.get("sample_titles") or []) + [e.get("max_title") or ""]
                )
                evaluated.append({
                    "channel_id": cid,
                    "title": e.get("title", cid),
                    "date": e.get("date", ""),
                    "decision": e.get("decision", ""),
                    "reason": e.get("reason", ""),
                    "subs": p.get("subs"),
                    "median_views": p.get("median_views"),
                    "lang": lang_of(lang_text, e.get("title", "")),
                })
            evaluated.sort(key=lambda x: x["date"], reverse=True)
        except json.JSONDecodeError:
            print("[warn] discovery_seen.json が壊れているためスキップ")

    return {
        "header": header,
        "keywords": [{"kw": k, "group": g, "note": notes.get(k, "")} for k, g in kws],
        "evaluated": evaluated,
    }


def _parse_report_metrics(root: Path) -> dict[str, dict]:
    """discovery_reports/*.md の表から全条件クリア (⏳cap溢れ / ✅AUTO) 行の指標を拾う。

    2026-10-13 より前は台帳 (discovery_seen.json) に指標が残っておらず、レポートが唯一の記録。
    ただし同日 2 回目の実行 (quota-drain) がレポートを上書きしていた期間があり、全件は揃わない。
    表の ch 名は 22 文字で切られているため「## 詳細」ブロックの `### 名前 (`UC…`)` で channel_id に戻す。
    """
    out: dict[str, dict] = {}
    rep_dir = root / "discovery_reports"
    if not rep_dir.exists():
        return out
    detail_re = re.compile(r"^### (.+?) \(`(UC[\w-]+)`\)$")
    for f in sorted(rep_dir.glob("*.md")):
        date = f.stem[:10]
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = text.splitlines()
        # 詳細ブロック: 名前 → (cid, keywords, max_title)
        details: list[tuple[str, dict]] = []
        for i, line in enumerate(lines):
            m = detail_re.match(line.strip())
            if not m:
                continue
            info = {"channel_id": m.group(2), "keywords": "", "max_title": ""}
            for nxt in lines[i + 1:i + 4]:
                if nxt.startswith("- マッチ KW"):
                    info["keywords"] = nxt.partition(":")[2].strip()
                elif nxt.startswith("- 最大 HIT:"):
                    mt = re.search(r"「(.*)」", nxt)
                    info["max_title"] = mt.group(1) if mt else ""
            details.append((m.group(1).strip(), info))
        header: list[str] = []
        for line in lines:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells[0] == "ch":
                header = cells
                continue
            if not header or set(cells[0]) <= {"-", ":"}:
                continue
            if len(cells) > len(header):
                # ch 名に "|" を含む行は列がずれる → 余りを名前側に畳む
                extra = len(cells) - len(header)
                cells = [" | ".join(cells[:extra + 1])] + cells[extra + 1:]
            if len(cells) != len(header):
                continue
            verdict = cells[-1]
            if "cap溢れ" not in verdict and "✅AUTO" not in verdict:
                continue
            row = dict(zip(header, cells))
            name = cells[0]
            info = next((d for n, d in details if n.startswith(name.rstrip())), None)
            if not info:
                continue

            def num(key_sub: str):
                for k, v in row.items():
                    if key_sub in k:
                        v = v.replace(",", "")
                        m2 = re.match(r"-?\d+(\.\d+)?", v)
                        return float(m2.group(0)) if m2 and "." in m2.group(0) else (int(m2.group(0)) if m2 else None)
                return None

            def txt(key_sub: str) -> str:
                for k, v in row.items():
                    if key_sub in k:
                        return v
                return ""

            usual = txt("普段")
            hits_txt = txt("HIT") if "相対HIT" not in header else txt("相対HIT")
            m_h = re.match(r"(\d+)(?:\s*\((\d+)\))?", hits_txt)
            lr = txt("like率")
            m_lr = re.match(r"([\d.]+)%", lr)
            m_hr = re.match(r"(\d+)%", txt("HIT率"))
            m_usual = re.match(r"([\d,]+)(?:→([\d,]+))?", usual)
            out[info["channel_id"]] = {
                "date": date,
                "subs": num("subs"),
                "n_videos": num("本編"),
                "median_views": int(m_usual.group(1).replace(",", "")) if m_usual else None,
                "recent_median_views": int(m_usual.group(2).replace(",", "")) if (m_usual and m_usual.group(2)) else None,
                "hits": int(m_h.group(1)) if m_h else None,
                "momentum_hits": int(m_h.group(2)) if (m_h and m_h.group(2)) else None,
                "hit_rate": int(m_hr.group(1)) / 100 if m_hr else None,
                "max_views": num("最大views"),
                "like_rate": float(m_lr.group(1)) / 100 if m_lr else None,
                "timing": txt("timing"),
                "keywords": info["keywords"],
                "max_title": info["max_title"],
            }
    return out


def load_all_clear(root: Path, icons: dict[str, str]) -> list[dict]:
    """discovery の全 AND ゲートを通った ch (= 確認待ち) の棚卸し一覧。

    正本は discovery_seen.json の decision ∈ {queued(cap), promoted} (2026-10-13 から指標も同梱)。
    それ以前の分はレポート表から補完。人間の判断状況 (priority 済 / boosted / 違う確定 / 未判断) を付ける。
    """
    seen_path = root / "discovery_seen.json"
    if not seen_path.exists():
        return []
    try:
        seen = json.loads(seen_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[warn] discovery_seen.json が壊れているため全条件クリア一覧をスキップ")
        return []
    reports = _parse_report_metrics(root)
    pool: dict = {}
    pool_path = root / "rss_pool.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    priority = set(load_channel_ids(root / "priority_channels.txt")) if (root / "priority_channels.txt").exists() else set()
    boosted = set(load_channel_ids(root / "boosted_channels.txt")) if (root / "boosted_channels.txt").exists() else set()
    rejected = load_rejected(root)
    keys = ("subs", "n_videos", "kw_hits", "keywords", "median_views", "recent_median_views",
            "hits", "momentum_hits", "fake_hits", "hit_rate", "max_views", "max_title", "like_rate", "timing")
    out = []
    for cid, e in seen.items():
        if e.get("decision") not in ("queued(cap)", "promoted"):
            continue
        rep = reports.get(cid, {})
        row = {"channel_id": cid, "title": e.get("title", cid), "date": e.get("date", ""),
               "icon": icons.get(cid, ""), "decision": e.get("decision", "")}
        for k in keys:
            v = e.get(k)
            if v is None:
                v = rep.get(k)
            row[k] = v
        row["has_metrics"] = row["hits"] is not None
        # 指標が無い (レポート上書きで喪失) 分は規模感だけ RSS プールの最新観測で補う
        pm = pool.get(cid, {})
        if row["subs"] is None:
            row["subs"] = pm.get("subs")
        if row["median_views"] is None:
            row["median_views"] = pm.get("median_views")
        row["latest_video_at"] = pm.get("latest_video_at", "")
        if cid in priority:
            row["status"] = "priority"
        elif cid in boosted:
            row["status"] = "boosted"
        elif cid in rejected:
            row["status"] = "rejected"
        else:
            row["status"] = "pending"
        out.append(row)
    order = {"pending": 0, "boosted": 1, "priority": 2, "rejected": 3}
    # 未判断を先頭・その中は新しい順
    out.sort(key=lambda r: (order.get(r["status"], 9), -int(r["date"].replace("-", "") or 0)))
    return out


def load_memo(root: Path) -> dict:
    """📝 メモタブ: docs/memo.md (運営者の自由メモ・アイデア) をそのまま載せる。
    編集は GitHub の edit 画面 (フロントにリンク) か、ローカルで直接。ダッシュボードは public なので公開前提。"""
    path = root / "docs" / "memo.md"
    if not path.exists():
        return {"text": "", "updated_at": ""}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"text": "", "updated_at": ""}
    # 更新日時は git の最終 commit 日時 (CI の checkout では mtime が実行時刻になるため)。git が無ければ mtime
    updated = ""
    try:
        import subprocess
        r = subprocess.run(["git", "log", "-1", "--format=%cI", "--", str(path)],
                           capture_output=True, text=True, timeout=10)
        updated = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not updated:
        updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"text": text, "updated_at": updated}


def load_rejected(root: Path) -> set[str]:
    """rejected_channels.txt = 人間の「違う確定」リスト。巡回・照合・通常一覧から除外する。"""
    p = root / "rejected_channels.txt"
    if not p.exists():
        return set()
    return set(load_channel_ids(p))


def load_rejected_entries(root: Path) -> list[dict]:
    """rejected_channels.txt を「🚫 違う確定」台帳として構造化 (ダッシュボードで確認できるように)。

    行形式: `<channel_id>  # <title> (違う確定 YYYY-MM-DD)` — 「✗違う」ボタンのコピー形式。
    コメントにタイトルが無い行は discovery_seen.json / rss_pool.json から補完する。
    """
    p = root / "rejected_channels.txt"
    if not p.exists():
        return []
    titles: dict[str, str] = {}
    for name in ("rss_pool.json", "discovery_seen.json"):
        f = root / name
        if f.exists():
            try:
                for cid, m in json.loads(f.read_text(encoding="utf-8")).items():
                    if m.get("title"):
                        titles.setdefault(cid, m["title"])
            except json.JSONDecodeError:
                pass
    out = []
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        cid, _, comment = raw.partition("#")
        cid = cid.strip()
        if not cid:
            continue
        m = re.search(r"\(違う確定\s*(\d{4}-\d{2}-\d{2})\)", comment)
        title = re.sub(r"\(違う確定[^)]*\)", "", comment).strip()
        out.append({
            "channel_id": cid,
            "title": title or titles.get(cid, cid),
            "date": m.group(1) if m else "",
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def load_pool_data(root: Path, icons: dict[str, str]) -> list[dict]:
    """rss_pool.json (発見済みだが未ウォッチの競合候補) をダッシュボード用に整形。

    subs_log (rss_patrol が日次追記) から「前月比の登録者の伸び」を計算:
    直近値 − 28日以上前で最も新しいエントリ。履歴が28日未満の間は None (表示は「—」)。
    """
    pool = {}
    pool_path = root / "rss_pool.json"
    if pool_path.exists():
        try:
            pool = json.loads(pool_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] rss_pool.json が壊れているためスキップ")
    rejected = load_rejected(root)
    # 言語判定 (ja / en / other) 用: ch名 + サンプル動画タイトル。ダッシュボードで EN/JA タブに分ける。
    from pipeline_board import lang_of
    try:
        from channel_filter import _channel_samples, load_labels as _load_ch_labels
        samples = _channel_samples()
        ch_labels = _load_ch_labels()   # IT関連度 in / out (channel_filter / ai_judge 由来)
    except Exception:
        samples = {}
        ch_labels = {}
    out = []
    for cid, m in pool.items():
        if m.get("dead") or cid in rejected:
            continue
        log = m.get("subs_log") or {}
        subs = m.get("subs")
        mom = None
        if log:
            dates = sorted(log)
            latest_d = dates[-1]
            try:
                base = [d for d in dates
                        if (datetime.fromisoformat(latest_d) - datetime.fromisoformat(d)).days >= 28]
                if base:
                    mom = log[latest_d] - log[base[-1]]
            except ValueError:
                pass
        out.append({
            "channel_id": cid,
            "title": m.get("title", cid),
            "icon": icons.get(cid, ""),
            "subs": subs,
            "subs_mom": mom,
            "median_views": m.get("median_views"),
            "latest_video_at": m.get("latest_video_at", ""),
            "source": m.get("source", ""),
            "added": m.get("added", ""),
            # ja / en / other。ch名だけでは英語名の日本語chを誤るのでサンプルタイトルも併せて見る
            "lang": lang_of(" ".join(list(samples.get(cid, [])) + list(m.get("sample_titles") or [])), m.get("title", "")),
            # IT関連度: in / out / unknown(未判定)。channel_filter / ai_judge が付ける
            "rel": (ch_labels.get(cid) or {}).get("rel", "unknown"),
            # 直近フィードの like率 (rss_patrol 由来・quota 0)。<0.4% を ⚠️盛られ疑いとして表示
            "like_rate": m.get("like_rate"),
        })
    # 前月比の伸び降順 (未計測は後ろ、その中では subs 降順)
    out.sort(key=lambda x: (x["subs_mom"] is not None, x["subs_mom"] or 0, x["subs"] or 0), reverse=True)
    return out


def load_analyses(root: Path) -> list[dict]:
    """docs/ の分析ドキュメント (case_*.md / analysis_*.md) をダッシュボード「📚 分析履歴」用に収集。
    タイトルは先頭の # 見出し、日付はファイル名の YYYY-MM-DD (無ければ更新日時)。

    情報源テーブル: doc 内の `<!-- evidence:json [...] -->` ブロック (どの動画が見つかり、
    そのchの普段中央値がいくつで、何倍だったか) をパースして "evidence" として渡す。
    タブは evidence 表を主役に表示し、本文全文は畳んで添える。新しい順。"""
    import re as _re
    out = []
    docs = root / "docs"
    if not docs.exists():
        return out
    for p in sorted(docs.glob("*.md")):
        if not (p.name.startswith("case") or p.name.startswith("analysis")):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        title = p.stem
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        m = _re.search(r"(\d{4}-\d{2}-\d{2})", p.name)
        date = m.group(1) if m else datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        evidence = []
        ev = _re.search(r"<!--\s*evidence:json\s*(\[.*?\])\s*-->", text, _re.DOTALL)
        if ev:
            try:
                evidence = json.loads(ev.group(1))
            except json.JSONDecodeError:
                print(f"[warn] {p.name} の evidence:json が壊れているためスキップ")
        out.append({"file": p.name, "title": title, "date": date,
                    "evidence": evidence, "content": text})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def load_daily_reports(root: Path, keep: int = 14) -> list[dict]:
    """daily_report.py が書く own_channel/daily_reports.jsonl → 📰 日次タブ用 (新しい順・直近N日)。"""
    p = root / "own_channel" / "daily_reports.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.sort(key=lambda r: r.get("date", ""), reverse=True)
    return out[:keep]


def load_channel_icons(root: Path, channel_ids: list[str]) -> dict[str, str]:
    """チャンネルアイコン URL。channel_icons.json にキャッシュし、無い分だけ API で取得 (1 unit/50ch)。"""
    cache_path = root / "channel_icons.json"
    icons: dict[str, str] = {}
    if cache_path.exists():
        try:
            icons = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            icons = {}
    missing = [c for c in channel_ids if not icons.get(c)]
    key = os.environ.get("YOUTUBE_API_KEY")
    if missing and key:
        try:
            for i in range(0, len(missing), 50):
                url = "https://www.googleapis.com/youtube/v3/channels?" + urllib.parse.urlencode(
                    {"part": "snippet", "id": ",".join(missing[i:i + 50]), "key": key}
                )
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = json.load(r)
                for it in data.get("items", []):
                    thumbs = it["snippet"].get("thumbnails", {})
                    u = (thumbs.get("default") or thumbs.get("medium") or {}).get("url", "")
                    if u:
                        icons[it["id"]] = u
            cache_path.write_text(
                json.dumps(icons, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:  # アイコンは装飾なので失敗しても本体は生成する
            print(f"[warn] channel icons 取得失敗: {e}")
    elif missing and not key:
        print(f"[warn] YOUTUBE_API_KEY 未設定のため icon {len(missing)} 件を取得できない")
    return icons


def main() -> int:
    root = Path(".")
    channels_file = root / "priority_channels.txt"
    base = root / "time_series"
    out_dir = root / "dashboard" / "data"
    out_dir.mkdir(parents=True, exist_ok=True)

    ids = load_channel_ids(channels_file)
    channels = [build_channel_data(cid, base) for cid in ids]

    # 広告混ざり枠 (別ディレクトリ・ダッシュボードでは ⚠️ 表示、急伸ウォッチ除外)
    boosted_file = root / "boosted_channels.txt"
    boosted_base = root / "time_series_boosted"
    if boosted_file.exists() and boosted_base.exists():
        for cid in load_channel_ids(boosted_file):
            ch = build_channel_data(cid, boosted_base)
            ch["boosted"] = True
            channels.append(ch)

    # アイコンは priority + RSS プールの全 ch 分をまとめて取得 (キャッシュ済みは 0u、新規 1u/50ch)
    pool_ids = []
    pool_path = root / "rss_pool.json"
    if pool_path.exists():
        try:
            pool_ids = [cid for cid, m in json.loads(pool_path.read_text(encoding="utf-8")).items()
                        if not m.get("dead")]
        except json.JSONDecodeError:
            pass
    # discovery 評価済み台帳 + 違う確定台帳の ch も一緒に取得 (🧭 新規発見タブのアイコン用)
    discovery = load_discovery_data(root)
    rejected_entries = load_rejected_entries(root)
    eval_ids = [e["channel_id"] for e in discovery.get("evaluated", [])]
    rej_ids = [r["channel_id"] for r in rejected_entries]
    icons = load_channel_icons(root, [c["id"] for c in channels] + pool_ids + eval_ids + rej_ids)
    for c in channels:
        c["icon"] = icons.get(c["id"], "")
    for e in discovery.get("evaluated", []):
        e["icon"] = icons.get(e["channel_id"], "")
    for r in rejected_entries:
        r["icon"] = icons.get(r["channel_id"], "")

    # 市場検証キャッシュ (market_validate.py が生成)。video_id → 全 YouTube 横断の需要検証結果。
    market = {}
    market_path = root / "time_series" / "market_checks.json"
    if market_path.exists():
        try:
            market = json.loads(market_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] market_checks.json が壊れているためスキップ")

    # 外部HITコーパス (market_validate / discovery_loop が蓄積した監視枠外の HIT)。
    # フロントの横断分析 (buildHitCorpus) が照合対象に合流させる。「違う確定」ch は除外。
    rejected = load_rejected(root)
    external = [
        {k: e.get(k) for k in ("video_id", "title", "channel", "channel_id",
                               "views", "subs", "score", "published_at", "url", "source",
                               "axis")}  # axis: corpus_filter の軸内/軸外 (🏆当たり事例タブが out を除外)
        for e in load_external_hits(base / CORPUS_FILE)
        if e.get("channel_id") not in rejected
    ]

    # YouTube API quota の自己計上 (各スクリプトが quota_log.jsonl に記録した推定消費。
    # 正確な値は Cloud Console のみ。日境界は quota リセットに合わせて太平洋時間)
    today_pt = quota_meter.pt_date()
    by_source: dict[str, int] = {}
    daily: dict[str, int] = {}
    for e in quota_meter.load_log(base):
        d = e.get("date_pt", "")
        u = int(e.get("units") or 0)
        daily[d] = daily.get(d, 0) + u
        if d == today_pt:
            s = e.get("source", "?")
            by_source[s] = by_source.get(s, 0) + u
    quota = {
        "date_pt": today_pt,
        "today_units": daily.get(today_pt, 0),
        "limit": quota_meter.DAILY_LIMIT,
        "by_source": by_source,
        "daily": [{"date": d, "units": u} for d, u in sorted(daily.items())[-14:]],
    }

    # 🌱 検索資産 (slow-burn): priority のみ (boosted は views が実需要を反映しないため除外)
    ch_meta = {c["id"]: c for c in channels if not c.get("boosted")}
    slow_burn = build_slow_burn(list(ch_meta), base, ch_meta)

    # 🗺 鉱脈マップ (サジェスト鉱脈探索の結果台帳。正本 docs/suggest_vein_scan.md / vein_map.json)
    vein_map = {}
    vein_path = root / "vein_map.json"
    if vein_path.exists():
        try:
            vein_map = json.loads(vein_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] vein_map.json が壊れているためスキップ")

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": channels,
        "slow_burn": slow_burn,
        "vein_map": vein_map,
        "market": market,
        "external_hits": external,
        "discovery": discovery,
        "all_clear": load_all_clear(root, icons),
        "memo": load_memo(root),
        "rejected": rejected_entries,
        "pool": load_pool_data(root, icons),
        "analyses": load_analyses(root),
        "daily_reports": load_daily_reports(root),
        "quota": quota,
    }

    out_path = out_dir / "dashboard.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(f"OK: {out_path} ({size_kb:.1f} KB)")
    print(f"  channels: {len(channels)}")
    for c in channels:
        print(f"  - {c['title']}: snapshots={len(c['subs_history'])} hits={len(c['recent_hits'])} videos_tracked={len(c['video_history'])}")
    n_search = sum(1 for s in slow_burn if s["klass"] == "search")
    print(f"  slow_burn: {len(slow_burn)}本 (🔎持続流入 {n_search})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
