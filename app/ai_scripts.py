"""预生成脚本库（ai_scripts/ 与 ai_scripts_hot/）— 读取预生成好的 sleep 脚本并导入脚本库。

每个目录结构：
  manifest.json          主题清单（index/en/zh/category/folder/lines）
  NNN_Topic/script.json  完整 sleep 脚本（Step 0 已完成的产物）

两个消费入口：
1. 脚本库（自动入库）：控制台每次列出预生成脚本时顺带跑一次增量 sync_library()，
   新目录建档、mtime 变了的刷新、跑过视频的主题补 used 标记 —— 不用再手点「导入」。
   之后用现有「脚本库」下拉 / 批量队列直接使用（复用 used 防重标记）。
2. 独立「预生成脚本」下拉：控制台直接列出预生成主题（按 script.json 修改时间倒序，
   新内容在前），选中即跳过 LLM 直接跑，运行时直接读 script.json。
"""
import json
import threading
from pathlib import Path

from .paths import WEB_ROOT

AI_SCRIPTS_DIR = WEB_ROOT / "ai_scripts"
# 多预生成目录（文件夹编号全局唯一）：ai_scripts=001-100, ai_scripts_hot=101-200
AI_SCRIPTS_DIRS = [AI_SCRIPTS_DIR, WEB_ROOT / "ai_scripts_hot"]
MANIFEST_PATH = AI_SCRIPTS_DIR / "manifest.json"
SLEEP_MODE = "sleep"
# 标准行数，与 skill(hot-sleep-dialogues)/script_library.DEFAULT_LINES 一致
EXPECTED_LINES = 400
# 管理页删除＝移入预生成目录下的 _recycle_bin（与运行历史回收站同一套约定；
# manifest 与 skill 脚本都跳过 _ 前缀目录，回收站里的不会被当成脚本）
RECYCLE_DIRNAME = "_recycle_bin"
TRASH_META_NAME = ".trash_meta.json"

_SYNC_LOCK = threading.Lock()


def source_dir(source: str) -> Path:
    """来源标识 → 预生成根目录："hot"=ai_scripts_hot/，其他=ai_scripts/。"""
    return AI_SCRIPTS_DIRS[1] if source == "hot" else AI_SCRIPTS_DIRS[0]



def _load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def list_ai_scripts(source: str = "all") -> list[dict]:
    """列出预生成目录里可用的脚本（含 folder/en/zh/category/lines/mtime/source）。

    以各目录 manifest.json 为索引，仅返回 script.json 实际存在的主题。
    顺序＝script.json 最后修改时间倒序（新内容、刚重审过的排最前）。

    source: "all"（默认，两个目录全部）/ "main"（ai_scripts/ 001-100）
            / "hot"（ai_scripts_hot/ 101-200）。
    """
    if source in ("main", "hot"):
        bases = [source_dir(source)]
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
            script_path = base / folder / "script.json"
            if not folder or not script_path.exists():
                continue
            try:
                mtime = int(script_path.stat().st_mtime)
            except OSError:
                mtime = 0
            out.append({
                "folder": folder,
                "index": int(t.get("index", 0) or 0),
                "en": t.get("en", ""),
                "zh": t.get("zh", ""),
                "category": t.get("category", ""),
                "lines": int(t.get("lines", 0) or 0),
                "mtime": mtime,
                "source": "hot" if base.name.endswith("_hot") else "main",
            })
    out.sort(key=lambda p: (p["mtime"], p["folder"]), reverse=True)
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


def _sync_state_path() -> Path:
    from .script_library import SCRIPTS_DIR
    return SCRIPTS_DIR / "ai_scripts_sync.json"


def _src_of(folder: str) -> str:
    """编号 101+ 属 ai_scripts_hot，001–100 属 ai_scripts（文件夹编号全局唯一）。"""
    try:
        return "hot" if int(str(folder)[:3]) >= 101 else "main"
    except ValueError:
        return "main"


def _load_state() -> dict:
    path = _sync_state_path()
    state = {"folders": {}, "used_marked": {}}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state["folders"] = data.get("folders") or {}
                state["used_marked"] = data.get("used_marked") or {}
        except (json.JSONDecodeError, OSError):
            pass
    for folder, entry in state["folders"].items():  # 老状态补 src（按来源分区裁剪用）
        if isinstance(entry, dict) and not entry.get("src"):
            entry["src"] = _src_of(folder)
    return state


def _save_state(state: dict) -> None:
    path = _sync_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_doc(script: dict, topic: str) -> dict:
    from . import script_library
    return script_library.save_new_script(script, {
        "topic": topic,
        "cefr": script.get("cefr") or "A2",
        "structure": SLEEP_MODE,
        "llm_provider": "ai_scripts",
        "llm_model": "",
        "num_lines": len(script.get("dialogue") or []),
        "review": None,
    })


def _apply_content(script_library, sid: str, script: dict, out: dict) -> None:
    """把 script.json 的最新内容写进库文档；已产过视频（status=used）的那份不覆盖。"""
    doc = script_library.get_script_doc(sid)
    if not doc:
        out["errors"].append(f"{sid}: 库文档读取失败")
        return
    if doc.get("status") == "used":
        out["skipped"] += 1
        return
    script_library.update_script(sid, {"script": script})
    out["refreshed"] += 1


def sync_library(source: str = "all", folders: list[str] | None = None) -> dict:
    """增量自动入库：新脚本建档、改过的脚本刷新、已产视频的补 used 标记。

    指纹＝folder → script.json mtime，记在 configs/script_library/ai_scripts_sync.json，
    所以稳态只有一次 stat 扫描 + 一个小 JSON 读，可以放在页面加载路径上；只有真的
    有新增/变更时才去读库文档。folders 非 None 时只处理这些文件夹（供手动导入用）。
    """
    from . import script_library

    presets = list_ai_scripts(source)
    if folders is not None:
        wanted = {Path(f).name for f in folders if str(f).strip()}
        presets = [p for p in presets if p["folder"] in wanted]

    lib_dir = script_library.SCRIPTS_DIR
    out = {"imported": 0, "adopted": 0, "refreshed": 0, "used_marked": 0,
           "skipped": 0, "errors": []}
    with _SYNC_LOCK:
        state = _load_state()
        known = state["folders"]

        new_ones = [p for p in presets if p["folder"] not in known]
        stale = [p for p in presets if p["folder"] in known and (
            int(known[p["folder"]].get("mtime", 0)) < int(p.get("mtime", 0))
            or not (lib_dir / f"{known[p['folder']].get('sid', '')}.json").exists())]
        # 只有要动库时才扫一次库（老的手工导入留下的文档要先认领，别重复建档）
        index = script_library.index_docs_by_topic(SLEEP_MODE) if new_ones or stale else {}

        for p in new_ones:
            topic = p["en"] or p["folder"]
            hit = index.get(topic)
            if hit:
                known[p["folder"]] = {"mtime": int(p["mtime"]), "sid": hit["sid"],
                                      "topic": topic, "src": p["source"]}
                out["adopted"] += 1
                # 认领老文档时判断内容是否过期：script.json 比那份文档新就刷进去
                if int(hit.get("created", 0) or 0) < int(p["mtime"]):
                    script = load_ai_script(p["folder"])
                    if script:
                        _apply_content(script_library, hit["sid"], script, out)
                continue
            script = load_ai_script(p["folder"])
            if not script:
                out["errors"].append(f"{p['folder']}: script.json 读取失败")
                continue
            doc = _new_doc(script, script.get("topic") or topic)
            index[str(doc.get("topic"))] = {"sid": doc["id"], "status": "draft"}
            known[p["folder"]] = {"mtime": int(p["mtime"]), "sid": doc["id"],
                                  "topic": str(doc.get("topic")), "src": p["source"]}
            out["imported"] += 1

        for p in stale:
            entry = known[p["folder"]]
            sid = str(entry.get("sid", ""))
            topic = str(entry.get("topic") or p["en"] or p["folder"])
            script = load_ai_script(p["folder"])
            if not script:
                out["errors"].append(f"{p['folder']}: script.json 读取失败")
                continue
            if sid.startswith("script_") and (lib_dir / f"{sid}.json").exists():
                _apply_content(script_library, sid, script, out)
            else:  # 库文档被删了 → 重新建档
                doc = _new_doc(script, script.get("topic") or topic)
                sid = str(doc["id"])
                out["imported"] += 1
            known[p["folder"]] = {"mtime": int(p["mtime"]), "sid": sid, "topic": topic,
                                  "src": p["source"]}

        # 「直接使用预生成脚本」跑视频时只写 used_by_mode、不写库状态 → 这里补齐，
        # 否则脚本库里 200 张卡永远是 draft，已用/未用对不上。
        marked = state["used_marked"]
        by_topic: dict[str, str] = {}
        for v in known.values():
            by_topic.setdefault(str(v.get("topic", "")), str(v.get("sid", "")))
        for topic, entry in script_library.used_detail_for_mode(SLEEP_MODE).items():
            sid = by_topic.get(topic, "")
            if not sid.startswith("script_"):
                continue  # 不是预生成脚本的主题，或本轮还没入库 → 下次再补
            if marked.get(topic) == "used":
                continue
            doc = script_library.get_script_doc(sid)
            if doc and doc.get("status") != "used":
                script_library.mark_used(sid, run_name=str(entry.get("run") or "ai_scripts"))
                out["used_marked"] += 1
            marked[topic] = "used"

        if folders is None:  # 只裁掉本来源的失焦目录，别把另一个来源的登记摘了
            in_scope = {p["folder"] for p in presets}
            for folder in [f for f, e in known.items()
                           if f not in in_scope
                           and (source == "all" or e.get("src") == source)]:
                known.pop(folder)
        _save_state(state)
    return out


def status_map() -> dict[str, dict]:
    """folder → {sid, used}：控制台的「已入库 / 已用 / 未用」来源（只读同步状态小文件）。"""
    from . import script_library
    used_topics = set(script_library.used_topics_for_mode(SLEEP_MODE))
    return {
        folder: {"sid": str(e.get("sid", "")),
                 "used": str(e.get("topic", "")) in used_topics}
        for folder, e in _load_state()["folders"].items()
    }


def import_ai_scripts_to_library(folders: list[str] | None = None) -> dict:
    """手动「一键导入」＝与自动入库同一套增量逻辑（返回字段保持旧契约）。"""
    r = sync_library("all", folders)
    return {"imported": r["imported"], "skipped": r["adopted"] + r["skipped"],
            "refreshed": r["refreshed"], "used_marked": r["used_marked"],
            "errors": r["errors"]}
