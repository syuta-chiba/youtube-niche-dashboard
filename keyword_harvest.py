"""外部HITコーパスから試験語を自動収穫し、在庫 (discovery_keywords_trial.txt) に補充する。

自己増殖ループの還流部 (2026-07-24 / hypotheses H-14):
  コーパスの HIT タイトル → 新語抽出 → 在庫 → quota_drain が消化 → コーパスが育つ → …

抽出は LLM なしの決定的ルール:
  - 材料は corpus_filter.py が技術軸 (axis=in) と判定した動画だけ
  - 直近 HARVEST_DAYS 日に first_seen したコーパス動画のタイトルから
    英語固有名詞 (TitleCase の 1〜2 語) とカタカナ語を候補化
  - **2 チャンネル以上の HIT タイトルに現れる語だけ** 採用 (= 軸① クロスチャンネル需要の兆候。
    1ch だけの語はそのチャンネルの口癖かもしれない)
  - core / trial と単語単位で重なる語、tried と完全一致する語は捨てる (再検索の無駄防止)
  - 1 回の実行で最大 MAX_ADD 語。--llm が使えれば claude -p が選別・整形する。
    使えない時は「ツール名らしい形」(数字入り / CamelCase / 大文字略語) の語だけを入れる。
    再現率寄りにしていた旧ルールは、AI経路が死んだ71日間に約700語の音楽・一般語で
    quota を使い切った (docs/keyword_audit_2026-10-04.md)。人間はいつでも trial から行を消してよい

GitHub Actions (quota-drain.yml) が quota_drain.py の直前に呼ぶ。ローカル確認:
  python keyword_harvest.py --dry-run
"""

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CORPUS = Path("time_series") / "external_hits.jsonl"
CORE_FILE = "discovery_keywords_tech.txt"
TRIAL_FILE = "discovery_keywords_trial.txt"
TRIED_FILE = "discovery_keywords_tried.txt"

HARVEST_DAYS = 14   # この日数以内に first_seen した HIT だけを見る (鮮度)
MIN_CHANNELS = 2    # 採用に必要な出現チャンネル数 (軸①の最小形)
MAX_ADD = 10        # 1 回に補充する上限 (queue レビュー洪水の防止)

# 一般語・既知の広すぎる語 (小文字比較)。ここに載る語は単独では検索語にしない。
STOP = {
    # 英語一般
    "the", "and", "for", "with", "you", "your", "this", "that", "how", "what",
    "why", "not", "all", "new", "best", "free", "full", "top", "use", "using",
    "guide", "tutorial", "review", "complete", "explained", "install", "setup",
    "build", "run", "running", "create", "make", "made", "test", "tested",
    "vs", "de", "en", "el", "la", "un", "para", "como", "con",
    # 技術一般 (単独では広すぎる)
    "ai", "llm", "llms", "gpt", "api", "ide", "os", "pc", "cpu", "gpu", "ram",
    "vram", "app", "web", "code", "coding", "agent", "agents", "model", "models",
    "local", "cloud", "server", "windows", "mac", "linux", "docker", "python",
    "javascript", "github", "youtube", "shorts", "google", "openai", "anthropic",
    "claude", "gemini", "chatgpt", "microsoft", "nvidia", "amd", "intel",
    # 日本語一般 (カタカナ)
    "ローカル", "クラウド", "エージェント", "ツール", "モデル", "サーバー", "パソコン",
    "チャンネル", "ニュース", "アップデート", "リリース", "バージョン", "インストール",
    "セットアップ", "チュートリアル", "レビュー", "ランキング", "プログラミング",
    "エンジニア", "ソフトウェア", "ハードウェア", "オープンソース", "コマンド",
    "プロンプト", "コンテキスト", "ワークフロー", "フレームワーク", "ライブラリ",
    "ブラウザ", "デスクトップ", "スマホ", "アプリ", "ゲーム", "メモリ", "ドル", "コスト",
}

# 単独語 (unigram) の時だけ弾く追加リスト。複合語の一部としては生きる:
# 例) "Skills" 単独は広すぎるが「Claude Skills」は候補になり得る。
UNI_STOP = {
    "pro", "max", "mini", "plus", "ultra", "one", "two", "three", "part",
    "demo", "live", "update", "updates", "news", "launch", "official", "skills",
    "step", "step-by-step", "steps", "course", "crash", "minutes", "ultimate",
    "easy", "better", "faster", "insane", "wild", "crazy", "secret", "hidden",
    "truth", "real", "finally", "stop", "start", "inside", "future", "money",
    "business", "tips", "tricks", "hack", "hacks", "every", "everything",
    "anything", "first", "last", "next", "now", "today", "apple", "ubuntu",
    "iphone", "android", "chrome", "edge", "safari", "get", "getting", "why",
    "just", "still", "here", "there", "watch", "video", "videos", "will",
    "which", "source", "open", "mode", "opus", "macbook", "excel", "word",
    "office", "gmail", "notion", "slack", "discord", "zoom", "figma",
    "data", "intelligence", "saas", "cost", "price", "pricing", "speed",
    "テクニック", "ポイント", "メリット", "デメリット", "パターン", "シリーズ",
    "スペック", "レベル", "システム", "データ", "ファイル", "フォルダ",
}
# "Open Source" のような STOP×UNI_STOP の複合も広すぎるので、bigram は
# 両方が (STOP ∪ UNI_STOP) に入る場合も弾く (extract_terms 内で判定)。

# 略語だが単独では広すぎるもの (tech_shaped の「大文字略語」判定から外す)
BROAD_ACRO = {"agi", "mcp", "cli", "rag", "llm", "api", "gpu", "cpu", "ssd", "usb",
              "pdf", "gui", "aws", "ceo", "sns", "fps", "lan", "ost", "bgm", "amv",
              "dvd", "mv", "jpn", "usa", "diy", "faq", "pov", "asmr", "live", "the"}


def tech_shaped(tok: str) -> bool:
    """ツール名・モデル名らしい形のトークンか (決定論で言える範囲だけ)。

    数字を含む (GPT-6 / Qwen3.8-27B) / 2文字目以降に大文字が混ざる (OmniRoute / vLLM / NotebookLM)。
    普通の英単語 (Film / Sunshine) はどちらにも当たらない。全部大文字の語 (TWICE / BTS) は
    音楽グループ名と区別できないので通さない。
    """
    t = tok.strip(".-")
    if not t or t.lower() in STOP or t.lower() in UNI_STOP or t.lower() in BROAD_ACRO:
        return False
    if any(c.isdigit() for c in t) and any(c.isalpha() for c in t):
        return True
    return any(c.isupper() for c in t[1:]) and any(c.islower() for c in t)


def looks_specific(term: str) -> bool:
    """AI の選別なしで在庫に入れてよい語か。

    2026-07-25〜10-03 の71日間、AI経路が死んだまま「2ch以上のタイトルに出る大文字始まりの語」を
    全部入れた結果、Film / Sunshine / カラオケ 等の約700語で quota を使い切った
    (docs/keyword_audit_2026-10-04.md)。決定論ではツール名らしい形の語だけを通す。
    普通の単語の固有名 (Unsloth / Obsidian) とカタカナ語は AI か人間の判断に回す。
    """
    toks = term.split()
    return all(not re.search(r"[ァ-ヴー]", t) for t in toks) and any(tech_shaped(t) for t in toks)


def is_known(term: str, core_trial: set[str], tried: set[str]) -> bool:
    """既に在庫/core にある語、または検索済みの語か。

    core/trial とは単語単位で双方向に照合 ("Hermes" と "Hermes Agent" は同じ扱い)。
    tried とは完全一致だけ: 過去に誤って検索した "Sub" / "Skill" / "Flash" が
    "Subagent" / "Claude Skills" / "Gemini Flash" を塞がないように。
    """
    tl = term.lower()
    if tl in tried:
        return True

    def has_phrase(hay: str, needle: str) -> bool:
        return re.search(r"(?<![0-9a-z])" + re.escape(needle) + r"(?![0-9a-z])", hay) is not None

    return any(has_phrase(tl, k) or has_phrase(k, tl) for k in core_trial)


def load_words(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    out = set()
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        # tried は "YYYY-MM-DD  word" 形式
        m = re.match(r"\d{4}-\d{2}-\d{2}\s+(.+)", s)
        out.add((m.group(1) if m else s).lower())
    return out


def extract_terms(title: str) -> set[str]:
    """タイトルから候補語 (英語 TitleCase 1〜2語 / カタカナ語) を抽出。"""
    terms = set()
    # 英語トークン列 (数字・ドット・ハイフン込み)
    for run in re.findall(r"[A-Za-z][A-Za-z0-9.\-]*(?:\s+[A-Za-z][A-Za-z0-9.\-]*)*", title):
        toks = run.split()
        caps = [t for t in toks if t[0].isupper() and len(t) >= 3]
        for t in caps:
            if t.lower() not in STOP and t.lower() not in UNI_STOP:
                terms.add(t)
        # 連続する TitleCase 2 語 (Nous Research / Open WebUI 型)
        broad = STOP | UNI_STOP
        for a, b in zip(toks, toks[1:]):
            if (a[0].isupper() and b[0].isupper() and len(a) >= 2 and len(b) >= 2
                    and not (a.lower() in broad and b.lower() in broad)):
                terms.add(f"{a} {b}")
        # 大文字始まり + 数字世代 (Gemma 4 / Kimi K3 型)
        for a, b in zip(toks, toks[1:]):
            if a[0].isupper() and re.fullmatch(r"[A-Z]?\d[\d.]*[Bb]?", b):
                terms.add(f"{a} {b}")
    # カタカナ語 (4 文字以上)
    for kt in re.findall(r"[ァ-ヴー]{4,}", title):
        if kt not in STOP and kt not in UNI_STOP:
            terms.add(kt)
    return terms


KEYWORD_RULES = """あなたは YouTube ニッチ発掘システムのキーワード選別器です。

背景: 開発者向け技術チャンネル (Claude Code / ローカルLLM / AIエージェント / 開発ツール実運用) の
真似元候補を YouTube 検索で発掘する。以下は複数チャンネルのヒット動画タイトルから機械抽出した
候補語です。この中から「YouTube 検索語として使うと、上記テーマの小規模技術チャンネルが
見つかりそうな語」を最大{max_add}個選んでください。

ルール:
- 固有のツール名・モデル名・技術トピックだけを選ぶ。一般語 (Workflow, CEO, デザイン等) は捨てる
- 語義が曖昧な語には修飾語を足して検索精度を上げる (例: Orca → Orca IDE, Jan → Jan AI ローカル)。
  過去の失敗: 「Jan AI」が曲名 Hitori Janai に誤爆した
- ビジネス一般・ガジェット一般・エンタメ寄りの語は捨てる (対象は開発者・実務者)
- 良い語が{max_add}個未満なら少なくてよい。無理に埋めない

"""


LLM_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["term"],
            },
        }
    },
    "required": ["keywords"],
}, ensure_ascii=False)


def llm_select(cands: list[tuple[int, int, str]], samples: dict[str, list[str]],
               max_add: int) -> list[str] | None:
    """claude -p (サブスク) で候補から検索語を選別・整形する。失敗時は None (決定的経路へ)。

    ユーザー方針 (~/.claude/CLAUDE.md): LLM 呼び出しは anthropic SDK でなく
    Claude Code CLI の print mode。--json-schema で structured_output を受ける。
    """
    lines = []
    for n, v, t in cands:
        ex = " / ".join(samples.get(t, [])[:2])
        lines.append(f"- {t} ({n}ch / 計{v:,}views) 例: {ex}")
    prompt = KEYWORD_RULES.format(max_add=max_add) + f"""候補:
{chr(10).join(lines)}

JSON Schema に従い JSON オブジェクトのみを返答してください。"""
    try:
        # --allowedTools は付けない: 空文字引数が Linux で parse エラーになった (2026-07-25 実測)。
        # 純テキスト生成なのでツールはそもそも呼ばれない。
        r = subprocess.run(
            ["claude", "-p", "--model", "haiku", "--output-format", "json",
             "--json-schema", LLM_SCHEMA],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=180, shell=(sys.platform == "win32"))
        envelope = None
        try:
            envelope = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass
        if r.returncode != 0 or (envelope or {}).get("is_error"):
            # エラー詳細は envelope の result フィールドに入る (auth 失敗等)
            detail = (envelope or {}).get("result") or r.stderr or r.stdout
            print(f"LLM_STATUS|ng|reason={str(detail)[:160].replace('|','/')}")
            print(f"[warn] claude -p 失敗 (exit {r.returncode}): {str(detail)[:400]}", file=sys.stderr)
            return None
        out = envelope.get("structured_output") or {}
        terms = [k["term"].strip() for k in out.get("keywords", []) if k.get("term", "").strip()]
        return terms[:max_add] or None
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError) as e:
        print(f"LLM_STATUS|ng|reason={str(e)[:160]}")
        print(f"[warn] claude -p 経路エラー: {e}", file=sys.stderr)
        return None


def collect_candidates():
    """コーパス直近 HARVEST_DAYS 日の技術軸 HIT から候補語を集める (決定的)。

    返り値: (cands[(ch数, 計views, 語)] 降順, samples, term_channels, term_views, core_trial, tried)
    ai_judge.py (Routine 経路) もこれを使う。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HARVEST_DAYS)).strftime("%Y-%m-%d")
    core_trial = load_words(CORE_FILE) | load_words(TRIAL_FILE)
    tried = load_words(TRIED_FILE)

    term_channels: dict[str, set] = defaultdict(set)
    term_views: dict[str, int] = defaultdict(int)
    samples: dict[str, list[str]] = defaultdict(list)
    if not CORPUS.exists():
        return [], samples, term_channels, term_views, core_trial, tried
    for line in CORPUS.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("first_seen", "")[:10] < cutoff:
            continue
        # 技術軸と判定済みの動画だけを材料にする (corpus_filter.py の axis)。
        # out (音楽・ゲーム) と unknown (未判定) を混ぜると、音楽の曲名が試験語になり、
        # その検索がまた音楽動画をコーパスに入れる悪循環になる (2026-07-25〜10-03 の実例)
        if r.get("axis") != "in":
            continue
        for t in extract_terms(r.get("title", "")):
            term_channels[t].add(r.get("channel_id") or r.get("channel", "?"))
            term_views[t] += r.get("views", 0)
            if len(samples[t]) < 3:
                samples[t].append(r.get("title", "")[:60])

    cands = []
    for t, chs in term_channels.items():
        if len(chs) < MIN_CHANNELS:
            continue
        if is_known(t, core_trial, tried):
            continue
        cands.append((len(chs), term_views[t], t))
    cands.sort(reverse=True)
    return cands, samples, term_channels, term_views, core_trial, tried


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="候補を表示するだけで在庫に書かない")
    ap.add_argument("--max-add", type=int, default=MAX_ADD)
    ap.add_argument("--llm", action="store_true",
                    help="claude -p (サブスク) で候補を選別・整形する。失敗時は決定的経路へフォールバック")
    args = ap.parse_args()

    if not CORPUS.exists():
        print("HARVEST_SUMMARY|added=0|reason=no_corpus")
        return 0
    cands, samples, term_channels, term_views, core_trial, tried = collect_candidates()

    picked = None
    mode = "deterministic"
    if args.llm and cands:
        # LLM には決定的上位30語を渡し、選別+修飾語付けを任せる (精度担当)
        picked = llm_select(cands[:30], samples, args.max_add)
        if picked is not None:
            mode = "llm"
            # LLM が整形で既知語を再発明することがある (実例: Kimi K3) — 出力側でも既知語を弾く
            picked = [t for t in picked if not is_known(t, core_trial, tried)]
    if picked is None:
        # AI の選別が無い時は、ツール名らしい形の語だけ (looks_specific)
        picked = [t for _, _, t in cands if looks_specific(t)][: args.max_add]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[info] 直近{HARVEST_DAYS}日のコーパスから候補 {len(cands)} 語 / 補充 {len(picked)} 語 ({mode})")
    for t in picked:
        st = term_channels.get(t)
        note = f"  ({len(st)}ch / 計{term_views[t]:,}v)" if st else "  (LLM整形語)"
        print(f"  {t}{note}")
    if picked and not args.dry_run:
        with Path(TRIAL_FILE).open("a", encoding="utf-8") as f:
            f.write(f"# auto-harvest {today} ({mode} / コーパス直近{HARVEST_DAYS}日・{MIN_CHANNELS}ch以上)\n")
            for t in picked:
                f.write(t + "\n")
    print(f"HARVEST_SUMMARY|added={len(picked) if not args.dry_run else 0}|candidates={len(cands)}|mode={mode}")
    return 0


if __name__ == "__main__":
    main()
