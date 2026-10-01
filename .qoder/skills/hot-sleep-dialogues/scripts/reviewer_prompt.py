"""Print a ready-to-paste round-1 reviewer prompt for one or more script numbers.

The template itself stays in references/reviewer-prompt.md (single source of
truth); this only fills its {PLACEHOLDERS} from disk — real folder name, real
line count, real CEFR — so a dispatch never carries a stale folder name or a
400-line assumption into an 800-line batch. 判据本体在 references/review_rubric.md。

Usage:
    python reviewer_prompt.py 1 3
    python reviewer_prompt.py 5 --extra "第八段结尾没收束，重点看故事弧线"
Exit 0 OK / 1 有编号在磁盘上找不到对应目录 / 3 模板文件缺失。
"""
import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_common as SC  # noqa: E402

ROOT = str(SC.ROOT)
DOC = str(SC.HERE.parent / "references" / "reviewer-prompt.md")


def template(heading):
    text = open(DOC, encoding="utf-8").read()
    tail = text[text.index(heading):]
    block = re.search(r"````text\n(.*?)\n````", tail, re.S)
    if not block:
        raise SystemExit(f"ERROR: no prompt block after {heading!r} in reviewer-prompt.md")
    return block.group(1)


def fill(tpl, mapping):
    return re.sub(r"\{([A-Z][A-Z_]+)\}",
                  lambda m: str(mapping[m.group(1)]) if m.group(1) in mapping else m.group(0),
                  tpl)


def find_folder(num: str) -> str | None:
    """编号 → 磁盘真实文件夹名。`1` 与 `001` 都接受（旧版只认已补零的写法，
    而 SKILL.md 一直写的是 <NNN>，实测 `reviewer_prompt.py 1` 会报 no folder）。"""
    key = num.zfill(3) if str(num).isdigit() else str(num)
    hits = [p for p in glob.glob(os.path.join(ROOT, "ai_scripts_hot", "%s_*" % key))
            if os.path.isdir(p)]
    return os.path.basename(hits[0]) if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("numbers", nargs="+")
    ap.add_argument("--extra", default="", help="topic-specific note appended to the brief")
    ap.add_argument("--resumed", action="store_true",
                    help="re-dispatch: a previous reviewer may have edited partially")
    args = ap.parse_args()

    tpl = template("## 派生 / 重审单个脚本")
    missing = 0
    for num in args.numbers:
        folder = find_folder(num)
        if not folder:
            SC.line("!! no folder for %s" % num, False)
            missing += 1
            continue
        meta = json.load(open(os.path.join(ROOT, "ai_scripts_hot", folder, "script.json"),
                              encoding="utf-8"))
        lines = len(meta.get("dialogue") or [])
        mapping = {"WORKDIR": ROOT.replace("\\", "/"), "FOLDER": folder, "NUM": num,
                   "EN": meta.get("topic", ""), "ZH": meta.get("title_zh", ""),
                   "CEFR": meta.get("cefr", "A2"), "LINES": lines, "PAIRS": lines // 2}
        out = fill(tpl, mapping)
        if args.extra:
            note = args.extra
            out = re.sub(r"(?m)^主题：.*$",
                         lambda m: m.group(0) + "\n本篇重点：" + note, out, count=1)
        if args.resumed:
            out = out.replace("硬性不变量（改完好检查）：",
                              "前人可能改了一部分：**以磁盘现状为准重新通读**，先跑一次审计再决定改什么。\n\n硬性不变量（改完好检查）：")
        print(out)
        print("-----")
    return SC.EXIT_BLOCK if missing else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    try:
        sys.exit(main())
    except SystemExit as e:
        if isinstance(e.code, str) and e.code:
            SC.line(e.code, False)
        sys.exit(e.code if isinstance(e.code, int) and e.code != 2 else SC.EXIT_USAGE)
