"""画面风格库：内置风格 + prompt 解析。

style_prompt 片段会被注入到图片/缩略图的所有提示词中，保证画面风格统一。
若 web 项目存在 configs/styles_custom.json，其中的追加风格会被一并列出
（只读；本项目已无自定义风格编辑入口）。
"""
import json
from pathlib import Path
from typing import Any

# web 项目 configs/ 目录（style_manager.py 位于 pipeline/，web 根在上一级）
_WEB_ROOT = Path(__file__).parent.parent
CUSTOM_STYLES_PATH = _WEB_ROOT / "configs" / "styles_custom.json"

# 现有硬编码默认风格（pipeline.py 原文，保证 100% 等价）
DEFAULT_STYLE_PROMPT = (
    "3D cartoon style, Pixar-like, warm soft lighting, "
    "cel-shaded with thin clean black outline, "
    "vibrant saturated colors, smooth surfaces"
)

# 内置风格（builtin=true，不可编辑/删除）
BUILTIN_STYLES: list[dict[str, Any]] = [
    {
        "id": "pixar3d",
        "name": "3D 卡通（皮克斯）",
        "name_en": "Pixar 3D Cartoon",
        "description": "经典皮克斯风格 3D 卡通。温暖柔光、卡通描边、高饱和色彩，当前项目的默认验证风格，角色一致性与视频生成效果最稳定。",
        "style_prompt": "3D cartoon style, Pixar-like, warm soft lighting, cel-shaded with thin clean black outline, vibrant saturated colors, smooth surfaces",
        "thumbnail_hint": "3D Pixar-style",
        "builtin": True,
    },
    {
        "id": "anime",
        "name": "日式动漫",
        "name_en": "Japanese Anime",
        "description": "日式 2D 赛璐璐动画风。干净线稿、柔和赛璐璐上色、生动眼神。Seedream 图像模型的强项风格，海外华人观众接受度高。",
        "style_prompt": "Japanese anime style, 2D cel-shaded animation, clean line art, soft cel shading, expressive eyes, vivid colors, studio anime production quality",
        "thumbnail_hint": "Japanese anime-style",
        "builtin": True,
    },
    {
        "id": "chibi3d",
        "name": "Q版盲盒",
        "name_en": "3D Chibi Blind-box",
        "description": "3D Q版手办/盲盒风。大头小身、亮晶晶大眼、光面玩具质感，泡泡玛特式设计感，在中文圈极受欢迎，适合轻松日常题材。",
        "style_prompt": "3D chibi style, cute blind-box designer toy figure, oversized head, big sparkling eyes, glossy vinyl toy material, soft studio lighting",
        "thumbnail_hint": "3D chibi toy-style",
        "builtin": True,
    },
    {
        "id": "claymation",
        "name": "黏土动画",
        "name_en": "Claymation",
        "description": "定格动画黏土质感。手工橡皮泥肌理、圆润可爱的造型，Seedance2 视频生成动画效果自然，风格辨识度极高。",
        "style_prompt": "claymation style, stop-motion clay look, handmade plasticine texture, soft studio lighting, playful rounded shapes",
        "thumbnail_hint": "claymation-style",
        "builtin": True,
    },
    {
        "id": "flat_vector",
        "name": "扁平矢量插画",
        "name_en": "Flat Vector Illustration",
        "description": "现代扁平矢量插画。几何造型、大色块、极简阴影，YouTube 教育频道主流风格，画面干净、字幕叠加可读性最佳。",
        "style_prompt": "flat vector illustration, clean geometric shapes, bold flat colors, simple outlines, minimal shading, modern editorial style",
        "thumbnail_hint": "flat vector illustration-style",
        "builtin": True,
    },
    {
        "id": "watercolor",
        "name": "水彩绘本",
        "name_en": "Watercolor Storybook",
        "description": "儿童绘本水彩风。透明水彩晕染、柔和粉彩色调、手绘肌理，温暖治愈的教育感，适合生活化慢节奏题材。",
        "style_prompt": "children's storybook watercolor illustration, soft washes, gentle pastel palette, hand-painted texture, warm cozy feel",
        "thumbnail_hint": "watercolor storybook-style",
        "builtin": True,
    },
    {
        "id": "ghibli",
        "name": "手绘治愈动画",
        "name_en": "Hand-drawn Pastoral Anime",
        "description": "吉卜力式手绘动画。绘本感背景、自然暖光、治愈氛围，画面细节丰富，观众好感度极高。",
        "style_prompt": "hand-drawn anime style, Ghibli-inspired, painterly detailed backgrounds, warm natural lighting, gentle nostalgic atmosphere",
        "thumbnail_hint": "hand-drawn anime-style",
        "builtin": True,
    },
    {
        "id": "comic",
        "name": "美式漫画",
        "name_en": "American Comic Book",
        "description": "美式漫画风。粗黑描边、网点阴影、高饱和撞色，画面冲击力强，适合冲突/夸张表情的剧情题材。",
        "style_prompt": "American comic book style, bold ink outlines, halftone shading, dynamic saturated colors, graphic novel look",
        "thumbnail_hint": "American comic-style",
        "builtin": True,
    },
    {
        "id": "papercraft",
        "name": "剪纸纸艺",
        "name_en": "Papercraft Cut-out",
        "description": "多层剪纸纸艺风。纸张切片、层叠投影、手工纸质感，画面有独特手作纵深感。",
        "style_prompt": "layered papercraft illustration, paper cut-out shapes, subtle paper shadows, craft paper texture",
        "thumbnail_hint": "papercraft-style",
        "builtin": True,
    },
    {
        "id": "felt",
        "name": "毛毡布艺",
        "name_en": "Needle-felted Wool",
        "description": "羊毛毡手工风。绒毛纤维肌理、温暖柔和的莫兰迪色，黏土风近亲但更软萌治愈。",
        "style_prompt": "needle-felted wool style, fuzzy soft fiber texture, cozy handmade craft aesthetic, warm muted colors",
        "thumbnail_hint": "felted wool-style",
        "builtin": True,
    },
    {
        "id": "pixel",
        "name": "像素风（实验）",
        "name_en": "16-bit Pixel Art",
        "description": "16位像素游戏风。复古游戏感强烈，但姿势图集和视频动画可能偏粗糙，标注为实验性，建议先小规模试跑。",
        "style_prompt": "16-bit pixel art style, retro game aesthetic, crisp pixels, limited color palette",
        "thumbnail_hint": "pixel art-style",
        "builtin": True,
        "experimental": True,
    },
    {
        "id": "realistic",
        "name": "真人写实（实验）",
        "name_en": "Photorealistic Cinematic",
        "description": "写实电影感真人风。Seedance2 视频生成擅长真人画面，但跨图片的角色一致性风险更高，标注为实验性。",
        "style_prompt": "photorealistic cinematic style, natural skin texture, real-life lighting, shallow depth of field",
        "thumbnail_hint": "photorealistic cinematic-style",
        "builtin": True,
        "experimental": True,
    },
]


def _read_custom() -> list[dict[str, Any]]:
    if not CUSTOM_STYLES_PATH.exists():
        return []
    try:
        data = json.loads(CUSTOM_STYLES_PATH.read_text(encoding="utf-8"))
        return data.get("styles", [])
    except (json.JSONDecodeError, OSError):
        return []


def list_styles() -> list[dict[str, Any]]:
    """内置 + 自定义风格（每项附 builtin 标记）。"""
    result = [dict(s, builtin=True) for s in BUILTIN_STYLES]
    for s in _read_custom():
        item = dict(s)
        item.setdefault("builtin", False)
        item["builtin"] = False
        result.append(item)
    return result


def get_style(style_id: str) -> dict[str, Any] | None:
    for s in list_styles():
        if s["id"] == style_id:
            return s
    return None


def resolve_style_prompt(style_id: str | None) -> str:
    """style_id → style_prompt；未知/空 → 默认风格 prompt。"""
    if style_id:
        s = get_style(style_id)
        if s:
            return s["style_prompt"]
    return DEFAULT_STYLE_PROMPT


def get_active_style_prompt() -> str:
    """读取 VISUAL_STYLE_PROMPT（pipeline_service / CLI 注入 os.environ；
    批量生成等 Web 线程经 set_llm_env_override 注入线程局部覆盖）。"""
    from llm_client import _env_get  # 延迟导入避免循环依赖（llm_client 顶层 import 本模块）
    return _env_get("VISUAL_STYLE_PROMPT", "") or DEFAULT_STYLE_PROMPT


def get_active_thumbnail_hint() -> str:
    """当前风格的缩略图提示短语（thumbnail 提示词用）。"""
    from llm_client import _env_get  # 延迟导入避免循环依赖
    style_id = _env_get("VISUAL_STYLE_ID", "")
    if style_id:
        s = get_style(style_id)
        if s:
            return s.get("thumbnail_hint", "custom-style")
    return "3D Pixar-style"


def get_style_options() -> dict[str, str]:
    """id → 中文名（配置页下拉选项）。"""
    return {s["id"]: s.get("name", s["id"]) for s in list_styles()}
