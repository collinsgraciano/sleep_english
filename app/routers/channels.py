"""频道矩阵 API — 频道实体 CRUD / 上下文开关 / 配置同步 / 主题库 / 矩阵填充队列.

数据流：
- 频道实体 configs/channels/{cid}.json（app/channel_profiles.py 存储层），
  内嵌 config 节 = 该频道**完整独立的配置快照**（整套 PARAM_SPEC）
- 频道上下文 = URL `?channel=`（多标签页并行，各 tab 独立，无服务端全局
  开关）；/context 返回频道清单供 base.html 上下文条
- /config/sync：从全局按参数组回填（身份键守卫，频道名/主题域不被覆盖）
- /fill_queue：对每个 active 频道从其主题库随机抽 N 个未用主题入批量队列
  （队列项带 channel_id，_build_config 开始时装载频道完整配置快照）
- 主题库 AI 生成复用 topics_ai.generate_topics，hint 自动注入频道定位
"""
import asyncio
import io
import json
import os
import random
import time
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..batch_queue_service import get_batch_queue
from ..channel_profiles import (
    IDENTITY_SYNC_GUARD, SYNC_SCOPES, _seed_channel_config,
    create_channel_from_favorite, delete_channel, generate_channel_id,
    get_channel, list_channels, load_channel_config, normalize_channel,
    resolve_topics_files, save_channel, save_channel_config,
    set_channel_status, sync_channel_config,
)
from ..config_manager import load_config
from ..paths import CHANNEL_ASSETS_DIR
from ..sse import sse_line as _sse, SSE_HEADERS as _SSE_HEADERS
from .. import topics_ai
from .channel_factory import _ID_RE as _FAV_ID_RE, _load_favorites

router = APIRouter()


# ===========================================================================
# 频道 CRUD
# ===========================================================================

@router.get("/api/channels/list")
async def api_channels_list():
    """全部频道 + 轻量统计（已产期数/最近运行，按 script.json.channel_id 扫描）。"""
    channels = list_channels()
    stats = _channel_stats({c["id"] for c in channels})
    for c in channels:
        s = stats.get(c["id"], {})
        c["runs_count"] = s.get("runs", 0)
        c["last_run_at"] = s.get("last_run_at", 0)
    return {"channels": channels}


def _channel_stats(channel_ids: set[str]) -> dict[str, dict]:
    """扫描 output 运行目录统计各频道期数（复用 iter_run_dirs 布局逻辑）。"""
    stats: dict[str, dict] = {}
    if not channel_ids:
        return stats
    from ..config_manager import iter_run_dirs
    for d in iter_run_dirs(load_config().get("output_dir", "./output")):
        sp = d / "script.json"
        if not sp.exists():
            continue
        try:
            cid = str(json.loads(sp.read_text(encoding="utf-8"))
                      .get("channel_id", "") or "")
        except (json.JSONDecodeError, OSError):
            continue
        if cid not in channel_ids:
            continue
        s = stats.setdefault(cid, {"runs": 0, "last_run_at": 0})
        s["runs"] += 1
        s["last_run_at"] = max(s["last_run_at"], d.stat().st_mtime)
    return stats


@router.post("/api/channels/create")
async def api_channels_create(request: Request):
    """手动新建频道：{name_en, name_zh?, niche?, audience?, brand_colors?}。

    新建即拥有完整配置快照（全局深拷贝 + 品牌色映射 + 身份键），此后独立演化。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    data.setdefault("id", generate_channel_id())
    if not (data.get("brand_colors") or []):
        data["brand_colors"] = ["#4F46E5", "#F59E0B", "#F8FAFC"]
    profile = normalize_channel(data)
    if profile is None:
        return JSONResponse({"ok": False, "error": "缺少 name_en，无法创建"}, status_code=400)
    if get_channel(profile["id"]) is not None:
        return JSONResponse({"ok": False, "error": "频道 id 已存在"}, status_code=409)
    profile["config"] = _seed_channel_config(
        profile["id"], profile["name_en"], profile["brand_colors"])
    saved = save_channel(profile)
    return {"ok": True, "channel": saved}


@router.post("/api/channels/from_favorite")
async def api_channels_from_favorite(request: Request):
    """频道工坊收藏一键转正（幂等：重复转正保留已有 overrides）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    fid = str((data if isinstance(data, dict) else {}).get("favorite_id", "") or "").strip()
    if not fid or not _FAV_ID_RE.match(fid):
        return JSONResponse({"ok": False, "error": "无效的收藏 id"}, status_code=400)
    fav = next((p for p in _load_favorites() if p.get("id") == fid), None)
    if fav is None:
        return JSONResponse({"ok": False, "error": "收藏不存在（可能已删除）"}, status_code=404)
    channel = create_channel_from_favorite(fid)
    return {"ok": True, "channel": channel}


@router.post("/api/channels/update")
async def api_channels_update(request: Request):
    """更新频道品牌字段（name/niche/brand_colors 等）。

    配置不在本端点修改：切上下文后在「参数配置页」编辑频道快照，
    或用 /config/sync 从全局按组回填。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    cid = str(data.get("id", "") or "").strip()
    existing = get_channel(cid)
    if existing is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    merged = {**existing, **{k: v for k, v in data.items()
                             if k not in ("overrides", "config")}}
    merged["id"] = existing["id"]  # id 不可变（输出归属/素材目录依赖）
    merged["created"] = existing["created"]
    merged["config"] = existing.get("config") or {}  # 配置快照不随品牌编辑变动
    saved = normalize_channel(merged)
    if saved is None:
        return JSONResponse({"ok": False, "error": "缺少 name_en"}, status_code=400)
    save_channel(saved)
    return {"ok": True, "channel": saved}


@router.post("/api/channels/status")
async def api_channels_status(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    cid = str((data if isinstance(data, dict) else {}).get("channel_id", "") or "").strip()
    status = str((data if isinstance(data, dict) else {}).get("status", "") or "").strip()
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    updated = set_channel_status(cid, status)
    return {"ok": updated is not None, "channel": updated}


@router.post("/api/channels/delete")
async def api_channels_delete(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    cid = str((data if isinstance(data, dict) else {}).get("channel_id", "") or "").strip()
    if not delete_channel(cid):
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    return {"ok": True}


# ===========================================================================
# 频道清单（上下文条数据源）+ 配置同步
# ===========================================================================

@router.get("/api/channels/context")
async def api_channels_context():
    """频道清单（base.html 上下文条数据源）。

    频道上下文由 URL `?channel=` 携带（多标签页并行，无服务端全局状态）。"""
    channels = [{"id": c["id"], "name_en": c["name_en"], "name_zh": c["name_zh"],
                 "status": c["status"], "logo": c.get("logo", "")}
                for c in list_channels()]
    return {"ok": True, "channels": channels}


@router.post("/api/channels/{cid}/config/sync")
async def api_channel_config_sync(cid: str, request: Request):
    """从全局按参数组回填频道配置快照（身份键守卫）。

    body: {scope: all|credentials|content|visual|bgm}。"""
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    scope = str((data if isinstance(data, dict) else {}).get("scope", "all") or "all")
    if scope not in SYNC_SCOPES:
        return JSONResponse({"ok": False,
                             "error": f"无效范围: {scope}（可选 {', '.join(SYNC_SCOPES)}）"},
                            status_code=400)
    saved = sync_channel_config(cid, scope)
    return {"ok": True, "scope": scope, "config": saved}


@router.get("/api/channels/{cid}/config")
async def api_channel_config_get(cid: str):
    """读取频道完整配置快照（defaults 补全后；旧格式频道触发自迁移）。"""
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    return {"ok": True, "config": load_channel_config(cid)}


@router.post("/api/channels/{cid}/config")
async def api_channel_config_set(cid: str, request: Request):
    """整档写回频道配置快照（身份键守卫：频道名/主题域/归属强制保留现值）。"""
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    config = (data if isinstance(data, dict) else {}).get("config")
    if not isinstance(config, dict):
        return JSONResponse({"ok": False, "error": "config 必须是对象"}, status_code=400)
    current = load_channel_config(cid)
    for key in IDENTITY_SYNC_GUARD:
        if key in current:
            config[key] = current[key]
    saved = save_channel_config(cid, config)
    return {"ok": True, "config": saved}


@router.get("/api/channels/{cid}/asset/{kind}")
async def api_channel_asset(cid: str, kind: str, variant: str = "auto"):
    """频道 Logo/Banner（转正后 id 沿用工坊收藏 id，素材目录天然对齐）。

    kind=logo 时 variant 控制返回版本：auto（默认，有抠图返回抠图否则
    原图）/ raw（原始带底图）/ cutout（透明底抠图，未生成则 404）。"""
    if kind not in ("logo", "banner"):
        return JSONResponse({"ok": False, "error": "无效素材类型"}, status_code=400)
    channel = get_channel(cid)
    if channel is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    path = CHANNEL_ASSETS_DIR / cid / f"{kind}.png"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "素材尚未生成（在频道工坊生成后自动关联）"},
                            status_code=404)
    cutout_path = _logo_cutout_path(cid)
    if kind == "logo" and variant in ("cutout", "auto") and cutout_path.exists():
        if variant == "cutout" or cutout_path.stat().st_mtime >= path.stat().st_mtime:
            return FileResponse(cutout_path, media_type="image/png",
                                headers={"Cache-Control": "no-cache"})
        if variant == "cutout":
            return JSONResponse({"ok": False, "error": "抠图缓存已过期，请重新抠图"},
                                status_code=404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


def _logo_cutout_path(cid: str) -> Path:
    """频道 Logo 抠图缓存路径（文件可能不存在，调用方自判）。"""
    from logo_cutout import cutout_path_for
    return cutout_path_for(CHANNEL_ASSETS_DIR / cid / "logo.png")


def _run_logo_cutout(cid: str, force: bool = True) -> bool:
    """对频道 Logo 执行抠图，返回是否产出透明底缓存。"""
    dest = CHANNEL_ASSETS_DIR / cid / "logo.png"
    if not dest.exists():
        return False
    from logo_cutout import ensure_logo_cutout
    cut_path = _logo_cutout_path(cid)
    ensure_logo_cutout(dest, force=force)
    return cut_path.exists()


@router.post("/api/channels/{cid}/asset/logo")
async def upload_channel_logo(cid: str, file: UploadFile = File(...)):
    """上传/更换频道 Logo（写入工坊素材同路径，两源统一；合成端直接叠加）。

    统一转存 PNG RGBA 并居中裁方形（最长边≤1024），与工坊生成的
    1024x1024 logo.png 规格对齐；tmp→os.replace 原子写防半文件。
    保存成功后立即抠透明底（纯色底 logo 上画面不再带底色块）。"""
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    ctype = (file.content_type or "").lower()
    if ctype not in ("image/png", "image/jpeg", "image/webp"):
        return JSONResponse({"ok": False, "error": "仅支持 PNG/JPG/WebP 图片"}, status_code=400)
    raw = await file.read()
    if not raw or len(raw) > 8 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "图片为空或超过 8MB"}, status_code=400)
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        return JSONResponse({"ok": False, "error": "图片解析失败"}, status_code=400)
    if img.width != img.height:  # 居中裁方形（合成端按方形等比缩放）
        side = min(img.size)
        left = (img.width - side) // 2
        top = (img.height - side) // 2
        img = img.crop((left, top, left + side, top + side))
    if img.width > 1024:
        img = img.resize((1024, 1024), Image.LANCZOS)
    dest_dir = CHANNEL_ASSETS_DIR / cid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "logo.png"
    tmp = dest_dir / ".logo.tmp.png"
    img.convert("RGBA").save(tmp, "PNG")
    os.replace(tmp, dest)
    cutout = False
    try:
        cutout = _run_logo_cutout(cid, force=True)
    except Exception:  # noqa: BLE001 — 抠图失败不影响上传成功
        pass
    return {"ok": True, "logo_at": time.time(), "cutout": cutout}


@router.post("/api/channels/{cid}/asset/logo/recut")
async def recut_channel_logo(cid: str):
    """手动重新抠图（源 Logo 更新/算法升级后前端「🔄 重新抠图」按钮）。"""
    if get_channel(cid) is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    if not (CHANNEL_ASSETS_DIR / cid / "logo.png").exists():
        return JSONResponse({"ok": False, "error": "Logo 素材尚未上传/生成"}, status_code=404)
    try:
        cutout = _run_logo_cutout(cid, force=True)
    except Exception as e:  # noqa: BLE001 — 错误信息原样回显
        return JSONResponse({"ok": False, "error": f"抠图失败: {e}"}, status_code=500)
    return {"ok": True, "cutout": cutout}


# ===========================================================================
# 频道主题库（独立主题域）
# ===========================================================================

def _load_channel_topics(channel: dict) -> tuple[dict, dict, str]:
    """→ (topics_data, used_topics, topics_file)。文件缺失返回空结构。"""
    topics_file, used_file = resolve_topics_files(channel)
    topics_data = {}
    p = Path(topics_file)
    if p.exists():
        try:
            topics_data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            topics_data = {}
    used = {}
    up = Path(used_file)
    if up.exists():
        try:
            used = json.loads(up.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            used = {}
    if not isinstance(topics_data, dict):
        topics_data = {}
    if not isinstance(used, dict):
        used = {}
    return topics_data, used, topics_file


def _save_channel_topics(channel: dict, topics_data: dict) -> str:
    topics_file, _ = resolve_topics_files(channel)
    p = Path(topics_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(topics_data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(p)
    return topics_file


@router.get("/api/channels/{cid}/topics")
async def api_channel_topics(cid: str):
    channel = get_channel(cid)
    if channel is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    topics_data, used, topics_file = _load_channel_topics(channel)
    total = sum(len(v) for v in topics_data.values() if isinstance(v, list))
    unused = sum(1 for cat in topics_data.values() if isinstance(cat, list)
                 for t in cat if t not in used)
    return {"ok": True, "topics": topics_data, "used": used, "topics_file": topics_file,
            "total": total, "unused": unused}


@router.post("/api/channels/{cid}/topics/save")
async def api_channel_topics_save(cid: str, request: Request):
    channel = get_channel(cid)
    if channel is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    topics_data = (data if isinstance(data, dict) else {}).get("topics")
    if not isinstance(topics_data, dict):
        return JSONResponse({"ok": False, "error": "topics 必须是 {category: [..]}"},
                            status_code=400)
    saved_to = _save_channel_topics(channel, topics_data)
    return {"ok": True, "topics_file": saved_to}


@router.post("/api/channels/{cid}/topics/ai_generate")
async def api_channel_topics_ai_generate(cid: str, request: Request):
    """SSE：按频道定位 AI 生成主题（复用 topics_ai.generate_topics，
    hint 自动注入频道 niche/audience）。body: {category, count, hint?}"""
    channel = get_channel(cid)
    if channel is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    category = str(data.get("category", "")).strip()
    try:
        count = max(1, min(int(data.get("count", 20) or 20), 50))
    except (TypeError, ValueError):
        count = 20
    hint = str(data.get("hint", "")).strip()[:500]
    brand_bits = [b for b in (channel.get("niche"), channel.get("audience")) if b]
    brand_hint = f"Channel positioning: {' | '.join(brand_bits)}" if brand_bits else ""
    full_hint = f"{brand_hint}\n{hint}".strip()

    topics_data, used, _ = _load_channel_topics(channel)

    async def event_stream():
        try:
            yield _sse({"type": "progress", "message": "正在调用 AI 生成频道主题，请稍候..."})
            if not category:
                yield _sse({"type": "error", "error": "请先填写分类名"})
                return
            result = await asyncio.to_thread(
                topics_ai.generate_topics, category, count, topics_data,
                list(used.keys()), full_hint)
            if not result.get("topics"):
                yield _sse({"type": "error",
                            "error": "AI 未返回有效话题（可能与现有话题重复），请重试"})
                return
            topics_data.setdefault(category, [])
            topics_data[category].extend(
                t for t in result["topics"] if t not in topics_data[category])
            _save_channel_topics(channel, topics_data)
            yield _sse({"type": "result", "data": {
                "topics": result["topics"], "all": topics_data}})
        except Exception as e:
            yield _sse({"type": "error", "error": str(e)[:300]})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ===========================================================================
# 矩阵填充：active 频道 × N 期 → 批量队列
# ===========================================================================

@router.post("/api/channels/fill_queue")
async def api_channels_fill_queue(request: Request):
    """对 active 频道从各自主题库随机抽未用主题入队。

    body: {episodes_per_channel: int, channel_ids?: [..]}（缺省=全部 active）。
    返回 (enqueued, rejected[{channel, reason}])。主题库为空的频道拒绝并提示。
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    try:
        episodes = max(1, min(int(data.get("episodes_per_channel", 1) or 1), 50))
    except (TypeError, ValueError):
        episodes = 1
    ids = data.get("channel_ids")
    channels = [c for c in list_channels() if c.get("status") == "active"]
    if isinstance(ids, list) and ids:
        id_set = {str(i) for i in ids}
        channels = [c for c in channels if c["id"] in id_set]
    if not channels:
        return JSONResponse({"ok": False, "error": "没有待填充的 active 频道"}, status_code=400)

    enqueued: list[dict] = []
    rejected: list[dict] = []
    queue = get_batch_queue()
    for c in channels:
        topics_data, used, _ = _load_channel_topics(c)
        pool = [t for cat in topics_data.values() if isinstance(cat, list)
                for t in cat if t not in used]
        if not pool:
            rejected.append({"channel": c["name_en"], "channel_id": c["id"],
                             "reason": "主题库无可用主题（请先在主题库生成/导入）"})
            continue
        random.shuffle(pool)
        picked = pool[:episodes]
        snap_cefr = str(load_channel_config(c["id"]).get("cefr", "") or "")
        items = [{"type": "topic", "mode": "sleep", "topic": t,
                  "channel_id": c["id"], "cefr": snap_cefr}
                 for t in picked]
        added, item_rejected = queue.add_items(items)
        enqueued.extend(added)
        rejected.extend({"channel": c["name_en"], "reason": r.get("reason", "")}
                        for r in item_rejected)
    return {"ok": bool(enqueued), "enqueued": len(enqueued), "items": enqueued,
            "rejected": rejected}


@router.get("/api/channels/meta")
async def api_channels_meta():
    """频道矩阵页辅助元数据：全部工坊收藏（供导入下拉）。"""
    return {"ok": True,
            "favorites": [{"id": p.get("id", ""), "name_en": p.get("name_en", ""),
                           "name_zh": p.get("name_zh", ""), "niche": p.get("niche", ""),
                           "logo": p.get("logo", "")}
                          for p in _load_favorites()]}
