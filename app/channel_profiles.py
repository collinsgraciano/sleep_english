"""频道实体（Channel Profile）—— 频道矩阵的存储、配置快照与工坊收藏转正。

数据模型（configs/channels/{channel_id}.json，一频道一文件）：
  品牌字段（name_en/name_zh/handle/slogan/description_*/niche/audience/tags/
  brand_colors/brand_style/logo/banner）沿用频道工坊 favorites 的 profile 结构；
  status: active | paused；
  config: **完整配置快照**（整套 PARAM_SPEC，2026-09-16 起）——创建/转正时
  从全局 mode_sleep.json 深拷贝种子，此后完全独立演化；overrides 为旧格式
  差异项（仅作读取迁移源，不再新增）。

配置快照：load_channel_config(cid) 返回频道私有完整配置（defaults 补全 +
structure/channel_id 盖章）；旧格式频道（无 config 节）读取时按
「全局 + overrides」迁移并惰性写回。save_channel_config 整档写回。
sync_channel_config(cid, scope) 从全局按参数组回填（身份键永不同步）。
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .paths import CHANNELS_DIR, CHANNEL_FAVORITES_PATH
from .config_manager import SLEEP_GROUPS

# 合法频道 id（与频道工坊 profile id 同前缀，手动/转正共用）
_CHANNEL_ID_RE = re.compile(r"^ch_[A-Za-z0-9_]+$")

# overrides 允许覆盖的配置键白名单 —— 仅作旧格式（无 config 节）频道的
# 读取迁移源；2026-09-16 起频道持有完整配置快照，不再新增 overrides
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
    "sleep_bg_style_mode", "sleep_bg_allow_people",
    "sleep_intro", "sleep_intro_video", "sleep_outro_video",
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
    config = raw.get("config")
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
        # 完整配置快照（2026-09-16 起；旧格式频道无此节 → 读取时自迁移）
        "config": config if isinstance(config, dict) and config else {},
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
    """频道主题域 (topics_file, used_topics_file)：config 快照显式值优先，
    其次旧格式 overrides，否则频道专属默认路径（频道间互不干扰）。"""
    cid = channel["id"]
    cfg = channel.get("config") or {}
    ov = channel.get("overrides") or {}
    topics_file = (str(cfg.get("topics_file", "") or "").strip()
                   or str(ov.get("topics_file", "") or "").strip()
                   or default_topics_file(cid))
    used_file = (str(cfg.get("used_topics_file", "") or "").strip()
                 or str(ov.get("used_topics_file", "") or "").strip()
                 or default_used_topics_file(cid))
    return topics_file, used_file


# ===========================================================================
# 配置快照（核心枢纽）：每频道一份完整独立配置
# ===========================================================================

# 同步守卫：频道身份键从全局同步时永不被覆盖
IDENTITY_SYNC_GUARD: frozenset[str] = frozenset({
    "sleep_channel_name", "sleep_outro_text",
    "topics_file", "used_topics_file",
    "channel_id", "structure",
})

# 同步范围 → 参数组（None = 全部组）
SYNC_SCOPES: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "credentials": ("llm", "mcp"),
    "content": ("content",),
    # Sleep 六分类分组全集（config_manager.SLEEP_GROUPS）
    "visual": tuple(sorted(SLEEP_GROUPS)),
    "bgm": ("bgm", "bgm_amix", "bgm_sidechain"),
}


def _seed_channel_topics(global_topics_file: str, channel_topics_file: str) -> bool:
    """新频道主题库种子：全局库整体拷贝到频道专属路径（仅当目标不存在）。

    任何 I/O/JSON 异常静默降级（返回 False），不阻塞频道创建；
    路径为空/相同（异常配置）自动跳过，防止把全局库写成自身。
    """
    try:
        if not global_topics_file or not channel_topics_file:
            return False
        gp, tp = Path(global_topics_file), Path(channel_topics_file)
        if tp.exists() or not gp.exists():
            return False
        if gp.resolve() == tp.resolve():
            return False
        data = json.loads(gp.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            return False
        from .topics_io import save_topics_data
        save_topics_data(channel_topics_file, data)
        return True
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _seed_channel_config(channel_id: str, name_en: str,
                         brand_colors: list[str]) -> dict[str, Any]:
    """频道配置快照种子：当前全局配置深拷贝 + 品牌色映射 + 身份键。

    创建/转正时调用一次；此后频道配置完全独立演化（快照语义）。
    同时把全局主题库种子到频道专属 topics.json（此后独立演化）。
    """
    from .config_manager import load_mode_config
    cfg = dict(load_mode_config("sleep"))
    cfg.update(brand_colors_to_sleep_overrides(brand_colors))
    cfg["sleep_channel_name"] = name_en
    global_topics_file = str(cfg.get("topics_file", "") or "").strip()
    cfg["topics_file"] = default_topics_file(channel_id)
    cfg["used_topics_file"] = default_used_topics_file(channel_id)
    cfg["channel_id"] = channel_id
    cfg["structure"] = "sleep"
    _seed_channel_topics(global_topics_file, cfg["topics_file"])
    return cfg


def load_channel_config(channel_id: str) -> dict[str, Any]:
    """频道私有完整配置（defaults 补全 + structure/channel_id 盖章）。

    旧格式频道（无 config 节）按「全局 + overrides」迁移，并惰性写回
    config 节 —— 存量频道首次读取即完成升级，无需手动迁移。
    频道不存在时回落全局（调用方语义与 load_config 一致）。
    """
    from .config_manager import get_default_config, load_mode_config
    channel = get_channel(channel_id) if channel_id else None
    if channel is None:
        return load_mode_config("sleep")
    snapshot = channel.get("config") or {}
    if snapshot:
        merged = {**get_default_config(), **snapshot}
    else:
        # --- 旧格式迁移：全局 + overrides 非空叠加（原 resolve_run_config 语义）---
        merged = load_mode_config("sleep")
        overrides = channel.get("overrides") or {}
        for key in OVERRIDABLE_KEYS:
            if key in overrides:
                merged[key] = overrides[key]
        merged["sleep_channel_name"] = (
            str(overrides.get("sleep_channel_name", "") or "").strip()
            or channel["name_en"])
        topics_file, used_file = resolve_topics_files(channel)
        merged["topics_file"] = topics_file
        merged["used_topics_file"] = used_file
        save_channel_config(channel_id, merged)  # 惰性写回，完成升级
    merged["structure"] = "sleep"
    merged["channel_id"] = channel_id
    return merged


def save_channel_config(channel_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """整档写回频道配置快照（defaults 补全缺失键 + 身份盖章 + 原子写）。"""
    from .config_manager import get_default_config
    channel = get_channel(channel_id)
    if channel is None:
        raise ValueError(f"频道不存在: {channel_id}")
    merged = {**get_default_config(), **(config or {})}
    merged["structure"] = "sleep"
    merged["channel_id"] = channel_id
    channel["config"] = merged
    _atomic_write(_channel_path(channel_id), channel)
    return merged


def sync_channel_config(channel_id: str, scope: str = "all") -> dict[str, Any]:
    """从全局 mode_sleep.json 按参数组回填频道快照。

    身份键（IDENTITY_SYNC_GUARD）永不同步 —— 频道名/主题域/归属
    属于频道自身，不会被全局改动抹掉。
    """
    from .config_manager import PARAM_SPEC, load_mode_config
    groups = SYNC_SCOPES.get(scope)
    if scope not in SYNC_SCOPES:
        raise ValueError(f"未知同步范围: {scope}")
    global_cfg = load_mode_config("sleep")
    channel_cfg = load_channel_config(channel_id)
    for key, spec in PARAM_SPEC.items():
        if groups is not None and spec.get("group") not in groups:
            continue
        if key in IDENTITY_SYNC_GUARD:
            continue
        channel_cfg[key] = global_cfg.get(key, spec.get("default"))
    return save_channel_config(channel_id, channel_cfg)


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

    - 品牌字段整体继承；brand_colors 映射写入配置快照 sleep_color_*
    - 频道 id 直接复用收藏 id（素材目录 configs/channel_assets/{id}/ 与
      logo/banner 字段名天然对齐，零迁移）；重复转正幂等（按
      source_favorite_id 找回已有实体，保留用户编辑过的 config/状态）
    - 新转正：完整配置快照种子（全局深拷贝 + 品牌色 + 身份键）
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
        # 重复转正：品牌字段刷新自收藏，但 config/overrides/状态以已有实体为准
        # （收藏源不带 config，直接沿用会把用户编辑抹掉）
        profile["config"] = existing.get("config") or {}
        profile["overrides"] = existing.get("overrides") or {}
        profile["status"] = existing.get("status", "active")
    else:
        # 新建：完整配置快照种子（全局 + 品牌色映射 + 身份键）
        profile["config"] = _seed_channel_config(
            base_id, profile["name_en"], profile["brand_colors"])
    return save_channel(profile)
