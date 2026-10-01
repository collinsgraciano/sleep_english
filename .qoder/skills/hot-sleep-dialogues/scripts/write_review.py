"""把 round2 验收结论写回 script.json 的 `review` 字段（控制台据此显示分数与「审查过期」）。

**本工具是本 skill 里唯一改写 script.json 的脚本**，而且只新增/覆盖 `review` 一个键，
其余字段与排版原样保留（内容相同则完全不写盘）。因此它必须串行使用：只能对「当前
没有 agent 在改」的编号跑 —— 与 `normalize_zh.py --apply` 同级的并发告警。

输入：ai_scripts_hot/_review/<FOLDER>.round<N>.json，必须含
    {"verdict": "APPROVED"|"NEEDS_ANOTHER_ROUND", "score": 0-100 整数, ...}
（判据与四维评分见 references/review_rubric.md；verdict/score 缺失或越界一律拒写）

写入的 review 块（形状同时记在 references/review_rubric.md 与 script-format.md）：
    {"round","score","verdict","dimensions","issues","summary_zh","model",
     "reviewed_at","content_hash"}
`content_hash` 由本工具自己算（不信任 agent 手填），app 侧用它决定 `stale`：
内容变了就显示「审查过期」，而不是永远停在「未审查」。

Usage:
    python write_review.py 1 2                 # 默认读 round2.json
    python write_review.py 5 --round 1         # 只跑了 round1 时
    python write_review.py 1 --dry-run         # 只打印将写入的块，不写盘
    python write_review.py 1 --score 88        # 报告里没写分数时显式补（存量报告）
    python write_review.py 1 --json
Exit 0 已写/已一致 / 1 输入不合法或编号找不到 / 3 用法错误。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

VERDICTS = ("APPROVED", "NEEDS_ANOTHER_ROUND")
REVIEW_FIELDS = ("round", "score", "score_source", "verdict", "dimensions", "issues",
                 "summary_zh", "model", "reviewed_at", "content_hash")


def find_folder(root: Path, num: str) -> Path | None:
    hits = [p for p in sorted(root.glob(f"{num}_*")) if p.is_dir()
            and (p / "script.json").is_file()]
    return hits[0] if hits else None


def load_round(path: Path, score_override: int | None = None):
    """读 round<N>.json 并校验必备字段 -> (block 的核心字段, error)。"""
    if not path.is_file():
        return None, f"缺验收报告：{path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"{path.name} 不是合法 JSON：{exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} 顶层必须是对象"
    verdict = str(data.get("verdict", "")).strip()
    if verdict not in VERDICTS:
        return None, f"{path.name} 的 verdict={verdict!r} 不在 {list(VERDICTS)}（按 review_rubric.md 填）"
    score = data.get("score")
    source = "report"
    if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 100:
        # 存量 round2 报告只有 verdict、没有分数（见 SKILL.md 基线）：允许显式 --score 补，
        # 但不替 agent 编分数 —— 没有就是没有，写不进去。
        if score_override is None:
            return None, (f"{path.name} 的 score={score!r} 必须是 0-100 的整数"
                          f"（四维各 25 分自评，判据见 references/review_rubric.md）；"
                          f"存量报告没写分数时可显式加 --score N，或补一轮评审")
        score, source = score_override, "cli"
    dims = data.get("dimensions")
    if dims is not None and not isinstance(dims, dict):
        return None, f"{path.name} 的 dimensions 必须是对象"
    issues = data.get("issues")
    if issues is not None and not isinstance(issues, list):
        return None, f"{path.name} 的 issues 必须是数组"
    block = {"score": score, "verdict": verdict,
             "score_source": source,
             "dimensions": dims or {}, "issues": issues or [],
             "summary_zh": str(data.get("summary_zh", "") or ""),
             "model": str(data.get("model", "") or "skill:hot-sleep-dialogues")}
    return block, ""


def main() -> int:
    argv = list(sys.argv[1:])
    as_json = "--json" in argv
    dry = "--dry-run" in argv
    argv = [a for a in argv if a not in ("--json", "--dry-run")]
    rnd = 2
    if "--round" in argv:
        i = argv.index("--round")
        if i + 1 >= len(argv) or not argv[i + 1].isdigit():
            print("ERROR: --round needs an integer", file=sys.stderr)
            return SC.EXIT_USAGE
        rnd = int(argv[i + 1])
        del argv[i:i + 2]
    dir_arg = None
    if "--dir" in argv:
        i = argv.index("--dir")
        if i + 1 >= len(argv):
            print("ERROR: --dir needs a value", file=sys.stderr)
            return SC.EXIT_USAGE
        dir_arg = argv[i + 1]
        del argv[i:i + 2]
    score_override = None
    if "--score" in argv:
        i = argv.index("--score")
        if i + 1 >= len(argv) or not argv[i + 1].lstrip("-").isdigit():
            print("ERROR: --score needs an integer 0-100", file=sys.stderr)
            return SC.EXIT_USAGE
        score_override = int(argv[i + 1])
        if not 0 <= score_override <= 100:
            print("ERROR: --score must be 0-100", file=sys.stderr)
            return SC.EXIT_USAGE
        del argv[i:i + 2]
    nums = [a for a in argv if not a.startswith("--")]
    if not nums:
        print(__doc__, file=sys.stderr)
        return SC.EXIT_USAGE

    root = SC.resolve_dir(dir_arg, "ai_scripts_hot")
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    review_dir = root / "_review"

    findings: list[dict] = []
    changed = unchanged = failed = 0
    for num in nums:
        key = str(int(num)) if str(num).isdigit() else str(num)
        folder = find_folder(root, key.zfill(3) if key.isdigit() else key)
        if folder is None:
            findings.append(SC.finding("error", "no-folder",
                                       f"{num}: {root} 下找不到 {num}_* 目录"))
            failed += 1
            continue
        name = folder.name
        script_path = folder / "script.json"
        script = SC.load_script(script_path)
        if script is None:
            findings.append(SC.finding("error", "json", f"{name}: script.json 不可读"))
            failed += 1
            continue
        block, err = load_round(review_dir / f"{name}.round{rnd}.json", score_override)
        if err:
            findings.append(SC.finding("error", "bad-round-report", err,
                                       fix="按 references/review_rubric.md 补齐 verdict 与 score"))
            failed += 1
            continue
        review = {"round": rnd, **block,
                  "reviewed_at": int(time.time()),
                  "content_hash": SC.content_hash(script)}
        ordered = {k: review[k] for k in REVIEW_FIELDS if k in review}
        if script.get("review") == ordered:
            SC.line(f"unchanged {name}: review 已是当前内容（round{rnd}, "
                    f"{ordered['verdict']} {ordered['score']}）", as_json)
            unchanged += 1
            continue
        script["review"] = ordered
        text = json.dumps(script, ensure_ascii=False, indent=2) + "\n"
        if dry:
            SC.line(f"dry-run {name}: 将写入 {json.dumps(ordered, ensure_ascii=False)}",
                    as_json)
            changed += 1
            continue
        SC.write_json(script_path, text)
        SC.line(f"updated {name}: round{rnd} {ordered['verdict']} {ordered['score']} "
                f"({ordered['content_hash'][:14]}…)", as_json)
        changed += 1

    summary = (f"{len(nums)} 篇：{changed} 写入 / {unchanged} 已一致 / {failed} 失败"
               + ("（dry-run，未写盘）" if dry else ""))
    SC.line(f"SUMMARY {summary}", as_json)
    payload = SC.envelope("write_review", root, findings, checked=len(nums),
                          summary=summary, extra={"changed": changed,
                                                  "unchanged": unchanged,
                                                  "failed": failed, "dry_run": dry,
                                                  "round": rnd})
    if as_json:
        SC.emit_json(payload)
    return SC.EXIT_BLOCK if failed else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
