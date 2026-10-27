"""AI判定を claude.ai のクラウドセッション (Routine) で回すための受け渡し口。

2026-10-13 制定。正本: docs/ci_claude_auth.md

以前は GitHub Actions のランナーで `claude -p` を呼んでいたが、そのために GitHub シークレットに
置いた長期トークン (CLAUDE_CODE_OAUTH_TOKEN) が切れたまま 8/05〜10/13 放置された。
Routine のクラウドセッションは claude.ai アカウントそのもので動くので、管理するトークンが無い。

役割分担:
  Routine (毎日 JST 13:30)  … export → Claude 本人が判定 → publish (claude/ai-judge ブランチへ)
  Actions (quota-drain 14:20) … claude/ai-judge の answers.json を apply して main に commit
Routine が main に直接 push しないのは、main への書き込みを Actions (quota-drain) の1本に
寄せて commit 競合を避けるため。(2026-10-20 追記: 当初「クラウドセッションは他人のコミットを含む
ブランチへ push できない」と書いていたが、実測で main 直 push は可能と判明 (commit d542fbab)。
orphan ブランチ方式は競合回避の設計として残す。正本: docs/claude_synthesis_loop_design.md)
claude/ai-judge は main と履歴を共有しない orphan ブランチで、answers.json だけが載る。

Usage:
    python ai_judge.py export --out tasks.json          # 判定対象を書き出す (Routine)
    python ai_judge.py check answers.json               # 回答の形式チェック (Routine)
    python ai_judge.py publish answers.json             # claude/ai-judge に push (Routine)
    python ai_judge.py apply answers.json               # main に反映 (Actions)

answers.json の形:
    {"generated_at": "2026-10-13T04:30:00Z",
     "keywords": [{"term": "Open WebUI", "reason": "..."}],
     "axis": [{"video_id": "xxxxxxxxxxx", "axis": "in", "reason": "..."}]}
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import corpus_filter as cf
import channel_filter as chf
import keyword_harvest as kh

BRANCH = "claude/ai-judge"
ANSWERS_NAME = "answers.json"
GRAY_LIMIT = 200      # 1回で判定するグレー行の上限 (新しい順)
CHANNEL_LIMIT = 200   # 1回で判定するグレーchの上限 (広めIN・ITと無関係だけ out)
MAX_AGE_HOURS = 30    # これより古い回答 = Routine が1日以上回っていない → 🚨


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status(ok, **kw):
    """quota-drain.yml が拾う機械可読の1行 (旧 claude -p 経路と同じ形式)。"""
    tail = "|".join(f"{k}={str(v).replace('|', '/')}" for k, v in kw.items())
    print(f"LLM_STATUS|{'ok' if ok else 'ng'}|{tail}")


# ---------------------------------------------------------------- export

def cmd_export(args):
    cands, samples, *_ = kh.collect_candidates()
    rows = cf.load()
    gray, _ = cf.deterministic_pass(rows)
    gray.sort(key=lambda r: r.get("first_seen", ""), reverse=True)
    tasks = {
        "generated_at": now_iso(),
        "keywords": {
            "max_add": kh.MAX_ADD,
            "instructions": kh.KEYWORD_RULES.format(max_add=kh.MAX_ADD),
            "candidates": [
                {"term": t, "channels": n, "views": v, "examples": samples.get(t, [])[:2]}
                for n, v, t in cands[:30]
            ],
        },
        "axis": {
            "instructions": cf.PROMPT_HEAD.split("--- 判定対象 ---")[0].strip(),
            "gray_total": len(gray),
            "items": [{"video_id": r["video_id"], "title": (r.get("title") or "")[:90]}
                      for r in gray[: args.limit]],
        },
    }
    # チャンネル関連度 (広めIN・ITと無関係だけ out)。決定論でグレーだったchだけ AI に回す。
    ch_labels = chf.load_labels()
    ch_rows = chf.load(ch_labels)
    ch_gray, _ = chf.deterministic_pass(ch_rows, ch_labels)
    tasks["channels"] = {
        "instructions": chf.PROMPT_HEAD.split("--- 判定対象 ---")[0].strip(),
        "gray_total": len(ch_gray),
        "items": [{"channel_id": r["channel_id"],
                   "channel_title": (r.get("channel_title") or "")[:60],
                   "samples": (r.get("samples") or [])[:3]}
                  for r in ch_gray[: CHANNEL_LIMIT]],
    }
    Path(args.out).write_text(json.dumps(tasks, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[info] 試験語候補 {len(tasks['keywords']['candidates'])} 語 / "
          f"軸グレー {len(tasks['axis']['items'])}/{len(gray)} 件 / "
          f"chグレー {len(tasks['channels']['items'])}/{len(ch_gray)} ch → {args.out}")


# ---------------------------------------------------------------- check

def load_answers(path):
    """形式を検証して dict を返す。ダメなら ValueError。"""
    a = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(a, dict):
        raise ValueError("トップレベルが object でない")
    datetime.strptime(a.get("generated_at", ""), "%Y-%m-%dT%H:%M:%SZ")
    kws = a.get("keywords", [])
    axis = a.get("axis", [])
    channels = a.get("channels", [])   # 後方互換: 無い回答も許す
    if not isinstance(kws, list) or not isinstance(axis, list) or not isinstance(channels, list):
        raise ValueError("keywords / axis / channels が配列でない")
    for k in kws:
        if not isinstance(k, dict) or not str(k.get("term", "")).strip():
            raise ValueError(f"keywords の要素が不正: {k}")
    for x in axis:
        if not isinstance(x, dict) or not x.get("video_id") or x.get("axis") not in ("in", "out"):
            raise ValueError(f"axis の要素が不正: {x}")
    for c in channels:
        if not isinstance(c, dict) or not c.get("channel_id") or c.get("rel") not in ("in", "out"):
            raise ValueError(f"channels の要素が不正: {c}")
    a["channels"] = channels
    return a


def cmd_check(args):
    a = load_answers(args.answers)
    print(f"OK: keywords {len(a['keywords'])} / axis {len(a['axis'])} / "
          f"channels {len(a['channels'])} (generated_at {a['generated_at']})")


# ---------------------------------------------------------------- publish

def git(*cmd, cwd=None, check=True):
    return subprocess.run(["git", *cmd], cwd=cwd, check=check, capture_output=True, text=True)


def cmd_publish(args):
    a = load_answers(args.answers)
    wt = tempfile.mkdtemp(prefix="ai-judge-")
    shutil.rmtree(wt)
    git("worktree", "add", "--detach", wt)
    try:
        if git("fetch", "origin", BRANCH, cwd=wt, check=False).returncode == 0:
            git("checkout", "-B", BRANCH, "FETCH_HEAD", cwd=wt)
        else:
            git("checkout", "--orphan", BRANCH, cwd=wt)
            git("rm", "-rf", "--quiet", ".", cwd=wt)
        Path(wt, ANSWERS_NAME).write_text(json.dumps(a, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
        git("add", ANSWERS_NAME, cwd=wt)
        git("commit", "-m", f"ai-judge: keywords {len(a['keywords'])} / axis {len(a['axis'])} "
                            f"/ channels {len(a['channels'])} ({a['generated_at']})", cwd=wt)
        for i, wait in enumerate([0, 2, 4, 8, 16]):
            time.sleep(wait)
            r = git("push", "-u", "origin", BRANCH, cwd=wt, check=False)
            if r.returncode == 0:
                print(f"[info] {BRANCH} に push 済み")
                return
            print(f"[warn] push 失敗 ({i + 1}/5): {r.stderr.strip()[:300]}", file=sys.stderr)
        sys.exit(1)
    finally:
        git("worktree", "remove", "--force", wt, check=False)


# ---------------------------------------------------------------- apply

def cmd_apply(args):
    if not os.path.exists(args.answers):
        status(False, reason=f"{BRANCH} に {ANSWERS_NAME} が無い (Routine が一度も回っていない)")
        return
    try:
        a = load_answers(args.answers)
    except (ValueError, json.JSONDecodeError) as e:
        status(False, reason=f"{ANSWERS_NAME} の形式不正: {str(e)[:120]}")
        return
    gen = datetime.strptime(a["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600
    if age_h > args.max_age_hours:
        status(False, reason=f"Routine の判定が {age_h:.0f} 時間更新されていない (最終 {a['generated_at']})")
        return

    # 試験語: 既知語は弾く (前日の回答を再適用しても増えない = 冪等)
    core_trial = kh.load_words(kh.CORE_FILE) | kh.load_words(kh.TRIAL_FILE)
    tried = kh.load_words(kh.TRIED_FILE)
    terms = []
    for k in a["keywords"][: kh.MAX_ADD]:
        t = k["term"].strip()
        if not kh.is_known(t, core_trial, tried) and t.lower() not in {x.lower() for x in terms}:
            terms.append(t)
    if terms and not args.dry_run:
        day = gen.strftime("%Y-%m-%d")
        with Path(kh.TRIAL_FILE).open("a", encoding="utf-8") as f:
            f.write(f"# auto-harvest {day} (claude-routine / コーパス直近{kh.HARVEST_DAYS}日・"
                    f"{kh.MIN_CHANNELS}ch以上)\n")
            for t in terms:
                f.write(t + "\n")
    for t in terms:
        print(f"  試験語 + {t}")

    # 軸判定: 決定論を先に当て、それでも unknown のままの行だけ AI の判定で埋める
    rows = cf.load()
    cf.deterministic_pass(rows)
    ans = {x["video_id"]: x["axis"] for x in a["axis"]}
    judged = 0
    for r in rows:
        if r.get("axis") == "unknown" and r.get("video_id") in ans:
            r["axis"] = ans[r["video_id"]]
            judged += 1
    if not args.dry_run:
        cf.save(rows)

    # ch関連度: 決定論を先に当て、残ったグレーを AI の回答で埋める (channel_labels.json)
    ch_labels = chf.load_labels()
    ch_rows = chf.load(ch_labels)
    chf.deterministic_pass(ch_rows, ch_labels)
    ch_ans = {c["channel_id"]: c for c in a.get("channels", [])}
    ch_judged = 0
    ts = now_iso()
    for cid, c in ch_ans.items():
        if (ch_labels.get(cid) or {}).get("rel") in ("in", "out"):
            continue   # 決定論で確定済みは触らない (冪等)
        ch_labels[cid] = {"rel": c["rel"], "cat": c.get("cat", ""),
                          "reason": c.get("reason", ""), "ts": ts}
        ch_judged += 1
    if not args.dry_run:
        chf.save_labels(ch_labels)

    print(f"[info] 試験語 +{len(terms)} / 軸判定 {judged} 件 / ch関連度 {ch_judged} 件を反映 "
          f"(回答 {age_h:.0f} 時間前)")
    status(True, keywords=len(terms), judged=judged, channels=ch_judged, age_h=f"{age_h:.0f}")


def main():
    ap = argparse.ArgumentParser(description="AI判定の Routine ⇄ Actions 受け渡し")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export"); p.add_argument("--out", default="tasks.json")
    p.add_argument("--limit", type=int, default=GRAY_LIMIT)
    p = sub.add_parser("check"); p.add_argument("answers")
    p = sub.add_parser("publish"); p.add_argument("answers")
    p = sub.add_parser("apply"); p.add_argument("answers")
    p.add_argument("--max-age-hours", type=float, default=MAX_AGE_HOURS)
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    {"export": cmd_export, "check": cmd_check, "publish": cmd_publish, "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
