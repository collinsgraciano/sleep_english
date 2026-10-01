"""skill 内所有脚本共用的基础设施：根解析 / 退出码 / JSON 信封 / 报告预算。

这一层**不含任何检查规则** —— 规则只在各自脚本里实现一次（`check_gates.py`
与 `preflight.py` 是纯编排/复用层，不重写规则）。统一以下四件事：

1. **项目根用标记法反推**，不用 `parents[N]`：索引法在「按文件路径执行」与
   「被当作模块 import」两种情形下会指到不同层级，标记法两种都稳。
   标记 = 同时存在 `app/ai_scripts.py` 与 `pipeline/pipeline.py`。
2. **退出码统一** `0 全清 / 1 有 blocker / 2 仅警告 / 3 用法错误`；
   编排器把 3 也计为失败（参考实现曾把 3 静默吞成 0）。
3. **`--json` 信封统一**，让编排器读 JSON 而不是正则抓人类可读文本：
   `{"tool","root","checked","errors","warnings","state","summary","findings"}`
   `findings[] = {"level":"error|warning|note","code","stage","detail","fix","lines"}`。
   `--json` 时 stdout 只输出这一个 JSON 对象，其余诊断走 stderr。
4. **报告预算** `budget()`：关键 findings 不被海量打磨项挤出屏幕。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- 退出码 ---------------------------------------------------------------
EXIT_OK = 0
EXIT_BLOCK = 1     # 有 blocker（机器可判定、必须改）
EXIT_WARN = 2      # 只有 warning（信号，由人或 agent 判断）
EXIT_USAGE = 3     # 用法错误 / 目标不存在 / 子脚本缺失 —— 编排器把它也算失败

_MARKERS = (("app", "ai_scripts.py"), ("pipeline", "pipeline.py"))
SCRIPT_DIR_RE = re.compile(r"^\d+_")
# 过程产物目录（_parts/_review/_audit/_research…）与回收站一律不算脚本
SKIP_PREFIX = ("_", ".")


def find_root(start: Path | None = None) -> Path:
    """项目根 = 最近的、同时含 `app/ai_scripts.py` 与 `pipeline/pipeline.py` 的祖先。

    找不到（脚本被拷到项目外、或仓库改结构）才回退 `HERE.parents[4]`
    （`.qoder/skills/<skill>/scripts` → 项目根）。
    """
    base = Path(start) if start is not None else HERE
    for cand in (base, *base.parents):
        if all((cand / a / b).is_file() for a, b in _MARKERS):
            return cand
    return HERE.parents[4]


ROOT = find_root()


def setup_stdout() -> None:
    """Windows 控制台默认 cp936，中文/emoji 会抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):   # 已被重定向成 StringIO 等
            pass


def resolve_path(arg: str | None, default: str | None = None) -> Path:
    """文件或目录通用：绝对路径原样；相对路径先按 CWD 找、找不到再按项目根找。"""
    p = Path(arg) if arg else Path(default or ".")
    if p.is_absolute():
        return p
    return p.resolve() if p.exists() else ROOT / p


def resolve_dir(arg: str | None, default: str = "ai_scripts_hot") -> Path:
    """相对路径先按当前 CWD 找、找不到再按项目根找 —— 任何目录下跑都指向同一份库。"""
    return resolve_path(arg, default)


def iter_scripts(root: Path):
    """`root` 下所有 `NNN_*/script.json`（跳过 `_`/`.` 前缀目录）。"""
    if not root.is_dir():
        return []
    out = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith(SKIP_PREFIX):
            continue
        if not SCRIPT_DIR_RE.match(sub.name):
            continue
        path = sub / "script.json"
        if path.is_file():
            out.append(path)
    return out


def load_script(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: Path, payload) -> None:
    """先写 .tmp 再 replace —— 控制台正在列目录时不会读到半截 JSON。

    序列化 dict 时补一个结尾换行（与仓库里 script.json / manifest.json 的排版一致，
    否则每次重写都会多出一条无意义的 diff）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def sha1_of_text(text: str) -> str:
    import hashlib
    return "sha1:" + hashlib.sha1(text.encode("utf-8")).hexdigest()


def content_hash(script: dict) -> str:
    """对话内容的稳定指纹（review 写回时用它锚定「这条结论对应哪一版内容」）。

    任何一行 text/phonetic/zh/speaker 变化都会变 → 控制台据此显示「审查过期」。
    """
    dialogue = (script or {}).get("dialogue") or []
    return sha1_of_text(json.dumps(dialogue, ensure_ascii=False, sort_keys=True))


# --- findings 与 JSON 信封 -------------------------------------------------

def finding(level: str, code: str, detail: str, stage: str = "",
            fix: str = "", lines: list[int] | None = None) -> dict:
    """一条机器可读结论。level: error(会/已阻断) | warning(会降级) | note(提示)。"""
    out = {"level": level, "code": code, "detail": detail}
    if stage:
        out["stage"] = stage
    if fix:
        out["fix"] = fix
    if lines:
        out["lines"] = list(lines)[:20]
    return out


def envelope(tool: str, root, findings: list[dict], checked: int = 0,
             summary: str = "", extra: dict | None = None,
             errors: int | None = None, warnings: int | None = None) -> dict:
    """统一 JSON 输出对象。`state` 由 findings 推导，脚本不得手填以规避不一致。

    `findings` 可能是按报告预算截断过的明细（见 `budget()`），所以计数允许显式
    传入真实总数（errors/warnings）——否则截断会让编排器低估问题量。
    """
    if errors is None:
        errors = sum(1 for f in findings if f.get("level") == "error")
    if warnings is None:
        warnings = sum(1 for f in findings if f.get("level") == "warning")
    state = "blockers" if errors else ("warnings" if warnings else "ok")
    payload = {
        "tool": tool,
        "root": str(root),
        "checked": int(checked),
        "errors": errors,
        "warnings": warnings,
        "state": state,
        "summary": summary or f"{checked} checked, {errors} errors, {warnings} warnings",
        "findings": findings,
    }
    if extra:
        payload.update(extra)
    return payload


def emit_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def exit_of(findings_or_errors, warnings: int | None = None) -> int:
    """0/1/2 —— 只看 error 是否阻断，warning 不阻断。"""
    if isinstance(findings_or_errors, int):
        errors, warns = findings_or_errors, int(warnings or 0)
    else:
        errors = sum(1 for f in findings_or_errors if f.get("level") == "error")
        warns = sum(1 for f in findings_or_errors if f.get("level") == "warning")
    if errors:
        return EXIT_BLOCK
    return EXIT_WARN if warns else EXIT_OK


def line(msg: str, as_json: bool = False) -> None:
    """人类可读诊断：JSON 模式下一律走 stderr，保证 stdout 是纯 JSON。"""
    print(msg, file=sys.stderr if as_json else sys.stdout)


def budget(findings: list[dict], low_max: int = 14, low_level: str = "warning",
           low_codes: tuple[str, ...] = (), high_codes: tuple[str, ...] = ()) -> list[dict]:
    """关键结论优先、打磨项截断。

    `low_codes` / `high_codes` 可显式指定分桶；缺省按 level 分：
    error + note 全留，warning 只留前 `low_max` 条。
    """
    if high_codes or low_codes:
        high = [f for f in findings if f.get("code") in high_codes]
        low = [f for f in findings if f.get("code") not in high_codes]
    else:
        high = [f for f in findings if f.get("level") != low_level]
        low = [f for f in findings if f.get("level") == low_level]
    return high + low[:low_max]


def add_json_flag(parser) -> None:
    parser.add_argument("--json", action="store_true",
                        help="stdout 只输出统一 JSON 信封（诊断走 stderr）")


def print_summary(tool: str, checked: int, errors: int, warnings: int,
                  as_json: bool = False, extra: str = "") -> None:
    """人类可读的收尾行（编排器不解析它，只解析 --json）。"""
    line(f"SUMMARY {tool}: checked={checked} errors={errors} warnings={warnings}"
         + (f" {extra}" if extra else ""), as_json)
