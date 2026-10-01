"""Print a ready-to-paste generation prompt for one or more planned topics.

模板本体在 references/agent-prompt.md（单一来源：要改派单内容就改那份文档），本脚本
只负责按 `_topics_batch.json`（本机没有时回退 references/topics.md 最新批次段）填占位符 ——
派单里永远是磁盘上真实的文件夹名/八段大纲/行数，而不是手抄的、已经过期的版本。

Usage:
    python generator_prompt.py                     # every planned topic
    python generator_prompt.py 001 002             # only these
Exit 0 OK / 1 请求的编号在清单里找不到 / 3 清单与台账都不可用。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_common as SC  # noqa: E402

ROOT = str(SC.ROOT)
BATCH = os.path.join(ROOT, "ai_scripts_hot", "_topics_batch.json")
DOC = str(SC.HERE.parent / "references" / "agent-prompt.md")
LEDGER = str(SC.HERE.parent / "references" / "topics.md")


def template() -> str:
    """从 agent-prompt.md 抽第一个 ````text 块（模板只在那份文档里定义一份）。"""
    text = open(DOC, encoding="utf-8").read()
    block = re.search(r"````text\n(.*?)\n````", text, re.S)
    if not block:
        raise SystemExit(f"ERROR: no ````text prompt block in {DOC}")
    return block.group(1)


def from_ledger():
    """台账兜底：_topics_batch.json 属过程产物、不入 git（.gitignore 排除了 ai_scripts*/_*），
    换机器或干净克隆后从 references/topics.md 最新批次段复原文件夹名与八段大纲。"""
    if not os.path.exists(LEDGER):
        raise SystemExit(f"ERROR: {BATCH} 与 {LEDGER} 都不在 —— 先跑 Step 1 选题定稿")
    sections = re.split(r"(?m)^(?=## )", open(LEDGER, encoding="utf-8").read())
    cand = [s for s in sections if "八段大纲" in s]
    if not cand:
        raise SystemExit("ERROR: 台账里没有带「八段大纲」列的批次段，按 Step 1 定稿后再派单")
    sec = cand[-1]
    hit = re.search(r"(\d+)\s*对", sec.splitlines()[0])
    pairs = int(hit.group(1)) if hit else 400
    rows = []
    for line in sec.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7 or not re.fullmatch(r"\d+", cells[0]):
            continue
        rows.append({"index": int(cells[0]), "en": cells[1], "zh": cells[2],
                     "category": cells[3], "folder": cells[4],
                     "eight_beats": cells[6], "pairs": pairs})
    if not rows:
        raise SystemExit("ERROR: 台账批次段里没解析出任何行")
    print(f"# source: references/topics.md（本机没有 _topics_batch.json）· {pairs} 对/篇")
    return rows, pairs


def sample_script():
    """磁盘上第一个真实脚本，给 agent 对照格式。整库被清空重编时没有样例可对照，
    这时以 references/script-format.md 为准（它在模板里已经是必读项）。
    800 行的 script.json 约 4 万 token，让 agent 整读等于开跑前先烧掉一半上下文，
    所以只报「读开头几十行」。"""
    for lib in ("ai_scripts_hot", "ai_scripts"):
        base = os.path.join(ROOT, lib)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name, "script.json")
            if name[:1].isdigit() and os.path.exists(p):
                return ("%s   # 只读它开头约 30 行看结构，整份不要读进上下文"
                        % p.replace("\\", "/"))
    return "（本批是第一篇，没有既有脚本可对照，照 script-format.md 写）"


def planned():
    if os.path.exists(BATCH):
        return json.loads(open(BATCH, encoding="utf-8").read()), None
    return from_ledger()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("numbers", nargs="*", help="planned topic numbers, e.g. 1 2")
    args = ap.parse_args()
    wanted = {int(re.sub(r"\D", "", n) or 0) for n in args.numbers}
    rows, fallback_pairs = planned()
    tpl = template()
    idx = lambda r: int(re.sub(r"\D", "", str(r.get("index"))) or 0)
    picked = [r for r in rows if not wanted or idx(r) in wanted]
    missing = sorted(wanted - {idx(r) for r in rows})
    if missing:
        SC.line("!! not in _topics_batch.json: %s"
                % " ".join("%03d" % m for m in missing), False)
    for r in picked:
        pairs = int(r.get("pairs") or fallback_pairs or 400)
        beats = str(r.get("eight_beats", "")).strip()
        steps = [re.sub(r"^\s*\d+\s*[.、)）]?\s*", "", b).strip(" →")
                 for b in re.split(r"→|\n", beats)]
        steps = [b for b in steps if b]
        beats = "\n".join("  %d. %s" % (i + 1, b) for i, b in enumerate(steps))
        print(tpl.format(WORKDIR=ROOT.replace("\\", "/"), NUM="%03d" % idx(r),
                         FOLDER=r.get("folder"), EN=r.get("en"), ZH=r.get("zh"),
                         CATEGORY=r.get("category", ""), CEFR=r.get("cefr", "A2"),
                         SAMPLE=sample_script(), PAIRS=pairs, LINES=pairs * 2,
                         N_CHUNKS=pairs // 50, EIGHT_BEATS=beats))
        print("-----")
    if wanted and not picked:
        return SC.EXIT_BLOCK
    return SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    try:
        rc = main()
    except SystemExit as e:
        # from_ledger / argparse 用 SystemExit 表达用法错误：统一映射到 EXIT_USAGE
        if isinstance(e.code, str) and e.code:
            SC.line(e.code, False)
        rc = e.code if isinstance(e.code, int) and e.code not in (2, ) else SC.EXIT_USAGE
    sys.exit(rc)
