"""预生成脚本库（ai_scripts/ 与 ai_scripts_hot/）— 读取预生成好的 sleep 脚本并导入脚本库。

每个目录结构：
  manifest.json          主题清单（index/en/zh/category/folder/lines）
  NNN_Topic/script.json  完整 sleep 脚本（Step 0 已完成的产物）

两个消费入口：
1. 一键导入脚本库：把全部（或指定）预生成脚本并入 configs/script_library/，
   之后用现有「脚本库」下拉 / 批量队列直接使用（复用 used 防重标记）。
2. 独立「预生成脚本」下拉：控制台直接列出 ai_scripts 主题，选中即跳过
   LLM 直接跑（不入脚本库，运行时直接读 script.json）。
"""
import json
from pathlib import Path

from .paths import WEB_ROOT

AI_SCRIPTS_DIR = WEB_ROOT / "ai_scripts"
# 多预生成目录（文件夹编号全局唯一）：ai_scripts=001-100, ai_scripts_hot=101-200
AI_SCRIPTS_DIRS = [AI_SCRIPTS_DIR, WEB_ROOT / "ai_scripts_hot"]
MANIFEST_PATH = AI_SCRIPTS_DIR / "manifest.json"


def _load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def list_ai_scripts(source: str = "all") -> list[dict]:
    """列出预生成目录里可用的脚本（含 folder/en/zh/category/lines）。

    以各目录 manifest.json 为索引，仅返回 script.json 实际存在的主题。

    source: "all"（默认，两个目录全部）/ "main"（ai_scripts/ 001-100）
            / "hot"（ai_scripts_hot/ 101-200）。
    """
    if source == "main":
        bases = [AI_SCRIPTS_DIR]
    elif source == "hot":
        bases = [WEB_ROOT / "ai_scripts_hot"]
    else:
        bases = AI_SCRIPTS_DIRS
    out: list[dict] = []
    for base in bases:
        manifest = _load_manifest(base / "manifest.json")
        themes = manifest.get("themes") or []
        for t in themes:
            if not isinstance(t, dict):
                continue
            folder = str(t.get("folder", "")).strip()
            if not folder or not (base / folder / "script.json").exists():
                continue
            out.append({
                "folder": folder,
                "en": t.get("en", ""),
                "zh": t.get("zh", ""),
                "category": t.get("category", ""),
                "lines": int(t.get("lines", 0) or 0),
            })
    return out


def load_ai_script(folder: str) -> dict | None:
    """读取某个预生成脚本的完整 script.json（folder 为目录名，跨目录查找）。"""
    folder = Path(folder).name  # 防目录穿越
    for base in AI_SCRIPTS_DIRS:
        p = base / folder / "script.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


def import_ai_scripts_to_library(folders: list[str] | None = None) -> dict:
    """把 ai_scripts 预生成脚本导入脚本库（按主题去重，跳过已存在）。

    folders=None 表示导入全部可用脚本。返回 {imported, skipped, errors}。
    """
    from . import script_library

    presets = list_ai_scripts()
    if folders is not None:
        wanted = {Path(f).name for f in folders if str(f).strip()}
        presets = [p for p in presets if p["folder"] in wanted]

    existing_topics = set(script_library.library_topics_by_mode().get("sleep", []))
    imported, skipped, errors = 0, 0, []
    for p in presets:
        script = load_ai_script(p["folder"])
        if not script:
            errors.append(f"{p['folder']}: script.json 读取失败")
            continue
        topic = script.get("topic") or p["en"] or p["folder"]
        if topic in existing_topics:
            skipped += 1
            continue
        script_library.save_new_script(script, {
            "topic": topic,
            "cefr": script.get("cefr", "A2"),
            "structure": "sleep",
            "llm_provider": "ai_scripts",
            "llm_model": "",
            "num_lines": len(script.get("dialogue", [])),
            "review": None,
        })
        existing_topics.add(topic)
        imported += 1
    return {"imported": imported, "skipped": skipped, "errors": errors}
