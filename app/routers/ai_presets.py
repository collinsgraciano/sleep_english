"""预生成脚本管理台 API（页面 /ai_presets）。

数据侧「读 + 入库同步」在 app/ai_scripts.py，「写 / 校验 / 回收站」在
app/ai_scripts_admin.py；本文件只做参数解析、线程池调度（磁盘扫描与打包是同步
IO，别卡事件循环）与响应形状。

接口挂在 /api/ai_presets/* 而不是 /api/scripts/ai_presets/*：scripts.py 里有
/api/scripts/{sid} 兜底路由，同名前缀会被它吃掉。
"""
import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import ai_scripts
from .. import ai_scripts_admin as admin
from .. import script_library

router = APIRouter()

# 一次校验/选择的上限，防止误传全库把响应撑爆
MAX_ITEMS = 400


def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


async def _body(request: Request) -> dict:
    if not (request.headers.get("content-type") or "").startswith("application/json"):
        return {}
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _folders_of(data: dict) -> list[str]:
    raw = data.get("folders") or data.get("folder") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(f).strip() for f in raw if str(f).strip()][:MAX_ITEMS]


def _clean_source(source: str) -> str:
    return source if source in ("main", "hot") else "all"


# ---------------------------------------------------------------------------
# 列表 / 详情
# ---------------------------------------------------------------------------

def _scan(source: str, do_sync: bool) -> tuple[list[dict], dict]:
    """列目录 + 标注库状态（in_library/used/sid）；do_sync 时顺带增量入库。"""
    summary = ai_scripts.sync_library(source) if do_sync else {}
    status = ai_scripts.status_map()
    presets = ai_scripts.list_ai_scripts(source)
    for p in presets:
        info = status.get(p["folder"]) or {}
        p["sid"] = str(info.get("sid", ""))
        p["in_library"] = bool(info.get("sid"))
        p["used"] = bool(info.get("used"))
    return presets, summary


def _stats(rows: list[dict]) -> dict:
    return {"total": len(rows),
            "used": sum(1 for r in rows if r["used"]),
            "unused": sum(1 for r in rows if not r["used"]),
            "in_library": sum(1 for r in rows if r["in_library"]),
            "lines": sum(int(r.get("lines") or 0) for r in rows)}


def _sort_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "index":
        rows.sort(key=lambda p: p["folder"])
    elif sort == "topic":
        rows.sort(key=lambda p: (str(p["en"]).lower(), p["folder"]))
    elif sort == "lines":
        rows.sort(key=lambda p: (-int(p["lines"] or 0), p["folder"]))
    elif sort == "category":
        rows.sort(key=lambda p: (str(p["category"]), p["folder"]))
    else:  # mtime：新内容/刚改过的在前
        rows.sort(key=lambda p: (-int(p["mtime"] or 0), p["folder"]))
    return rows


@router.get("/api/ai_presets")
async def api_list(source: str = "all", q: str = "", category: str = "",
                   status: str = "", sort: str = "mtime", sync: int = 1):
    """预生成脚本清单。status: all|used|unused|in_lib|not_in_lib。

    sync=1（默认）顺带跑一次增量入库（稳态只有一次 stat 扫描 + 读一个小 JSON）；
    想要秒开可传 sync=0。counts 随筛选变，counts_all 是全量基线。
    """
    src = _clean_source(source)
    presets, summary = await asyncio.to_thread(_scan, src, bool(sync))
    rows = presets
    if status == "used":
        rows = [p for p in rows if p["used"]]
    elif status == "unused":
        rows = [p for p in rows if not p["used"]]
    elif status == "in_lib":
        rows = [p for p in rows if p["in_library"]]
    elif status == "not_in_lib":
        rows = [p for p in rows if not p["in_library"]]
    if category:
        rows = [p for p in rows if p["category"] == category]
    if q.strip():
        needle = q.strip().lower()
        rows = [p for p in rows
                if needle in " ".join([p["folder"], p["en"], p["zh"],
                                       p["category"]]).lower()]
    return {"presets": _sort_rows(rows, sort), "counts": _stats(rows),
            "counts_all": _stats(presets),
            "categories": sorted({p["category"] for p in presets if p["category"]}),
            "sync": summary}


@router.get("/api/ai_presets/detail")
async def api_detail(folder: str):
    """单条完整详情：整份 script.json + 库文档摘要 + 机器体检 + 磁盘路径。"""
    d = admin.folder_dir(folder)
    if not d:
        return _err(f"预生成脚本不存在: {folder}", 404)
    src = "hot" if d.parent.name.endswith("_hot") else "main"
    preset = next((p for p in ai_scripts.list_ai_scripts(src)
                   if p["folder"] == d.name), {})
    info = ai_scripts.status_map().get(d.name) or {}
    sid = str(info.get("sid", ""))
    doc = script_library.get_script_doc(sid) if sid.startswith("script_") else None
    return {"folder": d.name, "path": str(d), "script": ai_scripts.load_ai_script(d.name),
            "preset": dict(preset, source=src, sid=sid, in_library=bool(sid),
                           used=bool(info.get("used"))),
            "library": script_library.doc_meta(doc) if doc else {},
            "issues": await asyncio.to_thread(admin.validate_script, d.name)}


@router.get("/api/ai_presets/library")
async def api_library_scripts():
    """脚本库里的 sleep 脚本清单（「从脚本库另存为预生成脚本」下拉数据）。"""
    rows = script_library.list_scripts(ai_scripts.SLEEP_MODE)
    return {"scripts": [{"id": r["id"], "topic": r["topic"], "status": r["status"],
                         "lines": r["lines"], "cefr": r["cefr"]} for r in rows]}


# ---------------------------------------------------------------------------
# 增 / 改
# ---------------------------------------------------------------------------

@router.post("/api/ai_presets/save")
async def api_save(request: Request):
    """保存编辑：整份 script.json 落盘（dialogue 按 a/b 重排）+ manifest 同步。"""
    data = await _body(request)
    folder = str(data.get("folder", "")).strip()
    script = data.get("script")
    if not folder or not isinstance(script, dict):
        return _err("缺少 folder 或 script")
    if not str(script.get("topic", "")).strip():
        return _err("主题（topic）不能为空")
    r = await asyncio.to_thread(admin.save_script, folder, script,
                                str(data.get("topic_zh", "")))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ai_presets/parse_pairs")
async def api_parse_pairs(request: Request):
    """粘贴文本 → dialogue 行（编辑器和「新增」共用的解析预览，不落盘）。"""
    data = await _body(request)
    rows = admin.parse_pairs_text(str(data.get("text", "")))
    return {"ok": True, "rows": rows, "count": len(rows)}


@router.post("/api/ai_presets/create")
async def api_create(request: Request):
    """新增：空白骨架 / 粘贴对话文本 / 直接粘贴 script.json。"""
    data = await _body(request)
    r = await asyncio.to_thread(admin.create_from_payload, data)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ai_presets/duplicate")
async def api_duplicate(request: Request):
    """复制一条改名另存（同主题会跟库文档撞车，故必须给新主题名）。"""
    data = await _body(request)
    folder = str(data.get("folder", "")).strip()
    topic = str(data.get("topic", "")).strip()
    if not folder or not topic:
        return _err("复制需要 folder 与新主题名 topic")
    r = await asyncio.to_thread(admin.duplicate_script, folder, topic,
                                str(data.get("source", "")))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ai_presets/from_library")
async def api_from_library(request: Request):
    data = await _body(request)
    sid = str(data.get("sid", "")).strip()
    if not sid:
        return _err("缺少脚本库文档 id")
    r = await asyncio.to_thread(admin.import_from_library, sid,
                                "hot" if data.get("source") == "hot" else "main")
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


# ---------------------------------------------------------------------------
# 删 / 回收站
# ---------------------------------------------------------------------------

@router.post("/api/ai_presets/delete")
async def api_delete(request: Request):
    """删除（默认进回收站可恢复）。body: {folders, hard?, drop_library?}"""
    data = await _body(request)
    folders = _folders_of(data)
    if not folders:
        return _err("未选择要删除的脚本")
    hard = bool(data.get("hard"))
    drop_lib = bool(data.get("drop_library"))
    results = await asyncio.to_thread(
        lambda: [admin.delete_script(f, hard=hard, drop_library=drop_lib)
                 for f in folders])
    bad = [r for r in results if not r.get("ok")]
    return {"ok": not bad, "deleted": len(results) - len(bad), "results": results,
            "error": bad[0]["error"] if bad else ""}


@router.get("/api/ai_presets/recycle")
async def api_recycle():
    return {"items": await asyncio.to_thread(admin.list_recycle)}


@router.post("/api/ai_presets/recycle/restore")
async def api_recycle_restore(request: Request):
    data = await _body(request)
    key = str(data.get("key", "")).strip()
    if not key:
        return _err("缺少回收站条目 key")
    r = await asyncio.to_thread(admin.restore_script, key)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ai_presets/recycle/purge")
async def api_recycle_purge(request: Request):
    """彻底删除：单条 key 或 all=true 清空。"""
    data = await _body(request)
    key = str(data.get("key", "")).strip()
    if not key and not data.get("all"):
        return _err("需要 key 或 all=true")
    r = await asyncio.to_thread(admin.purge_recycle, key, bool(data.get("all")))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


# ---------------------------------------------------------------------------
# 入库 / 校验 / manifest / 导出
# ---------------------------------------------------------------------------

@router.post("/api/ai_presets/import")
async def api_import(request: Request):
    """把选中的（folders 为空＝全部）预生成脚本导入脚本库，供脚本库/批量队列用。"""
    data = await _body(request)
    folders = _folders_of(data)
    r = await asyncio.to_thread(ai_scripts.import_ai_scripts_to_library,
                                folders or None)
    return {"ok": True, **r}


@router.post("/api/ai_presets/validate")
async def api_validate(request: Request):
    """机器体检。body: {folders?} 或 {source?|all?, cross?}；cross=跨主题整句查重。"""
    data = await _body(request)
    folders = _folders_of(data)
    if not folders:
        src = "all" if data.get("all") else _clean_source(str(data.get("source", "all")))
        folders = [p["folder"] for p in ai_scripts.list_ai_scripts(src)][:MAX_ITEMS]
    if not folders:
        return _err("没有可校验的脚本")
    r = await asyncio.to_thread(admin.validate_many, folders,
                                bool(data.get("cross")))
    return {"ok": True, **r}


@router.post("/api/ai_presets/manifest/rebuild")
async def api_manifest_rebuild(request: Request):
    """按磁盘目录重建 manifest.json（手工加目录/改名后补登记）。"""
    data = await _body(request)
    source = "hot" if data.get("source") == "hot" else "main"
    r = await asyncio.to_thread(admin.rebuild_manifest, source)
    if r.get("ok"):
        await asyncio.to_thread(ai_scripts.sync_library, source)
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.get("/api/ai_presets/download")
async def api_download(folder: str):
    d = admin.folder_dir(folder)
    if not d:
        return _err(f"预生成脚本不存在: {folder}", 404)
    return Response(content=(d / "script.json").read_bytes(),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="{d.name}-script.json"'})


@router.post("/api/ai_presets/export")
async def api_export(request: Request):
    """批量导出 zip（每条一个 <folder>/script.json）。"""
    data = await _body(request)
    folders = _folders_of(data)
    if not folders:
        return _err("未选择要导出的脚本")
    body, name = await asyncio.to_thread(admin.export_zip, folders)
    return Response(content=body, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})
