# -*- coding: utf-8 -*-
"""llm_judge.py — タイトルの「意味」を Claude に判定させる共通レイヤ

なぜ要るか:
  棚に本当に居る本かどうかは、正規表現では判定できない。
  実例 (2026-09-08): クエリ `ローカルLLM RAG` の検索結果には
    「【ローカルLLM】Gemma4をOllamaで動かしてみた」← RAG動画ではない
    「Mac mini品薄だけど、ローカルLLMは実用的じゃない」← RAG動画ではない
  が混ざる。語の有無で絞る `--term` はその代用でしかなく、
  絞り方次第で当たり率が 57.6% にも 0.0% にもなる。

  同じことが「型」にも言える。kata_of() は決着語の有無を見ているだけで、
  H-34 が本来言っている「視聴者がこれから下す選択に決着をつけるか」は
  中身の設計の話なので、語では測れない。

方針 (CLAUDE.md):
  Claude API ではなく **サブスクの `claude -p` を子プロセスで呼ぶ**。
  - 大きいプロンプトは stdin (位置引数を渡すと stdin が無視される)
  - `--json-schema` を付けると結果は envelope["structured_output"] に dict で入る
  - 軽い分類なので `--model haiku`
  - `--allowedTools ""` でツールを封じる (想定外のファイル操作を防ぐ)

キャッシュ:
  video_id 単位で time_series/llm_labels.json に永続化する。
  同じ動画を二度判定しない (棚が変われば shelf キーが変わるので判定し直す)。
"""
import hashlib
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "time_series", "llm_labels.json")

KATA_CHOICES = ["決着", "実演", "入門", "速報", "その他"]

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "on_shelf": {"type": "boolean"},
                    "kata": {"type": "string", "enum": KATA_CHOICES},
                    "native_ja": {"type": "boolean"},
                },
                "required": ["i", "on_shelf", "kata", "native_ja"],
            },
        }
    },
    "required": ["items"],
}

INSTRUCTIONS = """あなたは YouTube の企画分析アシスタントです。
与えられたタイトル群を、指定された「棚」の観点で判定してください。

# 棚
{shelf}

# 判定項目

## on_shelf — この動画はその棚の動画か
棚のテーマを**主題として**扱っているなら true。
隣接テーマに触れているだけ、あるいは別テーマの動画なら false。
例: 棚が「ローカルLLMでRAGを構築する」のとき
  「ローカルRAGをLMStudioとAnythingLLMでつくってみた」→ true
  「【ローカルLLM】Gemma4をOllamaで動かしてみた」→ false (RAGが主題でない)
  「Mac mini品薄だけど、ローカルLLMは実用的じゃない」→ false

## kata — 切り口の型（語ではなく中身で判断する）
- 決着: **視聴者がこれから下す選択に決着をつける**。どれを買うか/どっちを使うか/
        いくらかかるか/要るか要らないか。主語は視聴者の持ち物や予算
- 実演: 用途の実演・作ってみた・自動化してみた。作者が何かをやって見せる
- 入門: 機能や概念の解説・使い方・入門・完全ガイド
- 速報: 新機能/リリース/値上げ/制限変更など、出来事を伝えるもの
- その他: 上のどれでもない

語の見た目に釣られないこと。「〜の使い方」でも中身が
「どっちを選ぶか」なら 決着。「比較」と付いていても単に並べただけなら 入門。

## native_ja — 元から日本語で作られた動画か
日本語話者向けに作られたものは true。
英語などの動画にYouTubeの自動翻訳タイトルが付いているだけなら false。
不自然な機械翻訳調、原語の語順が残っている、海外プロダクト名の直訳などが手掛かり。

# 出力
入力の各行の番号 i をそのまま返し、全行について判定すること。
JSON Schema に従い、JSONオブジェクトのみを返答すること。

# タイトル一覧
{titles}
"""


def _load_cache():
    if os.path.exists(CACHE):
        try:
            with io.open(CACHE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(data):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with io.open(CACHE, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1))


def _shelf_key(shelf):
    return hashlib.sha1(shelf.encode("utf-8")).hexdigest()[:12]


def _call(shelf, batch, model, timeout):
    """1バッチを claude -p に投げる。batch = [(i, title), ...]"""
    titles = "\n".join("%d. %s" % (i, t) for i, t in batch)
    prompt = INSTRUCTIONS.format(shelf=shelf, titles=titles)
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA, ensure_ascii=False),
           "--allowedTools", ""]
    try:
        # 位置引数の prompt は渡さない (渡すと stdin が無視される)
        out = subprocess.run(cmd, input=prompt, capture_output=True,
                             text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        print("[warn] claude -p タイムアウト", file=sys.stderr)
        return {}
    if out.returncode != 0:
        print("[warn] claude -p 失敗: %s" % (out.stderr or "")[:200], file=sys.stderr)
        return {}
    try:
        env = json.loads(out.stdout)
        items = (env.get("structured_output") or {}).get("items") or []
    except Exception as e:
        print("[warn] 応答の解析に失敗: %s" % e, file=sys.stderr)
        return {}
    return {it["i"]: it for it in items if isinstance(it.get("i"), int)}


def judge(shelf, rows, model="haiku", batch_size=40, timeout=300, use_cache=True):
    """rows = [{video_id, title}, ...] に on_shelf / kata / native_ja を付ける。

    shelf = 棚の説明 (自然文)。判定はこの説明に対して行われるので、
    棚が変われば判定も変わる (キャッシュキーに含める)。
    """
    cache = _load_cache() if use_cache else {}
    key = _shelf_key(shelf)
    slot = cache.setdefault(key, {"shelf": shelf, "labels": {}})
    labels = slot["labels"]

    todo = [r for r in rows if r.get("video_id") not in labels]
    if todo:
        print("  claude -p で %d本を判定中 (%d本はキャッシュ済)..."
              % (len(todo), len(rows) - len(todo)), file=sys.stderr)
    for s in range(0, len(todo), batch_size):
        chunk = todo[s:s + batch_size]
        got = _call(shelf, [(i, r["title"]) for i, r in enumerate(chunk, 1)], model, timeout)
        if not got:
            print("[warn] バッチ %d-%d は判定できず（欠測扱い）"
                  % (s + 1, s + len(chunk)), file=sys.stderr)
            continue
        for i, r in enumerate(chunk, 1):
            it = got.get(i)
            if it:
                labels[r["video_id"]] = {"on_shelf": bool(it["on_shelf"]),
                                         "kata": it["kata"], "native_ja": bool(it["native_ja"])}
        # バッチごとに保存する。途中で落ちても判定済みを捨てないため
        # (2026-09-08: ループ後に一括保存していて、タイムアウトで240本ぶんを失った)
        if use_cache:
            _save_cache(cache)
        sys.stderr.write("\r  判定 %d/%d本" % (min(s + batch_size, len(todo)), len(todo)))
        sys.stderr.flush()
    if todo:
        sys.stderr.write("\n")

    for r in rows:
        lab = labels.get(r.get("video_id"))
        r["llm_on_shelf"] = lab["on_shelf"] if lab else None
        r["llm_kata"] = lab["kata"] if lab else None
        r["llm_native_ja"] = lab["native_ja"] if lab else None
    return rows
