"""YouTube thumbnail generator — generates thumbnail with text baked into the AI prompt.

Instead of generating a background then overlaying text via Pillow, this module
puts the title text, level badge, and Chinese subtitle directly into the generate_image
prompt so the AI renders everything in one step.

Fallback: if AI image gen fails, falls back to Pillow text overlay on scene image.
"""
import os
import re
import sys
import json
from pathlib import Path

_PARENT = str(Path(__file__).parent.resolve())
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from media_utils import FONT_EN, FONT_ZH

# YouTube thumbnail specs
THUMB_W = 1280
THUMB_H = 720


def assign_sleep_episode(script: dict, sleep_dir: str,
                         channel_id: str = "") -> int:
    """缩略图集数：按主题系列自动递增（首次生成时 bump 计数文件）。

    - script 已有 thumb_episode（重生成/复跑）→ 直接返回，不 bump；
    - 计数存 {sleep_dir}/.thumb_episode.json（点前缀文件不进运行列表），
      键 = topic（缺省 title）；频道矩阵下键加 channel_id 前缀
      （f"{cid}|{topic}"），各频道同主题系列各自独立递增；
    - 参考缩略图的红色圆底 01/02 徽章。
    """
    try:
        existing = int(script.get("thumb_episode", 0) or 0)
    except (TypeError, ValueError):
        existing = 0
    if existing > 0:
        return existing
    topic = str(script.get("topic", "") or script.get("title", "") or "default").strip()
    key = f"{channel_id}|{topic}" if str(channel_id or "").strip() else topic
    path = Path(sleep_dir) / ".thumb_episode.json"
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}
    try:
        ep = int(data.get(key, 0) or 0) + 1
    except (TypeError, ValueError):
        ep = 1
    data[key] = ep
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        print(f"  [Thumbnail] WARNING: 集数计数写入失败: {e}")
    script["thumb_episode"] = ep
    return ep


def _build_sleep_thumbnail_prompt(script: dict) -> str:
    """sleep 参考同款缩略图 prompt：3D 皮克斯角色 + 超大繁中标题 + 徽章/集数。

    文案来自脚本 thumb_* 字段（LLM 每期生成，旧脚本 setdefault 兜底），
    全部引号内嵌并要求逐字渲染（AI 整图文字，同 thumbnail_gen 现有模式）。
    """
    dialogue = script.get("dialogue", []) or []
    n_lines = len(dialogue)
    num_text = f"{n_lines}句" if n_lines else "300句"
    title_zh = str(script.get("title_zh", "") or "").strip()
    topic_text = f"{title_zh}英文" if title_zh else "生活英文"
    badge = str(script.get("thumb_badge", "") or "").strip() or "不用背！"
    main = str(script.get("thumb_main", "") or "").strip() or (title_zh or "睡覺聽")
    hook = (str(script.get("thumb_hook", "") or "").strip()
            or str(script.get("thumbnail_subtitle", "") or "").strip()
            or "零基礎自然開口說")
    episode = ""
    try:
        ep = int(script.get("thumb_episode", 0) or 0)
        if ep > 0:
            episode = f"{ep:02d}"
    except (TypeError, ValueError):
        episode = ""
    scene_en = str(script.get("scene", "") or script.get("title", "")
                   or "daily life").strip()

    episode_line = (f'\n  - A small red circle badge with the white bold number "{episode}"'
                    if episode else "")
    return f"""A vibrant professional YouTube thumbnail (16:9) for a "listen while you sleep" English learning video, in a cute cozy 3D Pixar animation movie style.

LAYOUT:
- Right half: an adorable 3D Pixar-style young woman with brown hair wearing big cream-white headphones, relaxed and cheerful with a warm smile, in a cozy {scene_en} scene, soft dreamy lighting, gentle bokeh background.
- Left half text stack (ALL text must be Traditional Chinese rendered EXACTLY as written, no extra text, no typos):
  - Top-left: a red brush-stroke banner with bold white text "{badge}"
  - Below it: HUGE bold 3D yellow text "{main}" with a thick dark-blue outline — the most prominent element on the thumbnail
  - Below it: bold black text "{num_text}{topic_text}" on a bright yellow rounded banner{episode_line}
- Bottom-left: a rounded ribbon banner with bold white text "{hook}"
- A small white headphone icon accent near the ribbon.

Style: high contrast, bright saturated colors, soft glow, clean composition, professional YouTube CTR design, no watermark, no subtitles, no other text."""


def _derive_bubbles(script: dict, limit: int = 2) -> list[str]:
    """从脚本对话抽真实英文短句做缩略图气泡（2-6 词、无数字，跨行去重）。"""
    out: list[str] = []
    seen: set[str] = set()
    for row in script.get("dialogue", []) or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text", "") or "").strip()
        if not text:
            continue
        words = text.split()
        if not (2 <= len(words) <= 6) or any(ch.isdigit() for ch in text):
            continue
        key = text.lower().rstrip(".,!?")
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _build_sleep_thumbnail_prompt_v2(script: dict, template: str) -> str:
    """竞品模板族 v2 prompt：数字锚点 / 痛点问句 / ✕✓对比 + 角色场景匹配。

    文案/角色/镜像来自 script thumb_* v2 键（弹窗挑选或手改后写回），
    句数 headline 与英文气泡从脚本确定性派生（不依赖 LLM）。
    """
    dialogue = script.get("dialogue", []) or []
    n_pairs = len(dialogue) // 2
    num_text = f"{n_pairs}句" if n_pairs else "300句"
    title_zh = str(script.get("title_zh", "") or "").strip()
    scene_en = str(script.get("scene", "") or script.get("title", "")
                   or "daily life").strip()

    badge = str(script.get("thumb_badge", "") or "").strip() or "不用背！"
    h1 = str(script.get("thumb_headline1", "") or "").strip() or num_text
    h2 = str(script.get("thumb_headline2", "") or "").strip() or (title_zh or "生活英文")
    pills = [str(p).strip() for p in (script.get("thumb_pills") or [])
             if str(p).strip()]
    for default_pill in ("美國人天天都在說", "每天聽，自然開口說"):
        if len(pills) >= 2:
            break
        if default_pill not in pills:
            pills.append(default_pill)
    pills = pills[:2]
    character = (str(script.get("thumb_character", "") or "").strip()
                 or ("an adorable 3D Pixar-style young woman with brown hair "
                     "wearing big cream-white headphones, relaxed and cheerful "
                     "with a warm smile"))
    mirror = bool(script.get("thumb_mirror"))
    bubbles = _derive_bubbles(script)

    episode = ""
    try:
        ep = int(script.get("thumb_episode", 0) or 0)
        if ep > 0:
            episode = f"{ep:02d}"
    except (TypeError, ValueError):
        episode = ""
    episode_line = (f'\n  - A small red circle badge with the white bold number "{episode}"'
                    if episode else "")

    pill_lines = ""
    pill_colors = [("bright blue", "white"), ("bright yellow", "black")]
    for i, p in enumerate(pills):
        color, txt_color = pill_colors[i % 2]
        pill_lines += (f'\n  - A {color} rounded banner with bold {txt_color} '
                       f'text "{p}"')

    bubble_lines = ""
    if bubbles:
        quoted = " / ".join(f'"{b}"' for b in bubbles)
        bubble_lines = ("\n- Near the character: 1-2 small white speech bubbles "
                        f"containing the English phrases {quoted} in a casual "
                        "handwritten style.")

    char_side = "Left half" if mirror else "Right half"
    text_side = "Right half" if mirror else "Left half"
    scene_line = (f"in a {scene_en} scene matched to the topic, soft dreamy "
                  "lighting, gentle bokeh background")

    if template == "contrast":
        layout = f"""LAYOUT:
- A split before/after comparison of THE SAME character ({character}):
  - Left half: the character looking confused and stuck, with a big red "✕" symbol above the head, {scene_line}
  - Right half: the same character, now happy and confidently speaking English, with a big green "✓" symbol above the head
- Between the two halves: a bold yellow arrow pointing from left to right.
- Top-center: a red brush-stroke banner with bold white text "{badge}" and a small flame icon.
- Bottom-center text stack (ALL text must be Traditional Chinese rendered EXACTLY as written, no extra text, no typos):
  - GIANT bold 3D yellow text "{h1}" with a thick dark-blue outline — the most prominent element on the thumbnail
  - Below it: bold white text "{h2}" with a thick black outline{pill_lines}{episode_line}{bubble_lines}
- A small white headphone icon accent near the bottom."""
    else:
        question_note = (" The character's expression should match the pain-point "
                         "question (slightly confused or thoughtful)."
                         if template == "question" else "")
        layout = f"""LAYOUT:
- {char_side}: {character}, {scene_line}.{question_note}
- {text_side} text stack (ALL text must be Traditional Chinese rendered EXACTLY as written, no extra text, no typos):
  - Top: a red brush-stroke banner with bold white text "{badge}" and a small flame icon
  - Below it: GIANT bold 3D yellow text "{h1}" with a thick dark-blue outline — the most prominent element on the thumbnail
  - Below it: bold white text "{h2}" with a thick black outline{pill_lines}{episode_line}{bubble_lines}
- A small white headphone icon accent near the bottom banner."""

    return f"""A vibrant professional YouTube thumbnail (16:9) for a "listen while you sleep" English learning video, in a cute cozy 3D Pixar animation movie style.

{layout}

Style: high contrast, bright saturated colors, soft glow, clean composition, professional YouTube CTR design, no watermark, no subtitles, no other text.

CRITICAL: The most prominent text on the thumbnail must be the Traditional Chinese headline "{h1}" — bright yellow with a thick dark outline. All Chinese text must be exactly as written (Traditional Chinese)."""


def _generate_sleep_thumbnail(script: dict, output_path: str,
                              mcp_call_tool=None, mcp_parse_task_id=None,
                              mcp_poll_task=None, mcp_download_file=None,
                              theme: dict | None = None,
                              channel_name: str = "",
                              image_gen_fn=None) -> str:
    """sleep 缩略图：参考同款 AI 生成（文字内嵌 prompt），失败兜底 Pillow 卡片。

    Provider 优先级：HTTP 生图通道（image_gen_fn）→ MCP → Pillow 兜底。
    """
    # v2 模板分派：script.thumb_template 由弹窗挑选后写回（缺省 = 经典模板）
    template = str(script.get("thumb_template", "") or "").strip()
    if template in ("number", "question", "contrast"):
        prompt = _build_sleep_thumbnail_prompt_v2(script, template)
    else:
        prompt = _build_sleep_thumbnail_prompt(script)

    if image_gen_fn is not None:
        print("  [Thumbnail] Generating sleep thumbnail via HTTP 生图通道 (baked-in text)...")
        try:
            if image_gen_fn(prompt, output_path):
                if os.path.getsize(output_path) // 1024 > 10:
                    print(f"  [Thumbnail] Saved (AI baked-in): {output_path}")
                    return output_path
                print("  [Thumbnail] AI image too small, falling back")
            else:
                print("  [Thumbnail] HTTP 生图通道返回失败, falling back")
        except Exception as e:
            print(f"  [Thumbnail] AI generation failed: {e}, falling back")
    elif mcp_call_tool and mcp_parse_task_id and mcp_poll_task and mcp_download_file:
        print("  [Thumbnail] Generating sleep thumbnail via MCP (baked-in text)...")
        try:
            gen_args = {
                "prompt": prompt,
                # frontier 高质量通道（~50 积分/张），须 confirm_cost=true 才真正建任务
                "provider": "frontier",
                "quality": "high",
                "image_size": '{"width": 1280, "height": 720}',
                "output_format": "jpeg",
                "confirm_cost": True,
            }
            result = mcp_call_tool("generate_image", gen_args)
            task_id = mcp_parse_task_id(result)
            if task_id:
                data = mcp_poll_task(task_id, interval=10, max_wait=300)
                url = data.get("url", "")
                if url and mcp_download_file(url, output_path):
                    if os.path.getsize(output_path) // 1024 > 10:
                        print(f"  [Thumbnail] Saved (AI baked-in): {output_path}")
                        return output_path
                    print("  [Thumbnail] AI image too small, falling back")
                else:
                    print("  [Thumbnail] AI generation returned no URL, falling back")
            else:
                # 不再静默：打印原始响应文本便于定位（高成本确认提示/参数错误等）
                raw = ""
                for item in result.get("result", {}).get("content", []):
                    if item.get("type") == "text":
                        raw = str(item.get("text", ""))[:300].replace("\n", " ")
                        break
                print(f"  [Thumbnail] MCP 未返回任务 ID（响应: {raw}），falling back")
        except Exception as e:
            print(f"  [Thumbnail] AI generation failed: {e}, falling back")
    else:
        print("  [Thumbnail] 无可用生图通道，使用 Pillow 兜底")

    # Pillow 兜底：现有 sleep 卡片缩略图
    print("  [Thumbnail] Using sleep Pillow fallback...")
    from sleep.sleep_cards import build_theme, render_sleep_thumbnail
    render_sleep_thumbnail(script, theme if theme is not None else build_theme({}),
                           output_path, badge_text="EN",
                           channel_name=channel_name or "English with me")
    return output_path


def generate_thumbnail(script: dict, output_path: str,
                        mcp_call_tool=None, mcp_parse_task_id=None,
                        mcp_poll_task=None, mcp_download_file=None,
                        sleep_theme: dict | None = None,
                        sleep_channel: str = "",
                        image_gen_fn=None) -> str:
    """生成 sleep 缩略图（AI 直出带字图，失败回退 Pillow 卡片）。

    本项目仅 sleep 结构，非 sleep 的通用缩略图路径已随上游模式一并移除。
    """
    return _generate_sleep_thumbnail(
        script, output_path,
        mcp_call_tool=mcp_call_tool,
        mcp_parse_task_id=mcp_parse_task_id,
        mcp_poll_task=mcp_poll_task,
        mcp_download_file=mcp_download_file,
        theme=sleep_theme, channel_name=sleep_channel,
        image_gen_fn=image_gen_fn)



# "⏱️ Chapters:" marker line + consecutive chapter timestamp lines (00:00 / 00:xx style)
_CHAPTERS_SECTION = re.compile(
    r"⏱️?\s*Chapters:[^\n]*\n"
    r"(?:[ \t]*[-•*]?[ \t]*(?:[\dx]{1,2}:)?[\dx]{1,2}:[\dx]{2}[^\n]*\n?)+"
)


def _inject_chapters(desc: str, chapters: list[str]) -> str:
    """Inject real chapter timestamps into a description.

    The LLM often leaves placeholder timestamps (00:xx) in its own Chapters
    section — replace that section with the real ones computed from the
    timeline. Append a new section only if none exists.
    """
    if not chapters:
        return desc
    block = "⏱️ Chapters:\n" + "\n".join(chapters)
    if _CHAPTERS_SECTION.search(desc):
        return _CHAPTERS_SECTION.sub(block + "\n", desc, count=1)
    if not desc.strip():
        return block + "\n"
    return desc.rstrip() + "\n\n" + block + "\n"


def save_youtube_metadata(script: dict, timeline: list[dict],
                           output_path: str, structure: str = "original") -> str:
    """Save YouTube metadata (title, description, tags) as JSON.

    Also post-processes the description to insert real timestamps from the timeline.
    """
    # Collect ordered chapter candidates (seconds, label) from the timeline.
    # YouTube chapter rules: the first chapter MUST start at 00:00 and every
    # chapter must span >= 10 seconds — candidates starting less than 10s
    # after the previously kept one are dropped.
    # sleep 时间轴段类型 intro/pair/gap/outro：按类型取首现做三章
    seg_labels = {
        "intro": "Intro",
        "pair": "Phrase Drills",
        "outro": "Outro",
    }

    marks: list[tuple[float, str]] = []
    seen_types: set[str] = set()
    t_cursor = 0.0
    for seg in timeline:
        seg_type = seg.get("type", "")
        if seg_type in seg_labels and seg_type not in seen_types:
            seen_types.add(seg_type)
            marks.append((t_cursor, seg_labels[seg_type]))
        t_cursor += seg.get("duration", 0)

    def _fmt_ts(seconds):
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m:02d}:{s:02d}"

    chapters = []
    kept_t = 0.0
    for i, (t, label) in enumerate(marks):
        if i == 0:
            kept_t = 0.0  # 首章节必须 00:00，否则 YouTube 禁用全部章节
        elif t - kept_t < 10.0:
            continue
        else:
            kept_t = t
        chapters.append(f"{_fmt_ts(kept_t)} {label}")

    description = script.get("youtube_description", "")
    description = _inject_chapters(description, chapters)

    description_en = script.get("youtube_description_en", "")
    description_en = _inject_chapters(description_en, chapters)

    tags = script.get("youtube_tags", [])
    # Append tags as #hashtag string at the end of description
    hashtags = " ".join(f"#{t.replace(' ', '')}" for t in tags) if tags else ""
    if hashtags:
        description = description.rstrip() + "\n" + hashtags
        if description_en:
            description_en = description_en.rstrip() + "\n" + hashtags

    # SEO 词池：并入高频搜索短语（脚本 LLM tags 优先 → 主题命中 → core），
    # 只丰富 metadata 的 tags 字段；上面的 hashtag 追加保持仅用脚本原 tags
    try:
        from seo_pool import merge_tags
        tags = merge_tags(
            tags,
            f"{script.get('title', '')} {script.get('scene', '')} "
            f"{script.get('youtube_title', '')} {script.get('topic', '')}")
    except Exception as e:
        print(f"  [YouTube] WARNING: SEO 词池合并失败: {e}")

    title = script.get("youtube_title", script.get("title", ""))
    title_en = script.get("youtube_title_en", "")
    # YouTube 标题硬上限 100 字符，超长会被截断 —— 最后防线
    if len(title) > 100:
        print(f"  [YouTube] WARNING: title {len(title)} chars > 100, truncated")
        title = title[:100]
    if len(title_en) > 100:
        print(f"  [YouTube] WARNING: title_en {len(title_en)} chars > 100, truncated")
        title_en = title_en[:100]

    metadata = {
        "title": title,
        "title_en": title_en,
        "description": description,
        "description_en": description_en,
        "tags": tags,
        "chapters": chapters,
    }

    # 多样式选项（title_options）：yt_meta_styles/ 下每个注册样式生成一套备选
    # 标题+简介（如"参考频道同款"），与默认选项并存供上传时多选一。
    try:
        from yt_meta_styles import collect_options
        options = collect_options(script, chapters=chapters, marks=marks, structure=structure)
    except Exception as e:
        print(f"  [YouTube] WARNING: 样式选项收集失败: {e}")
        options = []
    for opt in options:
        if len(opt["title"]) > 100:
            print(f"  [YouTube] WARNING: {opt['style_id']} title {len(opt['title'])} chars > 100, truncated")
            opt["title"] = opt["title"][:100]
        if hashtags:
            opt["description"] = opt["description"].rstrip() + "\n" + hashtags
    if options:
        metadata["title_options"] = options

    Path(output_path).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  [YouTube] Metadata saved: {output_path}")
    return output_path
