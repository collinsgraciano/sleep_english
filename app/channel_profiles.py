"""频道实体（Channel Profile）—— 频道矩阵的存储、配置合成与工坊收藏转正。

数据模型（configs/channels/{channel_id}.json，一频道一文件）：
  品牌字段（name_en/name_zh/handle/slogan/description_*/niche/audience/tags/
  brand_colors/brand_style/logo/banner）沿用频道工坊 favorites 的 profile 结构；
  status: active | paused；overrides: 仅显式差异项（空值键不覆盖全局配置）。

配置合成：resolve_run_config(mode, cid) = load_mode_config(mode) + overrides 非空叠加，
返回完整 config dict（不落盘）。channel_id 为空或频道不存在时原样返回 ——
「不选频道」行为与现状完全一致（新增层，不改写原链路）。
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .paths import CHANNELS_DIR, CHANNEL_FAVORITES_PATH, WEB_ROOT

# 合法频道 id（与频道工坊 profile id 同前缀，手动/转正共用）
_CHANNEL_ID_RE = re.compile(r"^ch_[A-Za-z0-9_]+$")

# overrides 允许覆盖的配置键白名单（照 sleep 组参数子集；不在名单的键一律忽略）
OVERRIDABLE_KEYS: tuple[str, ...] = (
    # 频道身份（卡片/播报）
    "sleep_channel_name", "sleep_outro_text",
    # 画面
    "sleep_show_leaves", "sleep_handwrite_font", "sleep_font_scale",
    "sleep_color_bg_top", "sleep_color_bg_bottom", "sleep_color_card",
    "sleep_color_card_border", "sleep_color_en_a", "sleep_color_en_b",
    "sleep_color_phonetic", "sleep_color_zh", "sleep_color_num",
    "sleep_color_badge_bg", "sleep_color_badge_text", "sleep_color_channel",
    "sleep_color_leaf",
    "sleep_bg_image", "sleep_bg_image_path", "sleep_bg_opacity",
    "sleep_intro", "sleep_intro_video",
    # 内容
    "cefr", "sleep_pairs",
    "topics_file", "used_topics_file",
    # 音色（空=现有 build_voice_map 行为）
    "sleep_voice_male", "sleep_voice_female",
    # 生图开关（按频道关缩略图省积分）
    "no_thumbnail",
)

_CHANNEL_OVERRIDE_INT_KEYS = {"sleep_pairs", "sleep_font_scale", "sleep_bg_opacity"}


def generate_channel_id() -> str:
    return f"ch_{int(time.time() * 1000)}"


# ===========================================================================
# 存储层
# ===========================================================================

def _channel_path(channel_id: str) -> Path:
    return CHANNELS_DIR / f"{channel_id}.json"


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _filter_overrides(raw: Any) -> dict[str, Any]:
    """overrides 白名单过滤 + 轻量规范化（空串剔除；数值键安全转 int）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in OVERRIDABLE_KEYS:
        if key not in raw:
            continue
        v = raw[key]
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
        if key in _CHANNEL_OVERRIDE_INT_KEYS:
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
        out[key] = v
    return out


def normalize_channel(raw: dict) -> dict | None:
    """频道实体规范化；name_en 缺失视为无效返回 None。"""
    if not isinstance(raw, dict):
        return None
    name_en = str(raw.get("name_en", "") or "").strip()
    if not name_en:
        return None
    cid = str(raw.get("id", "") or "").strip()
    if not _CHANNEL_ID_RE.match(cid):
        cid = generate_channel_id()
    status = str(raw.get("status", "") or "").strip() or "active"
    if status not in ("active", "paused"):
        status = "active"
    handle = str(raw.get("handle", "") or "").strip().replace(" ", "").lower()
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    colors = []
    for c in (raw.get("brand_colors") or [])[:3]:
        c = str(c or "").strip()
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", c):
            colors.append(c if c.startswith("#") else f"#{c}")
    while len(colors) < 3:
        colors.append(["#4F46E5", "#F59E0B", "#F8FAFC"][len(colors)])
    return {
        "id": cid,
        "name_en": name_en,
        "name_zh": str(raw.get("name_zh", "") or "").strip(),
        "handle": handle,
        "slogan": str(raw.get("slogan", "") or "").strip(),
        "description_en": str(raw.get("description_en", "") or "").strip(),
        "description_zh": str(raw.get("description_zh", "") or "").strip(),
        "niche": str(raw.get("niche", "") or "").strip(),
        "audience": str(raw.get("audience", "") or "").strip(),
        "tags": [str(t).strip() for t in (raw.get("tags") or [])
                 if t is not None and str(t).strip()],
        "brand_colors": colors,
        "brand_style": str(raw.get("brand_style", "") or "").strip(),
        "logo": str(raw.get("logo", "") or "").strip(),
        "banner": str(raw.get("banner", "") or "").strip(),
        "status": status,
        "created": float(raw.get("created", 0) or time.time()),
        "source_favorite_id": str(raw.get("source_favorite_id", "") or "").strip(),
        "overrides": _filter_overrides(raw.get("overrides")),
    }


def list_channels() -> list[dict]:
    """全部频道实体（按创建时间升序）。单文件损坏跳过。"""
    if not CHANNELS_DIR.is_dir():
        return []
    out: list[dict] = []
    for f in CHANNELS_DIR.glob("ch_*.json"):
        p = normalize_channel(_load_json(f, {}))
        if p:
            out.append(p)
    out.sort(key=lambda p: (p.get("created", 0), p.get("id", "")))
    return out


def get_channel(channel_id: str) -> dict | None:
    if not channel_id or not _CHANNEL_ID_RE.match(channel_id):
        return None
    p = normalize_channel(_load_json(_channel_path(channel_id), {}))
    return p


def save_channel(raw: dict) -> dict:
    """创建/更新频道实体（规范化 + 原子写），返回规范化结果。"""
    profile = normalize_channel(raw)
    if profile is None:
        raise ValueError("频道缺少 name_en，无法保存")
    _atomic_write(_channel_path(profile["id"]), profile)
    return profile


def delete_channel(channel_id: str) -> bool:
    if not _CHANNEL_ID_RE.match(channel_id):
        return False
    path = _channel_path(channel_id)
    if not path.exists():
        return False
    path.unlink()
    return True


def set_channel_status(channel_id: str, status: str) -> dict | None:
    profile = get_channel(channel_id)
    if profile is None:
        return None
    profile["status"] = status if status in ("active", "paused") else "active"
    _atomic_write(_channel_path(profile["id"]), profile)
    return profile


# ===========================================================================
# 频道专属内容路径（独立主题域）
# ===========================================================================

def default_topics_file(channel_id: str) -> str:
    return str(CHANNELS_DIR / channel_id / "topics.json")


def default_used_topics_file(channel_id: str) -> str:
    return str(CHANNELS_DIR / channel_id / "used_topics.json")


def resolve_topics_files(channel: dict) -> tuple[str, str]:
    """频道主题域 (topics_file, used_topics_file)：overrides 显式设置优先，
    否则用频道专属默认路径（保证频道间主题互不干扰）。"""
    cid = channel["id"]
    ov = channel.get("overrides") or {}
    topics_file = str(ov.get("topics_file", "") or "").strip() or default_topics_file(cid)
    used_file = str(ov.get("used_topics_file", "") or "").strip() or default_used_topics_file(cid)
    return topics_file, used_file


# ===========================================================================
# 配置合成（核心枢纽）
# ===========================================================================

def resolve_run_config(mode: str, channel_id: str = "") -> dict[str, Any]:
    """mode_sleep.json 深合并频道 overrides → 本次运行完整配置。

    - channel_id 为空 / 频道不存在 / status=paused → 原样返回全局配置
      （paused 频道不可被新运行意外使用，由调用方决定是否提示）
    - overrides 仅覆盖白名单内非空键；sleep_channel_name 强制品牌化
      （未显式设置时用 name_en —— 频道名是卡片/播报的声学标签，
      绝不回落全局，防止 A 频道的名字跑进 B 频道的成片）；
      topics_file/used_topics_file 强制按频道隔离（显式 override 优先，
      否则频道专属默认路径），避免各频道互烧公共主题池
    """
    from .config_manager import load_mode_config
    config = load_mode_config(mode)
    channel = get_channel(channel_id) if channel_id else None
    if channel is None:
        return config
    overrides = channel.get("overrides") or {}
    for key in OVERRIDABLE_KEYS:
        if key in overrides:
            config[key] = overrides[key]
    # 频道名强制品牌化（声学标签属于该频道）
    config["sleep_channel_name"] = (
        str(overrides.get("sleep_channel_name", "") or "").strip() or channel["name_en"])
    # 主题域强制按频道隔离
    topics_file, used_file = resolve_topics_files(channel)
    config["topics_file"] = topics_file
    config["used_topics_file"] = used_file
    # 运行级标记：_build_args/_step0 落 script.json、quick_test 过滤都依赖它
    config["channel_id"] = channel["id"]
    return config


# ===========================================================================
# 工坊收藏 → 频道转正（brand_colors → sleep 色板映射）
# ===========================================================================

def _parse_rgb(value: str) -> tuple[int, int, int] | None:
    v = str(value or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", v):
        return None
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        max(0, min(255, rgb[0])), max(0, min(255, rgb[1])), max(0, min(255, rgb[2])))


def _mix(c: str, other: str, t: float) -> str:
    a, b = _parse_rgb(c), _parse_rgb(other)
    if a is None or b is None:
        return c
    return _to_hex(tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)))


def _darken(c: str, factor: float) -> str:
    a = _parse_rgb(c)
    if a is None:
        return c
    return _to_hex(tuple(round(v * (1 - factor)) for v in a))


def _lighten(c: str, factor: float) -> str:
    return _mix(c, "#ffffff", factor)


def _luma(c: str) -> float:
    a = _parse_rgb(c)
    if a is None:
        return 0.5
    return (0.299 * a[0] + 0.587 * a[1] + 0.114 * a[2]) / 255


def _ensure_dark(c: str, max_luma: float = 0.60) -> str:
    """文本色保底加深：白卡上必须可读。"""
    out = c
    for _ in range(6):
        if _luma(out) <= max_luma:
            break
        out = _darken(out, 0.18)
    return out


def _ensure_light(c: str, min_luma: float = 0.80) -> str:
    """背景色保底提亮：保持睡前柔和观感。"""
    out = c
    for _ in range(6):
        if _luma(out) >= min_luma:
            break
        out = _lighten(out, 0.18)
    return out


def brand_colors_to_sleep_overrides(colors: list[str]) -> dict[str, str]:
    """brand_colors [primary, accent, background] → sleep_color_* 差异项。

    原则：白卡不变（正文底色恒白保证可读），品牌三色分摊到
    边框/A句/B句/序号/角标/频道名/渐变背景；文本色加深、背景色提亮。
    """
    primary = str(colors[0] if len(colors) > 0 else "#4F46E5")
    accent = str(colors[1] if len(colors) > 1 else "#F59E0B")
    background = str(colors[2] if len(colors) > 2 else "#F8FAFC")
    return {
        "sleep_color_bg_top": _ensure_light(_lighten(background, 0.35)),
        "sleep_color_bg_bottom": _ensure_light(
            _lighten(_mix(background, primary, 0.18), 0.15)),
        "sleep_color_card_border": primary,
        "sleep_color_en_a": _ensure_dark(_darken(primary, 0.45)),
        "sleep_color_en_b": _ensure_dark(_darken(accent, 0.15)),
        "sleep_color_phonetic": _ensure_dark(_darken(_mix(primary, accent, 0.5), 0.30)),
        "sleep_color_num": accent,
        "sleep_color_badge_bg": accent,
        "sleep_color_badge_text": "#FFFFFF",
        "sleep_color_channel": _ensure_dark(_darken(primary, 0.35)),
        "sleep_color_leaf": _ensure_light(_mix(background, primary, 0.30), 0.72),
    }


def create_channel_from_favorite(favorite_id: str) -> dict:
    """频道工坊收藏一键转正为频道实体。

    - 品牌字段整体继承；brand_colors 映射为 sleep 色板 overrides
    - 频道 id 直接复用收藏 id（素材目录 configs/channel_assets/{id}/ 与
      logo/banner 字段名天然对齐，零迁移）；重复转正幂等（按
      source_favorite_id 找回已有实体，保留用户编辑过的 overrides）
    - overrides 预置：频道名（name_en）、独立主题域路径（topics/used）
    """
    favorites = _load_json(CHANNEL_FAVORITES_PATH, {}).get("profiles", [])
    fav = next((p for p in favorites if isinstance(p, dict)
                and str(p.get("id", "")) == favorite_id), None)
    if fav is None:
        raise ValueError(f"频道工坊收藏不存在: {favorite_id}")

    existing = next((c for c in list_channels()
                     if c.get("source_favorite_id") == favorite_id
                     or c.get("id") == favorite_id), None)
    base_id = favorite_id if _CHANNEL_ID_RE.match(favorite_id) else generate_channel_id()

    profile = normalize_channel({**fav, "id": base_id})
    if profile is None:
        raise ValueError("收藏缺少 name_en，无法转正为频道")
    profile["source_favorite_id"] = favorite_id
    if existing:
        # 重复转正：品牌字段刷新自收藏，但 overrides/状态以已有实体为准
        # （收藏源不带 overrides，直接沿用会把用户编辑抹掉）
        profile["overrides"] = existing.get("overrides") or {}
        profile["status"] = existing.get("status", "active")
    else:
        # 新建：品牌色自动映射 + 频道名/独立主题域预置
        overrides = brand_colors_to_sleep_overrides(profile["brand_colors"])
        overrides.setdefault("sleep_channel_name", profile["name_en"])
        overrides.setdefault("topics_file", default_topics_file(base_id))
        overrides.setdefault("used_topics_file", default_used_topics_file(base_id))
        profile["overrides"] = _filter_overrides(overrides)
    return save_channel(profile)
