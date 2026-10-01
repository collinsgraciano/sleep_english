#!/usr/bin/env python
"""Print a ready-to-paste round-2 verifier prompt for one script number.

Reads topic / title_zh / cefr and the real folder name from disk so dispatch
prompts never carry a stale folder name (a known failure mode). 判据本体在
references/review_rubric.md，本脚本只组装派单文本。

Usage:
    python verifier_prompt.py 155 156
    python verifier_prompt.py 155 --scene "咖啡店流程顺序" --extra "注意结账段"
Exit 0 OK / 1 有编号在磁盘上找不到对应目录。
"""
import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skill_common as SC  # noqa: E402

ROOT = str(SC.ROOT)
SKILL = ".qoder/skills/hot-sleep-dialogues"


def find_folder(num: str) -> str | None:
    """编号 → 磁盘真实文件夹名。`1` 与 `001` 都接受（旧版只认已补零的写法）。"""
    key = num.zfill(3) if str(num).isdigit() else str(num)
    hits = [p for p in glob.glob(os.path.join(ROOT, "ai_scripts_hot", "%s_*" % key))
            if os.path.isdir(p)]
    return os.path.basename(hits[0]) if hits else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("numbers", nargs="+", help="script numbers, e.g. 155 156")
    ap.add_argument("--scene", default="", help="one-line A-group scene logic check")
    ap.add_argument("--extra", default="", help="extra topic-specific note for group B")
    args = ap.parse_args()

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
        pairs = lines // 2
        scene = args.scene or (
            "流程顺序、道具、品名/数量、金额与时间必须全篇前后一致，"
            "角色视角（谁在说话）不能中途错位，结尾必须把这件事做完"
        )
        extra = "\n%s" % args.extra if args.extra else ""
        key = num.zfill(3) if str(num).isdigit() else str(num)
        print("""你是独立验收员，没参与撰写。像第一次收听的用户一样通读这个 {lines} 行（{pairs} 对）睡前英文脚本，发现问题**就地改掉**；改写文字必须你自己写。

工作目录：{root}
脚本：ai_scripts_hot/{folder}/script.json
上一轮报告：ai_scripts_hot/_review/{folder}.round1.json
主题：{topic}（{zh}）｜CEFR {cefr}{extra}

先读 {skill}/references/review_rubric.md（4 维判据 + 循环策略 + What NOT to flag），再按 {skill}/references/reviewer-prompt.md 的「独立验收」段执行。**特别留意：① 本篇的{scene}；② 金额/时间/数量逐处核对，text 与 zh 必须是同一数额且全篇加总自洽；③ 音标符号级错标用 PYTHONIOENCODING=utf-8 python {skill}/scripts/ipa_vote.py {num} 拿全库多数表决清单逐条判定（本轮最大收益点）。**

写入安全：开工先 cp 一份 script.json 到 H:/tmp/{num}.bak 并保留到收工；改行只用 Edit 逐条替换，禁止用一次性脚本截断/整档重写 script.json（已有两名验收员把文件截空过）。禁止运行 assemble_script.py，禁止改其他脚本（含音标字形统一）。

收尾（三步都要做）：
1. PYTHONIOENCODING=utf-8 python {skill}/scripts/audit_script.py {num} → hard 必须为 0；
2. 用 Write 写 ai_scripts_hot/_review/{folder}.round2.json：**必须含 `verdict`（APPROVED 或 NEEDS_ANOTHER_ROUND）与 `score`（0-100 整数，按 review_rubric.md 的四维各 25 分自评）+ `dimensions` + `issues` + `summary_zh`**；只有你认可成品才写 APPROVED；schema 见 review_rubric.md；
3. PYTHONIOENCODING=utf-8 python {skill}/scripts/write_review.py {num} —— 把结论写进 script.json 的 review 字段（控制台脚本库据此显示分数；不跑这一步网页永远显示「未审查」）。

回复报：verdict、score、改了多少行及分类、audit 结果、结尾是否收束。""".format(
            root=ROOT.replace("\\", "/"), folder=folder, topic=meta["topic"],
            lines=lines, pairs=pairs, zh=meta.get("title_zh", ""),
            cefr=meta.get("cefr", ""), num=key, scene=scene, extra=extra, skill=SKILL))
        print("-----")
    return SC.EXIT_BLOCK if missing else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
