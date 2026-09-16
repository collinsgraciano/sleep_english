"""Config API — 参数配置 / 预设 / 模式切换."""
import asyncio
import io
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..config_manager import (
    MODES, MODE_LABELS,
    DEFAULT_QUICK_FIELDS, load_quick_fields, save_quick_fields,
    load_config, load_mode_config, save_mode_config,
    get_active_mode, set_active_mode,
    get_default_config,
    save_preset, load_preset, delete_preset,
    list_sleep_color_presets, save_sleep_color_preset, delete_sleep_color_preset,
)

router = APIRouter()


def _ctx(channel: str) -> tuple[str, dict]:
    """API 上下文解析：channel 非空 → 该频道完整配置快照；空 → 全局。

    返回 (channel_id, config)；写操作配合 _save_ctx 落回正确文件。"""
    channel = str(channel or "").strip()
    if channel:
        from ..channel_profiles import load_channel_config
        return channel, load_channel_config(channel)
    return "", load_config()


def _save_ctx(channel: str, config: dict) -> None:
    channel = str(channel or "").strip()
    if channel:
        from ..channel_profiles import save_channel_config
        save_channel_config(channel, config)
    else:
        save_config(config)


@router.post("/api/config/save")
async def api_save_config(request: Request):
    data = await request.json()
    channel = str(data.pop("channel", "") or "").strip()
    mode = data.pop("_mode", "") or get_active_mode()
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"未知模式: {mode}"}, status_code=400)
    channel, config = _ctx(channel)
    config.update(data)
    config["structure"] = mode
    _save_ctx(channel, config)
    return {"ok": True, "mode": mode, "channel": channel}


@router.post("/api/config/save_all")
async def api_save_all_config(request: Request):
    data = await request.json()
    channel = str(data.pop("channel", "") or "").strip()
    mode = data.pop("_mode", "") or get_active_mode()
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"未知模式: {mode}"}, status_code=400)
    data["structure"] = mode
    _save_ctx(channel, data)
    return {"ok": True, "mode": mode, "channel": channel}


@router.get("/api/config")
async def api_get_config(mode: str = "", channel: str = ""):
    _, config = _ctx(channel)
    return config


@router.get("/api/config/all")
async def api_get_all_configs(channel: str = ""):
    """一次性返回上下文（?channel= 或全局）配置 + 当前激活模式（控制台预载）。

    channel 非空时 modes 内为该频道完整配置快照（快捷面板/启动按频道工作）。"""
    channel, config = _ctx(channel)
    modes = load_all_mode_configs()
    modes[get_active_mode()] = config
    return {
        "active_mode": get_active_mode(),
        "active_channel": channel,
        "modes": modes,
        "mode_labels": MODE_LABELS,
    }


@router.post("/api/config/active")
async def api_set_active_mode(request: Request):
    data = await request.json()
    mode = data.get("mode", "")
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"未知模式: {mode}"}, status_code=400)
    set_active_mode(mode)
    return {"ok": True, "active_mode": mode}


@router.get("/api/config/defaults")
async def api_get_defaults():
    return get_default_config()


@router.post("/api/config/preset/save")
async def api_save_preset(request: Request):
    data = await request.json()
    name = data.get("name", "")
    config = data.get("config", {})
    if not name:
        return JSONResponse({"ok": False, "error": "名称不能为空"}, status_code=400)
    save_preset(name, config)
    return {"ok": True, "name": name}


@router.get("/api/config/preset/load/{name}")
async def api_load_preset(name: str):
    try:
        return load_preset(name)
    except FileNotFoundError:
        return JSONResponse({"error": "Preset not found"}, status_code=404)


@router.delete("/api/config/preset/{name}")
async def api_delete_preset(name: str):
    delete_preset(name)
    return {"ok": True}


# --- 控制台「常用配置」面板字段清单（每模式独立） ---

@router.get("/api/quick_config/fields")
async def api_get_quick_fields(mode: str = ""):
    m = mode if mode in MODES else get_active_mode()
    return {"mode": m, "fields": load_quick_fields(m),
            "defaults": DEFAULT_QUICK_FIELDS}


@router.post("/api/quick_config/fields")
async def api_save_quick_fields(request: Request):
    data = await request.json()
    mode = data.get("mode", "") or get_active_mode()
    if mode not in MODES:
        return JSONResponse({"ok": False, "error": f"未知模式: {mode}"}, status_code=400)
    fields = save_quick_fields(mode, data.get("fields", []))
    return {"ok": True, "mode": mode, "fields": fields}


# --- Sleep 卡片实时预览（配置页 😴 Sleep 组边改边看，真实 Pillow 渲染）---

_SLEEP_SAMPLE_A = {"text": "The house is quiet now",
                   "phonetic": "/ðə haʊs ɪz ˈkwaɪət naʊ/",
                   "zh": "房子現在安靜下來了"}
_SLEEP_SAMPLE_B = {"text": "Time to close your eyes",
                   "phonetic": "/taɪm tə kloʊz jɔːr aɪz/",
                   "zh": "該閉上眼睛了"}


def _render_sleep_preview_png(cfg: dict) -> bytes:
    """intro/pair/outro 三卡纵向合成 → PNG bytes（同步渲染，跑线程池）。"""
    from PIL import Image

    from sleep.sleep_cards import (build_theme, render_intro_card,
                                   render_outro_card, render_pair_card)

    cfg = cfg or {}
    theme = build_theme(cfg)
    channel = str(cfg.get("sleep_channel_name", "") or "")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        intro = render_intro_card(theme, str(base / "intro.png"), channel_name=channel)
        pair = render_pair_card(_SLEEP_SAMPLE_A, _SLEEP_SAMPLE_B, 1, theme,
                                str(base / "pair.png"), channel_name=channel)
        outro = render_outro_card(theme, str(base / "outro.png"),
                                  str(cfg.get("sleep_outro_text", "") or ""),
                                  channel_name=channel)
        imgs = [Image.open(p) for p in (intro, pair, outro)]
        canvas = Image.new("RGB", (max(im.width for im in imgs),
                                   sum(im.height for im in imgs)), (255, 255, 255))
        y = 0
        for im in imgs:
            canvas.paste(im, (0, y))
            y += im.height
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        return buf.getvalue()


@router.get("/api/config/sleep_preview")
async def api_sleep_preview_get(channel: str = ""):
    """用当前上下文（?channel= 频道快照 / 全局）已保存配置渲染预览。"""
    _, config = _ctx(channel)
    png = await asyncio.to_thread(_render_sleep_preview_png, config)
    return Response(content=png, media_type="image/png")


@router.post("/api/config/sleep_preview")
async def api_sleep_preview_post(request: Request):
    """配置页实时预览：body = 表单收集的 sleep_* 键值（未保存草稿值亦可）。"""
    data = await request.json()
    cfg = {k: v for k, v in data.items()
           if isinstance(k, str) and k.startswith("sleep_")}
    png = await asyncio.to_thread(_render_sleep_preview_png, cfg)
    return Response(content=png, media_type="image/png")


# --- Sleep 配色组合（配置页「🎲 随机配色」的保存/载入/删除）---

@router.get("/api/config/sleep_colors")
async def api_sleep_colors_list():
    return {"combos": list_sleep_color_presets()}


@router.post("/api/config/sleep_colors")
async def api_sleep_colors_save(request: Request):
    data = await request.json()
    name = str(data.get("name", "") or "").strip()
    colors = data.get("colors")
    if not name:
        return JSONResponse({"ok": False, "error": "名称不能为空"}, status_code=400)
    if not isinstance(colors, dict):
        return JSONResponse({"ok": False, "error": "colors 需为对象"}, status_code=400)
    safe = save_sleep_color_preset(name, colors)
    return {"ok": True, "name": safe}


@router.delete("/api/config/sleep_colors/{name}")
async def api_sleep_colors_delete(name: str):
    return {"ok": delete_sleep_color_preset(name)}
