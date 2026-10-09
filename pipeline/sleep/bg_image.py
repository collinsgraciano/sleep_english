"""sleep 模式背景图生成：按本期主题 AI 生成 1 张（低透明度衬底用）。

- AI 生成走 MCP 通道（由调用方传入 call_tool 等函数；Step 2 对
  sleep 按需初始化 MCP，照缩略图先例）；
- 文件级续传：images/sleep_bg.png 已存在即跳过（resume 零消耗）；
- 失败非致命：背景图是增强功能，任何失败只告警并返回 ""，卡片回退纯渐变，
  不中断与生图无关的 sleep 运行。

prompt 组成（2026-10 重构，修「不管什么主题都像睡觉场景」）
------------------------------------------------------------------
旧 prompt 把主体交给 ``script["scene"]``（常常是「情境對話」这类零信息泛化标签），
后面又写死 ``night lighting`` + ``sleep atmosphere`` + ``no people`` ⇒ 无论主题是
钢琴课还是领养狗，出图都像昏暗卧室。现在：

1. **主体段**：``pick_bg_subject()`` 先过滤泛化标签，主题（topic）优先，其次
   非泛化 scene，再次标题（去掉频道固定前缀），都没有才用 ``everyday life``，
   并明确要求「主体必须清晰可辨」；
2. **光线段**：只描述光线/对比度/构图，不再写死夜晚与睡眠氛围；
3. **风格段**：``style_prompt`` 收尾并声明「严格遵循」，避免与我们的形容词打架；
4. **人物**：默认允许出现人物/角色（多数主题是人的活动），可用
   ``allow_people=False`` 回到「不要人」的老取向。

``style_mode="mood_first"`` 时走 ``_legacy_prompt()``，输出与重构前**逐字符一致**，
作为「还是想要旧观感」的回退开关。
"""
import os
import re
import time
from pathlib import Path

# 泛化场景标签（对生图没有主体信息量）：命中则视为「没给主体」，继续往下回退
_GENERIC_SUBJECTS = (
    "情境對話", "情境对话", "情境演練", "情境演练", "日常", "對話", "对话",
    "場景", "场景", "生活", "生活對話", "短句", "常用短句", "英文短句",
    "everyday life", "daily life", "everyday phrases", "dialogue", "dialog",
    "conversation", "scene", "situational dialogue", "short sentences",
)
# 泛化关键词：短标签里出现这些词也判为泛化（如「客服 · 情境對話」「日常對話」）
_GENERIC_HINTS = ("對話", "对话", "情境", "日常", "場景", "场景", "生活", "短句")

_NEGATIVE = "no text, no words, no letters, no watermark"
_LIGHTING = ("Soft diffused ambient lighting, low contrast, calm pastel palette "
             "suitable for a bedtime video, clean simple composition, wide shot")
# 兼容旧 mood_first 文案里的固定串（逐字符保留）
_LEGACY_MOOD = ("Soft muted colors, gentle diffused night lighting, peaceful "
                "relaxing sleep atmosphere, clean simple composition, wide shot.")


def _is_generic_part(part: str) -> bool:
    """单个标签是否泛化：空 / 极短 / 命中词表 / 短标签含泛化关键词。"""
    p = str(part or "").strip().lower()
    if not p or len(p) <= 1:
        return True
    if p in _GENERIC_SUBJECTS:
        return True
    if len(p) <= 6 and any(h.lower() in p for h in _GENERIC_HINTS):
        return True
    return False


def is_generic_subject(value: str) -> bool:
    """整体判定（是否为「无主体信息」的泛化标签）。

    1) 空 / 命中词表 → 泛化；
    2) 单段：短标签含泛化关键词（對話/情境/日常/生活/短句…）也算泛化；
    3) 多段（LLM 的 scene 标签格式「X · Y」，如「客服 · 情境對話」「居家打掃 · 短句」）：
       以**最后一段**为准 —— 它是泛化标签就整体判泛化；否则视为含具体内容
       （如「情境 · 取快遞」→ 不泛化，主体就是「取快遞」）。
    """
    v = str(value or "").strip()
    if not v:
        return True
    if v.lower() in _GENERIC_SUBJECTS:
        return True
    parts = [p.strip() for p in re.split(r"[·|、/]", v)
             if p.strip() and p.strip() not in ("-", "—")]
    if not parts:
        return True
    if len(parts) == 1:
        return _is_generic_part(parts[0])
    return _is_generic_part(parts[-1])


def pick_bg_subject(topic: str = "", scene: str = "", title: str = "") -> tuple[str, str]:
    """挑生图主体。返回 ``(subject, source)``，source ∈ topic/scene/title/fallback。

    优先级：**主题优先**（英文 topic 最贴本期内容）→ 非泛化 scene →
    标题（去掉频道固定前缀「Everyday Phrases —」）→ ``everyday life``。
    """
    t = str(topic or "").strip()
    if t and not is_generic_subject(t):
        return t, "topic"
    s = str(scene or "").strip()
    if s and not is_generic_subject(s):
        return s, "scene"
    ti = str(title or "").strip()
    if ti:
        cleaned = re.sub(r"^\s*everyday\s+phrases\s*[—\-–:：|]*\s*", "", ti,
                         flags=re.IGNORECASE).strip()
        cleaned = cleaned or ti
        if not is_generic_subject(cleaned):
            return cleaned, "title"
    return "everyday life", "fallback"


def _legacy_prompt(topic: str, style_prompt: str = "") -> str:
    """重构前的 prompt（逐字符保留，供 style_mode="mood_first" 回退）。"""
    t = str(topic or "").strip() or "everyday life"
    parts = [
        f"Calm dreamy pastel scene related to: {t}.",
        _LEGACY_MOOD,
        "no text, no words, no letters, no watermark, no people",
    ]
    if style_prompt:
        parts.append(style_prompt)
    return ", ".join(parts)


def build_bg_prompt(scene_or_topic: str, style_prompt: str = "", *,
                    topic_en: str = "", scene: str = "", title: str = "",
                    style_mode: str = "topic_first",
                    allow_people: bool = True) -> str:
    """背景图 prompt。

    scene_or_topic: 旧调用点的主体（``scene or topic``）—— mood_first 用它保持逐字符一致；
    topic_en/scene/title: topic_first 模式下用于挑主体（主题优先）。
    """
    if str(style_mode or "").strip().lower() == "mood_first":
        return _legacy_prompt(scene_or_topic, style_prompt)
    subject, _src = pick_bg_subject(topic_en or scene_or_topic, scene, title)
    parts = [
        f"Wide establishing shot of: {subject}.",
        f"The main subject must be clearly recognizable: {subject}.",
        _LIGHTING,
        _NEGATIVE if allow_people else f"{_NEGATIVE}, no people",
    ]
    if style_prompt:
        parts.append(f"Follow the art style below strictly: {style_prompt}")
    return ", ".join(parts)


def _write_prompt_file(dest: str, prompt: str, subject: str, source: str) -> None:
    """把实际 prompt 落盘（复盘/重生成用），失败不影响主流程。"""
    try:
        Path(f"{dest}.prompt.txt").write_text(
            f"# {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# subject={subject!r} source={source}\n{prompt}\n",
            encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        print(f"  [SleepBG] prompt 落盘失败（忽略）: {e}")


def ensure_sleep_bg_image(img_dir: str, scene_or_topic: str, style_prompt: str = "",
                          mcp_call_tool=None, mcp_parse_task_id=None,
                          mcp_poll_task=None, mcp_download_file=None,
                          image_gen_fn=None, *, topic_en: str = "", scene: str = "",
                          title: str = "", style_mode: str = "topic_first",
                          allow_people: bool = True) -> str:
    """确保 sleep 背景图存在。返回图片绝对路径；失败/关闭回退返回 ""。

    AI 生成走 MCP 通道（由调用方传入 call_tool 等函数；Step 2 对
    sleep 按需初始化 MCP，照缩略图先例）；image_gen_fn 为 HTTP 生图通道
    （image_provider != mcp）时由调用方传入，优先级高于 MCP。
    """
    dest = str(Path(img_dir) / "sleep_bg.png")
    if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
        print("  [SleepBG] sleep_bg.png 已存在，跳过生成（resume 续传）")
        return dest

    prompt = build_bg_prompt(scene_or_topic, style_prompt, topic_en=topic_en,
                             scene=scene, title=title, style_mode=style_mode,
                             allow_people=allow_people)
    subject, src = pick_bg_subject(topic_en or scene_or_topic, scene, title)
    if str(style_mode or "").strip().lower() == "mood_first":
        subject, src = str(scene_or_topic or "").strip() or "everyday life", "legacy"
    print(f"  [SleepBG] 主体={subject!r}（来源={src}，style_mode={style_mode}，"
          f"allow_people={allow_people}）")
    print(f"  [SleepBG] prompt: {prompt[:300]}{'…' if len(prompt) > 300 else ''}")

    try:
        if image_gen_fn is not None:
            print("  [SleepBG] HTTP 生图通道生成中（1536x1024）...")
            if not image_gen_fn(prompt, dest):
                raise RuntimeError("HTTP 生图通道返回失败")
        elif not all((mcp_call_tool, mcp_parse_task_id, mcp_poll_task,
                      mcp_download_file)):
            print("  [SleepBG] MCP 通道未初始化 —— 跳过背景图生成（卡片回退纯渐变）")
            return ""
        else:
            print("  [SleepBG] MCP 生成背景图中（landscape_16_9）...")
            result = mcp_call_tool("generate_image", {
                "prompt": prompt,
                "provider": "seedream",
                "image_size": "landscape_16_9",
                "output_format": "png",
            })
            task_id = mcp_parse_task_id(result)
            if not task_id:
                raise RuntimeError("MCP 未返回任务 ID")
            data = mcp_poll_task(task_id, interval=10, max_wait=600)
            url = data.get("url", "")
            if not url or not mcp_download_file(url, dest):
                raise RuntimeError(f"生成失败（status={data.get('status')}）")
    except Exception as e:  # noqa: BLE001 — 增强功能失败不中断 sleep 运行
        print(f"  [SleepBG] WARNING: 背景图生成失败（忽略，卡片回退纯渐变）: {e}")
        return ""

    if not os.path.exists(dest) or os.path.getsize(dest) < 10_000:
        print("  [SleepBG] WARNING: 背景图文件无效（忽略）")
        return ""
    _write_prompt_file(dest, prompt, subject, src)
    print(f"  [SleepBG] 背景图已生成: {dest}")
    return dest


__all__ = ["build_bg_prompt", "pick_bg_subject", "is_generic_subject",
           "ensure_sleep_bg_image"]
