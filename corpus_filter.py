"""外部HITコーパスに「軸内かどうか」のラベルを付ける — 決定論フィルタ ＋ AIフィルタの2段。

正本の背景: docs/repo_overview.md 4章。
2026-08-11 に実測したところ、`external_hits.jsonl` 3,727本のうち技術軸に該当するのは
438本 (12%)。残りは `score = views/subs` で拾う旧基準 (H-90 で廃止したはずの指標が
取り込み条件だけ生き残っていた) が、登録者数が実態と合わないミラー系chを大量に通していた
(例: subs 1,560 の ch に 6億再生 = score 40万)。

**2段構えにする理由**
  - 決定論だけ … 速くて再現するが、キーワード辞書に無い新ツール名を取りこぼす
  - AIだけ     … 賢いが、毎回コストがかかり、判定がぶれて差分ノイズになる
  → **決定論で 白/黒 を確定させ、グレーだけ AI に回す。** AI の判定結果も
    コーパスに書き戻すので、同じ動画を二度 AI に見せない。

判定結果は各行の `axis` フィールドに入る:
  in      … 技術軸（この ch の企画に使える）
  out     … 軸外（音楽・ゲーム・雑多）
  unknown … 未判定（--llm を付けずに走らせたグレー）

Usage:
    python corpus_filter.py                 # 決定論のみ。グレーは unknown のまま
    python corpus_filter.py --llm           # グレーを claude -p (haiku) で判定
    python corpus_filter.py --llm --limit 60   # AI に回す上限（既定 80）
    python corpus_filter.py --stats         # ラベルの分布を見るだけ（書き換えない）
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(ROOT, "time_series", "external_hits.jsonl")

# ---------------------------------------------------------------- 決定論フィルタ

# 軸内と断定してよい語（この ch のターゲット = 開発者・実務者）
IN_PAT = re.compile(
    r"ローカルllm|ローカルai|local llm|ollama|lm ?studio|llama\.cpp|gguf|量子化|vram|"
    r"claude ?code|claude|codex|cursor|copilot|\bmcp\b|agents?\.md|agent skills|"
    r"gemini ?cli|antigravity|anythingllm|open ?webui|\brag\b|検索拡張|ナレッジベース|"
    r"ファインチューニング|fine[- ]?tun|lora|langchain|dify|\bn8n\b|"
    r"ai ?エージェント|ai ?agent|vscode|obsidian|notebooklm|プロンプト|生成ai|"
    r"gpt|llm|api|python|typescript|自動化|開発", re.I)

# 軸外と断定してよい語（音楽・ゲーム・エンタメ・スポーツ等）
OUT_PAT = re.compile(
    r"official (video|audio|music)|\blyrics?\b|\bmv\b|feat\.|remix|cover|"
    r"ゲーム実況|ゲーミング|フォートナイト|原神|マイクラ|\bapex\b|荒野行動|"
    r"歌ってみた|カラオケ|弾いてみた|ライブ映像|\bvlog\b|"
    r"料理|レシピ|旅行|釣り|競馬|パチンコ|野球|サッカー|相撲|"
    r"アンパンマン|こどものうた|童謡", re.I)

# score = views/subs が異常に高い = 登録者数が実態と合わない (ミラー/転載系)。
# H-90 で score は主指標から廃止済み。ここは「明らかな偽物」を弾く上限としてだけ使う。
SCORE_CEILING = 500
# 絶対再生数が桁違い = そもそも参照する層が違う
VIEWS_CEILING = 3_000_000


def classify_deterministic(r):
    """in / out / gray を返す。gray は AI に回す候補。"""
    title = r.get("title") or ""
    if (r.get("score") or 0) > SCORE_CEILING:
        return "out"                      # 偽HIT（ミラー系）
    if (r.get("views") or 0) > VIEWS_CEILING:
        return "out"                      # 層が違う
    if OUT_PAT.search(title):
        return "out"
    if IN_PAT.search(title):
        return "in"
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
                    "axis": {"type": "string", "enum": ["in", "out"]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "axis"],
            },
        }
    },
    "required": ["results"],
}, ensure_ascii=False)

PROMPT_HEAD = """あなたは YouTube チャンネル「開発者・実務者向けの技術系」の企画リサーチ担当です。
以下の動画タイトルが、そのチャンネルの企画リサーチに**使える動画か**を判定してください。

「使える (in)」= 開発者・実務者が見る技術コンテンツ。
  例: ローカルLLM / Claude Code / Codex / Cursor / MCP / RAG / AIエージェント /
      プログラミング / 開発ツール / AI活用の実務 / PCスペックとAI推論 / 生成AIの実演

「使えない (out)」= それ以外。
  例: 音楽・ゲーム実況・エンタメ・スポーツ・料理・旅行・ニュース一般・
      技術と無関係なビジネス論・スピリチュアル・子供向け

迷ったら out にしてください（コーパスを汚さない方を優先）。
JSON Schema に従い、JSON オブジェクトのみを返答してください。

--- 判定対象 ---
"""


def classify_llm(rows, limit):
    """gray の行を claude -p (haiku) で in/out に振る。失敗時は None。

    方針 (~/.claude/CLAUDE.md): anthropic SDK でなく Claude Code CLI の print mode。
    大きいプロンプトは stdin 経由（位置引数の prompt を渡すと stdin が無視されるので省く）。
    """
    batch = rows[:limit]
    if not batch:
        return {}
    lines = [f"{i}: {r.get('title', '')[:90]}" for i, r in enumerate(batch)]
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
            # auth 失敗 (401) は --output-format json だと stdout の envelope.result に入る。
            # stderr だけ見ると空になり 理由 が欠落する (2026-08-12 実測)。
            detail = (envelope or {}).get("result") or r.stderr or r.stdout or "unknown (stdout/stderr とも空)"
            detail = str(detail)[:200].replace("|", "/").replace("\n", " ")
            print(f"LLM_STATUS|ng|reason={detail}")
            print(f"[warn] claude -p 失敗 (exit {r.returncode}): {detail}", file=sys.stderr)
            return None
        out = envelope.get("structured_output") or {}
        got = {}
        for item in out.get("results", []):
            i = item.get("id")
            if isinstance(i, int) and 0 <= i < len(batch) and item.get("axis") in ("in", "out"):
                got[batch[i]["video_id"]] = item["axis"]
        print(f"LLM_STATUS|ok|judged={len(got)}/{len(batch)}")
        print(f"[info] AI判定 {len(got)}/{len(batch)} 件", file=sys.stderr)
        return got
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"LLM_STATUS|ng|reason={str(e)[:160]}")
        print(f"[warn] claude -p 経路エラー: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- 本体

def deterministic_pass(rows, relabel=False):
    """1段目 (決定論) を rows に in-place で適用し、(グレー行, 確定させた件数) を返す。
    ai_judge.py (Routine 経路) もこれを使う。"""
    gray = []
    changed = 0
    for r in rows:
        if r.get("axis") in ("in", "out") and not relabel:
            continue                      # 確定済みは触らない（AIの結果もここで保護される）
        d = classify_deterministic(r)
        if d == "gray":
            r["axis"] = r.get("axis") or "unknown"
            gray.append(r)
        else:
            if r.get("axis") != d:
                changed += 1
            r["axis"] = d
    return gray, changed


def load():
    if not os.path.exists(CORPUS):
        return []
    out = []
    for line in io.open(CORPUS, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def save(rows):
    with io.open(CORPUS, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description="外部HITコーパスの軸内/軸外ラベリング")
    ap.add_argument("--llm", action="store_true",
                    help="決定論でグレーだった行を claude -p (haiku) で判定する")
    ap.add_argument("--limit", type=int, default=80, help="1回で AI に回す上限")
    ap.add_argument("--stats", action="store_true", help="分布を見るだけ（書き換えない）")
    ap.add_argument("--relabel", action="store_true",
                    help="既存の axis を無視して決定論から付け直す")
    args = ap.parse_args()

    rows = load()
    if not rows:
        print("コーパスが空"); return

    if args.stats:
        c = Counter(r.get("axis", "unlabeled") for r in rows)
        print(f"全 {len(rows)} 本")
        for k, v in c.most_common():
            print(f"  {k:9s} {v:>5}  ({v/len(rows)*100:.0f}%)")
        return

    # 1段目: 決定論
    gray, changed = deterministic_pass(rows, args.relabel)
    print(f"決定論: 確定 {changed} 件 / グレー {len(gray)} 件")

    # 2段目: AI
    if args.llm and gray:
        got = classify_llm(gray, args.limit)
        if got:
            for r in rows:
                if r["video_id"] in got:
                    r["axis"] = got[r["video_id"]]
            print(f"AI: {len(got)} 件を確定")
        else:
            print("AI 判定はスキップ（グレーは unknown のまま）")

    save(rows)
    c = Counter(r.get("axis", "unlabeled") for r in rows)
    print("結果: " + " / ".join(f"{k}={v}" for k, v in c.most_common()))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
