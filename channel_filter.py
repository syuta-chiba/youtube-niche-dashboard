"""channel_filter.py — 発見済みチャンネルに「IT関連か」のラベルを付ける(決定論＋AIの2段)。

なぜ要るか:
  discovery の関連度ゲートは 2026-07-21 に廃止され(確認制=機械は関連度を弾かず人間が決める)、
  数値ゲート(subs帯・KWヒット・相対HIT・like率)は通るが**内容が的外れなch**
  (懐メロ/一般ニュース/占い/料理/非日本語など)が毎日のプール流入(~18ch/日)に混ざって溜まる。
  corpus_filter.py は「動画」の軸in/outを見るが、「チャンネル」の関連度は誰も見ていなかった。
  そこを埋める。方針(ユーザー 2026-10-20): **広めにIN。ITに全く関係ないものだけ OUT。**

2段構えの理由は corpus_filter.py と同じ:
  決定論で白/黒を確定 → グレーだけ AI(claude -p haiku) に回す → 結果をキャッシュに書き戻し二度見しない。

ラベルは time_series/channel_labels.json に貯める:
  { "<channel_id>": {"rel": "in"|"out"|"unknown", "cat": "...", "reason": "...", "ts": "..."} }

判定の素材(追加 quota 0):
  - チャンネル名        … rss_pool.json / discovery_seen.json
  - サンプル動画タイトル … external_hits.jsonl をchでグループ化(あれば)

Usage:
    python channel_filter.py                 # 決定論のみ。グレーは unknown のまま
    python channel_filter.py --llm           # グレーを claude -p (haiku) で判定
    python channel_filter.py --llm --limit 80
    python channel_filter.py --stats         # ラベル分布を見るだけ
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(ROOT, "rss_pool.json")
SEEN = os.path.join(ROOT, "discovery_seen.json")
CORPUS = os.path.join(ROOT, "time_series", "external_hits.jsonl")
LABELS = os.path.join(ROOT, "time_series", "channel_labels.json")
SAMPLES_PER_CH = 6

# ---------------------------------------------------------------- 決定論フィルタ

# IT関連と断定してよい語(広め = 開発に限らず IT 全般)。1つでも当たれば in。
IN_PAT = re.compile(
    r"ローカルllm|ローカルai|local llm|ollama|lm ?studio|llama\.cpp|gguf|量子化|vram|"
    r"claude ?code|claude|codex|cursor|copilot|chatgpt|gemini|grok|\bmcp\b|agents?\.md|"
    r"anythingllm|open ?webui|\brag\b|langchain|dify|\bn8n\b|notebooklm|"
    r"ai ?エージェント|ai ?agent|生成ai|\bai\b|機械学習|\bml\b|ディープ|深層学習|"
    r"プログラ|program|coding|コーディング|エンジニア|engineer|開発|developer|devops|"
    r"python|javascript|typescript|golang|\brust\b|java\b|\bc\+\+|linux|ubuntu|docker|"
    r"kubernetes|サーバ|server|クラウド|cloud|\baws\b|azure|gcp|データベース|database|\bsql\b|"
    r"\bapi\b|\bgit\b|github|vscode|vim|\bide\b|ターミナル|cli|ガジェット|gadget|"
    r"自作pc|\bgpu\b|\bcpu\b|パソコン|スマホ|iphone|android|アプリ開発|app ?dev|"
    r"セキュリティ|security|ハッキング|ネットワーク|network|(?<![a-z])it(?![a-z])|テック|tech|dx|自動化|"
    r"ノーコード|no[- ]?code|スプレッドシート|excel ?関数|gas\b|スクリプト", re.I)

# IT と無関係と断定してよい語(明確に非IT なものだけ・広めIN方針なので控えめに)。
OUT_PAT = re.compile(
    r"official (video|audio|music)|\blyrics?\b|\bmv\b|feat\.|remix|cover|歌ってみた|"
    r"カラオケ|弾いてみた|ライブ映像|懐メロ|昭和歌謡|名曲|童謡|こどものうた|アンパンマン|"
    r"ゲーム実況|フォートナイト|原神|マイクラ|\bapex\b|荒野行動|プロスピ|"
    r"料理|レシピ|グルメ|食べ|旅行|観光|釣り|キャンプ|競馬|競艇|パチンコ|スロット|"
    r"野球|サッカー|相撲|ゴルフ|格闘技|プロレス|"
    r"占い|スピリチュアル|霊視|タロット|開運|風水|恋愛|復縁|モテ|"
    r"メイク|コスメ|美容|ダイエット|筋トレ| asmr|心霊|都市伝説|"
    r"vlog|モーニングルーティン|購入品|ルームツアー|"
    r"ペット|猫|犬|ネコ|ねこ|にゃん|イヌ|いぬ|子猫|保護猫|愛猫|愛犬|柴犬|"
    r"ハムスター|うさぎ|インコ|金魚|メダカ|水槽|アクアリウム|昆虫|"
    r"赤ちゃん|育児|子育て|ゴルフ|将棋|囲碁|麻雀|競輪", re.I)


def classify_deterministic(row):
    """in / out / gray を返す。gray は AI に回す候補。ch名＋サンプルタイトルで判定。"""
    text = (row.get("channel_title") or "") + " \n " + " \n ".join(row.get("samples") or [])
    # IN を先に見る(広めIN方針): IT語が1つでもあれば非ITワードが混じっても in に倒す。
    if IN_PAT.search(text):
        return "in"
    if OUT_PAT.search(text):
        return "out"
    return "gray"


# ---------------------------------------------------------------- AI フィルタ

LLM_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "rel": {"type": "string", "enum": ["in", "out"]},
                    "cat": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "rel"],
            },
        }
    },
    "required": ["results"],
}, ensure_ascii=False)

PROMPT_HEAD = """あなたは「開発者・実務者向けの技術系 YouTube チャンネル」のリサーチ担当です。
発見した各チャンネルが、**IT・技術に少しでも関係するか**を広めに判定してください。

方針: **広く IN を取る。IT に全く関係ないものだけ OUT。**
  in  = IT・技術・ソフトウェア・AI・開発・ガジェット・PC/スマホ・プログラミング・
        自動化・ネット/クラウド・エンジニアリングに少しでも触れている
        (ビジネス寄りでも「AI活用」「業務自動化」「開発ツール」の話なら in)
  out = IT と全く関係ない。例: 音楽/歌/ゲーム実況/エンタメ/スポーツ/料理/旅行/
        美容/恋愛/占い・スピリチュアル/ペット/一般ニュース/子供向け/生活 vlog

判断材料はチャンネル名とサンプル動画タイトル。**迷ったら in**(広めに拾う)。
JSON Schema に従い JSON オブジェクトのみを返答してください。

--- 判定対象 (id: チャンネル名 — サンプルタイトル) ---
"""


def _fmt(row):
    s = " / ".join((row.get("samples") or [])[:3])
    return f"{row.get('channel_title','(名称不明)')[:40]}" + (f" — {s[:140]}" if s else "")


def classify_llm(rows, limit):
    """gray の ch を claude -p (haiku) で in/out に。失敗時 None。corpus_filter と同方針。"""
    batch = rows[:limit]
    if not batch:
        return {}
    lines = [f"{i}: {_fmt(r)}" for i, r in enumerate(batch)]
    prompt = PROMPT_HEAD + "\n".join(lines) + "\n"
    try:
        r = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "json",
             "--json-schema", LLM_SCHEMA],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=300, shell=(sys.platform == "win32"))
        envelope = None
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
        if r.returncode != 0 or envelope is None or envelope.get("is_error"):
            detail = (envelope or {}).get("result") or r.stderr or r.stdout or "unknown"
            detail = str(detail)[:200].replace("|", "/").replace("\n", " ")
            print(f"LLM_STATUS|ng|reason={detail}")
            print(f"[warn] claude -p 失敗 (exit {r.returncode}): {detail}", file=sys.stderr)
            return None
        out = envelope.get("structured_output") or {}
        got = {}
        for item in out.get("results", []):
            i = item.get("id")
            if isinstance(i, int) and 0 <= i < len(batch) and item.get("rel") in ("in", "out"):
                got[batch[i]["channel_id"]] = {"rel": item["rel"], "cat": item.get("cat", ""),
                                               "reason": item.get("reason", "")}
        print(f"LLM_STATUS|ok|judged={len(got)}/{len(batch)}")
        print(f"[info] AI判定 {len(got)}/{len(batch)} 件", file=sys.stderr)
        return got
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"LLM_STATUS|ng|reason={str(e)[:160]}")
        print(f"[warn] claude -p 経路エラー: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- データ読み込み

def _load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        return json.loads(io.open(path, encoding="utf-8").read())
    except (json.JSONDecodeError, OSError):
        return default


def load_labels():
    return _load_json(LABELS, {})


def save_labels(labels):
    os.makedirs(os.path.dirname(LABELS), exist_ok=True)
    with io.open(LABELS, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(labels, ensure_ascii=False, indent=1))


def _channel_samples():
    """external_hits.jsonl を channel_id → [動画タイトル] に畳む(最大 SAMPLES_PER_CH)。"""
    samples = defaultdict(list)
    if not os.path.exists(CORPUS):
        return samples
    for line in io.open(CORPUS, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        cid = r.get("channel_id")
        t = r.get("title")
        if cid and t and len(samples[cid]) < SAMPLES_PER_CH:
            samples[cid].append(t)
    return samples


def load(labels=None):
    """プール全ch を [{channel_id, channel_title, subs, samples, rel}] で返す。
    rel は既存ラベル(無ければ unknown)。"""
    labels = labels if labels is not None else load_labels()
    samples = _channel_samples()
    pool = _load_json(POOL, {})
    seen = _load_json(SEEN, {})
    rows = {}
    for cid, meta in pool.items():
        m = meta if isinstance(meta, dict) else {}
        rows[cid] = {"channel_id": cid, "channel_title": m.get("title") or "",
                     "subs": m.get("subs"), "samples": samples.get(cid, []),
                     "rel": (labels.get(cid) or {}).get("rel", "unknown")}
    for cid, meta in seen.items():
        if cid in rows:
            continue
        m = meta if isinstance(meta, dict) else {}
        rows[cid] = {"channel_id": cid, "channel_title": m.get("title") or m.get("channel_title") or "",
                     "subs": m.get("subs"), "samples": samples.get(cid, []),
                     "rel": (labels.get(cid) or {}).get("rel", "unknown")}
    return list(rows.values())


# ---------------------------------------------------------------- 本体

def deterministic_pass(rows, labels, relabel=False):
    """1段目(決定論)を適用。確定した in/out を labels に書き、(グレー行, 確定数)を返す。
    ai_judge.py(Routine経路)もこれを使う。確定済みラベルは触らない。"""
    gray = []
    changed = 0
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in rows:
        existing = (labels.get(r["channel_id"]) or {}).get("rel")
        if existing in ("in", "out") and not relabel:
            r["rel"] = existing
            continue
        d = classify_deterministic(r)
        if d == "gray":
            r["rel"] = existing or "unknown"
            gray.append(r)
        else:
            labels[r["channel_id"]] = {"rel": d, "cat": "決定論", "reason": "キーワード一致",
                                       "ts": ts}
            r["rel"] = d
            changed += 1
    return gray, changed


def is_offtarget(channel_id, labels=None):
    """このchが AI/決定論で out 判定済みか。ラベル無し=未判定は False(弾かない=確認制を保つ)。"""
    labels = labels if labels is not None else load_labels()
    return (labels.get(channel_id) or {}).get("rel") == "out"


OFFTARGET_REASON = "🚫 AI関連度: ITと無関係 — 確認待ちから除外 (channel_filter)"


def apply_to_seen(labels=None, dry_run=False):
    """ラベルを discovery_seen.json に遡及適用する (2026-10-24 追加)。

    discovery_loop は評価時にしか is_offtarget を見ないので、ラベルが後から付いた
    既存の「全条件クリア (queued(cap))」には効かない。ここで out 付きを queued に落とし、
    元の判定は prior_decision に残す (ラベルが in に変われば次回呼び出しで復帰する)。
    promoted (priority 入り済み) は人間の判断領域なので触らない。
    戻り値: (除外した件数, 復帰した件数)
    """
    labels = labels if labels is not None else load_labels()
    seen = _load_json(SEEN, {})
    demoted, restored = 0, 0
    for cid, e in seen.items():
        if not isinstance(e, dict):
            continue
        rel = (labels.get(cid) or {}).get("rel")
        if e.get("decision") == "queued(cap)" and rel == "out":
            e["prior_decision"] = "queued(cap)"
            e["decision"] = "queued"
            e["reason"] = OFFTARGET_REASON
            demoted += 1
        elif e.get("prior_decision") == "queued(cap)" and e.get("decision") == "queued" and rel == "in":
            e["decision"] = e.pop("prior_decision")
            if e.get("reason") == OFFTARGET_REASON:
                e.pop("reason", None)
            restored += 1
    if (demoted or restored) and not dry_run:
        io.open(SEEN, "w", encoding="utf-8").write(json.dumps(seen, ensure_ascii=False, indent=2))
    return demoted, restored


def main():
    ap = argparse.ArgumentParser(description="発見済みchのIT関連ラベリング(広めIN)")
    ap.add_argument("--llm", action="store_true", help="グレーを claude -p (haiku) で判定")
    ap.add_argument("--limit", type=int, default=80, help="1回でAIに回す上限")
    ap.add_argument("--stats", action="store_true", help="分布を見るだけ")
    ap.add_argument("--relabel", action="store_true", help="既存ラベルを無視して付け直す")
    ap.add_argument("--apply-seen", action="store_true",
                    help="out ラベルを discovery_seen.json の全条件クリアに遡及適用 (queued(cap)→queued)")
    args = ap.parse_args()

    if args.apply_seen:
        d, r = apply_to_seen()
        print(f"遡及適用: 確認待ちから除外 {d} ch / 復帰 {r} ch")
        return

    labels = load_labels()
    rows = load(labels)
    if not rows:
        print("プールが空 (rss_pool.json / discovery_seen.json が無い)"); return

    if args.stats:
        c = Counter(r["rel"] for r in rows)
        print(f"全 {len(rows)} ch")
        for k, v in c.most_common():
            print(f"  {k:9s} {v:>5}  ({v/len(rows)*100:.0f}%)")
        return

    gray, changed = deterministic_pass(rows, labels, args.relabel)
    print(f"決定論: 確定 {changed} ch / グレー {len(gray)} ch")

    if args.llm and gray:
        got = classify_llm(gray, args.limit)
        if got:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for cid, v in got.items():
                labels[cid] = {**v, "ts": ts}
            print(f"AI: {len(got)} ch を確定")
        else:
            print("AI判定はスキップ(グレーは unknown のまま)")

    save_labels(labels)
    c = Counter((labels.get(r["channel_id"]) or {}).get("rel", "unknown") for r in rows)
    print("結果: " + " / ".join(f"{k}={v}" for k, v in c.most_common()))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
