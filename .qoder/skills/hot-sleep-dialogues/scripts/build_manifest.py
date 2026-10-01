"""Rebuild / verify a preset-script manifest (`<dir>/manifest.json`).

manifest.json 是控制台唯一的索引：`app/ai_scripts.py::list_ai_scripts` 只遍历
`manifest["themes"]`，磁盘上有文件夹但没登记的脚本，「预生成脚本」下拉、脚本库自动
入库、`colab_worker --ai-script` 全都看不见。所以装配完就要登记（`assemble_script.py`
会顺带上报本篇），批次收尾再整体重建一次。

Usage:
    python build_manifest.py                 # 扫描 ai_scripts_hot/ 重建 manifest.json
    python build_manifest.py ai_scripts      # 其他目录（名字/相对路径/绝对路径均可）
    python build_manifest.py --check         # 只报盘与索引的漂移，不写（exit 1 = 有漂移）
    python build_manifest.py --check --json  # stdout 只输出统一 JSON 信封

Schema 与 `app/ai_scripts_admin.py` 写的完全一致（index/en/zh/category/folder/lines），
两边互相覆盖不会打架。
Exit 0 clean / 1 drift or unreadable / 2 nothing to do but warnings / 3 usage error.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

ROOT = SC.ROOT
INDEX_RE = re.compile(r"^(\d+)_")


def resolve_dir(arg: str | None = None, default: str = "ai_scripts_hot") -> Path:
    """相对路径先按当前 CWD 找，找不到再按项目根找 —— 脚本在任何目录下跑都指向同一份库。"""
    return SC.resolve_dir(arg, default)


def entry_of(folder: str, script: dict) -> dict:
    """manifest 条目（与 `app/ai_scripts_admin._entry_of` 同构）。"""
    m = INDEX_RE.match(folder)
    return {"index": int(m.group(1)) if m else 0,
            "en": str(script.get("topic") or folder.split("_", 1)[-1]).replace("_", " "),
            "zh": str(script.get("title_zh", "")),
            "category": str(script.get("category", "")),
            "folder": folder,
            "lines": len(script.get("dialogue") or [])}


def scan(root: Path):
    """磁盘上所有 NNN_Folder/script.json -> (themes, failed)。_ 前缀目录一律跳过。"""
    themes, failed = [], []
    if not root.is_dir():
        return themes, failed
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        if not INDEX_RE.match(sub.name) or not (sub / "script.json").exists():
            continue
        try:
            script = json.loads((sub / "script.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            failed.append(f"{sub.name}: {e}")
            continue
        themes.append(entry_of(sub.name, script))
    themes.sort(key=lambda t: t["index"])
    return themes, failed


def read_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"count": 0, "failed": 0, "lines_per_script": 0, "note": "", "themes": []}


def write_manifest(root: Path, themes: list[dict], failed: list[str] | None = None) -> Path:
    data = read_manifest(root)
    sizes = sorted({t["lines"] for t in themes})
    data.update({
        "count": len(themes),
        "failed": len(failed) if failed is not None else data.get("failed", 0),
        "lines_per_script": sizes[-1] if sizes else 0,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "themes": themes,
    })
    if not data.get("note") and themes:
        data["note"] = (f"热门主题 {themes[0]['index']}-{themes[-1]['index']} · "
                        "AI 逐主题生成（skill: hot-sleep-dialogues）")
    out = root / "manifest.json"
    SC.write_json(out, data)
    return out


def upsert(root: Path, entry: dict) -> Path:
    """单篇登记：装配/编辑完一篇就把它写进索引，不等批次收尾。"""
    themes = [t for t in (read_manifest(root).get("themes") or [])
              if isinstance(t, dict) and t.get("folder") != entry["folder"]]
    themes.append(entry)
    themes.sort(key=lambda t: int(t.get("index", 0) or 0))
    return write_manifest(root, themes)


def drift(root: Path) -> dict:
    """盘 ↔ manifest 对账：漏登记 / 索引指向已不存在的目录 / 行数过期。"""
    themes, _ = scan(root)
    disk = {t["folder"]: t for t in themes}
    indexed = {str(t.get("folder")): t for t in (read_manifest(root).get("themes") or [])
               if isinstance(t, dict) and t.get("folder")}
    return {
        "unindexed": sorted(set(disk) - set(indexed)),
        "ghosts": sorted(set(indexed) - set(disk)),
        "stale_lines": sorted(f for f in disk.keys() & indexed.keys()
                              if int(indexed[f].get("lines", 0) or 0) != disk[f]["lines"]),
    }


DRIFT_LABELS = (("漏登记（磁盘有、索引没有）", "unindexed"),
                ("幽灵条目（索引有、磁盘没有）", "ghosts"),
                ("行数过期（两边不一致）", "stale_lines"))


def main(arg: str | None = None, check: bool = False, as_json: bool = False) -> int:
    root = resolve_dir(arg)
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    if check:
        d = drift(root)
        findings = []
        for label, key in DRIFT_LABELS:
            rows = d[key]
            if rows:
                findings.append(SC.finding("error", key, f"{label}: {len(rows)}",
                                           fix=f"python {SC.HERE / 'build_manifest.py'} {root}",
                                           lines=None))
            SC.line(f"{'! ' if rows else '  '}{label}: {len(rows)}"
                    + ("".join(f"\n    {r}" for r in rows[:20]) if rows else ""), as_json)
        bad = any(d.values())
        summary = f"{'DRIFT' if bad else 'CLEAN'} {root}"
        SC.line(f"\n{summary}", as_json)
        payload = SC.envelope("build_manifest", root, findings,
                              checked=len(d["unindexed"]) + len(d["ghosts"])
                              + len(d["stale_lines"]),
                              summary=summary, extra={"mode": "check", "drift": d})
        if as_json:
            SC.emit_json(payload)
        return SC.EXIT_BLOCK if bad else SC.EXIT_OK
    themes, failed = scan(root)
    out = write_manifest(root, themes, failed)
    findings = [SC.finding("warning", "unreadable", f)
                for f in failed]
    for f in failed:
        SC.line(f"WARN {f}", as_json)
    summary = f"OK {out} ({len(themes)} themes, lines={sorted({t['lines'] for t in themes})})"
    SC.line(summary, as_json)
    payload = SC.envelope("build_manifest", root, findings, checked=len(themes),
                          summary=summary,
                          extra={"mode": "rebuild", "themes": len(themes),
                                 "lines": sorted({t["lines"] for t in themes})})
    if as_json:
        SC.emit_json(payload)
    return SC.EXIT_WARN if failed else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    argv = [a for a in sys.argv[1:]]
    check = "--check" in argv
    as_json = "--json" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) > 1:
        print(__doc__, file=sys.stderr)
        sys.exit(SC.EXIT_USAGE)
    sys.exit(main(pos[0] if pos else None, check=check, as_json=as_json))
