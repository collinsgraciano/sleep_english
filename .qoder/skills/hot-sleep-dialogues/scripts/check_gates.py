"""一条命令跑完 skill 的全部六道门（纯编排：不重写任何检查规则）。

每道门的规则仍只在各自脚本里实现一次，这里只负责排队、传参、汇总与退出码 ——
所以不会出现「同一规则两处定义」的漂移。所有门都必须支持 `--json`，编排器**只解析
JSON**，不再像早期实现那样正则抓人类可读文本（一改措辞就静默失效）。

门表：
  1 结构 + 索引   validate_all.py --lines 800      结构/索引
  2 索引漂移      build_manifest.py --check        结构/索引
  3 机器审计      audit_script.py --fast           内容工艺（会写 <dir>/_audit/）
  4 繁化闭合      normalize_zh.py（dry run）       内容工艺
  5 音标表决      ipa_vote.py --limit 40           内容工艺
  6 出片就绪      preflight.py                     出片就绪

Usage:
    python check_gates.py [dir] [--only 1,2] [--lines 800] [--expect N] [--expect-lines 800] [--json]

`--lines`/`--expect` 只透传给门 1（`validate_all.py`），`--expect-lines` 只透传给门 6
（`preflight.py`）—— 不认识这些开关的门收到它们会把值当位置参数，所以按门声明透传。
Exit 0 全清 / 1 任一门有 blocker 或用法错误 / 2 仅警告 / 3 用法错误（本脚本自身）。
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

GATES = [
    {"no": 1, "name": "结构 + 索引", "script": "validate_all.py", "args": [],
     "dir_style": "pos", "group": "结构/索引", "writes": False,
     "pass": ("lines", "expect")},          # 只有 validate_all 认这两个
    {"no": 2, "name": "索引漂移", "script": "build_manifest.py", "args": ["--check"],
     "dir_style": "pos", "group": "结构/索引", "writes": False, "pass": ()},
    {"no": 3, "name": "机器审计", "script": "audit_script.py", "args": ["--fast"],
     "dir_style": "--dir", "group": "内容工艺", "writes": True, "pass": ()},
    {"no": 4, "name": "繁化闭合(dry)", "script": "normalize_zh.py", "args": [],
     "dir_style": "--dir", "group": "内容工艺", "writes": False, "pass": ()},
    {"no": 5, "name": "音标表决", "script": "ipa_vote.py", "args": ["--limit", "40"],
     "dir_style": "--dir", "group": "内容工艺", "writes": False, "pass": ()},
    {"no": 6, "name": "出片就绪", "script": "preflight.py", "args": [],
     "dir_style": "pos", "group": "出片就绪", "writes": False,
     "pass": ("expect-lines",)},            # 只有 preflight 认这个
]
# 可按门透传的开关（透传给不认识它的门 = 值会被当成位置参数，直接 usage error）
PASSTHROUGH = ("lines", "expect", "expect-lines")
# 退出码 → 显示与聚合：0 全清 / 2 仅警告 / 1 有 blocker / 3 用法错误（也算失败）
MARK = {0: "✅", 2: "🟡", 1: "🔴", 3: "⚪"}
TIMEOUT = 1800


def run_gate(gate: dict, target: Path, opts: dict) -> dict:
    script = SC.HERE / gate["script"]
    extra = []
    for key in gate.get("pass", ()):
        if key in opts:
            extra += ["--" + key, str(opts[key])]
    row = {"no": gate["no"], "name": gate["name"], "group": gate["group"],
           "script": gate["script"], "writes": gate["writes"],
           "code": SC.EXIT_USAGE, "summary": "", "errors": 0, "warnings": 0, "first": ""}
    if not script.exists():
        row["summary"] = f"脚本缺失：{script.name}"
        return row
    cmd = [sys.executable, str(script)]
    if gate["dir_style"] == "pos":
        cmd.append(str(target))
    else:
        cmd += ["--dir", str(target)]
    cmd += list(gate["args"]) + extra + ["--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", cwd=str(SC.ROOT), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        row["summary"] = f"超时（>{TIMEOUT}s）"
        return row
    row["code"] = proc.returncode
    payload = None
    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        # 子脚本没给出 JSON 信封：退回非零/零判定，并明说不能用它的输出做汇总
        row["summary"] = (f"未输出 JSON 信封（exit {proc.returncode}）"
                          + (f"；stderr: {proc.stderr.strip()[-120:]}" if proc.stderr.strip() else ""))
        if proc.returncode == 0:
            row["code"] = SC.EXIT_USAGE
        return row
    row["summary"] = str(payload.get("summary", ""))[:150]
    row["errors"] = int(payload.get("errors", 0) or 0)
    row["warnings"] = int(payload.get("warnings", 0) or 0)
    findings = payload.get("findings") or []
    # 首个「真问题」优先：note（成本提示之类）不该占掉这一行的可读性
    for f in findings:
        if f.get("level") in ("error", "warning"):
            row["first"] = f"[{f.get('stage', '-')}] {f.get('detail', '')}"[:150]
            break
    else:
        if findings:
            f0 = findings[0]
            row["first"] = f"[{f0.get('stage', '-')}] {f0.get('detail', '')}"[:150]
    if row["errors"] and row["code"] == 0:
        row["code"] = SC.EXIT_BLOCK     # 子脚本自己报错却 exit 0：以计数为准
    return row


def aggregate(rows: list[dict]) -> int:
    """任一门 1/3 → 1（用法错误也算失败，别静默吞掉）；否则任一门 2 → 2；否则 0。"""
    if any(r["code"] in (SC.EXIT_BLOCK, SC.EXIT_USAGE) for r in rows):
        return SC.EXIT_BLOCK
    if any(r["code"] == SC.EXIT_WARN for r in rows):
        return SC.EXIT_WARN
    return SC.EXIT_OK


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    opts: dict[str, str] = {}
    for key in PASSTHROUGH:
        flag = "--" + key
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv):
                print(f"ERROR: {flag} needs a value", file=sys.stderr)
                return SC.EXIT_USAGE
            opts[key] = argv[i + 1]
            del argv[i:i + 2]
    only: set[int] = set()
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 >= len(argv):
            print("ERROR: --only needs a value like 1,6", file=sys.stderr)
            return SC.EXIT_USAGE
        only = {int(x) for x in argv[i + 1].replace(" ", "").split(",") if x.isdigit()}
        del argv[i:i + 2]
    pos = [a for a in argv if not a.startswith("--")]

    target = SC.resolve_dir(pos[0] if pos else None, "ai_scripts_hot")
    if not target.is_dir():
        print(f"ERROR: {target} not found", file=sys.stderr)
        return SC.EXIT_USAGE

    gates = [g for g in GATES if not only or g["no"] in only]
    SC.line("─" * 72, as_json)
    SC.line(f"门禁编排 · {target} · {len(gates)} 道"
            + (f"（--only {sorted(only)}）" if only else "")
            + (f"（{' '.join('--' + k + ' ' + v for k, v in opts.items())}）" if opts else ""),
            as_json)
    rows = [run_gate(g, target, opts) for g in gates]

    for r in rows:
        SC.line(f"  {MARK.get(r['code'], '?')} [{r['no']}] {r['name']:<14}"
                f" ({r['group']})  {r['summary']}", as_json)
        if r["first"] and r["code"]:
            SC.line(f"       ↳ {r['first']}", as_json)

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    SC.line("", as_json)
    for gname, items in groups.items():
        ok = sum(1 for r in items if r["code"] == 0)
        warn = sum(1 for r in items if r["code"] == 2)
        bad = len(items) - ok - warn
        SC.line(f"  {gname}: {ok} 通过 / {warn} 仅提示 / {bad} 阻断", as_json)

    code = aggregate(rows)
    total_ok = sum(1 for r in rows if r["code"] == 0)
    writes = [r["name"] for r in rows if r["writes"]]
    tail = {0: f"✅ {total_ok}/{len(rows)} 道全部通过",
            2: f"🟡 {total_ok}/{len(rows)} 道通过，其余仅有提示（新稿要求清零）",
            1: f"🔴 {total_ok}/{len(rows)} 道通过 —— 先修上面标 🔴/⚪ 的门"}[code]
    summary = (f"{total_ok}/{len(rows)} gates ok · exit={code}"
               + (f" · 写盘的门: {writes}" if writes else ""))
    SC.line(f"\n{tail}", as_json)
    if writes:
        SC.line(f"注：{writes} 会写盘（audit 写 <dir>/_audit/<FOLDER>.json）", as_json)

    findings = []
    for r in rows:
        if r["code"] in (SC.EXIT_BLOCK, SC.EXIT_USAGE):
            findings.append(SC.finding("error", f"gate{r['no']}",
                                       f"[{r['no']}] {r['name']}: {r['summary']}",
                                       stage=r["group"],
                                       fix=f"复跑单门：python {SC.HERE / r['script']} "
                                           f"{target} --json"))
        elif r["code"] == SC.EXIT_WARN:
            findings.append(SC.finding("warning", f"gate{r['no']}",
                                       f"[{r['no']}] {r['name']}: {r['summary']}",
                                       stage=r["group"]))
    payload = SC.envelope("check_gates", target, findings, checked=len(rows),
                          summary=summary, extra={
                              "gates": rows, "by_group": {k: len(v) for k, v in groups.items()},
                              "exit": code})
    if as_json:
        SC.emit_json(payload)
    return code


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
