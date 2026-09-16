"""HTML 页面路由 — 全部 14 个管理界面页面（渲染层，依赖最多）."""
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..config_manager import (
    GROUP_META, MODES, MODE_LABELS, MODE_SHORT_LABELS, PARAM_SPEC,
    RECYCLE_DIRNAME, LEGACY_RECYCLE_DIRNAME,
    DEFAULT_QUICK_FIELDS, EXCLUDED_QUICK_KEYS, load_all_quick_fields,
    effective_param_spec, find_run_dir, get_active_mode, get_provider_options,
    iter_run_dirs, list_presets, load_all_mode_configs, load_config,
    load_llm_providers, load_mode_config, set_active_mode,
    SLEEP_VISUAL_KEYS, SLEEP_ORCHESTRATION_KEYS,
)
from ..paths import TRASH_META_FILENAME
from ..pipeline_service import get_service
from ..templating import templates
import style_manager as style_lib
from .ai_test import _load_ai_test_config
from .runs import _THUMB_NAME_RE, _resolve_main_thumbnail, _resolve_video_copy_paths

router = APIRouter()

# 组内步骤序列编辑器（/arrangement 页）的步骤中文标签
SLEEP_STEP_LABELS = {
    "a_m": "A句 · 男声常速",
    "a_slow": "A句 · 女声慢速",
    "b_m": "B句 · 男声常速",
    "b_slow": "B句 · 女声慢速",
    "b_f": "B句 · 女声常速",
    "combo": "AB 连贯",
}

# 预览渲染会消费、但归属「内容编排」页编辑的文案字段 —— 画面页以隐藏域
# 参与预览提交与合并保存，避免预览/保存回落默认文案
SLEEP_PREVIEW_HIDDEN_FIELDS = ("sleep_channel_name", "sleep_outro_text")


# ===========================================================================
# Page routes
# ===========================================================================

def _config_page_context(mode: str) -> dict:
    """参数配置页 / Sleep 睡前短句页 共用的渲染上下文组装。"""
    if mode and mode in MODES:
        set_active_mode(mode)
    mode = get_active_mode()
    config = load_mode_config(mode)
    presets = list_presets()
    # Inject dynamic LLM provider options into PARAM_SPEC
    PARAM_SPEC["llm_provider"]["options"] = get_provider_options()
    # Inject visual style options (built-in + custom styles)
    style_opts = style_lib.get_style_options()
    cur_style = config.get("visual_style", "pixar3d")
    if cur_style and cur_style not in style_opts:
        # 自定义风格已删除：诚实显示，让用户重新选择
        style_opts = {cur_style: f"{cur_style}（已失效，请重新选择）", **style_opts}
    PARAM_SPEC["visual_style"]["options"] = style_opts
    # 按模式过滤：只渲染该模式实际消费的参数（PARAM_SPEC "modes" 标注）
    mode_spec = effective_param_spec(mode)
    # Group params by group
    grouped = {}
    for key, spec in mode_spec.items():
        if key == "structure":
            continue  # 结构由 Tab 决定，不渲染下拉
        g = spec["group"]
        if g not in grouped:
            grouped[g] = []
        grouped[g].append((key, spec, config.get(key, spec["default"])))
    # Sort groups by order
    sorted_groups = sorted(grouped.items(), key=lambda x: GROUP_META.get(x[0], {}).get("order", 99))
    # sleep 组色盘「留空=内置默认」显示色（来自 pipeline/sleep/sleep_cards）
    try:
        from sleep.sleep_cards import color_defaults
        sleep_color_defaults = color_defaults()
    except Exception:
        sleep_color_defaults = {}
    return {
        "config": config,
        "params": PARAM_SPEC,
        "grouped": sorted_groups,
        "group_meta": GROUP_META,
        "presets": presets,
        "mode": mode,
        "mode_labels": MODE_LABELS,
        "sleep_color_defaults": sleep_color_defaults,
        # 自定义 Provider 模型列表（不含 api_key 等敏感字段；去重保持顺序）
        "custom_providers": [
            {"id": p.get("id", ""), "name": p.get("name", ""),
             "models": list(dict.fromkeys(p.get("models") or []))}
            for p in load_llm_providers()
        ],
    }


def _sleep_page_grouped(mode: str, keys: frozenset) -> tuple[dict, list]:
    """专项页上下文：全量配置上下文中仅保留 sleep 组内属于 keys 的参数。

    返回 (ctx, grouped)；非 sleep 组不带入专项页（完整清单见「参数配置」页）。
    """
    ctx = _config_page_context(mode)
    grouped = []
    for g, params in ctx["grouped"]:
        if g == "sleep":
            kept = [(k, s, v) for k, s, v in params if k in keys]
            if kept:
                grouped.append((g, kept))
    return ctx, grouped


@router.get("/sleep", response_class=HTMLResponse)
async def sleep_config_page(request: Request, mode: str = ""):
    """😴 Sleep 睡前短句：画面样式专项页（原配置页 sleep 组同款布局 ——
    左侧 sticky 实时预览 + 右侧参数网格），只放影响画面的设置。"""
    ctx, grouped = _sleep_page_grouped(mode, SLEEP_VISUAL_KEYS)
    config = ctx["config"]
    ctx.update({
        "active_page": "sleep",
        "grouped": grouped,
        "sleep_inline_preview": True,
        # 部分参数页 → 合并保存（/api/config/save），防止覆盖未渲染参数
        "config_save_all": False,
        "sleep_hidden_fields": [
            (k, str(config.get(k, "") or "")) for k in SLEEP_PREVIEW_HIDDEN_FIELDS],
    })
    return templates.TemplateResponse(request, "sleep_config.html", ctx)


@router.get("/arrangement", response_class=HTMLResponse)
async def arrangement_page(request: Request, mode: str = ""):
    """📋 内容编排：组内步骤序列可视化编辑器 + 节奏/结构/播报文案参数。"""
    ctx, grouped = _sleep_page_grouped(mode, SLEEP_ORCHESTRATION_KEYS)
    config = ctx["config"]
    # sleep_sequence 由上方可视化编辑器承载，分组网格不重复渲染原始 JSON 框
    grouped = [(g, [(k, s, v) for k, s, v in ps if k != "sleep_sequence"])
               for g, ps in grouped]
    # 服务端解析当前序列（损坏/留空 → None，前端回落默认结构展示）
    try:
        from sleep.timeline_sleep import parse_sleep_sequence
        sequence = parse_sleep_sequence(str(config.get("sleep_sequence", "") or ""))
    except Exception:
        sequence = None
    ctx.update({
        "active_page": "arrangement",
        "grouped": grouped,
        # 组卡标签用「编排参数」，避免与页面标题「内容编排」重复且与画面页混淆
        "group_meta": {**GROUP_META, "sleep": {"label": "编排参数", "icon": "📋", "order": 9}},
        "sleep_inline_preview": False,
        "config_save_all": False,
        "sleep_sequence_effective": sequence,
        "sleep_step_labels": SLEEP_STEP_LABELS,
        "sleep_gap_params": {
            "short": config.get("sleep_gap_short", 1.0),
            "long": config.get("sleep_gap_long", 2.0),
            "pair": config.get("sleep_pair_gap", 3.0),
        },
    })
    return templates.TemplateResponse(request, "arrangement.html", ctx)


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, mode: str = ""):
    # ?mode= 切换 Tab：同步激活模式并渲染该模式配置
    ctx = _config_page_context(mode)
    return templates.TemplateResponse(request, "config.html", {
        **ctx,
        "active_page": "config",
    })


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    service = get_service()
    config = load_config()
    mode_configs = load_all_mode_configs()
    # 快捷启动「画面风格」下拉选项（内置+自定义；各模式当前值不在选项中时兜底显示）
    style_options = style_lib.get_style_options()
    for mcfg in mode_configs.values():
        vs = mcfg.get("visual_style")
        if vs and vs not in style_options:
            style_options[vs] = f"{vs}（已失效，请重新选择）"
    # 「常用配置」面板动态渲染上下文：全参数规格（前端按模式过滤渲染/挑选）
    quick_spec = {}
    for key, spec in PARAM_SPEC.items():
        if key in EXCLUDED_QUICK_KEYS:
            continue
        entry = {"label": spec.get("label", key), "type": spec.get("type", "text"),
                 "group": spec.get("group", ""), "default": spec.get("default", ""),
                 "modes": spec.get("modes"), "help": spec.get("help", "")}
        if key == "llm_provider":
            entry["options"] = get_provider_options()
        elif key == "visual_style":
            entry["options"] = style_options
        elif key == "subtitle_style":
            entry["options"] = {"": "跟随参数配置（默认）",
                                **subtitle_style_lib.get_style_options()}
        elif "options" in spec:
            entry["options"] = spec["options"]
        quick_spec[key] = entry
    return templates.TemplateResponse(request, "dashboard.html", {
        "config": config,
        "runner": service,
        "active_page": "dashboard",
        "mode_configs": mode_configs,
        "active_mode": get_active_mode(),
        "mode_labels": MODE_LABELS,
        "style_options": style_options,
        "quick_spec": quick_spec,
        "quick_fields_map": load_all_quick_fields(),
        "quick_group_meta": GROUP_META,
        "quick_default_fields": DEFAULT_QUICK_FIELDS,
    })


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, mode: str = ""):
    # ?mode= 切换 Tab：同步激活模式并渲染该模式配置
    if mode and mode in MODES:
        set_active_mode(mode)
    mode = get_active_mode()
    config = load_mode_config(mode)
    presets = list_presets()
    # Inject dynamic LLM provider options into PARAM_SPEC
    PARAM_SPEC["llm_provider"]["options"] = get_provider_options()
    # Inject visual style options (built-in + custom styles)
    style_opts = style_lib.get_style_options()
    cur_style = config.get("visual_style", "pixar3d")
    if cur_style and cur_style not in style_opts:
        # 自定义风格已删除：诚实显示，让用户重新选择
        style_opts = {cur_style: f"{cur_style}（已失效，请重新选择）", **style_opts}
    PARAM_SPEC["visual_style"]["options"] = style_opts
    # 按模式过滤：只渲染该模式实际消费的参数（PARAM_SPEC "modes" 标注）
    mode_spec = effective_param_spec(mode)
    # Group params by group
    grouped = {}
    for key, spec in mode_spec.items():
        if key == "structure":
            continue  # 结构由 Tab 决定，不渲染下拉
        g = spec["group"]
        if g not in grouped:
            grouped[g] = []
        grouped[g].append((key, spec, config.get(key, spec["default"])))
    # Sort groups by order
    sorted_groups = sorted(grouped.items(), key=lambda x: GROUP_META.get(x[0], {}).get("order", 99))
    # sleep 组色盘「留空=内置默认」显示色（来自 pipeline/sleep/sleep_cards）
    try:
        from sleep.sleep_cards import color_defaults
        sleep_color_defaults = color_defaults()
    except Exception:
        sleep_color_defaults = {}
    return templates.TemplateResponse(request, "config.html", {
        "config": config,
        "params": PARAM_SPEC,
        "grouped": sorted_groups,
        "group_meta": GROUP_META,
        "presets": presets,
        "active_page": "config",
        "mode": mode,
        "mode_labels": MODE_LABELS,
        "sleep_color_defaults": sleep_color_defaults,
        # 自定义 Provider 模型列表（不含 api_key 等敏感字段；去重保持顺序）
        "custom_providers": [
            {"id": p.get("id", ""), "name": p.get("name", ""),
             "models": list(dict.fromkeys(p.get("models") or []))}
            for p in load_llm_providers()
        ],
    })


@router.get("/topics", response_class=HTMLResponse)
async def topics_page(request: Request):
    config = load_config()
    topics_file = config.get("topics_file", "")
    topics_data = {}
    if topics_file and Path(topics_file).exists():
        try:
            topics_data = json.loads(Path(topics_file).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    used_file = config.get("used_topics_file", "")
    if not used_file:
        output_dir = config.get("output_dir", "./output")
        used_file = str(Path(output_dir) / "used_topics.json")
    used_topics = []
    if Path(used_file).exists():
        try:
            used_topics = json.loads(Path(used_file).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    return templates.TemplateResponse(request, "topics.html", {
        "topics_data": topics_data,
        "used_topics": used_topics,
        "topics_file": topics_file,
        "active_page": "topics",
    })


@router.get("/runs/{name}/gallery", response_class=HTMLResponse)
async def gallery_page(request: Request, name: str, mode: str = ""):
    config = load_config()
    output_dir = Path(config.get("output_dir", "./output"))
    # 运行卡片带 ?mode= 消歧；查不到时保持原「渲染空页面」行为
    run_dir = find_run_dir(output_dir, name, mode) or (output_dir / name)
    script_path = run_dir / "script.json"
    script = {}
    if script_path.exists():
        try:
            script = json.loads(script_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # YouTube metadata: prefer final youtube_metadata.json (real chapter timestamps
    # + hashtags injected in step 4.5), fallback to raw script fields
    yt_meta = {}
    yt_meta_path = run_dir / "youtube_metadata.json"
    if yt_meta_path.exists():
        try:
            yt_meta = json.loads(yt_meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    images_dir = run_dir / "images"
    images = sorted([f.name for f in images_dir.glob("*.png")]) if images_dir.exists() else []
    clips_dir = run_dir / "clips"
    clips = sorted([f.name for f in clips_dir.glob("*.mp4")]) if clips_dir.exists() else []
    audio_dir = run_dir / "audio"
    audio = sorted([f.name for f in audio_dir.glob("*.mp3")]) if audio_dir.exists() else []

    # Final videos are in work_dir root (not videos/ subdir which has intermediates)
    videos = []
    for v in sorted(run_dir.glob("*.mp4")):
        # Skip intermediate files
        if v.name.startswith("final_no_sub") or v.name.startswith("final_video_norm"):
            continue
        videos.append(v.name)
    # Also check videos/ dir for any extras
    videos_subdir = run_dir / "videos"
    if videos_subdir.exists():
        for v in sorted(videos_subdir.glob("*.mp4")):
            if v.name not in videos and not v.name.startswith("final_no_sub") and not v.name.startswith("final_video_norm"):
                videos.append(v.name)

    # 画廊页操作按钮所需状态：已上传标记 + 主缩略图绝对路径（无缩略图传空串）
    thumb = _resolve_main_thumbnail(run_dir)
    # 「一键复制 4K / 4K BGM 路径」按钮所需成片绝对路径（缺失为空串 → 按钮禁用）
    copy_paths = _resolve_video_copy_paths(run_dir)

    return templates.TemplateResponse(request, "gallery.html", {
        "run_name": name,
        "uploaded": (run_dir / "uploaded.flag").exists(),
        "thumb_path": str(thumb) if thumb.exists() else "",
        "path_4k": copy_paths["4k"],
        "path_4k_bgm": copy_paths["4k_bgm"],
        "script": script,
        "images": images,
        "clips": clips,
        "audio": audio,
        "videos": videos,
        "has_yt_meta": bool(yt_meta),
        "yt_title": yt_meta.get("title") or script.get("youtube_title", ""),
        "yt_title_en": yt_meta.get("title_en") or script.get("youtube_title_en", ""),
        "yt_desc": yt_meta.get("description") or script.get("youtube_description", ""),
        "yt_desc_en": yt_meta.get("description_en") or script.get("youtube_description_en", ""),
        "yt_tags": yt_meta.get("tags") or script.get("youtube_tags", []),
        "yt_options": yt_meta.get("title_options", []),
        "active_page": "runs",
    })


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    config = load_config()
    output_dir = Path(config.get("output_dir", "./output"))
    runs = []
    for d in iter_run_dirs(output_dir):
        script_path = d / "script.json"
        videos_dir = d / "videos"
        thumbnail = _resolve_main_thumbnail(d)
        thumb_count = sum(1 for f in d.glob("thumbnail*.jpg")
                          if _THUMB_NAME_RE.fullmatch(f.name))
        has_4k = any(d.glob("*_4K.mp4"))
        copy_paths = _resolve_video_copy_paths(d)
        run_info = {
            "name": d.name,
            "path": str(d),
            "created": d.stat().st_mtime,
            "has_script": script_path.exists(),
            "has_thumbnail": thumb_count > 0,
            # mtime_ns 版本参数：删图/换主图后 URL 变化，绕开浏览器缓存
            "thumbnail_url": (f"/api/runs/{d.name}/thumbnail?v={thumbnail.stat().st_mtime_ns}"
                              if thumb_count > 0 else ""),
            "thumbnail_count": thumb_count,
            "uploaded": (d / "uploaded.flag").exists(),
            "has_4k": has_4k,
            # 「一键复制 4K / 4K BGM 路径」所需成片绝对路径（缺失为空串 → 按钮禁用）
            "copy_4k": copy_paths["4k"],
            "copy_4k_bgm": copy_paths["4k_bgm"],
            "structure": "",
        }
        # Find video files — final videos are in work_dir root, not videos/
        video_files = []
        for v in d.glob("*.mp4"):
            if v.name.startswith("final_no_sub") or v.name.startswith("final_video_norm"):
                continue
            video_files.append({
                "name": v.name,
                "size_mb": round(v.stat().st_size / (1024*1024), 1),
                "url": f"/api/runs/{d.name}/video/{v.name}",
            })
        # Also check videos/ subdir for intermediates (but don't show them as main)
        run_info["videos"] = video_files
        # Load script metadata
        if script_path.exists():
            try:
                script = json.loads(script_path.read_text(encoding="utf-8"))
                run_info["title"] = script.get("youtube_title", script.get("title", d.name))
                run_info["title_en"] = script.get("youtube_title_en", "")
                run_info["cefr"] = script.get("cefr", "")
                run_info["structure"] = script.get("structure", "")
                run_info["channel_id"] = str(script.get("channel_id", "") or "")
            except (json.JSONDecodeError, OSError):
                run_info["title"] = d.name
        else:
            run_info["title"] = d.name
        run_info.setdefault("channel_id", "")
        # 卡片模式徽标：脚本缺 structure 时回退所在模式文件夹名
        if run_info["structure"] not in MODES:
            run_info["structure"] = d.parent.name if d.parent.name in MODES else ""
        run_info["structure_label"] = MODE_SHORT_LABELS.get(run_info["structure"],
                                                            run_info["structure"])
        runs.append(run_info)

    # 回收站列表（_recycle_bin 下所有已删除运行；兼容旧版 .recycle_bin）
    trash_runs = []
    for recycle_root in (output_dir / RECYCLE_DIRNAME, output_dir / LEGACY_RECYCLE_DIRNAME):
        if not recycle_root.is_dir():
            continue
        for d in sorted(recycle_root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            item = {
                "name": d.name,
                "original_name": d.name,
                "deleted_at": d.stat().st_mtime,
                "title": d.name,
                "structure_label": "",
            }
            meta_path = d / TRASH_META_FILENAME
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    item["deleted_at"] = meta.get("deleted_at", item["deleted_at"])
                    item["original_name"] = meta.get("original_name", d.name)
                except (json.JSONDecodeError, OSError):
                    pass
            structure = ""
            script_path = d / "script.json"
            if script_path.exists():
                try:
                    s = json.loads(script_path.read_text(encoding="utf-8"))
                    item["title"] = s.get("youtube_title", s.get("title", d.name))
                    structure = s.get("structure", "")
                except (json.JSONDecodeError, OSError):
                    pass
            item["structure_label"] = MODE_SHORT_LABELS.get(structure, structure)
            trash_runs.append(item)

    # 频道筛选条（频道矩阵）：id → 展示名（已删除频道不出现在筛选条）
    from ..channel_profiles import list_channels
    channel_labels = {c["id"]: (c.get("name_en") or c["id"])
                      for c in list_channels()}
    return templates.TemplateResponse(request, "runs.html", {
        "runs": runs,
        "trash_runs": trash_runs,
        "mode_labels": MODE_LABELS,
        "channel_labels": channel_labels,
        "active_page": "runs",
    })


@router.get("/scripts", response_class=HTMLResponse)
async def scripts_page(request: Request):
    config = load_config()
    return templates.TemplateResponse(request, "scripts.html", {
        "config": config,
        "active_page": "scripts",
    })


@router.get("/voices", response_class=HTMLResponse)
async def voices_page(request: Request):
    """QwenTTS voice management page."""
    config = load_config()
    return templates.TemplateResponse(request, "voices.html", {
        "config": config,
        "active_page": "voices",
    })


@router.get("/kokoro_voices", response_class=HTMLResponse)
async def kokoro_voices_page(request: Request):
    """Kokoro voice management page."""
    config = load_config()
    return templates.TemplateResponse(request, "kokoro_voices.html", {
        "config": config,
        "active_page": "kokoro_voices",
    })


# ===========================================================================
@router.get("/moss_voices", response_class=HTMLResponse)
async def moss_voices_page(request: Request):
    """MOSS-TTS-Nano voice management page."""
    config = load_config()
    return templates.TemplateResponse(request, "moss_voices.html", {
        "config": config,
        "active_page": "moss_voices",
    })


# ===========================================================================
@router.get("/ai_test", response_class=HTMLResponse)
async def ai_test_page(request: Request):
    config = load_config()
    ai_cfg = _load_ai_test_config()
    return templates.TemplateResponse(request, "ai_test.html", {
        "config": config,
        "active_page": "ai_test",
        "system_prompt": ai_cfg.get("system_prompt", ""),
        # Provider / 模型清单唯一数据源（与 PARAM_SPEC 同步，避免前端硬编码漂移）
        "provider_options": get_provider_options(),
        "sensenova_models": PARAM_SPEC["sensenova_model"]["options"],
        "openai_models": PARAM_SPEC["openai_model"]["options"],
        "gemini_models": PARAM_SPEC["gemini_model"]["options"],
    })


# ===========================================================================
@router.get("/channel_factory", response_class=HTMLResponse)
async def channel_factory_page(request: Request):
    """频道工坊：LLM 批量生成频道信息 + 收藏 + Logo/Banner 生成。"""
    config = load_config()
    return templates.TemplateResponse(request, "channel_factory.html", {
        "config": config,
        "active_page": "channel_factory",
    })


@router.get("/channels", response_class=HTMLResponse)
async def channels_page(request: Request):
    """频道矩阵：频道实体管理（品牌/视听 overrides）+ 独立主题库 + 矩阵填充。"""
    config = load_config()
    return templates.TemplateResponse(request, "channels.html", {
        "config": config,
        "active_page": "channels",
    })


@router.get("/intro_videos", response_class=HTMLResponse)
async def intro_videos_page(request: Request):
    """片头库：sleep 模式 10 秒片头生成（本地动画 / AI 视频）+ 入库管理。"""
    config = load_config()
    return templates.TemplateResponse(request, "intro_videos.html", {
        "config": config,
        "active_page": "intro_videos",
    })


# ===========================================================================
