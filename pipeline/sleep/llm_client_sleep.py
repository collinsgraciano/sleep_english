"""sleep 模式脚本生成：LLM 分批产出 A/B 短对话组 + 元数据。

- 每批默认 50 组（100 行 dialogue），批间落盘 cache_dir 防中断丢失；
- 批 1 同时产出标题/YouTube 元数据（繁中），后续批只产 pairs；
- 批级校验（数量/字段/词长），不合格整批重试（≤2 次），词长软失败降级警告；
- script.json 行格式与 listening 完全一致（speaker char_a/char_b 交替 +
  text/phonetic/zh），组 = 相邻 A/B 两行；char_a 恒男声、char_b 恒女声
  （朗读声部，非剧情角色）。
"""
import json
import hashlib
from pathlib import Path

from llm_client import (_chat, _extract_json, resolve_max_line_words,
                        _env_int_clamped)


def _sleep_max_line_words() -> int:
    """单句最大词数（参考视频 ≤7 词）：SLEEP_MAX_LINE_WORDS → 默认 7，clamp [4,10]。"""
    return _env_int_clamped("SLEEP_MAX_LINE_WORDS", 7, 4, 10)


def _pairs_to_rows(pairs: list[dict]) -> list[dict]:
    """pairs [{a:{text,phonetic,zh}, b:{...}}] → listening 风格 dialogue 行。"""
    rows = []
    for p in pairs:
        a, b = p.get("a") or {}, p.get("b") or {}
        rows.append({"speaker": "char_a", "text": a.get("text", ""),
                     "phonetic": a.get("phonetic", ""), "zh": a.get("zh", "")})
        rows.append({"speaker": "char_b", "text": b.get("text", ""),
                     "phonetic": b.get("phonetic", ""), "zh": b.get("zh", "")})
    return rows


def _cache_key(topic: str, cefr: str, num_pairs: int, channel_ctx=None) -> str:
    raw = f"sleep|{topic}|{cefr}|{num_pairs}"
    if channel_ctx:
        # 频道上下文参与键：同主题不同频道不复用脚本缓存（标题/简介归属各自频道）
        ctx_sig = json.dumps(channel_ctx, ensure_ascii=False, sort_keys=True)
        raw += "|" + hashlib.md5(ctx_sig.encode("utf-8")).hexdigest()[:8]
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _channel_brand_block(channel_ctx) -> str:
    """频道品牌上下文 prompt 段（无频道返回空串，维持现状 prompt）。"""
    if not channel_ctx:
        return ""
    name = str(channel_ctx.get("name_en", "") or "").strip()
    if not name:
        return ""
    tags = ", ".join(str(t) for t in (channel_ctx.get("tags") or [])[:12])
    lines = [
        f"\nCHANNEL BRAND CONTEXT — this video is published on the channel \"{name}\""
        + (f" ({channel_ctx.get('name_zh')})" if channel_ctx.get("name_zh") else "") + ":",
    ]
    if channel_ctx.get("niche"):
        lines.append(f"- Channel niche: {channel_ctx['niche']}")
    if channel_ctx.get("audience"):
        lines.append(f"- Target audience: {channel_ctx['audience']}")
    if channel_ctx.get("brand_style"):
        lines.append(f"- Brand style/tone: {channel_ctx['brand_style']}")
    if tags:
        lines.append(f"- Channel SEO tags: {tags}")
    lines.append(
        "- Keep the sleep-listening phrase-drill format unchanged; tilt pair "
        "angles, titles and descriptions toward THIS channel's positioning.")
    return "\n".join(lines) + "\n"


def _validate_pairs(pairs: list[dict], expected: int, max_words: int) -> tuple[bool, str, int]:
    """批级校验。返回 (ok, message, soft_violations)。"""
    if len(pairs) != expected:
        return False, f"expected {expected} pairs, got {len(pairs)}", 0
    soft = 0
    for i, p in enumerate(pairs):
        for side in ("a", "b"):
            obj = p.get(side)
            if not isinstance(obj, dict):
                return False, f"pair {i+1} '{side}' missing", 0
            text = (obj.get("text") or "").strip()
            phon = (obj.get("phonetic") or "").strip()
            zh = (obj.get("zh") or "").strip()
            if not text or not phon or not zh:
                return False, f"pair {i+1} '{side}' has empty text/phonetic/zh", 0
            if len(text.split()) > max_words:
                soft += 1
    return True, "", soft


def _batch_prompt(topic: str, cefr: str, count: int, start_idx: int,
                  total_pairs: int, max_words: int, with_meta: bool,
                  channel_ctx=None) -> str:
    cefr_guide = {
        "A1": "very basic everyday words, present tense only",
        "A2": "common daily phrases, present/past tense",
        "B1": "moderate vocabulary, some idioms",
        "B2": "advanced vocabulary, natural idioms and phrasal verbs",
    }.get(cefr, "common daily phrases")
    meta_schema = ""
    meta_reqs = ""
    if with_meta:
        meta_schema = '''
  "title": string,
  "title_zh": string,
  "scene_zh": string,
  "title_quote": string,
  "cefr": string,
  "youtube_title": string,
  "youtube_title_en": string,
  "youtube_description": string,
  "youtube_description_en": string,
  "youtube_tags": [string],
  "thumb_badge": string,
  "thumb_main": string,
  "thumb_hook": string,'''
        meta_reqs = f'''
- "title": short English title of the phrase collection topic (e.g. "Everyday Phrases — Cleaning the House")
- "title_zh": Traditional Chinese short title (max 8 characters, e.g. "打掃房間")
- "scene_zh": Traditional Chinese scene label (e.g. "居家打掃 · 短句")
- "title_quote": the single most useful/catchy sentence from THIS batch, copied VERBATIM (under 10 words)
- "cefr": exactly "{cefr}"
- "youtube_title": high-CTR YouTube title for overseas Chinese learners, ALL Chinese in Traditional Chinese (繁體中文). Format: 【睡前英文聽力】+ emoji + 主題短語英文 ｜ 痛點句（如"不用背！睡覺聽就會"） ｜ 🌱{cefr} ｜ 💡{total_pairs*2}句循環聽. Length 55-95 chars (NEVER exceed 95). Use ｜ separators, include 3-6 emoji.
- "youtube_title_en": high-CTR YouTube title in PURE ENGLISH (no Chinese), under 100 chars. Mention the topic and the "listen while you sleep / no memorization" angle (e.g. "400 Everyday English Phrases While You Sleep — Cleaning the House").
- "youtube_description": full YouTube description (max 3000 chars), ALL Chinese in Traditional Chinese (繁體中文). First line = hook with 睡前聽/不用背 keyword. Explain the AB-loop listening method (男聲常速 → 女聲慢速 → 男女連貫). End with call-to-action (點讚/訂閱/留言想學的場景) + 3 hashtags (#英文聽力 #睡前英文 #LearnEnglish). Do NOT write timestamps.
- "youtube_description_en": same in PURE ENGLISH (no Chinese), max 3000 chars, same structure. Do NOT write timestamps.
- "youtube_tags": 15-20 SEO tags mixing English and Traditional Chinese.
- "thumb_badge": YouTube thumbnail badge slogan, Traditional Chinese, AT MOST 6 characters, high-CTR hook (e.g. "不用背！" / "聽著聽著就會說").
- "thumb_main": YouTube thumbnail MAIN title, Traditional Chinese, 2-5 characters, the series brand word matching this topic (e.g. "睡覺聽" / "聽就會").
- "thumb_hook": YouTube thumbnail bottom banner line, Traditional Chinese, AT MOST 14 characters (e.g. "零基礎自然開口說" / "聽久自然開口說").'''
    return f"""You are an expert ESL teacher creating a "listen while you sleep" English phrase-drill video for overseas Chinese learners (zero-basics friendly, background/ASMR style).

Topic: {topic}
CEFR Level: {cefr} ({cefr_guide})
{_channel_brand_block(channel_ctx)}
This is batch {start_idx // count + 1}: generate pairs {start_idx + 1} to {start_idx + count} (of {total_pairs} total).

Each pair = a short A sentence (opening/trigger) + a short B sentence (natural reply/continuation) about the topic. These are NOT a connected story — each pair stands alone (different mini-situations welcome: doing the task, asking about it, small talk about it).

CONTENT REQUIREMENTS:
- High-frequency American English only — words people use every single day. NO rare/literary words.
- Each sentence AT MOST {max_words} words (HARD LIMIT).
- Natural, conversational, immediately useful (contractions like don't/I'm/let's are good).
- Pairs should feel like a mini exchange: question→answer, statement→reply, problem→solution.
- Across ALL batches: cover MANY different angles of the topic (actions, questions, polite requests, small complaints, encouragement) — avoid near-duplicate pairs within this batch.

Every sentence MUST include:
- "text": the English sentence
- "phonetic": IPA transcription in /slashes/ (proper IPA symbols)
- "zh": Traditional Chinese (繁體中文) translation — ALL Chinese text MUST be Traditional Chinese{meta_reqs}

Output a JSON object ONLY (no markdown, no explanation):
{{
  "pairs": [
    {{"a": {{"text": string, "phonetic": string, "zh": string}},
      "b": {{"text": string, "phonetic": string, "zh": string}}}}
  ]{meta_schema}
}}

Exactly {count} pairs in "pairs"."""


def _meta_from_batch(batch: dict, topic: str, cefr: str, num_pairs: int) -> None:
    """批 1 产出的元数据键抽出到 meta dict（就地写入 batch 弹出）。"""
    meta = {}
    for k in ("title", "title_zh", "scene_zh", "title_quote", "cefr",
              "youtube_title", "youtube_title_en", "youtube_description",
              "youtube_description_en", "youtube_tags",
              "thumb_badge", "thumb_main", "thumb_hook"):
        if k in batch:
            meta[k] = batch.pop(k)
    meta.setdefault("title", topic.upper())
    meta.setdefault("title_zh", "")
    meta.setdefault("scene_zh", "")
    meta.setdefault("title_quote", "")
    meta.setdefault("thumb_badge", "")
    meta.setdefault("thumb_main", "")
    meta.setdefault("thumb_hook", "")
    meta["cefr"] = cefr
    meta.setdefault("youtube_title", "")
    meta.setdefault("youtube_title_en", "")
    meta.setdefault("youtube_description", "")
    meta.setdefault("youtube_description_en", "")
    meta.setdefault("youtube_tags", [])
    return meta


def _generate_batch(topic, cefr, count, start_idx, total_pairs, max_words,
                    with_meta, temperature, channel_ctx=None):
    prompt = _batch_prompt(topic, cefr, count, start_idx, total_pairs,
                           max_words, with_meta, channel_ctx=channel_ctx)
    last_err = None
    for attempt in range(3):
        try:
            content = _chat(
                [{"role": "system",
                  "content": "You are an expert ESL teacher creating sleep-listening English phrase drills. Output valid JSON only — no markdown, no explanations."},
                 {"role": "user", "content": prompt}],
                temperature=round(max(0.3, temperature - 0.1 * attempt), 2),
                max_tokens=16384)
            batch = _extract_json(content)
            pairs = batch.get("pairs")
            if not isinstance(pairs, list):
                raise ValueError("JSON has no 'pairs' array")
            ok, msg, soft = _validate_pairs(pairs, count, max_words)
            if not ok:
                raise ValueError(f"batch invalid: {msg}")
            if soft:
                print(f"  [Sleep] WARNING: {soft} sentence(s) exceed {max_words} words — accepted (cards auto-shrink text).")
            return batch, pairs
        except Exception as e:
            last_err = e
            print(f"  [Sleep][Batch retry {attempt + 1}/3] {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError(f"Sleep batch generation failed after 3 retries: {last_err}")


def generate_sleep_script(topic: str, cefr: str = "A2", num_pairs: int = 200,
                          batch_pairs: int = 50, lessons_dir: str = None,
                          cache_dir: str = None,
                          use_cache: bool = True,
                          channel_ctx: dict | None = None) -> dict:
    """分批生成 sleep 脚本，返回 listening 兼容 script dict。

    批间落盘 cache_dir（默认 lessons_dir 或系统临时目录）——中断后重跑同
    topic/cefr/num_pairs 自动复用已成功批次；use_cache=False 跳过读取，
    每批现场重新生成（落盘写入保留，便于之后重新开启复用）。
    channel_ctx（频道品牌上下文，pipeline_service._channel_ctx 构建）：
    注入 prompt 让选题角度/标题/简介贴合频道定位；参与缓存键防跨频道串稿。
    """
    num_pairs = max(10, min(400, int(num_pairs)))
    batch_pairs = max(10, min(80, int(batch_pairs)))
    max_words = _sleep_max_line_words()

    cdir = Path(cache_dir) if cache_dir else (
        Path(lessons_dir) if lessons_dir else Path.home() / ".sleep_cache")
    cdir.mkdir(parents=True, exist_ok=True)
    ck = _cache_key(topic, cefr, num_pairs, channel_ctx)

    all_pairs: list[dict] = []
    meta: dict = {}
    start = 0
    while start < num_pairs:
        count = min(batch_pairs, num_pairs - start)
        batch_file = cdir / f"sleep_{ck}_{start:04d}.json"
        if use_cache and batch_file.exists():
            print(f"  [Sleep] Batch cache hit: {batch_file.name}")
            data = json.loads(batch_file.read_text(encoding="utf-8"))
            batch, pairs = data.get("batch", {}), data.get("pairs", [])
        else:
            if not use_cache and start == 0:
                print("  [Sleep] Batch cache disabled — regenerating fresh content")
            batch, pairs = _generate_batch(topic, cefr, count, start, num_pairs,
                                           max_words, with_meta=(start == 0),
                                           temperature=0.85,
                                           channel_ctx=channel_ctx)
            batch_file.write_text(json.dumps({"batch": batch, "pairs": pairs},
                                             ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        if start == 0:
            meta = _meta_from_batch(dict(batch), topic, cefr, num_pairs)
        all_pairs.extend(pairs)
        start += count
        print(f"  [Sleep] Pairs {start}/{num_pairs} done")

    script = dict(meta)
    script["lesson_type"] = "listening"
    script["structure"] = "sleep"
    # 缩略图集数按主题系列计数（thumbnail_gen.assign_sleep_episode 消费）
    script["topic"] = topic
    # 朗读声部（非剧情角色）：char_a 恒男声、char_b 恒女声，voice_map 按性别映射
    script["char_a_description"] = "male narrator voice"
    script["char_b_description"] = "female narrator voice"
    script["char_a_gender"] = "male"
    script["char_b_gender"] = "female"
    script["char_a_role"] = "narrator (male voice)"
    script["char_b_role"] = "narrator (female voice)"
    # 冷开场：无旁白字幕卡；intro/outro 文案来自配置（sleep_channel_name 等）
    script["welcome_en"] = ""
    script["welcome_zh"] = ""
    script["story_hook"] = ""
    script["intro_zh"] = ""
    script["outro"] = ""
    script["outro_zh"] = ""
    script["practice_intro_en"] = ""
    script["practice_intro_zh"] = ""
    script.setdefault("scene", "")
    script.setdefault("thumbnail_expression", "")
    script.setdefault("thumbnail_action", "")
    script.setdefault("thumbnail_subtitle", f"{num_pairs * 2}句睡前英文")
    script.setdefault("thumbnail_icons", [])
    script["dialogue"] = _pairs_to_rows(all_pairs)
    return script


def generate_thumb_text_candidates(ctx: dict, count: int = 5) -> list[dict]:
    """LLM 产出 N 组缩略图文案候选（badge/main/hook），供 Web 端挑选后重生成。

    ctx 键：topic / title_zh / scene / cefr / n_pairs /
    current{badge, main, hook} / channel{name_en, niche, audience}|None。
    返回 [{badge, main, hook}, ...]（繁體中文，长度软校验 + 去重）。
    频道上下文存在时文案向该频道定位倾斜（同脚本生成品牌段语义）。
    """
    topic = str(ctx.get("topic", "") or "").strip()
    title_zh = str(ctx.get("title_zh", "") or "").strip()
    scene = str(ctx.get("scene", "") or "").strip()
    cefr = str(ctx.get("cefr", "") or "").strip()
    n_pairs = int(ctx.get("n_pairs", 0) or 0)
    current = ctx.get("current") or {}
    cur_badge = str(current.get("badge", "") or "").strip()
    cur_main = str(current.get("main", "") or "").strip()
    cur_hook = str(current.get("hook", "") or "").strip()

    channel_line = ""
    ch = ctx.get("channel") or {}
    if str(ch.get("name_en", "") or "").strip():
        parts = [f'published on channel "{ch["name_en"]}"']
        if ch.get("niche"):
            parts.append(f"channel niche: {ch['niche']}")
        if ch.get("audience"):
            parts.append(f"target audience: {ch['audience']}")
        channel_line = ("\nCHANNEL CONTEXT: " + "; ".join(parts)
                        + " — tilt the copy toward this positioning.")

    prompt = f"""You are a YouTube CTR copywriter for a "listen while you sleep" English learning channel (overseas Chinese audience, zero-basics friendly).

Video context:
- Topic: {topic or "(unknown)"}
- Chinese title: {title_zh or "(unknown)"}
- Scene: {scene or "(everyday life)"}
- CEFR: {cefr or "A2"}
- Size: {n_pairs or "several hundred"} short phrase pairs ({n_pairs * 2 if n_pairs else "several hundred"} sentences total)
- Current thumbnail copy (reference only — generate BETTER / DIFFERENT ones): badge="{cur_badge}", main="{cur_main}", hook="{cur_hook}"{channel_line}

Generate {count} thumbnail copy CANDIDATE SETS. Each set has 3 Traditional Chinese (繁體中文) fields:
- "badge": top-left red banner slogan, AT MOST 6 characters, high-CTR hook (e.g. "不用背！" / "聽著聽著就會說")
- "main": the HUGE main title, 2-5 characters, a punchy series/brand word matching this topic (e.g. "睡覺聽" / "聽就會")
- "hook": bottom ribbon line, AT MOST 14 characters, benefit/promise statement (e.g. "零基礎自然開口說" / "聽久自然開口說")

Requirements:
- ALL text MUST be Traditional Chinese (繁體中文), no Simplified characters
- Each set takes a DIFFERENT angle: pain point / curiosity / benefit promise / identity / call-to-action
- Short and punchy, no filler words — must read instantly as big thumbnail text
- Do NOT simply repeat the current copy

Output JSON ONLY (no markdown, no explanation):
{{"candidates": [{{"badge": "...", "main": "...", "hook": "..."}}, ...]}}"""

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            content = _chat(
                [{"role": "system",
                  "content": "You are a YouTube thumbnail copywriter for an English-learning channel. Output valid JSON only — no markdown, no explanations."},
                 {"role": "user", "content": prompt}],
                temperature=0.95,
                max_tokens=2048,
                reasoning_effort="low")
            data = _extract_json(content)
            raw = data.get("candidates", []) if isinstance(data, dict) else (
                data if isinstance(data, list) else [])
            seen: set[tuple[str, str, str]] = set()
            result: list[dict] = []
            for c in raw:
                if not isinstance(c, dict):
                    continue
                badge = str(c.get("badge", "") or "").strip()
                main = str(c.get("main", "") or "").strip()
                hook = str(c.get("hook", "") or "").strip()
                # 软长度上限（prompt 建议值略放宽），空字段/超长直接丢弃
                if not (badge and main and hook):
                    continue
                if len(badge) > 8 or len(main) > 6 or len(hook) > 16:
                    continue
                key = (badge, main, hook)
                if key in seen:
                    continue
                seen.add(key)
                result.append({"badge": badge, "main": main, "hook": hook})
                if len(result) >= count:
                    break
            if result:
                return result
            last_err = RuntimeError("LLM 候选全部无效（空字段或超长）")
        except Exception as e:  # noqa: BLE001 — 记录后重试
            last_err = e
            print(f"  [ThumbText][retry {attempt + 1}/2] {type(e).__name__}: {str(e)[:200]}")
    raise RuntimeError(f"缩略图文案候选生成失败: {last_err}")
