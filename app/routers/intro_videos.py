"""🎬 片头库 API — sleep 模式片头生成（本地动画 / MCP AI 视频，时长可设 4-15s）
+ 自定义片头上传 + 库管理 + LLM 随机片头提示词生成。

- 频道上下文（2026-09-17 起）：全部端点收 channel 参数（?channel= / body /
  FormData，URL 为唯一事实源）—— 频道读写自己的独立片头库
  （configs/channels/{cid}/intro_library.json + intro_videos/{id}/），
  绑定写频道配置快照 sleep_intro_video；全局上下文行为不变（全局库 +
  mode_sleep.json）。运行解析：pipeline_service 按频道库优先、全局库回退。
- 本地路线：pipeline/sleep/intro_video.build_local_intro —— Pillow 逐帧渲染
  sleep 主题动画（渐变背景 + 叶片漂动 + 频道名淡入），零积分；
- AI 路线：PageMcpSession generate_video（text_to_video / 时长可设 / 16:9 /
  720p / 无音频）→ 下载 → finalize_ai_intro 标准化（频道名由 AI 画进画面，
  本地不再叠加文字防重复）；
- 提示词：POST /gen_prompts 仅凭频道名让 LLM 一次生成 count 个（默认 5，
  上限 20）片头场景
  提示词（prompt_en 含频道名入画 title moment + desc_zh 简体中文说明），
  支持场景主题可选（PROMPT_THEMES：空 = 数量不足时各取不同主题 1 条、
  数量≥主题数时按序全覆盖，指定主题 id = count 条同主题不同子场景），
  前端点选填入 AI 画面描述；
- 上传：POST /upload 自带视频 → standardize_upload_intro 规格统一（保留
  原声与原时长，无音轨补静音）→ source=upload 入库；
- 音频统一：BGM（bgm_music 库选一/随机）淡入淡出 + 可选频道名 TTS 播报
  （sleep 模式 tts_engine 合成，TTS_SYNTH_LOCK 内执行）；
- 产物 {上下文库}/intro_videos/{id}/intro.mp4，索引 {上下文库}/intro_library.json；
- POST /use 写入上下文配置 sleep_intro_video（pipeline_service 注入 CLI），
  空 = 回退默认片头（静态卡片 + 频道名播报）。
"""
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ..config_manager import (load_mode_config, resolve_provider,
                              resolve_mcp_tokens, save_mode_config)
from ..intro_library import (INTRO_ID_RE, intro_file, load_library,
                             save_library, videos_dir)
from ..page_mcp import PageMcpSession
from ..paths import WEB_ROOT
from ..tts_state import TTS_SYNTH_LOCK

router = APIRouter()

_gen_status: dict = {"status": "idle", "error": "", "intro_id": "", "logs": []}
_prompt_status: dict = {"status": "idle", "prompts": [], "error": ""}

_DEFAULT_AI_SCENE = (
    "A cozy dreamy night scene for a sleep English learning channel intro: "
    "soft fluffy clouds drifting under a starry night sky, warm moonlight "
    "through a window, a few gentle fireflies, soft pastel colors, slow calm "
    "camera drift, smooth 3D Pixar animation style, peaceful sleep atmosphere"
)


def _log(msg: str) -> None:
    _gen_status["logs"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
    print(f"  [IntroLibrary] {msg}")


def _sleep_cfg(channel: str = "") -> dict:
    """上下文配置：channel 非空 → 频道完整快照（品牌色/凭据/TTS/BGM 独立）。

    空 → 全局 mode_sleep.json（历史行为）。"""
    cid = str(channel or "").strip()
    if cid:
        from ..channel_profiles import load_channel_config
        return load_channel_config(cid)
    return load_mode_config("sleep")


def _check_channel(channel: str):
    """频道参数校验：非空但频道实体不存在时返回 404 响应，否则 None 放行。"""
    cid = str(channel or "").strip()
    if cid:
        from ..channel_profiles import get_channel
        if get_channel(cid) is None:
            return JSONResponse({"ok": False, "error": f"频道不存在: {cid}"},
                                status_code=404)
    return None


# ---------------------------------------------------------------------------
# 片头生成
# ---------------------------------------------------------------------------

def _synth_announce(channel_name: str, cfg: dict, cid: str = "") -> str:
    """频道名 TTS 播报（sleep 引擎，char_a 男声旁白）。返回 mp3 路径。

    cfg/ cid = 上下文配置与频道 id（临时文件落对应库目录）。"""
    from sleep.audio_sleep import build_engine_and_voice_map
    engine_name = str(cfg.get("tts_engine", "kokoro") or "kokoro")
    fake = {"structure": "sleep", "char_a_gender": "male",
            "char_b_gender": "female"}
    out = str(videos_dir(cid) / ".announce_tmp.mp3")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with TTS_SYNTH_LOCK:
        tts, vmap = build_engine_and_voice_map(engine_name, fake)
        voice = vmap.get("char_a") or "am_adam"
        tts.synth_english(channel_name, voice, out, rate="+0%")
    if not Path(out).exists() or Path(out).stat().st_size < 1000:
        raise RuntimeError("频道名播报 TTS 合成失败（无有效音频）")
    return out


def _resolve_bgm(bgm_choice: str, cfg: dict) -> str:
    from sleep.intro_video import resolve_bgm
    bgm_dir = str(cfg.get("bgm_music_dir", "") or "").strip() or str(WEB_ROOT / "bgm_music")
    return resolve_bgm(bgm_dir, bgm_choice)


def _parse_duration(v) -> float:
    """片头时长（秒）：clamp [4, 15]（MCP Seedance 2.0 默认模型上限）；空/非法回退 10。"""
    if v is None or str(v).strip() == "":
        return 10.0
    try:
        dur = float(v)
    except (TypeError, ValueError):
        return 10.0
    return round(max(4.0, min(15.0, dur)), 1)


def _build_ai_prompt(scene_prompt: str, channel: str) -> str:
    """场景提示词 + 频道名入画约束（AI 画字需逐字母强调拼写，且为画面唯一文字）。"""
    base = scene_prompt.strip() or _DEFAULT_AI_SCENE
    name = (channel or "").strip() or "English with me"
    return (base + f' The channel name "{name}" appears in the scene, spelled '
            f'EXACTLY "{name}" letter-for-letter — it is the ONLY text allowed; '
            "once it appears, it stays continuously visible in the CENTER of "
            "the frame until the very end, never disappearing and never "
            "drifting away from the center; "
            "no other text, no captions, no watermarks.")


def _generate_ai_video(scene_prompt: str, dest: Path, duration: float = 10.0,
                       channel: str = "", cfg: dict | None = None) -> None:
    """MCP generate_video 原始场景视频（频道名由 AI 画进画面，无音频，播报本地混）。

    MCP token：频道上下文快照 mcp_tokens 优先，空回退全局解析链
    （模式配置 → legacy default.json → 本机 CLI 检测）。"""
    raw_tokens = str((cfg or {}).get("mcp_tokens", "") or "").strip()
    tokens = [t.strip() for t in (raw_tokens or resolve_mcp_tokens("sleep")).splitlines()
              if t.strip()]
    if not tokens:
        raise RuntimeError("未配置 MCP Token（模式配置 / default.json / 本地检测均为空）"
                           "—— AI 片头需要 MCP，或改用本地动画路线")
    session = PageMcpSession(tokens).initialize()
    prompt = _build_ai_prompt(scene_prompt, channel)
    _log(f"MCP generate_video 提交中（{duration:g}s / 720p / 16:9 / 无音频）...")
    result = session.call_tool("generate_video", {
        "mode": "text_to_video", "prompt": prompt,
        "duration": duration, "ratio": "16:9", "resolution": "720p",
        "generate_audio": False,
    })
    task_id = session.parse_task_id(result)
    if not task_id:
        raw = ""
        for item in result.get("result", {}).get("content", []):
            if item.get("type") == "text":
                raw = str(item.get("text", ""))[:300].replace("\n", " ")
                break
        raise RuntimeError(f"MCP 未返回任务 ID（响应: {raw}）")
    _log(f"Task: {task_id[:16]}... 轮询中（AI 视频通常 2-8 分钟）")
    data = session.poll_task(task_id, interval=15, max_wait=1800)
    url = data.get("url", "")
    if data.get("status") != "completed" or not url:
        raise RuntimeError(f"MCP 视频任务未完成: {data.get('status') or 'no status'}"
                           + (f" error={data.get('error')}" if data.get("error") else ""))
    if not session.download_file(url, str(dest)):
        raise RuntimeError("AI 视频下载落盘失败")


def _generate_worker(params: dict) -> None:
    cid = str(params.get("channel", "") or "").strip()
    intro_id = f"intro_{int(time.time() * 1000)}"
    _gen_status.update({"status": "running", "error": "", "intro_id": intro_id,
                        "logs": []})
    out_dir = videos_dir(cid) / intro_id
    final_path = out_dir / "intro.mp4"
    try:
        route = params["route"]
        channel_name = params["channel_name"]
        subtitle = params["subtitle"]
        dur = _parse_duration(params.get("duration"))
        cfg = _sleep_cfg(cid)
        out_dir.mkdir(parents=True, exist_ok=True)
        _log(f"生成片头（{'AI 视频' if route == 'ai' else '本地动画'}，{dur:g}s）: {channel_name}"
             + (f"（频道 {cid} 独立库）" if cid else ""))
        # "none" = 用户显式不使用 BGM（成片时由 bgm_mix 统一混入，避免片头双重 BGM）；
        # resolve_bgm 对未知名本就返回 ""，这里显式短路让日志语义准确
        if str(params["bgm"]) == "none":
            bgm_path = ""
            _log("BGM: 不使用（成片生成时由 bgm_mix 统一混入）")
        else:
            bgm_path = _resolve_bgm(params["bgm"], cfg)
            _log(f"BGM: {Path(bgm_path).name}" if bgm_path else "BGM: 无可用音乐（静音）")
        announce_path = ""
        if params["announce"]:
            _log("合成频道名播报 TTS ...")
            announce_path = _synth_announce(channel_name, cfg, cid)

        from sleep.sleep_cards import build_theme
        theme = build_theme(cfg)
        if route == "ai":
            raw_path = out_dir / "raw.mp4"
            _generate_ai_video(params["scene_prompt"], raw_path, dur, channel_name, cfg)
            from sleep.intro_video import finalize_ai_intro
            finalize_ai_intro(str(raw_path), channel_name, subtitle, str(final_path),
                              theme, bgm_path=bgm_path,
                              bgm_volume_db=params["bgm_volume_db"],
                              announce_path=announce_path,
                              duration=dur,
                              overlay_text=False,
                              progress_cb=lambda p, m: _log(f"[{p}%] {m}"))
            try:
                raw_path.unlink()
            except OSError:
                pass
        else:
            from sleep.intro_video import build_local_intro
            build_local_intro(channel_name, subtitle, str(final_path), theme,
                              bgm_path=bgm_path,
                              bgm_volume_db=params["bgm_volume_db"],
                              announce_path=announce_path,
                              duration=dur,
                              progress_cb=lambda p, m: _log(f"[{p}%] {m}"))

        from media_utils import get_duration
        entry = {"id": intro_id, "name": channel_name, "source": route,
                 "duration": round(get_duration(str(final_path)), 2),
                 "created": time.time(), "subtitle": subtitle,
                 "bgm": Path(bgm_path).name if bgm_path else "",
                 "announce": bool(announce_path)}
        lib = load_library(cid)
        lib.insert(0, entry)
        save_library(lib, cid)
        _log(f"片头已入库: {intro_id}" + (f"（频道 {cid} 独立库）" if cid else ""))
        _gen_status.update({"status": "done", "intro_id": intro_id})
    except Exception as e:  # noqa: BLE001 — 错误原样落状态供前端展示
        print(f"  [IntroLibrary] ERROR: {e}")
        _log(f"ERROR: {e}")
        _gen_status.update({"status": "error", "error": str(e)[:300]})
        shutil.rmtree(out_dir, ignore_errors=True)


@router.post("/api/intro_videos/generate")
async def api_generate(request: Request):
    """启动片头生成（单槽 409 守卫；静态路径须在 {intro_id} 动态路由之前注册）。

    body.channel = 频道 id：产物入频道独立库，主题色/TTS/BGM 按频道快照。"""
    if _gen_status.get("status") == "running":
        return JSONResponse({"ok": False, "error": "已有片头生成任务进行中，请稍候"},
                            status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    cid = str(data.get("channel", "") or "").strip()
    bad = _check_channel(cid)
    if bad is not None:
        return bad
    route = str(data.get("route", "local") or "local")
    if route not in ("local", "ai"):
        route = "local"
    cfg = _sleep_cfg(cid)
    channel = str(data.get("channel_name", "") or "").strip()[:60] \
        or str(cfg.get("sleep_channel_name", "") or "").strip() \
        or "English with me"
    subtitle = str(data.get("subtitle", "") or "").strip()[:60]
    bgm = str(data.get("bgm", "") or "").strip()
    try:
        bgm_volume_db = max(-40.0, min(0.0, float(data.get("bgm_volume_db", -16))))
    except (TypeError, ValueError):
        bgm_volume_db = -16.0
    announce = bool(data.get("announce", True))
    scene_prompt = str(data.get("scene_prompt", "") or "").strip()[:600]
    duration = _parse_duration(data.get("duration"))

    threading.Thread(target=_generate_worker,
                     args=({"route": route, "channel": cid,
                            "channel_name": channel,
                            "subtitle": subtitle, "bgm": bgm,
                            "bgm_volume_db": bgm_volume_db,
                            "announce": announce,
                            "scene_prompt": scene_prompt,
                            "duration": duration},),
                     daemon=True).start()
    return {"ok": True, "message": "片头生成中（本地约 1-2 分钟 / AI 约 3-10 分钟）..."}


@router.get("/api/intro_videos/status")
async def api_status():
    return _gen_status


@router.get("/api/intro_videos/bgm_list")
async def api_bgm_list(channel: str = ""):
    from sleep.intro_video import list_bgm_files
    cfg = _sleep_cfg(channel)
    bgm_dir = str(cfg.get("bgm_music_dir", "") or "").strip() or str(WEB_ROOT / "bgm_music")
    return {"files": list_bgm_files(bgm_dir), "dir": bgm_dir}


# ---------------------------------------------------------------------------
# LLM 随机片头提示词（仅凭频道名 → 10 个 prompt_en + desc_zh 中文说明）
# ---------------------------------------------------------------------------

_PROMPT_SYSTEM = (
    "You are a creative director for cinematic AI-generated intro videos. "
    "Output valid JSON only — no markdown, no explanations."
)

# 场景主题库（sleep 片头专属氛围场景预设）：「全部主题」= 按数量取不同主题
# 各 1 条（数量 ≥ 主题数时按给定顺序全覆盖）；点选单主题 = count 条同主题
# 不同子场景/机位。label 供前端标签与卡片徽章展示，en_hint 注入 LLM 提示词
# 约束场景方向。
PROMPT_THEMES: list[dict[str, str]] = [
    {"id": "starry_sky", "label": "🌌 星空云海",
     "en_hint": "starry night sky with soft clouds drifting under the Milky Way"},
    {"id": "rainy_window", "label": "🌧️ 雨夜窗边",
     "en_hint": "cozy warm bedroom by a rainy window at night, raindrops on the glass"},
    {"id": "moonlit_forest", "label": "🌲 月光森林",
     "en_hint": "moonlit forest at night, silver moonlight through tall trees"},
    {"id": "night_ocean", "label": "🌊 夜海波浪",
     "en_hint": "calm night ocean, gentle moonlit waves, starlight reflections on water"},
    {"id": "lantern_clouds", "label": "🏮 灯笼云船",
     "en_hint": "floating glowing lanterns drifting over a dreamy sea of clouds"},
    {"id": "snow_cabin", "label": "🏔️ 雪山小屋",
     "en_hint": "snowy mountain cabin at dusk, warm golden window glow, gently falling snow"},
    {"id": "moonlight_sail", "label": "⛵ 月下帆船",
     "en_hint": "a small sailboat gliding on a moonlit calm sea"},
    {"id": "firefly_garden", "label": "✨ 萤火虫花园",
     "en_hint": "midnight garden full of drifting fireflies and blooming night flowers"},
    {"id": "moon_windowsill", "label": "🌕 月光窗台",
     "en_hint": "a cozy windowsill bathed in warm full-moon light, potted plants and a cup of tea"},
    {"id": "cloud_drift", "label": "☁️ 云端漂浮",
     "en_hint": "slowly floating above a sea of soft night clouds"},
]
_PROMPT_THEME_IDS = {t["id"] for t in PROMPT_THEMES}


def _build_prompts_prompt(channel: str, theme: str = "", count: int = 5) -> str:
    name = (channel or "").strip() or "English with me"
    theme_lines = "\n".join(
        f'- "{t["id"]}": {t["label"]} — {t["en_hint"]}' for t in PROMPT_THEMES)
    n_themes = len(PROMPT_THEMES)
    if theme:
        theme_rule = (
            f'ALL {count} concepts belong to the SINGLE theme "{theme}" — explore {count} clearly '
            "different sub-scenes / camera angles / moments within that theme "
            "(use its en_hint as the direction, but vary freely inside it)."
        )
    elif count == n_themes:
        theme_rule = (
            f"The {count} concepts must cover ALL {n_themes} themes below exactly once each, "
            "IN THE GIVEN ORDER (concept 1 = theme 1, concept 2 = theme 2, ...)."
        )
    elif count > n_themes:
        theme_rule = (
            f"The {count} concepts must cover ALL {n_themes} themes below at least once each, "
            f"IN THE GIVEN ORDER (concept 1 = theme 1, ..., concept {n_themes} = theme "
            f"{n_themes}, then cycle back to theme 1 for the remaining concepts)."
        )
    else:
        theme_rule = (
            f"Pick {count} DIFFERENT themes from the list below (no theme repeats) — "
            f"exactly one concept per theme, {count} concepts in total."
        )
    entries = ", ".join(
        '{"theme": "theme_id", "prompt_en": "...", "desc_zh": "..."}' for _ in range(count))
    return f"""Create {count} clearly different ambient intro video scene concepts for a sleep-relaxation English learning YouTube channel named "{name}". Audience: overseas Chinese ESL learners winding down before sleep.

Each concept is an AI text-to-video prompt. Mood: calm, dreamy and peaceful — perfect for falling asleep. Every concept MUST feature ONE title moment: the channel name "{name}" appears in the scene, spelled EXACTLY "{name}", and once it appears it stays in the CENTER of the frame continuously until the end.

Scene themes (each concept belongs to exactly one):
{theme_lines}

{theme_rule}

For each concept output:
- "theme": the theme id of this concept, copied EXACTLY from the list above
- "prompt_en": one rich English paragraph (70-120 words) describing ONE continuous very slow shot: the scene, lighting, color mood, art style (vary across concepts: soft 3D Pixar animation, dreamy pastel illustration, cinematic realism, watercolor, etc.), and a very slow gentle camera drift. Include the title moment: the channel name "{name}" appears within the first two seconds and then remains continuously visible in the CENTER of the frame for the ENTIRE rest of the video — once it is visible it must NEVER disappear or leave the center (no fading out, no drifting around or out of frame, no vanishing and re-appearing) — describe how it materializes and its lettering style (e.g. elegant glowing handwritten script traced by fireflies, soft 3D golden letters drifting out of the clouds, starlight gathering into letters). The channel name "{name}" is the ONLY text in the scene, spelled EXACTLY letter-for-letter — never any other words, letters, captions, subtitles or watermarks.
- "desc_zh": 1-2 句简体中文，概括这段画面长什么样（含频道名如何出现，让用户不看英文也能想象出视频的大致样子）。

Art styles must still vary across the {count} concepts even when the theme repeats.

Output valid JSON only:
{{"intros": [{entries}]}}"""


def _llm_chat(base_url: str, api_key: str, model: str, p_type: str,
              prompt: str, proxy_url: str = "",
              wbk_effort: str = "default") -> str:
    """同步调 LLM 生成提示词，返回 content。独立函数便于测试 monkeypatch。

    与 channel_factory._llm_chat 同模式：gemini 走 SDK，其余 OpenAI 兼容
    /chat/completions（wbk 按模型规格表）。
    """
    from llm_client import gemini_chat, llm_urlopen, wbk_thinking_for  # pipeline/ 已在 sys.path
    messages = [{"role": "system", "content": _PROMPT_SYSTEM},
                {"role": "user", "content": prompt}]
    if p_type == "gemini":
        return gemini_chat(api_key, model, messages, temperature=0.95,
                           max_tokens=8192, timeout=180, proxy_url=proxy_url)
    body = {"model": model, "messages": messages,
            "temperature": 0.95, "max_tokens": 8192}
    if p_type == "wbk":
        _effort = wbk_thinking_for(model, wbk_effort)
        if _effort:
            body["reasoning_effort"] = _effort
    elif p_type != "openai":
        body["reasoning_effort"] = "low"
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "CodelyLLM/1.0")
    try:
        with llm_urlopen(req, 180, proxy_url) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"LLM API HTTP {e.code}: {detail}") from e
    choice = (result.get("choices") or [{}])[0]
    return str(choice.get("message", {}).get("content", ""))


def _normalize_prompts(raw_list) -> list[dict]:
    """LLM 返回列表 → [{"prompt_en", "desc_zh", "theme"}]（无 prompt_en 的条目丢弃）。

    theme 仅保留已知主题 id（LLM 偶发输出标签/漏字段 → 置空，前端隐藏徽章）。"""
    out: list[dict] = []
    for raw in raw_list or []:
        if not isinstance(raw, dict):
            continue
        en = str(raw.get("prompt_en", "") or "").strip()
        zh = str(raw.get("desc_zh", "") or "").strip()
        if en:
            theme = str(raw.get("theme", "") or "").strip()
            if theme not in _PROMPT_THEME_IDS:
                theme = ""
            out.append({"prompt_en": en[:900], "desc_zh": zh[:160],
                        "theme": theme[:40]})
    return out


# 提示词生成重试：LLM 偶发返回非法/残缺 JSON（如 "Expecting ',' delimiter"，
# 截断修复救不回的畸形输出），解析失败或无有效条目时整体重新生成。
_PROMPTS_MAX_ATTEMPTS = 3
_PROMPTS_RETRY_WAIT = 2
# 单次生成的条数上限：prompt_en 每条约 150-200 token，20 条仍稳在 max_tokens=8192 内
_PROMPTS_MAX_COUNT = 20


def _prompts_worker(channel_name: str, cid: str = "", theme: str = "",
                    count: int = 5) -> None:
    _prompt_status.update({"status": "generating", "prompts": [], "error": ""})
    try:
        import time as _time
        from llm_client import _extract_json, proxy_url_from_config  # pipeline/ 已在 sys.path
        cfg = _sleep_cfg(cid)  # 频道上下文 → 频道 LLM Provider/Key/模型
        p_type, base_url, api_key, model = resolve_provider(cfg)
        if not api_key:
            raise RuntimeError(f"未配置 {p_type} 的 API Key，请在参数配置页填写")
        if not model:
            raise RuntimeError("未指定模型（该 Provider 未配置模型列表）")
        prompt_text = _build_prompts_prompt(channel_name, theme, count)
        proxy_url = proxy_url_from_config(cfg)
        prompts: list[dict] = []
        last_err: Exception | None = None
        theme_label = next((t["label"] for t in PROMPT_THEMES if t["id"] == theme), "")
        for attempt in range(1, _PROMPTS_MAX_ATTEMPTS + 1):
            _log(f"LLM 生成片头提示词：{model} ({p_type})，频道「{channel_name}」"
                 + (f"，主题「{theme_label}」" if theme_label else "（全部主题）")
                 + (f"（第 {attempt}/{_PROMPTS_MAX_ATTEMPTS} 次）" if attempt > 1 else ""))
            try:
                content = _llm_chat(base_url, api_key, model, p_type,
                                    prompt_text, proxy_url=proxy_url,
                                    wbk_effort=str(cfg.get("wbk_thinking") or "default"))
                data = _extract_json(content)
                if isinstance(data, dict):
                    raw_list = data.get("intros")
                elif isinstance(data, list):
                    raw_list = data
                else:
                    raw_list = None
                prompts = _normalize_prompts(raw_list)
                if prompts:
                    break
                raise ValueError("LLM 返回内容中没有有效提示词")
            except Exception as e:  # noqa: BLE001 — 调用/解析失败重试
                last_err = e
                prompts = []
                if attempt < _PROMPTS_MAX_ATTEMPTS:
                    print(f"  [IntroLibrary] 第 {attempt} 次生成失败（{e}），重试...")
                    _time.sleep(_PROMPTS_RETRY_WAIT)
        if not prompts:
            raise RuntimeError(f"生成失败（已尝试 {_PROMPTS_MAX_ATTEMPTS} 次）：{last_err}")
        _log(f"已生成 {len(prompts)} 条提示词")
        _prompt_status.update({"status": "done", "prompts": prompts, "error": ""})
    except Exception as e:  # noqa: BLE001 — 错误原样落状态供前端展示
        print(f"  [IntroLibrary] PROMPTS ERROR: {e}")
        _log(f"ERROR: {e}")
        _prompt_status.update({"status": "error", "prompts": [], "error": str(e)[:300]})


@router.post("/api/intro_videos/gen_prompts")
async def api_gen_prompts(request: Request):
    """LLM 生成 count 个（body.count 可调，默认 5，clamp [1, 20]）随机片头提示词
    （单槽 409 守卫；静态路径须在 {intro_id} 动态路由之前）。

    body.channel = 频道 id：LLM Provider/Key/模型按频道快照解析。"""
    if _prompt_status.get("status") == "generating":
        return JSONResponse({"ok": False, "error": "提示词生成进行中，请稍候"},
                            status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    cid = str(data.get("channel", "") or "").strip()
    bad = _check_channel(cid)
    if bad is not None:
        return bad
    cfg = _sleep_cfg(cid)
    channel = str(data.get("channel_name", "") or "").strip()[:60] \
        or str(cfg.get("sleep_channel_name", "") or "").strip() \
        or "English with me"
    theme = str(data.get("theme", "") or "").strip()
    if theme and theme not in _PROMPT_THEME_IDS:
        theme = ""
    try:
        count = int(data.get("count", 5))
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(_PROMPTS_MAX_COUNT, count))
    threading.Thread(target=_prompts_worker, args=(channel, cid, theme, count),
                     daemon=True).start()
    return {"ok": True, "message": "提示词生成中（约 10-60 秒）..."}


@router.get("/api/intro_videos/prompts_status")
async def api_prompts_status():
    return _prompt_status


# ---------------------------------------------------------------------------
# 库管理
# ---------------------------------------------------------------------------

@router.get("/api/intro_videos")
async def api_list(channel: str = ""):
    """列上下文库（?channel= 频道独立库 / 空 = 全局库）+ 当前绑定。

    used_missing：频道上下文的绑定项不在频道库（历史全局绑定）→ 前端提示。
    """
    cid = str(channel or "").strip()
    used = str(_sleep_cfg(cid).get("sleep_intro_video", "") or "").strip()
    intros = []
    for e in load_library(cid):
        iid = str(e.get("id", ""))
        f = intro_file(iid, cid) if INTRO_ID_RE.match(iid) else None
        exists = bool(f and f.exists())
        q = f"channel={cid}&" if cid else ""
        intros.append({**e, "exists": exists, "used": used == iid,
                       "video_url": f"/api/intro_videos/{iid}/video?{q}"
                                    + (str(int(f.stat().st_mtime_ns)) if exists else "0")})
    used_missing = bool(cid and used
                        and not any(e.get("id") == used for e in intros))
    return {"intros": intros, "used": used, "used_missing": used_missing}


@router.post("/api/intro_videos/use")
async def api_use(request: Request):
    """绑定/解绑上下文片头（body {id: ""} = 回退默认片头）。

    body.channel = 频道 id：绑定写频道配置快照（空 = 写全局 sleep 配置）。
    频道上下文仅允许绑定频道库内条目（保持各频道库互不影响）。
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    cid = str(data.get("channel", "") or "").strip()
    bad = _check_channel(cid)
    if bad is not None:
        return bad
    iid = str(data.get("id", "") or "").strip()
    if iid:
        if not INTRO_ID_RE.match(iid):
            return JSONResponse({"ok": False, "error": "无效的片头 id"}, status_code=400)
        if not intro_file(iid, cid).exists():
            return JSONResponse({"ok": False, "error": "片头文件不存在"}, status_code=404)
    cfg = _sleep_cfg(cid)
    cfg["sleep_intro_video"] = iid
    if cid:
        from ..channel_profiles import save_channel_config
        save_channel_config(cid, cfg)
    else:
        save_mode_config("sleep", cfg)
    return {"ok": True, "used": iid, "channel": cid}


_UPLOAD_MAX_BYTES = 200 * 1024 * 1024
_UPLOAD_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")


@router.post("/api/intro_videos/upload")
async def api_upload(video: UploadFile = File(...), name: str = Form(""),
                     channel: str = Form("")):
    """上传自定义片头 → 规格标准化入库（source=upload，保留原声与原时长）。

    Form.channel = 频道 id：入频道独立库（空 = 全局库）。
    纯本地 ffmpeg 处理，与生成槽/MCP 互斥无关，同步返回。
    """
    cid = str(channel or "").strip()
    bad = _check_channel(cid)
    if bad is not None:
        return bad
    filename = (video.filename or "").lower()
    if not filename.endswith(_UPLOAD_EXTS):
        return JSONResponse({"ok": False,
                             "error": "仅支持 mp4/mov/webm/mkv/m4v/avi 视频文件"},
                            status_code=400)
    content = await video.read()
    if len(content) < 10240:
        return JSONResponse({"ok": False, "error": "视频文件过小"}, status_code=400)
    if len(content) > _UPLOAD_MAX_BYTES:
        return JSONResponse({"ok": False, "error": "视频过大（上限 200MB）"},
                            status_code=400)
    display = str(name or "").strip()[:60] \
        or Path(filename).stem.strip()[:60] or "自定义片头"
    intro_id = f"intro_{int(time.time() * 1000)}"
    out_dir = videos_dir(cid) / intro_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / f"_raw{Path(filename).suffix or '.mp4'}"
    final_path = out_dir / "intro.mp4"
    try:
        raw_path.write_bytes(content)
        from sleep.intro_video import standardize_upload_intro
        dur = standardize_upload_intro(str(raw_path), str(final_path))
    except Exception as e:  # noqa: BLE001 — 清理后原样返回错误
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"  [IntroLibrary] UPLOAD ERROR: {e}")
        return JSONResponse({"ok": False,
                             "error": f"片头标准化失败: {str(e)[:200]}"},
                            status_code=500)
    finally:
        try:
            raw_path.unlink()
        except OSError:
            pass
    entry = {"id": intro_id, "name": display, "source": "upload",
             "duration": round(dur, 2), "created": time.time()}
    lib = load_library(cid)
    lib.insert(0, entry)
    save_library(lib, cid)
    print(f"  [IntroLibrary] 片头上传入库: {intro_id} ({display}, {dur:.1f}s)"
          + (f"（频道 {cid} 独立库）" if cid else ""))
    return {"ok": True, "id": intro_id, "duration": round(dur, 2), "channel": cid}


@router.delete("/api/intro_videos/{intro_id}")
async def api_delete(intro_id: str, channel: str = ""):
    """删除上下文库中的片头（?channel= 频道库 / 空 = 全局库），互不波及对方。"""
    if not INTRO_ID_RE.match(intro_id):
        return JSONResponse({"ok": False, "error": "无效的片头 id"}, status_code=400)
    cid = str(channel or "").strip()
    bad = _check_channel(cid)
    if bad is not None:
        return bad
    lib = load_library(cid)
    remaining = [e for e in lib if e.get("id") != intro_id]
    if len(remaining) == len(lib):
        return JSONResponse({"ok": False, "error": "未找到该片头"}, status_code=404)
    save_library(remaining, cid)
    shutil.rmtree(intro_file(intro_id, cid).parent, ignore_errors=True)
    cfg = _sleep_cfg(cid)
    if str(cfg.get("sleep_intro_video", "") or "") == intro_id:
        cfg["sleep_intro_video"] = ""
        if cid:
            from ..channel_profiles import save_channel_config
            save_channel_config(cid, cfg)
        else:
            save_mode_config("sleep", cfg)
    return {"ok": True}


@router.get("/api/intro_videos/{intro_id}/video")
async def api_video(intro_id: str, channel: str = ""):
    if not INTRO_ID_RE.match(intro_id):
        return JSONResponse({"ok": False, "error": "无效的片头 id"}, status_code=400)
    f = intro_file(intro_id, str(channel or "").strip())
    if not f.exists():
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    return FileResponse(str(f), media_type="video/mp4",
                        headers={"Cache-Control": "no-cache"})
