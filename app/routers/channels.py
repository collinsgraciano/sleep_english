"""频道矩阵 API — 频道实体 CRUD / 工坊收藏转正 / 主题库 / 矩阵填充队列.

数据流：
- 频道实体 configs/channels/{cid}.json（app/channel_profiles.py 存储层）
- /fill_queue：对每个 active 频道从其主题库随机抽 N 个未用主题入批量队列
  （队列项带 channel_id，_build_config 开始时叠加频道 overrides）
- 主题库 AI 生成复用 topics_ai.generate_topics，hint 自动注入频道定位
"""
import asyncio
import json
import random
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ..batch_queue_service import get_batch_queue
from ..channel_profiles import (
    OVERRIDABLE_KEYS, brand_colors_to_sleep_overrides,
    create_channel_from_favorite, default_topics_file,
    default_used_topics_file, delete_channel, generate_channel_id, get_channel,
    list_channels, normalize_channel, resolve_topics_files, save_channel,
    set_channel_status,
)
from ..config_manager import load_config
from ..paths import CHANNEL_ASSETS_DIR
from ..sse import sse_line as _sse, SSE_HEADERS as _SSE_HEADERS
from .. import topics_ai
from .channel_factory import _ID_RE as _FAV_ID_RE, _load_favorites

router = APIRouter()

_INT_OVERRIDE_KEYS = {"sleep_pairs", "sleep_font_scale", "sleep_bg_opacity"}


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
    """手动新建频道：{name_en, name_zh?, niche?, audience?, brand_colors?}。"""
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
    # 新建预置：频道名 + 独立主题域（与收藏转正同款默认）
    profile["overrides"].setdefault("sleep_channel_name", profile["name_en"])
    profile["overrides"].setdefault("topics_file", default_topics_file(profile["id"]))
    profile["overrides"].setdefault("used_topics_file", default_used_topics_file(profile["id"]))
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
    """更新频道（整档覆盖保存）：品牌字段 + overrides（白名单过滤）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    cid = str(data.get("id", "") or "").strip()
    existing = get_channel(cid)
    if existing is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    merged = {**existing, **{k: v for k, v in data.items() if k != "overrides"}}
    overrides = dict(existing.get("overrides") or {})
    raw_overrides = data.get("overrides")
    if isinstance(raw_overrides, dict):
        # 整档语义：以提交值为准（空串=清除该覆盖项，回落全局）
        overrides = {k: v for k, v in raw_overrides.items() if k in OVERRIDABLE_KEYS}
    merged["overrides"] = overrides
    merged["id"] = existing["id"]  # id 不可变（输出归属/素材目录依赖）
    merged["created"] = existing["created"]
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


@router.post("/api/channels/brand_colors_map")
async def api_channels_brand_colors_map(request: Request):
    """brand_colors → sleep_color_* 映射预览（编辑页「由品牌色生成配色」）。"""
    try:
        data = await request.json()
    except Exception:
        data = {}
    colors = (data if isinstance(data, dict) else {}).get("brand_colors") or []
    if not isinstance(colors, list) or len(colors) != 3:
        return JSONResponse({"ok": False, "error": "需要 3 个品牌色"}, status_code=400)
    return {"ok": True, "colors": brand_colors_to_sleep_overrides(
        [str(c) for c in colors])}


@router.get("/api/channels/{cid}/asset/{kind}")
async def api_channel_asset(cid: str, kind: str):
    """频道 Logo/Banner（转正后 id 沿用工坊收藏 id，素材目录天然对齐）。"""
    if kind not in ("logo", "banner"):
        return JSONResponse({"ok": False, "error": "无效素材类型"}, status_code=400)
    channel = get_channel(cid)
    if channel is None:
        return JSONResponse({"ok": False, "error": "频道不存在"}, status_code=404)
    path = CHANNEL_ASSETS_DIR / cid / f"{kind}.png"
    if not path.exists():
        return JSONResponse({"ok": False, "error": "素材尚未生成（在频道工坊生成后自动关联）"},
                            status_code=404)
    return FileResponse(path, media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


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
        items = [{"type": "topic", "mode": "sleep", "topic": t,
                  "channel_id": c["id"],
                  "cefr": str(c.get("overrides", {}).get("cefr", "") or "")}
                 for t in picked]
        added, item_rejected = queue.add_items(items)
        enqueued.extend(added)
        rejected.extend({"channel": c["name_en"], "reason": r.get("reason", "")}
                        for r in item_rejected)
    return {"ok": bool(enqueued), "enqueued": len(enqueued), "items": enqueued,
            "rejected": rejected}


@router.get("/api/channels/meta")
async def api_channels_meta():
    """频道矩阵页辅助元数据：可覆盖键清单 + 全部工坊收藏（供导入下拉）。"""
    return {"ok": True,
            "overridable_keys": list(OVERRIDABLE_KEYS),
            "favorites": [{"id": p.get("id", ""), "name_en": p.get("name_en", ""),
                           "name_zh": p.get("name_zh", ""), "niche": p.get("niche", ""),
                           "logo": p.get("logo", "")}
                          for p in _load_favorites()]}
