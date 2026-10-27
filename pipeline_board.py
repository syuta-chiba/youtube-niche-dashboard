"""企画の優先度ボード — 10日〜1ヶ月先までのランキングを常時保持する。

正本: docs/pipeline_board.md（本スクリプトが自動生成・上書き）
方針: docs/methodology.md §1.4a（絞り込み かつ 本数）/ docs/shelf_check_methodology.md

**優先の原則（ユーザー指定 2026-08-11）**
  1. まず「**当たる動画**」= 今まさに波が立っているテーマ（直近45日にJPの新規HITがある）
  2. 無ければ「**検索狙い動画**」= 波は無いが棚が流れ続けているテーマ（日速で確認済み）

入力（全部ローカル・追加 quota ゼロ）:
  time_series/external_hits.jsonl  … 外部HITコーパス（波の検知）
  time_series/UC*/videos.jsonl     … 監視chの相対HIT（波の検知・ch相対）
  shelf_checks.json                … 棚の日速（検索狙いの根拠）
  vein_map.json                    … 棚台帳（人手の verdict）
  own_channel/videos.jsonl         … 自ch実績（既出テーマの除外 = H-15）

Usage:
    python pipeline_board.py                 # docs/pipeline_board.md を再生成
    python pipeline_board.py --days 45       # 波とみなす窓（デフォルト45日）
    python pipeline_board.py --top 30        # 出力件数
"""
import argparse
import datetime as dt
import glob
import io
import json
import os
import re
import statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "docs", "pipeline_board.md")

# テーマ辞書: 表示名 -> 判定パターン。ASCII 語は単語境界で判定（rag が drags を拾う事故の再発防止）
THEMES = {
    "ローカルLLM": r"ローカルllm|ローカルai|local llm|ollama|lm ?studio|llama\.cpp|gguf|量子化",
    "ローカルLLM×RAG": r"(rag|検索拡張|ナレッジベース|anythingllm|open ?webui)",
    "Claude Code": r"claude ?code",
    "Codex": r"codex|agents\.md",
    "Cursor": r"cursor",
    "MCP": r"\bmcp\b|mcpサーバー",
    "Agent Skills": r"agent skills|スキル|skills",
    "Gemini CLI / Antigravity": r"gemini ?cli|antigravity",
    "AIエージェント": r"ai ?エージェント|ai ?agent|autogen|langgraph",
    "n8n / 自動化": r"\bn8n\b|dify|zapier|make\.com",
    "ファインチューニング": r"ファインチューニング|fine ?tun|lora|qlora",
    "VRAM / PC選び": r"vram|rtx ?\d+.{0,12}(llm|ai|推論)|ミニpc.{0,12}(llm|ai)|メモリ ?\d+gb.{0,12}(llm|ai)",
    "Obsidian / ノート": r"obsidian|notebooklm",
    "動画生成AI": r"動画生成|veo|sora|nano ?banana|framepack",
}
NEG = re.compile(r"drags|storage|arrang|fragr|\brage\b", re.I)
# 軸外（ゲーム実況・自作PC実況・音楽など）を弾く
OFFAXIS = re.compile(r"ゲーミング|ゲーム実況|アンパンマン|フォートナイト|原神|apex|マイクラ|歌って|カラオケ|vlog", re.I)
# 中国語も 一-龥 を使うので、JP判定は ひらがな/カタカナ の有無で行う
JPPAT = re.compile(r"[ぁ-んァ-ヴー]")

# --- 言語判定 (2026-09-08 追加・hit_system §5a) ---
# 旧実装は「かなが無い = EN」だったので、韓国語・中国語・スペイン語が EN 欄に混ざり、
# 「EN先行・JP空白」の窓シグナルを汚していた。ja / en / other の3値に分ける。
HANGUL = re.compile(r"[가-힣]")
CJK = re.compile(r"[一-鿿]")
# ラテン文字圏のうち EN 以外を弾く軽い手掛かり（完全ではない。docs 側に限界を明記）
ROMANCE = re.compile(
    r"[áéíóúñõçâêô¿¡]"
    r"|(?<![A-Za-z])(de|la|el|los|las|para|con|que|una|como|voce|nao)(?![A-Za-z])", re.I)


def lang_of(title, channel=None):
    """'ja' / 'en' / 'other' を返す。other = 韓国語・中国語・スペイン語等。"""
    t = (title or "") + " " + (channel or "")
    if JPPAT.search(t):
        return "ja"
    if HANGUL.search(t):
        return "other"
    if CJK.search(t):
        return "other"      # かな無しの漢字のみ = 中国語
    if ROMANCE.search(t):
        return "other"
    return "en"


# --- 型（棚の中の切り口）分類 (2026-09-08 追加・hit_system §5a) ---
# THEMES はツール名の正規表現なので、1テーマに需要も参入条件も違う棚が同居する。
# 当面は「その波が何クラスタでできているか」を表示するためだけに使う（集計キーは変えない）。
KATA = [
    ("速報", r"速報|値上げ|制限|リセット|突破|発表|登場|新機能|アップデート|リリース"),
    ("決着", r"どっち|どちら|結局|比較|(?<![A-Za-z])vs(?![A-Za-z])|違い|選ぶ|買うべき|おすすめ|最強|要る|いらない|必要"),
    ("入門", r"入門|使い方|完全ガイド|初心者|全体像|とは|始め方|解説|まとめ"),
    ("実演", r"やってみた|試して|自動化|作って|実演|構築|導入して"),
]
KATA_RE = [(name, re.compile(pat, re.I)) for name, pat in KATA]


def kata_of(title):
    """タイトルから切り口の型を読む。当てはまらなければ 'その他'。"""
    t = (title or "").lower()
    for name, pat in KATA_RE:
        if pat.search(t):
            return name
    return "その他"


def kata_breakdown(recs):
    """[{title:...}] -> '速報3/入門7/決着4' の内訳文字列とクラスタ数を返す。"""
    c = defaultdict(int)
    for r in recs:
        c[kata_of(r.get("title"))] += 1
    if not c:
        return "—", 0
    items = sorted(c.items(), key=lambda x: -x[1])
    return "/".join("%s%d" % (k, v) for k, v in items), len(items)




def theme_of(title):
    t = (title or "").lower()
    if NEG.search(t):
        t = NEG.sub(" ", t)
    hits = [name for name, pat in THEMES.items() if re.search(pat, t, re.I)]
    # より具体的なテーマを優先（RAG は ローカルLLM より具体）
    for pref in ("ローカルLLM×RAG", "Codex", "Cursor", "MCP", "Claude Code"):
        if pref in hits:
            return pref
    return hits[0] if hits else None


def load_external(days, today):
    """外部HITコーパスから、テーマ別に「直近の波」を集計する。"""
    path = os.path.join(ROOT, "time_series", "external_hits.jsonl")
    best = {}
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            vid = r.get("video_id")
            if not vid:
                continue
            if vid not in best or r["first_seen"] < best[vid]["first_seen"]:
                best[vid] = r
    agg = defaultdict(lambda: {"recent": [], "all": [], "chs": set()})
    for r in best.values():
        if OFFAXIS.search(r["title"] or ""):
            continue
        th = theme_of(r["title"])
        if not th:
            continue
        try:
            pub = dt.date.fromisoformat(r["published_at"][:10])
        except Exception:
            continue
        age = (today - pub).days
        subs = r.get("subs") or 0
        mult = (r["views"] / subs) if subs else None
        rec = {"title": r["title"], "views": r["views"], "subs": subs, "age": age,
               "url": r.get("url"), "ch": r.get("channel"), "mult": mult,
               "lang": lang_of(r["title"], r.get("channel")),
               "jp": lang_of(r["title"], r.get("channel")) == "ja",
               "first_seen": r.get("first_seen")}
        agg[th]["all"].append(rec)
        agg[th]["chs"].add(r.get("channel_id") or r.get("channel"))
        if 0 <= age <= days:
            agg[th]["recent"].append(rec)
    return agg


def load_monitored(days, today):
    """監視chの ch相対HIT（普段の5倍・下限500）をテーマ別に集計。"""
    agg = defaultdict(list)
    for d in glob.glob(os.path.join(ROOT, "time_series", "UC*/")):
        latest = {}
        p = os.path.join(d, "videos.jsonl")
        if not os.path.exists(p):
            continue
        for line in io.open(p, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r["video_id"] not in latest or r["ts"] > latest[r["video_id"]]["ts"]:
                latest[r["video_id"]] = r
        vs = list(latest.values())
        if len(vs) < 5:
            continue
        med = statistics.median(v["views"] for v in vs)
        thr = max(med * 5, 500)
        # ch 名は snapshots.jsonl の最終行から（today.py の真似元表示用。無ければ ID のまま）
        ch_name = os.path.basename(os.path.normpath(d))
        ch_subs = 0
        ps = os.path.join(d, "snapshots.jsonl")
        if os.path.exists(ps):
            for line in io.open(ps, encoding="utf-8"):
                try:
                    snap = json.loads(line)
                    ch_name = snap.get("channel_title") or ch_name
                    ch_subs = snap.get("subscriber_count") or ch_subs
                except Exception:
                    pass
        for v in vs:
            if v["views"] < thr:
                continue
            try:
                age = (today - dt.date.fromisoformat(v["published_at"][:10])).days
            except Exception:
                continue
            if age > days:
                continue
            th = theme_of(v["title"])
            if not th:
                continue
            agg[th].append({"title": v["title"], "views": v["views"], "age": age, "jp": True,
                            "lang": "ja",
                            "subs": ch_subs, "url": f"https://www.youtube.com/watch?v={v['video_id']}",
                            "video_id": v["video_id"],
                            "mult": v["views"] / med if med else None, "ch": ch_name})
    return agg


def load_shelves():
    """shelf_checks.json / vein_map.json から検索狙いの棚を読む。"""
    shelves = {}
    p = os.path.join(ROOT, "shelf_checks.json")
    if os.path.exists(p):
        led = json.load(io.open(p, encoding="utf-8"))
        for q, rec in led.items():
            snaps = rec.get("snapshots") or []
            velocity = None
            if len(snaps) >= 2:
                a, b = snaps[-2], snaps[-1]
                days = (dt.datetime.fromisoformat(b["ts"].replace("Z", "")) -
                        dt.datetime.fromisoformat(a["ts"].replace("Z", ""))).total_seconds() / 86400
                pm = {x["id"]: x for x in a["items"]}
                sp = [(x["views"] - pm[x["id"]]["views"]) / days for x in b["items"] if x["id"] in pm]
                if sp and days >= 1:
                    velocity = statistics.median(sp)
            items = snaps[-1]["items"] if snaps else []
            shelves[q] = {"term": rec.get("term"), "seed": rec.get("seed"),
                          "velocity": velocity, "snapshots": len(snaps),
                          "min_views": min((i["views"] for i in items), default=None),
                          "med_views": (statistics.median([i["views"] for i in items]) if items else None)}
    p = os.path.join(ROOT, "vein_map.json")
    if os.path.exists(p):
        vm = json.load(io.open(p, encoding="utf-8"))
        for e in vm.get("entries", []):
            q = e["query"]
            s = shelves.setdefault(q, {"term": q, "velocity": None, "snapshots": 0,
                                       "min_views": None, "med_views": None})
            s["verdict"] = e.get("verdict")
            s["status"] = e.get("status")
            sup = e.get("supply") or []
            if sup:
                s["min_views"] = min(x.get("views", 0) for x in sup)
    return shelves


def own_themes(today):
    """自chが既に出したテーマ（直近180日）= H-15 の重複回避用。"""
    p = os.path.join(ROOT, "own_channel", "videos.jsonl")
    used = defaultdict(list)
    if not os.path.exists(p):
        return used
    latest = {}
    for line in io.open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r["video_id"] not in latest or r["ts"] > latest[r["video_id"]]["ts"]:
            latest[r["video_id"]] = r
    for r in latest.values():
        try:
            age = (today - dt.date.fromisoformat(r["published_at"][:10])).days
        except Exception:
            continue
        if age > 180:
            continue
        th = theme_of(r["title"])
        if th:
            used[th].append({"title": r["title"], "views": r["views"], "age": age})
    return used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45, help="波とみなす窓（日）")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD（テスト用）")
    args = ap.parse_args()
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()

    ext = load_external(args.days, today)
    mon = load_monitored(args.days, today)
    shelves = load_shelves()
    used = own_themes(today)

    # ---- トラック1: 当たる動画（波が立っている）----
    t1 = []
    for th in set(list(ext) + list(mon)):
        recent = ext.get(th, {}).get("recent", []) + mon.get(th, [])
        if not recent:
            continue
        jp = [r for r in recent if r.get("lang", "ja") == "ja"]
        # EN は「英語圏先行」の窓シグナル(H-13)なので、韓国語・中国語・スペイン語(other)は数えない
        en = [r for r in recent if r.get("lang") == "en"]
        small = [r for r in recent if (r.get("subs") or 0) and r["subs"] <= 5000]
        newest_jp = min((r["age"] for r in jp), default=None)
        newest_en = min((r["age"] for r in en), default=None)
        chs = len({r.get("ch") for r in jp})
        pool = jp or recent
        best = max(pool, key=lambda r: r.get("mult") or 0)
        # JP の鮮度が主。JP空白×EN直近 は H-13 の参入窓としてボーナス
        if newest_jp is not None:
            score = (args.days - newest_jp) / args.days * 100 + chs * 8 + len(small) * 4
            window = ""
        else:
            score = 25 + (args.days - (newest_en or args.days)) / args.days * 25
            window = "🪟 EN先行・JP空白"
        if th in used:
            score *= 0.45
        # hit_system §5a: 1テーマに別の棚が同居していないかを可視化する（集計キーは変えない）
        hot = [r for r in jp if r["age"] <= 14] or jp
        kb, kn = kata_breakdown(hot)
        t1.append({"theme": th, "score": score, "newest": newest_jp, "newest_en": newest_en,
                   "chs": chs, "n": len(recent), "small": len(small), "best": best,
                   "window": window, "used": used.get(th), "kata": kb, "kata_n": kn})
    t1.sort(key=lambda x: -x["score"])

    # ---- トラック2: 検索狙い（棚が流れている）----
    t2 = []
    for q, s in shelves.items():
        v = s.get("velocity")
        score = 0
        if v is not None:
            score += v * 4
        if s.get("verdict") == "opportunity":
            score += 30
        elif s.get("verdict") == "lean":
            score += 15
        if s.get("min_views") is not None and s["min_views"] < 1500:
            score += 20          # 小規模が上位に食い込めている棚
        if s.get("med_views") and s["med_views"] < 10000:
            score += 10          # 大手の壁でない
        if s.get("status") and "候補化" in str(s.get("status")):
            score += 10
        t2.append({"query": q, "score": score, **s})
    t2.sort(key=lambda x: -x["score"])

    # ---- 出力 ----
    L = []
    L.append("# 企画 優先度ボード（自動生成）")
    L.append("")
    L.append(f"> **生成 {today.isoformat()}** — `python pipeline_board.py` で再生成（追加 quota 0）。**手で編集しない**。")
    L.append("> 優先の原則: **① まず「当たる動画」（今まさに波が立っているテーマ）→ ② 無ければ「検索狙い動画」（棚が流れているテーマ）**")
    L.append("> 方針 [methodology §1.4a](methodology.md) / ゲート [shelf_check_methodology](shelf_check_methodology.md) / 収録単位 [video_queue_2026-08](video_queue_2026-08.md)")
    L.append(f"> 波の窓 = 直近 **{args.days}日** / 自ch が直近180日に出したテーマは score×0.45（H-15 同内容反復の減衰）")
    L.append("")
    L.append("## ① 当たる動画（波あり = 急伸型 📣）")
    L.append("")
    L.append("判定は **day7 ≒ 700**（H-33）。タイトルは **決着型 × 視聴者の持ち物**（H-34）。「入門/使い方」は使わない（H-10）。")
    L.append("")
    L.append("| # | テーマ | JP最新HIT | EN最新 | JP横断ch | 小規模 | 型内訳 | 直近の代表HIT | 自ch既出 |")
    L.append("|---:|---|---:|---:|---:|---:|---|---|---|")
    for i, r in enumerate(t1[:args.top], 1):
        b = r["best"]
        mult = f"×{b['mult']:.0f}" if b.get("mult") else ""
        link = f"[{b['title'][:32]}]({b['url']})" if b.get("url") else b["title"][:32]
        u = f"⚠️{len(r['used'])}本" if r["used"] else "—"
        njp = f"{r['newest']}日前" if r["newest"] is not None else f"**なし** {r['window']}"
        nen = f"{r['newest_en']}日前" if r.get("newest_en") is not None else "—"
        kb = r["kata"] + (" ⚠️" if r["kata_n"] >= 3 else "")
        L.append(f"| {i} | **{r['theme']}** | {njp} | {nen} | {r['chs']} | {r['small']} | {kb} | {link} {b['views']:,}v {mult} | {u} |")
    L.append("")
    L.append("## ② 検索狙い動画（波なし・棚が流れている 🔍）")
    L.append("")
    L.append("判定は **28/56日の日速と窓比**（H-21/H-23）。**初速で当落判定しない**。タイトルは**クエリ語そのまま**（H-24）。")
    L.append("")
    L.append("| # | 棚（クエリ） | 棚の日速 | 棚の最小views | 棚の中央 | verdict | 状態 |")
    L.append("|---:|---|---:|---:|---:|---|---|")
    for i, r in enumerate(t2[:args.top], 1):
        v = f"{r['velocity']:+.1f}/日" if r.get("velocity") is not None else ("2点目待ち" if r.get("snapshots") == 1 else "—")
        mn = f"{r['min_views']:,}" if r.get("min_views") is not None else "—"
        md = f"{r['med_views']:,.0f}" if r.get("med_views") else "—"
        L.append(f"| {i} | `{r['query']}` | {v} | {mn} | {md} | {r.get('verdict') or '—'} | {r.get('status') or '—'} |")
    L.append("")
    L.append("## 使い方")
    L.append("")
    L.append("1. **①の上から順に**、まだ撮っていないテーマを取る。①が枯れたら②へ落ちる")
    L.append("2. **①を撮る前に `python feed_check.py \"<テーマ>\" --title \"<タイトル案>\"`**（波・同層・横展開・参入期限・型 = [feed_check_methodology](feed_check_methodology.md)）")
    L.append("3. **②を撮る前に `python shelf_check.py \"<クエリ>\" --seed <seed> --term <term>`**（需要・供給・棚の太さ）")
    L.append("4. 収録は**セッション単位**でまとめ、後から論点で割る（[video_queue](video_queue_2026-08.md) §1a）")
    L.append("5. 公開したら本ボードを再生成 — 自ch既出テーマは自動で優先度が下がる")
    L.append("")
    L.append("## 限界")
    L.append("")
    L.append("- テーマ判定は**タイトルの正規表現**。取りこぼし・誤爆はある（`THEMES` を育てる）")
    L.append("- **`THEMES` はツール名の軸しか持たない**（hit_system §5a）。1テーマに需要も参入条件も違う棚が"
             "同居しうるので、**型内訳**列でクラスタ構成を出している。⚠️ は3クラスタ以上＝「1つの波」では"
             "ないサイン。型は**タイトル語の代理判定**なので、撮る前に `feed_check.py \"<テーマ>\"` で"
             "実タイトルを目視すること")
    L.append("- 言語判定は ja / en / other の3値。**other（韓国語・中国語・スペイン語等）は JP にも EN にも"
             "数えない**。ラテン文字圏の EN 以外は機能語ベースの軽い判定なので取りこぼしがある")
    L.append("- 外部HITコーパスは **HIT側に偏った母集団**（flopの分母が無い）。ここから打率は出せない（supply_gap §2 落とし穴A）")
    L.append("- 「最新HIT が n日前」は**コーパスに載った範囲**の話。載っていない波は見えない")
    L.append("- ②の日速は**スナップショット2点目**が必要。1点目だけの棚は「2点目待ち」表示")

    with io.open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {OUT}  (①{len(t1)}件 / ②{len(t2)}件)")


if __name__ == "__main__":
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
