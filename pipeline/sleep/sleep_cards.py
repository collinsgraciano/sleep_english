"""sleep 模式画面卡片：纯 Pillow 渲染（截图 1:1 布局，配色/文案全部配置化）。

布局（1280x720，参照参考视频截图）：
浅绿渐变背景 + 角落叶片装饰 → 白色圆角描边卡片 → 左上手写体频道名 →
右上圆角「EN」徽标 → A 句块（深棕 EN / 橄榄绿 IPA / 深灰繁中）→
B 句块（橙 EN / IPA / 繁中）→ 左下粉色发光序号。
IPA 用 cambria（msyh 渲染 IPA 会变豆腐块），EN 句子用 Nunito Bold（回退 msyhbd）。
频道名按文字内容智能选字体（font_scanner 字形覆盖检测）：英文走手写体链，
中文/emoji 自动切到含对应字形的字体 —— Pillow 不做浏览器式逐字回退，
单字体硬画会把无字形字符渲染成豆腐块 □。

分辨率自适应：所有渲染函数按 scale = w / 1280 缩放全部绝对像素常量
（字号/边距/圆角/叶片/光晕），原生 4K（3840x2160，scale=3）时文字像素级
清晰；scale=1（720p 默认）输出与历史版本逐像素一致。
"""
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from font_scanner import covered_count, font_covers
from media_utils import FONT_EN, FONT_ZH, FONT_PH, TARGET_W, TARGET_H

_FONTS_DIR = Path(__file__).resolve().parent.parent / "fonts"
_NUNITO = str(_FONTS_DIR / "Nunito-Bold.ttf")
_FONT_EN_CARD = _NUNITO if os.path.exists(_NUNITO) else FONT_EN
# 频道名候选链：手写体 → 符号/emoji → 中文可用字体（FONT_EN=msyhbd /
# NotoCJK-Bold）。每字符取链上第一个含其字形的字体，全链皆无才落豆腐。
_FONT_HANDWRITE_CANDIDATES = [
    r"C:\Windows\Fonts\Inkfree.ttf",
    r"C:\Windows\Fonts\segoepr.ttf",
    r"C:\Windows\Fonts\seguisym.ttf",
    r"C:\Windows\Fonts\seguiemj.ttf",
]
_FONT_HANDWRITE_FALLBACKS = [*_FONT_HANDWRITE_CANDIDATES, FONT_EN, FONT_ZH]

# 默认主题（与 configs sleep_color_* 配置键一一对应；hex 取自参考截图采样）
DEFAULT_THEME = {
    "bg_top": "#eaf4e2",
    "bg_bottom": "#d9edcf",
    "card": "#ffffff",
    "card_border": "#8fb0c9",
    "en_a": "#422006",
    "en_b": "#e05a12",
    "phonetic": "#7d8c1e",
    "zh_text": "#3a3a3a",
    "num": "#ff6fa5",
    "badge_bg": "#f2b8c6",
    "badge_text": "#ffffff",
    "channel_text": "#5a6b52",
    "leaf_a": "#c9e2f2",
    "leaf_b": "#bfe0c8",
}


def _hex_rgb(value, fallback: tuple) -> tuple:
    try:
        v = str(value).strip().lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return fallback


# sleep_color_* 配置键 → 主题键（build_theme 与 color_defaults 的唯一映射来源）
CONFIG_COLOR_KEYS = {
    "sleep_color_bg_top": "bg_top", "sleep_color_bg_bottom": "bg_bottom",
    "sleep_color_card": "card", "sleep_color_card_border": "card_border",
    "sleep_color_en_a": "en_a", "sleep_color_en_b": "en_b",
    "sleep_color_phonetic": "phonetic", "sleep_color_zh": "zh_text",
    "sleep_color_num": "num", "sleep_color_badge_bg": "badge_bg",
    "sleep_color_badge_text": "badge_text", "sleep_color_channel": "channel_text",
    "sleep_color_leaf": "leaf_a",
}


def color_defaults() -> dict[str, str]:
    """sleep_color_* 配置键 → 内置默认 hex（配置页色盘「留空=默认」的显示色）。"""
    return {ck: DEFAULT_THEME[tk] for ck, tk in CONFIG_COLOR_KEYS.items()}


def build_theme(cfg: dict) -> dict:
    """配置 dict（含 sleep_color_* / sleep_show_leaves / 背景图键）→ 渲染主题 dict。"""
    cfg = cfg or {}
    theme = dict(DEFAULT_THEME)
    for ck, tk in CONFIG_COLOR_KEYS.items():
        v = str(cfg.get(ck, "") or "").strip()
        if v:
            theme[tk] = v
    theme["show_leaves"] = bool(cfg.get("sleep_show_leaves", True))
    theme["handwrite_font"] = str(cfg.get("sleep_handwrite_font", "") or "").strip()
    # 句子区字体（留空=内置默认；所选字体缺文字字形时渲染侧自动回退内置链）
    theme["font_en"] = str(cfg.get("sleep_font_en", "") or "").strip()
    theme["font_ph"] = str(cfg.get("sleep_font_ph", "") or "").strip()
    theme["font_zh"] = str(cfg.get("sleep_font_zh", "") or "").strip()
    # 句子区三字体描边假粗 0-4（与频道名 sleep_handwrite_weight 同机制；
    # 0=原样与历史渲染逐像素一致；配置缺键/非法值回落 0）
    for _key in ("font_en", "font_ph", "font_zh"):
        try:
            theme[_key + "_weight"] = min(4, max(0, int(float(
                cfg.get("sleep_" + _key + "_weight", 0) or 0))))
        except (TypeError, ValueError):
            theme[_key + "_weight"] = 0
    # 频道名字体粗细（描边假粗 0-4，任意字体生效；0=原样逐像素不变）
    try:
        theme["handwrite_weight"] = min(4, max(0, int(float(cfg.get("sleep_handwrite_weight", 0) or 0))))
    except (TypeError, ValueError):
        theme["handwrite_weight"] = 0
    # 背景图（低透明度衬底）：开关 + 固定路径 + 不透明度（渲染时路径无效自动忽略）
    theme["bg_image"] = bool(cfg.get("sleep_bg_image", False))
    theme["bg_image_path"] = str(cfg.get("sleep_bg_image_path", "") or "").strip()
    try:
        theme["bg_opacity"] = max(0.0, min(1.0, float(cfg.get("sleep_bg_opacity", 20) or 0) / 100.0))
    except (TypeError, ValueError):
        theme["bg_opacity"] = 0.2
    # 背景图层级：bottom=底层衬底（渐变之上、白卡之下，现状）；
    # top=第二级（整幅盖过白卡/边框/叶片，文字/角标/序号仍绘制在最上层）
    theme["bg_layer"] = ("top" if str(cfg.get("sleep_bg_layer", "") or "")
                         .strip().lower() == "top" else "bottom")
    # 句子区排版：font_scale=字号缩放（100=原大，clamp 60-160）；
    # line_spacing=英文行距 px@720p（默认 14，clamp 0-48）；
    # letter_spacing=字距 px@720p（默认 0，clamp 0-24，作用于英文/音标/中文）
    try:
        theme["font_scale"] = min(1.6, max(0.6, float(cfg.get("sleep_font_scale", 100) or 100) / 100.0))
    except (TypeError, ValueError):
        theme["font_scale"] = 1.0
    try:
        theme["line_spacing"] = min(48, max(0, int(cfg.get("sleep_line_spacing", 14))))
    except (TypeError, ValueError):
        theme["line_spacing"] = 14
    try:
        theme["letter_spacing"] = min(24, max(0, int(cfg.get("sleep_letter_spacing", 0))))
    except (TypeError, ValueError):
        theme["letter_spacing"] = 0
    return theme


def _channel_font_chain(theme: dict) -> list[str]:
    """频道名字体候选链：自定义 → 手写体 → 符号/emoji → 中文可用字体。

    fontTools 缺失时 font_covers 恒 True（链退化为首候选=旧行为）；
    文件缺失/损坏恒 False（链上自动跳过，不会把坏路径交给 Pillow）。
    """
    custom = str(theme.get("handwrite_font", "") or "").strip()
    chain: list[str] = []
    if custom and os.path.exists(custom):
        chain.append(custom)
    chain.extend(_FONT_HANDWRITE_FALLBACKS)
    seen: set[str] = set()
    return [p for p in chain if not (p in seen or seen.add(p))]


def _channel_font_runs(theme: dict, text: str) -> list[list]:
    """频道名按字形覆盖切分：[[片段, 字体路径], ...]（保持原文字顺序）。

    每字符取链上第一个含其字形的字体；全链皆无的字符归主字体
    （首候选，极生僻字符理论仍豆腐，系统内已无字体可救）。
    """
    chain = _channel_font_chain(theme)
    primary = chain[0]
    runs: list[list] = []
    for ch in text:
        path = next((p for p in chain if font_covers(p, ch)), primary)
        if runs and runs[-1][1] == path:
            runs[-1][0] += ch
        else:
            runs.append([ch, path])
    return runs


def _pick_font(custom: str, fallbacks: list[str], text: str = "") -> str:
    """字体链的「单字体最优解」：自定义优先，全覆盖者优先按序，否则覆盖最多。

    text 为空=链首（等价旧行为）。句子区三字体（EN/IPA/中文）与频道名
    单字体场景共用；频道名逐段混排走 _channel_font_runs。
    """
    chain: list[str] = []
    if custom and os.path.exists(custom):
        chain.append(custom)
    for p in fallbacks:
        if p not in chain:
            chain.append(p)
    if not text or not text.strip():
        return chain[0]
    for p in chain:
        if font_covers(p, text):
            return p
    return max(chain, key=lambda p: covered_count(p, text))


def _handwrite_path(theme: dict, text: str = "") -> str:
    """频道名字体链单字体最优解（intro_video 片头文字层沿用）。"""
    return _pick_font(str(theme.get("handwrite_font", "") or ""),
                      _FONT_HANDWRITE_FALLBACKS, text)


def _theme_font(theme: dict, key: str, fallbacks: list[str], text: str) -> str:
    """主题字体键（font_en/font_ph/font_zh）→ 覆盖回退后的单字体路径。"""
    return _pick_font(str(theme.get(key, "") or ""), fallbacks, text)


def _tracked_bbox_w(draw: ImageDraw.ImageDraw, text: str, font,
                    spacing: float, stroke: int = 0) -> float:
    """带字距/描边的文本显示宽度（stroke>0 时 textbbox 含描边外扩）。

    stroke=0 与历史调用逐像素一致。"""
    if stroke:
        box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    else:
        box = draw.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    if spacing and len(text) > 1:
        w += spacing * (len(text) - 1)
    return w


# ---------------------------------------------------------------------------
# 渲染缓存：背景渐变/叶片/背景图/白卡边框与字号字体在主题不变时逐卡重复，
# 整幅缓存 + copy 后绘制（叶片坐标为固定常量，缓存前后输出逐字节一致）。
# 缓存键含全部影响画面的主题字段；容量上限防配置预览页反复调色时累积。
# ---------------------------------------------------------------------------
_CARD_BASE_CACHE: dict[tuple, Image.Image] = {}
_CARD_BASE_CACHE_MAX = 4
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_CACHE_MAX = 256


def _cached_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    """(路径, 字号) → 字体对象缓存（_fit_font 逐档试号时免重复加载字体文件）。"""
    key = (path, int(size))
    f = _FONT_CACHE.get(key)
    if f is None:
        if len(_FONT_CACHE) >= _FONT_CACHE_MAX:
            _FONT_CACHE.pop(next(iter(_FONT_CACHE)))
        f = ImageFont.truetype(path, int(size))
        _FONT_CACHE[key] = f
    return f


def _draw_tracked(draw: ImageDraw.ImageDraw, xy: tuple, text: str, font,
                  fill, spacing: float, stroke: int = 0) -> None:
    """带字距绘制：逐字符推进 x；spacing<=0 走整串 draw.text 保持像素一致。

    stroke>0 时描边假粗（stroke_fill=fill 同色描边，机制与频道名一致）；
    stroke=0 不传描边参数，输出与历史逐字节一致。"""
    x, y = xy
    if not spacing or len(text) <= 1:
        if stroke:
            draw.text((x, y), text, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=fill)
        else:
            draw.text((x, y), text, font=font, fill=fill)
        return
    for ch in text:
        if stroke:
            draw.text((x, y), ch, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=fill)
        else:
            draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _fit_font(draw: ImageDraw.ImageDraw, text: str, font_path: str,
              start_size: int, min_size: int, max_w: int,
              spacing: float = 0.0, stroke: int = 0) -> ImageFont.FreeTypeFont:
    size = start_size
    while size > min_size:
        font = _cached_font(font_path, size)
        if _tracked_bbox_w(draw, text, font, spacing, stroke) <= max_w:
            return font
        size -= 4
    return _cached_font(font_path, min_size)


def _wrap_words(draw: ImageDraw.ImageDraw, text: str, font, max_w: int,
                spacing: float = 0.0, stroke: int = 0) -> list[str]:
    words = text.split()
    if not words:
        [""]
    lines, cur = [], words[0]
    for wd in words[1:]:
        trial = f"{cur} {wd}"
        if _tracked_bbox_w(draw, trial, font, spacing, stroke) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    lines.append(cur)
    return lines


def _draw_text_block(img: Image.Image, draw: ImageDraw.ImageDraw, en: str,
                     phonetic: str, zh: str, cy: int, en_color, theme: dict,
                     en_start: int = 72, s: float = 1.0) -> None:
    """居中一块：EN 大字 → IPA → 繁中。s = 分辨率缩放系数。

    排版来自 theme：font_scale（字号缩放，1.0=原大）、line_spacing（英文
    行距 px@720p，默认 14）、letter_spacing（字距 px@720p，默认 0，>0 时
    逐字符绘制，作用于英文/音标/中文）；font_en/font_ph/font_zh 为用户所选
    字体（缺文字字形自动回退内置链）。默认值与历史渲染逐像素一致。
    """
    w = img.width
    fs = float(theme.get("font_scale", 1.0) or 1.0)
    sp = float(theme.get("letter_spacing", 0) or 0) * s
    max_w = w - int(240 * s)
    # 三字体描边假粗（0-4）：按初始字号估宽（封顶随字号），缩号后按实际
    # 字号重算绘制描边；档位 0 不注入描边（与历史渲染逐像素一致）
    en_stroke_est = _stroke_px(int(theme.get("font_en_weight", 0)),
                               int(en_start * s * fs), s)
    en_font = _fit_font(draw, en, _theme_font(theme, "font_en",
                                              [_FONT_EN_CARD, FONT_EN], en),
                        int(en_start * s * fs), int(30 * s), max_w,
                        spacing=sp, stroke=en_stroke_est)
    en_lines = _wrap_words(draw, en, en_font, max_w, spacing=sp,
                           stroke=en_stroke_est)
    line_h = en_font.size + int(theme.get("line_spacing", 14) * s)
    ph_font = _cached_font(_theme_font(theme, "font_ph", [FONT_PH], phonetic),
                           int(34 * s * fs)) if phonetic else None
    zh_font = _cached_font(_theme_font(theme, "font_zh", [FONT_ZH], zh),
                           int(42 * s * fs)) if zh else None
    # 实际绘制描边（按各自最终字号封顶；0=无描边）
    en_stroke = _stroke_px(int(theme.get("font_en_weight", 0)), en_font.size, s)
    ph_stroke = (_stroke_px(int(theme.get("font_ph_weight", 0)),
                            ph_font.size, s) if phonetic else 0)
    zh_stroke = (_stroke_px(int(theme.get("font_zh_weight", 0)),
                            zh_font.size, s) if zh else 0)
    ph_h = (ph_font.size + int(10 * s)) if phonetic else 0
    zh_h = (zh_font.size + int(12 * s)) if zh else 0
    y = cy - (len(en_lines) * line_h + ph_h + zh_h) / 2
    for ln in en_lines:
        tw = _tracked_bbox_w(draw, ln, en_font, sp, stroke=en_stroke)
        _draw_tracked(draw, ((w - tw) / 2, y), ln, en_font, en_color, sp,
                      stroke=en_stroke)
        y += line_h
    if phonetic:
        tw = _tracked_bbox_w(draw, phonetic, ph_font, sp, stroke=ph_stroke)
        _draw_tracked(draw, ((w - tw) / 2, y), phonetic, ph_font,
                      _hex_rgb(theme["phonetic"], (125, 140, 30)), sp,
                      stroke=ph_stroke)
        y += ph_h
    if zh:
        tw = _tracked_bbox_w(draw, zh, zh_font, sp, stroke=zh_stroke)
        _draw_tracked(draw, ((w - tw) / 2, y), zh, zh_font,
                      _hex_rgb(theme["zh_text"], (58, 58, 58)), sp,
                      stroke=zh_stroke)


def _draw_leaf(base: Image.Image, cx: int, cy: int, lw: int, lh: int,
               angle: float, color: tuple, s: float = 1.0) -> None:
    lw, lh = int(lw * s), int(lh * s)
    leaf = Image.new("RGBA", (lw * 3, lh * 3), (0, 0, 0, 0))
    ld = ImageDraw.Draw(leaf)
    cx0, cy0 = lw, lh
    ld.ellipse([cx0, cy0, cx0 + lw, cy0 + lh], fill=color,
               outline=(255, 255, 255, 210), width=max(1, int(2 * s)))
    ld.line([cx0 + lw // 2, cy0 + 2, cx0 + lw // 2, cy0 + lh - 2],
            fill=(255, 255, 255, 170), width=max(1, int(2 * s)))
    leaf = leaf.rotate(angle, expand=True, resample=Image.BICUBIC)
    base.paste(leaf, (int(cx - leaf.width / 2), int(cy - leaf.height / 2)), leaf)


def _draw_leaves(base: Image.Image, theme: dict, s: float = 1.0) -> None:
    if not theme.get("show_leaves", True):
        return
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ca = (*_hex_rgb(theme["leaf_a"], (201, 226, 242)), 215)
    cb = (*_hex_rgb(theme["leaf_b"], (191, 224, 200)), 215)
    _draw_leaf(layer, 70 * s, 645 * s, 95, 46, -35, ca, s)
    _draw_leaf(layer, 28 * s, 600 * s, 82, 42, -10, ca, s)
    _draw_leaf(layer, 115 * s, 690 * s, 88, 44, -60, ca, s)
    _draw_leaf(layer, 1212 * s, 655 * s, 98, 48, 30, cb, s)
    _draw_leaf(layer, 1256 * s, 608 * s, 82, 40, 60, cb, s)
    _draw_leaf(layer, 1168 * s, 698 * s, 88, 42, 10, cb, s)
    base.paste(layer, (0, 0), layer)


def _bg_cover_rgba(base: Image.Image, path: str) -> Image.Image | None:
    """载入背景图并 cover 缩放居中裁剪到 base 尺寸，返回 RGBA（失败 None）。"""
    if not path or not os.path.exists(path):
        print(f"  [SleepCards] 背景图不存在，忽略: {path}")
        return None
    try:
        img = Image.open(path).convert("RGB")
        scale = max(base.width / img.width, base.height / img.height)
        nw = max(1, int(img.width * scale + 0.5))
        nh = max(1, int(img.height * scale + 0.5))
        img = img.resize((nw, nh), Image.LANCZOS)
        x = (nw - base.width) // 2
        y = (nh - base.height) // 2
        img = img.crop((x, y, x + base.width, y + base.height))
        return img.convert("RGBA")
    except Exception as e:  # noqa: BLE001 — 任何图片问题都回退纯渐变
        print(f"  [SleepCards] WARNING: 背景图加载失败（忽略）: {e}")
        return None


def _blend_bg_image(base: Image.Image, theme: dict) -> Image.Image:
    """背景图低透明度混合（bottom 层级）：cover 铺满 → 按 bg_opacity 叠在渐变之上。

    路径无效/开启失败静默回退原底（背景图是增强功能）。
    """
    path = str(theme.get("bg_image_path", "") or "")
    opacity = float(theme.get("bg_opacity", 0.2))
    if not path or opacity <= 0:
        return base
    overlay = _bg_cover_rgba(base, path)
    if overlay is None:
        return base
    overlay.putalpha(int(255 * min(1.0, opacity)))
    out = base.convert("RGBA")
    out.alpha_composite(overlay)
    return out.convert("RGB")


def _overlay_bg_image(img: Image.Image, theme: dict) -> None:
    """背景图第二级（top 层级）：整幅叠在白卡/边框/叶片之上。

    文字/角标/序号在调用方随后绘制，仍处于最上层。不透明度沿用
    sleep_bg_opacity（第二级建议 40-100 才有「整幅背景」效果）。
    """
    path = str(theme.get("bg_image_path", "") or "")
    opacity = float(theme.get("bg_opacity", 0.2))
    if not path or opacity <= 0:
        return
    cover = _bg_cover_rgba(img, path)
    if cover is None:
        return
    cover.putalpha(int(255 * min(1.0, opacity)))
    img.alpha_composite(cover)


def _draw_background(w: int, h: int, theme: dict, s: float = 1.0) -> Image.Image:
    top = _hex_rgb(theme["bg_top"], (234, 244, 226))
    bottom = _hex_rgb(theme["bg_bottom"], (217, 237, 207))
    base = Image.new("RGB", (w, h))
    for y in range(h):
        t = y / max(1, h - 1)
        base.paste(tuple(int(top[c] + (bottom[c] - top[c]) * t) for c in range(3)),
                   [0, y, w, y + 1])
    if theme.get("bg_image") and theme.get("bg_layer", "bottom") != "top":
        base = _blend_bg_image(base, theme)
    _draw_leaves(base, theme, s)
    return base


def _card_base_key(theme: dict, w: int, h: int, s: float) -> tuple:
    """模板缓存键：覆盖影响背景/白卡画面的全部主题字段与尺寸。"""
    return (w, h, round(s, 4),
            theme.get("bg_top"), theme.get("bg_bottom"),
            theme.get("card"), theme.get("card_border"),
            bool(theme.get("bg_image")), theme.get("bg_layer", "bottom"),
            str(theme.get("bg_image_path", "") or ""),
            theme.get("bg_opacity", 20),
            bool(theme.get("show_leaves", True)),
            theme.get("leaf_a"), theme.get("leaf_b"))


def _draw_card_base(theme: dict, w: int, h: int,
                    s: float = 1.0) -> tuple[Image.Image, ImageDraw.ImageDraw, dict]:
    key = _card_base_key(theme, w, h, s)
    tmpl = _CARD_BASE_CACHE.get(key)
    if tmpl is None:
        img = _draw_background(w, h, theme, s).convert("RGBA")
        draw = ImageDraw.Draw(img)
        card = {"x0": int(w * 0.028), "y0": int(h * 0.042),
                "x1": int(w * 0.972), "y1": int(h * 0.972)}
        draw.rounded_rectangle([card["x0"], card["y0"], card["x1"], card["y1"]],
                               radius=int(36 * s), fill=_hex_rgb(theme["card"], (255, 255, 255)),
                               outline=_hex_rgb(theme["card_border"], (143, 176, 201)),
                               width=max(1, int(2 * s)))
        if theme.get("bg_image") and theme.get("bg_layer") == "top":
            # 第二级：白卡绘制后整幅叠加背景图，后续文字元素仍在其上
            _overlay_bg_image(img, theme)
        if len(_CARD_BASE_CACHE) >= _CARD_BASE_CACHE_MAX:
            _CARD_BASE_CACHE.pop(next(iter(_CARD_BASE_CACHE)))
        _CARD_BASE_CACHE[key] = img
        img = img.copy()  # 模板入库后保持素净，返回副本供本卡绘制
    else:
        img = tmpl.copy()
    return img, ImageDraw.Draw(img), {"x0": int(w * 0.028), "y0": int(h * 0.042),
                                      "x1": int(w * 0.972), "y1": int(h * 0.972)}


def _stroke_px(weight: int, size_px: int, s: float) -> int:
    """粗细档位 → 描边像素：weight×scale，并按字号的 1/18 封顶。

    描边假粗在小字号场景过度会糊死汉字计数器（36px 中文 3px 已不可读），
    故小字号高档位自动收敛到可读上限；大字号（片头卡 100px）保留全档位。
    """
    if weight <= 0:
        return 0
    return min(int(weight * s), max(1, int(size_px) // 18))


def _draw_channel(draw: ImageDraw.ImageDraw, card: dict, theme: dict,
                  channel_name: str, s: float = 1.0) -> None:
    """左上频道名：按文字内容智能选字体 + 可选描边假粗（weight 0-4）。"""
    if not channel_name:
        return
    base_size = int(36 * s)
    stroke = _stroke_px(int(theme.get("handwrite_weight", 0)), base_size, s)
    fill = _hex_rgb(theme["channel_text"], (90, 107, 82))
    x, y = card["x0"] + int(40 * s), card["y0"] + int(26 * s)
    for part, path in _channel_font_runs(theme, channel_name):
        font = _cached_font(path, base_size)
        draw.text((x, y), part, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill=fill)
        x += draw.textlength(part, font=font)


def _draw_badge(draw: ImageDraw.ImageDraw, card: dict, theme: dict,
                badge_text: str, s: float = 1.0) -> None:
    if not badge_text:
        return
    bw, bh = int(96 * s), int(66 * s)
    bx0, by0 = card["x1"] - bw, card["y0"]
    draw.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=int(14 * s),
                           fill=_hex_rgb(theme["badge_bg"], (242, 184, 198)))
    font = _cached_font(FONT_EN, int(40 * s))
    box = draw.textbbox((0, 0), badge_text, font=font)
    draw.text((bx0 + (bw - (box[2] - box[0])) / 2,
               by0 + (bh - (box[3] - box[1])) / 2 - box[1]),
              badge_text, font=font,
              fill=_hex_rgb(theme["badge_text"], (255, 255, 255)))


def _draw_number(img: Image.Image, num_text: str, theme: dict, x: int, y: int,
                 s: float = 1.0) -> None:
    """左下发光序号：模糊光晕层 + 锐利文字层。"""
    font = _cached_font(FONT_EN, int(46 * s))
    color = _hex_rgb(theme["num"], (255, 111, 165))
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.text((x, y), num_text, font=font, fill=(*color, 200))
    glow = glow.filter(ImageFilter.GaussianBlur(max(1, int(7 * s))))
    img.paste(glow, (0, 0), glow)
    ImageDraw.Draw(img).text((x, y), num_text, font=font, fill=color)


def render_pair_card(a_line: dict, b_line: dict, idx: int, theme: dict,
                     out_path: str, channel_name: str = "English with me",
                     badge_text: str = "EN", w: int = TARGET_W,
                     h: int = TARGET_H) -> str:
    """渲染一组（A+B）静态卡片 PNG。idx 为 1-based 序号。"""
    s = w / float(TARGET_W)
    img, draw, card = _draw_card_base(theme, w, h, s)
    _draw_channel(draw, card, theme, channel_name, s)
    _draw_badge(draw, card, theme, badge_text, s)
    card_top = card["y0"] + int(96 * s)
    card_h = card["y1"] - card["y0"] - int(140 * s)
    _draw_text_block(img, draw, a_line.get("text", ""), a_line.get("phonetic", ""),
                     a_line.get("zh", ""), int(card_top + card_h * 0.35),
                     _hex_rgb(theme["en_a"], (66, 32, 6)), theme, en_start=74, s=s)
    _draw_text_block(img, draw, b_line.get("text", ""), b_line.get("phonetic", ""),
                     b_line.get("zh", ""), int(card_top + card_h * 0.80),
                     _hex_rgb(theme["en_b"], (224, 90, 18)), theme, en_start=68, s=s)
    _draw_number(img, f"{idx:02d}", theme, card["x0"] + int(40 * s),
                 card["y1"] - int(86 * s), s)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def render_intro_card(theme: dict, out_path: str,
                      channel_name: str = "English with me",
                      badge_text: str = "EN", w: int = TARGET_W,
                      h: int = TARGET_H) -> str:
    """片头卡：频道名居中（手写体大字）+ 副题 + 徽标 + 叶片。"""
    s = w / float(TARGET_W)
    img, draw, card = _draw_card_base(theme, w, h, s)
    _draw_badge(draw, card, theme, badge_text, s)
    if channel_name:
        fill = _hex_rgb(theme["channel_text"], (90, 107, 82))
        weight = int(theme.get("handwrite_weight", 0))
        runs = _channel_font_runs(theme, channel_name)
        # 描边按初始字号估（封顶随字号；缩号后重算一次用于绘制）
        stroke = _stroke_px(weight, int(100 * s), s)
        max_w = w - int(320 * s) - 2 * stroke  # 描边两侧外扩，宽度预算等量收缩
        size, min_size = int(100 * s), int(40 * s)
        fonts = [(t, _cached_font(p, size)) for t, p in runs]
        while size > min_size and sum(draw.textlength(t, font=f)
                                      for t, f in fonts) > max_w:
            size -= 4
            fonts = [(t, _cached_font(p, size)) for t, p in runs]
        stroke = _stroke_px(weight, fonts[0][1].size, s)
        if len(fonts) == 1 and not stroke:
            # 单字体无描边：保持历史 bbox 居中（默认英文频道名逐像素一致）
            part, f = fonts[0]
            box = draw.textbbox((0, 0), part, font=f)
            draw.text(((w - (box[2] - box[0])) / 2,
                       h / 2 - (box[3] - box[1]) / 2 - int(40 * s)),
                      part, font=f, fill=fill)
        else:
            # 多字体（中英/emoji 混排）或带描边：按 advance 总宽居中逐段绘制
            total = sum(draw.textlength(t, font=f) for t, f in fonts)
            x = (w - total) / 2
            box = draw.textbbox((0, 0), fonts[0][0], font=fonts[0][1])
            y = h / 2 - (box[3] - box[1]) / 2 - int(40 * s)
            for part, f in fonts:
                draw.text((x, y), part, font=f, fill=fill,
                          stroke_width=stroke, stroke_fill=fill)
                x += draw.textlength(part, font=f)
    sub_font = _cached_font(_theme_font(theme, "font_zh", [FONT_ZH],
                                        "閉上眼睛 · 輕鬆聽"), int(34 * s))
    sub = "閉上眼睛 · 輕鬆聽"
    zh_stroke = _stroke_px(int(theme.get("font_zh_weight", 0)),
                           sub_font.size, s)
    sub_w = _tracked_bbox_w(draw, sub, sub_font, 0.0, stroke=zh_stroke)
    _draw_tracked(draw, ((w - sub_w) / 2, h / 2 + int(60 * s)), sub,
                  sub_font, _hex_rgb(theme["zh_text"], (58, 58, 58)), 0.0,
                  stroke=zh_stroke)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def render_outro_card(theme: dict, out_path: str, outro_text: str,
                      channel_name: str = "English with me",
                      badge_text: str = "EN", w: int = TARGET_W,
                      h: int = TARGET_H) -> str:
    """片尾卡：结束语居中 + 频道名 + 徽标。"""
    s = w / float(TARGET_W)
    img, draw, card = _draw_card_base(theme, w, h, s)
    _draw_channel(draw, card, theme, channel_name, s)
    _draw_badge(draw, card, theme, badge_text, s)
    text = (outro_text or "Thanks for listening. See you next time!").strip()
    max_w = w - int(300 * s)
    en_stroke_est = _stroke_px(int(theme.get("font_en_weight", 0)),
                               int(64 * s), s)
    en_font = _fit_font(draw, text, _theme_font(theme, "font_en",
                                                [_FONT_EN_CARD, FONT_EN], text),
                        int(64 * s), int(30 * s), max_w,
                        stroke=en_stroke_est)
    en_stroke = _stroke_px(int(theme.get("font_en_weight", 0)), en_font.size, s)
    lines = _wrap_words(draw, text, en_font, max_w, stroke=en_stroke)
    y = h / 2 - len(lines) * (en_font.size + int(14 * s)) / 2
    for ln in lines:
        tw = _tracked_bbox_w(draw, ln, en_font, 0.0, stroke=en_stroke)
        _draw_tracked(draw, ((w - tw) / 2, y), ln, en_font,
                      _hex_rgb(theme["en_a"], (66, 32, 6)), 0.0,
                      stroke=en_stroke)
        y += en_font.size + int(14 * s)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path


def render_sleep_thumbnail(script: dict, theme: dict, out_path: str,
                           badge_text: str = "EN",
                           channel_name: str = "English with me",
                           w: int = TARGET_W, h: int = TARGET_H) -> str:
    """缩略图：首组 A/B 卡片 + 顶部句数横条（thumbnail_subtitle）。"""
    s = w / float(TARGET_W)
    dialogue = script.get("dialogue", [])
    a_line = dialogue[0] if dialogue else {}
    b_line = dialogue[1] if len(dialogue) > 1 else {}
    img, draw, card = _draw_card_base(theme, w, h, s)
    _draw_channel(draw, card, theme, channel_name, s)
    _draw_badge(draw, card, theme, badge_text, s)
    _draw_text_block(img, draw, a_line.get("text", ""), a_line.get("phonetic", ""),
                     a_line.get("zh", ""), int(card["y0"] + 185 * s),
                     _hex_rgb(theme["en_a"], (66, 32, 6)), theme, en_start=62, s=s)
    _draw_text_block(img, draw, b_line.get("text", ""), b_line.get("phonetic", ""),
                     b_line.get("zh", ""), int(card["y0"] + 430 * s),
                     _hex_rgb(theme["en_b"], (224, 90, 18)), theme, en_start=56, s=s)
    strip = str(script.get("thumbnail_subtitle", "") or "").strip()
    if strip:
        st_font = _cached_font(_theme_font(theme, "font_zh", [FONT_ZH], strip),
                               int(44 * s))
        box = draw.textbbox((0, 0), strip, font=st_font)
        strip_stroke = _stroke_px(int(theme.get("font_zh_weight", 0)),
                                  st_font.size, s)
        pad_x = int(26 * s)
        sw = box[2] - box[0] + pad_x * 2
        sx = (w - sw) / 2
        draw.rounded_rectangle([sx, int(14 * s), sx + sw, int(76 * s)],
                               radius=int(14 * s),
                               fill=(*_hex_rgb(theme["num"], (255, 111, 165)), 235))
        _draw_tracked(draw, (sx + pad_x,
                             int(14 * s) + (int(62 * s) - (box[3] - box[1])) / 2 - box[1]),
                      strip, st_font, (255, 255, 255), 0.0,
                      stroke=strip_stroke)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    img.convert("RGB").save(out_path, "PNG")
    return out_path
