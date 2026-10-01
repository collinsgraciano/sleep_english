"""Validate every `<dir>/*/script.json` — structure, duplicates, and the same rules
the web console's 体检 uses.

控制台的索引只有 `manifest.json`（`app/ai_scripts.py::list_ai_scripts` 只遍历
`themes`），所以这里最后一定会做一次盘↔索引对账；否则「脚本全过」但下拉列表看不见
新批次，是最难发现的一类漂移。

Usage: python validate_all.py [dir] [--expect N] [--lines 800] [--console] [--json]
  --expect N   require exactly N script folders (default: don't check)
  --lines N    required dialogue line count (default 800; 控制台认可的标准行数只记 warn)
  --console    再用 app.ai_scripts_admin.validate_script 逐条复核（＝管理页里点「体检」）
  --json       stdout 只输出统一 JSON 信封
Exit 0 clean / 1 any error or manifest drift / 2 warnings only / 3 usage error.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_manifest as BM  # noqa: E402
import skill_common as SC    # noqa: E402

ROOT = SC.ROOT

# 必填字段以控制台管理台为准（两处手抄迟早分家，skill 说干净、网页报红的事故就来自这里）；
# app 不可导入时退回这份副本，内容与 app/ai_scripts_admin.META_REQUIRED 对齐。
try:
    sys.path.insert(0, str(ROOT))
    from app.ai_scripts_admin import META_REQUIRED, STANDARD_LINES  # type: ignore
    FROM_APP = True
except ImportError:
    META_REQUIRED = ("title", "title_zh", "topic", "cefr", "category", "structure",
                     "lesson_type", "youtube_title", "youtube_description",
                     "youtube_tags", "thumb_badge", "thumb_main", "thumb_hook",
                     "char_a_gender", "char_b_gender")
    STANDARD_LINES = (400, 800)
    FROM_APP = False


def check_script(sp: Path, expected_lines: int):
    """一篇的确定性校验 -> (errors, warnings)，元素为 skill_common finding。"""
    errors, warnings = [], []
    try:
        s = json.loads(sp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [SC.finding("error", "json", f"cannot load: {e}")], []
    if not isinstance(s, dict):
        return [SC.finding("error", "json", "顶层不是 JSON 对象")], []
    for k in META_REQUIRED:
        if k not in s or s[k] in ("", [], None):
            errors.append(SC.finding("error", "meta", f"meta '{k}' missing/empty"))
    if s.get("structure") != "sleep":
        errors.append(SC.finding("error", "structure", "structure must be 'sleep'"))
    d = s.get("dialogue", [])
    n = len(d)
    if n != expected_lines:
        if n in STANDARD_LINES:
            warnings.append(SC.finding(
                "warning", "lines",
                f"dialogue has {n} lines (不是本批目标的 {expected_lines})"))
        else:
            errors.append(SC.finding(
                "error", "lines",
                f"dialogue has {n} lines, expected {expected_lines} "
                f"(控制台认可 {list(STANDARD_LINES)})"))
    seen = set()
    for i, line in enumerate(d):
        want = "char_a" if i % 2 == 0 else "char_b"
        if line.get("speaker") != want:
            errors.append(SC.finding("error", "speaker",
                                     f"line {i}: speaker {line.get('speaker')!r} != {want!r}",
                                     lines=[i + 1]))
        for k in ("text", "phonetic", "zh"):
            if not str(line.get(k, "")).strip():
                errors.append(SC.finding("error", "empty-field",
                                         f"line {i}: '{k}' empty", lines=[i + 1]))
        ph = str(line.get("phonetic", ""))
        if ph and not (ph.startswith("/") and ph.endswith("/")):
            errors.append(SC.finding("error", "ipa-shape",
                                     f"line {i}: phonetic not /.../: {ph[:30]!r}",
                                     lines=[i + 1]))
        t = str(line.get("text", "")).strip().lower()
        if t:
            if t in seen:
                warnings.append(SC.finding("warning", "dup",
                                           f"line {i}: duplicate sentence", lines=[i + 1]))
            seen.add(t)
    return errors[:15], warnings[:5]


def console_check(root: Path, folders):
    """跑 Web 管理页同一套体检 —— 网页报的错就是这里报的错，别自报平安。"""
    from app import ai_scripts_admin as admin
    from app.ai_scripts import AI_SCRIPTS_DIRS
    known = {d.resolve() for d in AI_SCRIPTS_DIRS}
    if root.resolve() not in known:
        print(f"  console-skip: 管理页只认 app 登记的预生成目录（{root} 不在其中）",
              file=sys.stderr)
        return []
    out = []
    for f in folders:
        r = admin.validate_script(f)
        if not r["ok"]:
            out.append(SC.finding("error", "console", f"{f}: {r['errors'][:3]}"))
        elif r["warnings"]:
            out.append(SC.finding("warning", "console",
                                  f"{f}: {len(r['warnings'])} 条管理页提示"))
    return out


def main(hot_dir: str = "ai_scripts_hot", expect: int = 0, expected_lines: int = 800,
         console: bool = False, as_json: bool = False) -> int:
    root = SC.resolve_dir(hot_dir)
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    SC.line(f"# rules: META_REQUIRED/STANDARD_LINES from "
            f"{'app' if FROM_APP else 'local fallback'}"
            f"{' (app/ai_scripts_admin)' if FROM_APP else ''}", as_json)
    folders = sorted(p for p in root.iterdir()
                     if p.is_dir() and not p.name.startswith(("_", "."))
                     and re.match(r"^\d+_", p.name))
    findings: list[dict] = []
    n_err = 0
    for sub in folders:
        sp = sub / "script.json"
        if not sp.exists():
            findings.append(SC.finding("error", "missing", f"{sub.name}: script.json missing"))
            n_err += 1
            continue
        errors, warnings = check_script(sp, expected_lines)
        for f in errors:
            findings.append({**f, "detail": f"{sub.name}: {f['detail']}"})
        for f in warnings:
            findings.append({**f, "detail": f"{sub.name}: {f['detail']}"})
        if errors:
            n_err += 1
            SC.line(f"FAIL {sub.name}: {errors[0]['detail']}", as_json)
        else:
            tag = f" ({len(warnings)} warn)" if warnings else ""
            SC.line(f"pass {sub.name}{tag}", as_json)
    if console:
        cf = console_check(root, [f.name for f in folders])
        for f in cf:
            findings.append(f)
            if f["level"] == "error":
                n_err += 1
        SC.line(f"  console: {len(folders)} 条按管理页规则复核，"
                f"{sum(1 for f in cf if f['level'] == 'error')} 条报红", as_json)
    missing = max(0, expect - len(folders)) if expect else 0
    size = f"expecting {expected_lines} lines" if expected_lines else "any size"

    d = BM.drift(root)
    drift_bad = any(d.values())
    if drift_bad:
        findings.append(SC.finding(
            "error", "manifest-drift",
            f"manifest 与磁盘不同步：漏登记 {len(d['unindexed'])} / "
            f"幽灵条目 {len(d['ghosts'])} / 行数过期 {len(d['stale_lines'])} "
            f"—— 控制台列表与脚本库只会显示 manifest 里的条目",
            fix=f"python {SC.HERE / 'build_manifest.py'} {root}"))
    if missing:
        findings.append(SC.finding("error", "missing-folders",
                                   f"{missing} 篇缺失（expect {expect}，实到 {len(folders)}）"))

    warnings = sum(1 for f in findings if f["level"] == "warning")
    summary = (f"{len(folders)} folders ({size}), {n_err} with errors"
               + (f", {missing} missing (expected {expect})" if expect else "")
               + f", manifest drift={'yes' if drift_bad else 'no'}")
    SC.line(f"\nSUMMARY: {summary}", as_json)
    payload = SC.envelope("validate_all", root, findings, checked=len(folders),
                          summary=summary,
                          errors=sum(1 for f in findings if f["level"] == "error"),
                          warnings=warnings,
                          extra={"drift": d, "from_app": FROM_APP,
                                 "expected_lines": expected_lines})
    if as_json:
        SC.emit_json(payload)
    if n_err or missing or drift_bad:
        return SC.EXIT_BLOCK
    return SC.EXIT_WARN if warnings else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    argv, expect, lines, pos = sys.argv[1:], 0, 800, []
    console = "--console" in argv
    as_json = "--json" in argv
    while argv:
        arg = argv.pop(0)
        if arg in ("--console", "--json"):
            continue
        if arg not in ("--expect", "--lines"):
            pos.append(arg)
            continue
        if not argv:
            print(f"ERROR: {arg} needs a value", file=sys.stderr)
            sys.exit(SC.EXIT_USAGE)
        value = int(argv.pop(0))
        if arg == "--expect":
            expect = value
        else:
            lines = value
    sys.exit(main(*(pos or ["ai_scripts_hot"]), expect=expect, expected_lines=lines,
                  console=console, as_json=as_json))
