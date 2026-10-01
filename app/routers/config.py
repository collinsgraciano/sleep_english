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
    load_config, save_config, load_all_mode_configs,
    get_active_mode, set_active_mode,
    get_default_config,
    save_preset, load_preset, delete_preset,
    list_sleep_color_presets, save_sleep_color_preset, delete_sleep_color_preset,
)
from ..paths import CHANNEL_ASSETS_DIR, PIPELINE_DIR

router = APIRouter()

# 库绑定键（片头/片尾库页面管理，非参数页表单字段）：save_all 整文件覆盖
# 时若不清保留，绑定会被静默清空（「🌙 用于 Sleep」绑定丢失、生成回退默认片头）
LIBRARY_BINDING_KEYS = ("sleep_intro_video", "sleep_outro_video")


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
    # 保留库绑定键（sleep_intro_video / sleep_outro_video）：由片头/片尾库
    # 页面管理、不在参数页表单字段里，整文件覆盖会静默清空绑定
    _, existing = _ctx(channel)
    for k in LIBRARY_BINDING_KEYS:
        if k not in data and k in existing:
            data[k] = existing[k]
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

# 背景图开关开启但无可用固定路径（留空/失效）时，预览改用内置示例图演示
# 混合/层级效果；成片路径留空时仍按本期主题 AI 生成，不受影响。
SAMPLE_BG_PATH = PIPELINE_DIR / "sleep" / "assets" / "preview_bg_sample.jpg"

_SLEEP_SAMPLE_A = {"text": "The house is quiet now",
                   "phonetic": "/ðə haʊs ɪz ˈkwaɪət naʊ/",
                   "zh": "房子現在安靜下來了"}
_SLEEP_SAMPLE_B = {"text": "Time to close your eyes",
                   "phonetic": "/taɪm tə kloʊz jɔːr aɪz/",
                   "zh": "該閉上眼睛了"}


def _resolve_preview_logo(cfg: dict, channel: str) -> str:
    """预览 Logo 路径解析（探测顺序与成片 pipeline.py 一致）。

    总开关关 = 不叠；显式路径优先（文件缺失成片也只跳过，这里原样返回由
    盖章端静默处理）；显式留空且带频道上下文 → 自动探测
    configs/channel_assets/{频道id}/logo.png。找不到返回空。
    命中后过 logo_cutout 抠透明底再返回（与成片 ensure_logo_cutout 同源
    缓存，预览所见即成片所得）。"""
    path = ""
    if cfg.get("sleep_logo", True):
        explicit = str(cfg.get("sleep_logo_path", "") or "").strip()
        if explicit:
            path = explicit
        else:
            channel = str(channel or "").strip()
            # 仅接受纯目录名，防路径穿越（与频道 id 形态一致）
            if channel and Path(channel).name == channel:
                cand = CHANNEL_ASSETS_DIR / channel / "logo.png"
                if cand.exists():
                    path = str(cand)
    if path:
        try:
            from logo_cutout import ensure_logo_cutout
            path = ensure_logo_cutout(path)
        except Exception:
            pass  # 抠图模块不可用时退回原图叠加（行为同旧行为）
    return path


def _stamp_logo_on_card(img, logo_path: str, position: str, size: int,
                        opacity: int, pos_x: float = 92.0,
                        pos_y: float = 6.0) -> None:
    """按成片 overlay 数学把 Logo 盖到预览卡上（原位修改 RGBA 图）。

    复刻 video_compose_sleep._logo_overlay：宽=s（k=图高/720）、边距
    m=0.35*s、四角定位、alpha=opacity/100 夹取 0.1~1.0（乘进原 alpha
    通道，保留 PNG 形状透明，等价 FFmpeg colorchannelmixer）。"""
    from PIL import Image

    try:
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        return
    k = img.height / 720.0
    s = max(8, round(size * k))
    m = max(4, round(size * 0.35 * k))
    w = s
    h = max(1, round(logo.height * s / max(1, logo.width)))
    if w >= img.width or h >= img.height:
        return
    logo = logo.resize((w, h))
    alpha = min(1.0, max(0.1, float(opacity or 90) / 100.0))
    r, g, b, a = logo.split()
    a = a.point(lambda v: int(v * alpha))
    logo = Image.merge("RGBA", (r, g, b, a))
    if position == "top_left":
        xy = (m, m)
    elif position == "bottom_left":
        xy = (m, img.height - h - m)
    elif position == "bottom_right":
        xy = (img.width - w - m, img.height - h - m)
    elif position == "custom":
        # 任意位置：Logo 左上角在可移动空间（画幅-Logo）内的百分比（与成片
        # FFmpeg overlay 表达式同源：int 截断 ≈ ffmpeg 整数化）
        fx = min(100.0, max(0.0, float(pos_x))) / 100.0
        fy = min(100.0, max(0.0, float(pos_y))) / 100.0
        xy = (int((img.width - w) * fx), int((img.height - h) * fy))
    else:  # top_right（默认）
        xy = (img.width - w - m, m)
    img.alpha_composite(logo, xy)


def _render_sleep_preview_png(cfg: dict, channel: str = "") -> bytes:
    """intro/pair/outro 三卡纵向合成 → PNG bytes（同步渲染，跑线程池）。

    channel 仅用于 Logo 自动探测（成片同一探测顺序）；Logo 盖章是预览
    专用注入（build_theme 不携带 logo 键），成片卡片渲染路径零影响。"""
    from PIL import Image

    from sleep.sleep_cards import (build_theme, render_intro_card,
                                   render_pair_card, render_outro_card)

    cfg = cfg or {}
    theme = build_theme(cfg)
    # 背景图开启但无可用固定路径（留空/文件失效）→ 示例图占位，
    # 让不透明度/层级混合效果在预览可见（此前静默回退渐变底易误判）
    bg_path = str(theme.get("bg_image_path", "") or "").strip()
    if theme.get("bg_image") and not (bg_path and Path(bg_path).exists()):
        if SAMPLE_BG_PATH.exists():
            theme["bg_image_path"] = str(SAMPLE_BG_PATH)
    logo_path = _resolve_preview_logo(cfg, channel)
    channel_name = str(cfg.get("sleep_channel_name", "") or "")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        intro = render_intro_card(theme, str(base / "intro.png"), channel_name=channel_name)
        pair = render_pair_card(_SLEEP_SAMPLE_A, _SLEEP_SAMPLE_B, 1, theme,
                                str(base / "pair.png"), channel_name=channel_name)
        outro = render_outro_card(theme, str(base / "outro.png"),
                                  str(cfg.get("sleep_outro_text", "") or ""),
                                  channel_name=channel_name)
        imgs = []
        for p in (intro, pair, outro):
            im = Image.open(p)
            if logo_path:
                try:
                    _size = int(float(cfg.get("sleep_logo_size", 96) or 96))
                except (TypeError, ValueError):
                    _size = 96
                try:
                    _opacity = int(float(cfg.get("sleep_logo_opacity", 90) or 90))
                except (TypeError, ValueError):
                    _opacity = 90
                try:
                    _pos_x = float(cfg.get("sleep_logo_pos_x", 92.0) or 92.0)
                except (TypeError, ValueError):
                    _pos_x = 92.0
                try:
                    _pos_y = float(cfg.get("sleep_logo_pos_y", 6.0) or 6.0)
                except (TypeError, ValueError):
                    _pos_y = 6.0
                im = im.convert("RGBA")
                _stamp_logo_on_card(im, logo_path,
                                    str(cfg.get("sleep_logo_position", "") or "top_right"),
                                    _size, _opacity, pos_x=_pos_x, pos_y=_pos_y)
                im = im.convert("RGB")
            imgs.append(im)
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
    png = await asyncio.to_thread(_render_sleep_preview_png, config, channel)
    return Response(content=png, media_type="image/png")


@router.post("/api/config/sleep_preview")
async def api_sleep_preview_post(request: Request):
    """配置页实时预览：body = 表单收集的 sleep_* 键值（未保存草稿值亦可）；
    ?channel= 频道上下文，决定 Logo 留空时的自动探测来源。"""
    data = await request.json()
    channel = str(request.query_params.get("channel", "") or "").strip()
    cfg = {k: v for k, v in data.items()
           if isinstance(k, str) and k.startswith("sleep_")}
    png = await asyncio.to_thread(_render_sleep_preview_png, cfg, channel)
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
