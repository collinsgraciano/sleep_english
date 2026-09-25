"""Pipeline service: directly imports and calls pipeline.py step functions.

Replaces the subprocess approach with direct Python function calls,
enabling real-time progress tracking and intermediate result access.
"""
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .config_manager import (
    MODES, resolve_provider, load_config, load_mode_config, find_run_dir,
)
from . import run_mutex

# Add pipeline source to path (local copy — fully independent)
PIPELINE_DIR = Path(__file__).parent.parent / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

# Import pipeline modules (all lazy-heavy, import is cheap)
from topic_manager import pick_random_topic
from media_utils import safe_filename as _safe_dirname
from llm_client import LLMStoppedError, set_llm_stop_hook
from checkpoint import (
    save_checkpoint as _save_checkpoint,
    load_checkpoint as _load_checkpoint,
    step_done as _step_done,
)

# Step detection patterns
STEP_PATTERNS = [
    (r"Step 0[:\s]", "step0_script", "LLM 脚本生成"),
    (r"Step 1[:\s]", "step1_mcp", "MCP 初始化"),
    (r"Step 2[:\s]", "step2_images_tts", "图片 + TTS 生成"),
    (r"Step 3[:\s]", "step3_video", "视频片段生成"),
    (r"Step 4\.5[:\s]", "step45_thumbnail", "缩略图 + 元数据"),
    (r"Step 4[:\s]", "step4_timeline", "时间轴 + SRT"),
    (r"Step 5\.5[:\s]", "step55_bgm", "BGM 音乐混合"),
    (r"Step 5[:\s]", "step5_compose", "视频合成"),
    (r"Step 6[:\s]", "step6_4k", "4K 超分辨率"),
]

STEP_ORDER = [
    "step0_script", "step1_mcp", "step2_images_tts",
    "step3_video", "step4_timeline", "step45_thumbnail",
    "step5_compose", "step55_bgm", "step6_4k",
]


class _LineBuffer(io.StringIO):
    """Capture stdout line-by-line, forwarding complete lines to a callback."""

    def __init__(self, on_line):
        super().__init__()
        self._on_line = on_line
        self._buf = ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._on_line(line.rstrip("\r"))
        return len(text)

    def flush(self):
        if self._buf:
            self._on_line(self._buf.rstrip("\r"))
            self._buf = ""

    def reconfigure(self, *args, **kwargs):
        # Pipeline 模块在 win32 下 import 时会调用 sys.stdout.reconfigure()；
        # Web 模式下 stdout 是本对象，接受并忽略该调用避免 AttributeError。
        pass


def _cfg_int(config: dict, key: str, default: int, lo: int = 0, hi: int = 10) -> int:
    """配置值安全转 int（空串/非法值回退默认），并 clamp 到 [lo, hi]。

    默认边界 0-10 供小整数（QA 轮数/重复次数等）沿用；大数值参数
    （sleep_pairs/sleep_batch_pairs/max_line_words 等）必须显式传边界，
    否则会被默认钳制成 10（曾致 sleep_pairs=200 实际只生成 10 组）。
    """
    try:
        return max(lo, min(hi, int(config.get(key, default))))
    except (TypeError, ValueError):
        return default


def _cfg_float(config: dict, key: str, default: float,
               lo: float = -30.0, hi: float = 15.0) -> float:
    """配置值安全转 float（空串/非法值回退默认），并 clamp 到 [lo, hi]。

    默认边界 -30~15 供音量偏移 dB 类参数沿用；其他 float 参数须显式传边界。
    """
    try:
        return max(lo, min(hi, float(config.get(key, default))))
    except (TypeError, ValueError):
        return default


def _cfg_line_words(config: dict) -> int:
    """max_line_words 配置解析：空/0/缺省 → 默认 10，非 0 值 clamp [4, 20]。

    语义与 llm_client.resolve_max_line_words 对齐（0=用默认而非钳到下限）。
    """
    raw = config.get("max_line_words", 10)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 10
    if v <= 0:
        return 10
    return max(4, min(20, v))


def _resolve_sleep_intro_video(config: dict) -> str:
    """sleep_intro_video 配置（片头库 intro_id 或 mp4 绝对路径）→ 文件路径。

    频道运行（config.channel_id 非空）按频道库优先、全局库回退解析
    （存量频道可能绑定着全局片头 id）。
    绑定无效（两库均不存在等）回退空串 = 默认片头（静态卡片 + 频道名播报）。
    """
    sel = str(config.get("sleep_intro_video", "") or "").strip()
    if not sel:
        return ""
    from .intro_library import resolve_video_path
    path = resolve_video_path(sel, str(config.get("channel_id", "") or "").strip())
    if not path:
        print(f"  [SleepIntro] 片头绑定无效（库中不存在）: {sel} —— 回退默认片头")
    else:
        print(f"  [SleepIntro] 片头绑定生效: {sel} → {path}")
    return path


def _resolve_sleep_outro_video(config: dict) -> str:
    """sleep_outro_video 配置（片尾库 outro_id 或 mp4 绝对路径）→ 文件路径。

    频道运行（config.channel_id 非空）按频道库优先、全局库回退解析。
    绑定无效（两库均不存在等）回退空串 = 默认片尾（静态卡片 + 结束语播报）。
    """
    sel = str(config.get("sleep_outro_video", "") or "").strip()
    if not sel:
        return ""
    from .outro_library import resolve_video_path
    path = resolve_video_path(sel, str(config.get("channel_id", "") or "").strip())
    if not path:
        print(f"  [SleepOutro] 片尾绑定无效（库中不存在）: {sel} —— 回退默认片尾")
    return path


def _channel_ctx(channel_id: str) -> dict | None:
    """channel_id → LLM 品牌上下文（脚本标题/简介/选题贴合频道定位）。

    无频道 / 频道缺失时返回 None（CLI 与全局运行维持现状）。
    """
    if not channel_id:
        return None
    try:
        from .channel_profiles import get_channel
        ch = get_channel(channel_id)
    except Exception:  # noqa: BLE001 — 频道上下文缺失不阻塞运行
        return None
    if not ch:
        return None
    return {
        "channel_id": ch.get("id", ""),
        "name_en": ch.get("name_en", ""),
        "name_zh": ch.get("name_zh", ""),
        "niche": ch.get("niche", ""),
        "audience": ch.get("audience", ""),
        "tags": ch.get("tags") or [],
        "brand_style": ch.get("brand_style", ""),
    }


class PipelineService:
    """Manages pipeline execution in a background thread with direct imports."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()
        self.log_lines: list[str] = []
        self.status: str = "idle"
        self.current_step: str = ""
        self.current_step_label: str = ""
        self.started_at: float = 0
        self.finished_at: float = 0
        self.config: dict = {}
        self.error: str = ""
        self.work_dir: str = ""
        self.final_path: str = ""
        self._step_mode: bool = False
        self._paused_after_step: str = ""

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._step_mode and self.status == "paused"

    def start(self, config: dict[str, Any], resume: bool = False, step_mode: bool = False) -> bool:
        if self.is_running:
            return False
        # 与模式测试互斥（MCP/TTS/FFmpeg 抢资源）；失败时在日志区提示占用方
        if not run_mutex.try_acquire("pipeline"):
            self._on_log_line(
                f"⏳ 启动被拒绝：当前被「{run_mutex.current_owner()}」占用，"
                f"请等待其完成后再启动主 pipeline。")
            return False

        self.config = config
        self._stop_flag.clear()
        self._step_mode = step_mode
        self._paused_after_step = ""
        with self._lock:
            self.log_lines = []
            self.status = "running"
            self.current_step = ""
            self.current_step_label = ""
            self.started_at = time.time()
            self.finished_at = 0
            self.error = ""
            self.work_dir = ""
            self.final_path = ""

        self._thread = threading.Thread(
            target=self._run, args=(config, resume), daemon=True)
        self._thread.start()
        return True

    def _wait_for_step_approval(self, step_name: str):
        """In step mode, pause after each step and wait for user to continue."""
        if not self._step_mode:
            return
        self._paused_after_step = step_name
        with self._lock:
            self.status = "paused"
        self._on_log_line(f"\n⏸ [Step Mode] Paused after {step_name}. Review the output, then click 'Continue' to proceed.")

        # Wait until continue or stop
        while self._paused_after_step and not self._stop_flag.is_set():
            time.sleep(0.5)

        if self._stop_flag.is_set():
            return
        with self._lock:
            self.status = "running"
        self._on_log_line(f"▶ [Step Mode] Continuing after {step_name}...")

    def continue_step(self):
        """Resume from a step-mode pause."""
        self._paused_after_step = ""

    def get_progress(self) -> dict:
        with self._lock:
            if self.current_step:
                try:
                    idx = STEP_ORDER.index(self.current_step)
                    progress = int((idx + 1) / len(STEP_ORDER) * 100)
                except ValueError:
                    progress = 0
            else:
                progress = 0

            elapsed = 0
            if self.started_at:
                end = self.finished_at if self.finished_at else time.time()
                elapsed = int(end - self.started_at)

            run_name = ""
            if self.work_dir:
                run_name = Path(self.work_dir).name

            return {
                "status": self.status,
                "current_step": self.current_step,
                "current_step_label": self.current_step_label,
                "progress": progress,
                "elapsed": elapsed,
                "log_count": len(self.log_lines),
                "is_running": self.is_running,
                "error": self.error,
                "work_dir": self.work_dir,
                "run_name": run_name,
                "final_path": self.final_path,
                "step_mode": self._step_mode,
                "paused_after_step": self._paused_after_step,
            }

    def _set_env(self, config: dict) -> None:
        """Set env vars from config dict before importing pipeline modules."""
        os.environ["LLM_RETRIES"] = str(config.get("llm_retries", 10))
        if config.get("llm_min_interval"):
            os.environ["LLM_MIN_INTERVAL"] = str(config["llm_min_interval"])

        # 画面风格：注入 env 供 llm_client / thumbnail_gen / 各 step 读取
        from style_manager import resolve_style_prompt as _rsp
        style_id = str(config.get("visual_style", "pixar3d"))
        os.environ["VISUAL_STYLE_ID"] = style_id
        os.environ["VISUAL_STYLE_PROMPT"] = _rsp(style_id)

        # 生图 Provider：mcp（TJGenerators 积分）
        os.environ["IMAGE_PROVIDER"] = str(config.get("image_provider", "mcp"))

        provider = config.get("llm_provider", "wbk")
        p_type, p_base_url, p_api_key, p_model = resolve_provider(config)
        os.environ["LLM_PROVIDER"] = p_type
        if p_type == "wbk":
            if p_api_key:
                os.environ["WBK_API_KEY"] = p_api_key
            os.environ["WBK_MODEL"] = str(p_model or "cn:auto")
            os.environ["WBK_THINKING"] = str(config.get("wbk_thinking") or "default")
        elif p_type == "gemini":
            if p_api_key:
                os.environ["GEMINI_API_KEY"] = p_api_key
            os.environ["GEMINI_MODEL"] = str(p_model or "models/gemini-3.8-flash")
        else:
            if p_base_url:
                os.environ["OPENAI_BASE_URL"] = p_base_url
            if p_api_key:
                os.environ["OPENAI_API_KEY"] = p_api_key
            if p_model:
                os.environ["OPENAI_MODEL"] = p_model
        # LLM 代理（全部 Provider 生效；代理窗口在 llm_client 侧按调用实现）
        os.environ["LLM_PROXY_ENABLED"] = "1" if config.get("llm_proxy_enabled") else ""
        os.environ["LLM_PROXY_URL"] = str(config.get("llm_proxy_url") or "").strip()

        if sys.platform == "win32" and "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        # MOSS-TTS env vars (read by generate_tts -> MossTTSEngine)
        if config.get("moss_model_path"):
            os.environ["MOSS_MODEL_PATH"] = str(config["moss_model_path"])
        if config.get("moss_tokenizer_path"):
            os.environ["MOSS_TOKENIZER_PATH"] = str(config["moss_tokenizer_path"])
        if config.get("moss_device"):
            os.environ["MOSS_DEVICE"] = str(config["moss_device"])
        if config.get("moss_repo_dir"):
            os.environ["MOSS_REPO_DIR"] = str(config["moss_repo_dir"])
        if config.get("moss_tts_temperature"):
            os.environ["MOSS_TTS_TEMPERATURE"] = str(config["moss_tts_temperature"])
        if config.get("moss_tts_retry"):
            os.environ["MOSS_TTS_RETRY"] = str(config["moss_tts_retry"])
        if config.get("moss_tts_top_p"):
            os.environ["MOSS_TTS_TOP_P"] = str(config["moss_tts_top_p"])
        if config.get("moss_tts_top_k"):
            os.environ["MOSS_TTS_TOP_K"] = str(config["moss_tts_top_k"])
        if config.get("moss_tts_rep_penalty"):
            os.environ["MOSS_TTS_REP_PENALTY"] = str(config["moss_tts_rep_penalty"])
        if config.get("moss_tts_text_temperature"):
            os.environ["MOSS_TTS_TEXT_TEMPERATURE"] = str(config["moss_tts_text_temperature"])
        if config.get("moss_tts_greedy"):
            os.environ["MOSS_TTS_GREEDY"] = "1"

        # Qwen-TTS env vars（与 CLI main() 注入保持一致；不设则引擎用内置默认）
        if config.get("qwen_model_path"):
            os.environ["QWEN_MODEL_PATH"] = str(config["qwen_model_path"])
        if config.get("qwen_base_model_path"):
            os.environ["QWEN_BASE_MODEL_PATH"] = str(config["qwen_base_model_path"])
        if config.get("qwen_voicedesign_model_path"):
            os.environ["QWEN_VOICEDSIGN_MODEL_PATH"] = str(config["qwen_voicedesign_model_path"])
        if config.get("qwen_device"):
            os.environ["QWEN_DEVICE"] = str(config["qwen_device"])

    # Character image file patterns per structure
    # （仅 char_scene.png 为 original/original_static 的角色合图；
    #   旧清单中的 char_a_ref/char_b_ref 从未由 image_gen 生成，已移除）
    _CHAR_FILES_ORIGINAL = ["char_scene.png"]

    def _build_args(self, config: dict) -> SimpleNamespace:
        """Convert config dict → SimpleNamespace matching pipeline.py args."""
        # Resolve LLM provider (handles custom: prefix → openai)
        p_type, p_base_url, p_api_key, p_model = resolve_provider(config)

        num_lines = config.get("num_lines", "")
        try:
            num_lines = int(num_lines) if num_lines else None
        except (ValueError, TypeError):
            num_lines = None

        pad = config.get("pad", "")
        try:
            pad = float(pad) if pad else None
        except (ValueError, TypeError):
            pad = None

        # 序列帧新模式：身份保留在 mode_name（输出目录/配置），行为族归一给 structure
        from .config_manager import structure_family
        mode_name = config.get("structure", "original")
        structure = structure_family(mode_name)
        if num_lines is None:
            # 行数 = 组数×2（与 pipeline.main() 双处一致；--num-lines 对 sleep 不生效）
            num_lines = _cfg_int(config, "sleep_pairs", 200, 10, 400) * 2
        if pad is None:
            pad = 0.4

        # sleep 无动画概念（占位属性，保持 SimpleNamespace 形状）
        animation = "none"

        # tts_rate 为旧全局覆盖（兼容）；分项参数优先（tts_pipeline.resolve_tts_rate）
        tts_rate = config.get("tts_rate", "") or None

        # sleep 卡片提前量（秒，0-2；空串/非法回退默认 0.3，0=出现即出声）
        try:
            sleep_card_lead = min(2.0, max(0.0, float(config.get("sleep_card_lead", 0.3))))
        except (TypeError, ValueError):
            sleep_card_lead = 0.3

        # sleep 组间交叉溶解（默认关=原硬切行为；开启时 clamp 0.2-2.0，
        # 折叠为单一 xfade_sec 值，0=关闭）
        sleep_xfade_sec = 0.0
        if config.get("sleep_xfade"):
            try:
                sleep_xfade_sec = min(2.0, max(0.2, float(config.get("sleep_xfade_sec", 0.5) or 0.5)))
            except (TypeError, ValueError):
                sleep_xfade_sec = 0.5

        # mcp_tokens：模式配置为空时回落 legacy default.json / 本机 CLI 检测
        # （如 sleep 模式文件 seed 时未带 token）
        from .config_manager import resolve_mcp_tokens
        tokens_raw = resolve_mcp_tokens(mode_name)
        mcp_tokens = ",".join(
            t.strip() for t in tokens_raw.split("\n") if t.strip()
        ) if tokens_raw else ""

        return SimpleNamespace(
            topic=config.get("topic", "") or None,
            cefr=config.get("cefr", "A2"),
            num_lines=num_lines,
            max_line_words=_cfg_line_words(config),
            structure=structure,
            mode_name=mode_name,
            animation=animation,
            visual_style=str(config.get("visual_style", "pixar3d")),
            llm_provider=config.get("llm_provider", "wbk"),
            model=p_model if p_type == "openai" else "",
            api_key=p_api_key if p_type == "openai" else "",
            openai_base_url=p_base_url if p_type == "openai" else "",
            openai_api_key=p_api_key if p_type == "openai" else "",
            openai_model=p_model if p_type == "openai" else "grok-4.6",
            gemini_api_key=p_api_key if p_type == "gemini" else "",
            gemini_model=p_model if p_type == "gemini" else "models/gemini-3.8-flash",
            wbk_api_key=p_api_key if p_type == "wbk" else "",
            wbk_model=p_model if p_type == "wbk" else "cn:auto",
            wbk_thinking=str(config.get("wbk_thinking") or "default"),
            llm_proxy_url=(str(config.get("llm_proxy_url") or "").strip()
                           if config.get("llm_proxy_enabled") else None),
            llm_retries=int(config.get("llm_retries", 10)),
            mcp_tokens=mcp_tokens or None,
            mcp_token=None,
            clip_duration=int(config.get("clip_duration", 15)),
            image_concurrency=int(config.get("image_concurrency", 4)),
            clip_concurrency=int(config.get("clip_concurrency", 4)),
            output=config.get("output_dir", "./output"),
            topics_file=config.get("topics_file", str(PIPELINE_DIR / "topics.json")),
            used_topics_file=config.get("used_topics_file", "") or None,
            lessons_dir=config.get("lessons_dir", "") or None,
            practice_duration=float(config.get("practice_duration", 3.0)),
            sleep_pairs=_cfg_int(config, "sleep_pairs", 200, 10, 400),
            sleep_slow_rate=float(config.get("sleep_slow_rate", 0.8) or 0.8),
            sleep_male_rate=float(config.get("sleep_male_rate", 1.0) or 1.0),
            sleep_gap_short=float(config.get("sleep_gap_short", 1.0) or 1.0),
            sleep_gap_long=float(config.get("sleep_gap_long", 2.0) or 2.0),
            sleep_pair_gap=float(config.get("sleep_pair_gap", 3.0) or 3.0),
            sleep_batch_pairs=_cfg_int(config, "sleep_batch_pairs", 50, 10, 80),
            sleep_use_cache=bool(config.get("sleep_use_cache", True)),
            llm_single_shot_script=bool(config.get("llm_single_shot_script", False)),
            sleep_channel_name=str(config.get("sleep_channel_name", "") or ""),
            sleep_outro_text=str(config.get("sleep_outro_text", "") or ""),
            channel_id=str(config.get("channel_id", "") or ""),
            channel_profile=_channel_ctx(str(config.get("channel_id", "") or "")),
            sleep_voice_male=str(config.get("sleep_voice_male", "") or ""),
            sleep_voice_female=str(config.get("sleep_voice_female", "") or ""),
            sleep_show_leaves=bool(config.get("sleep_show_leaves", True)),
            sleep_handwrite_font=str(config.get("sleep_handwrite_font", "") or ""),
            sleep_handwrite_weight=_cfg_int(config, "sleep_handwrite_weight", 0, 0, 4),
            sleep_font_en=str(config.get("sleep_font_en", "") or ""),
            sleep_font_ph=str(config.get("sleep_font_ph", "") or ""),
            sleep_font_zh=str(config.get("sleep_font_zh", "") or ""),
            sleep_font_en_weight=_cfg_int(config, "sleep_font_en_weight", 0, 0, 4),
            sleep_font_ph_weight=_cfg_int(config, "sleep_font_ph_weight", 0, 0, 4),
            sleep_font_zh_weight=_cfg_int(config, "sleep_font_zh_weight", 0, 0, 4),
            sleep_color_bg_top=str(config.get("sleep_color_bg_top", "") or ""),
            sleep_color_bg_bottom=str(config.get("sleep_color_bg_bottom", "") or ""),
            sleep_color_card=str(config.get("sleep_color_card", "") or ""),
            sleep_color_card_border=str(config.get("sleep_color_card_border", "") or ""),
            sleep_color_en_a=str(config.get("sleep_color_en_a", "") or ""),
            sleep_color_en_b=str(config.get("sleep_color_en_b", "") or ""),
            sleep_color_phonetic=str(config.get("sleep_color_phonetic", "") or ""),
            sleep_color_zh=str(config.get("sleep_color_zh", "") or ""),
            sleep_color_num=str(config.get("sleep_color_num", "") or ""),
            sleep_color_badge_bg=str(config.get("sleep_color_badge_bg", "") or ""),
            sleep_color_badge_text=str(config.get("sleep_color_badge_text", "") or ""),
            sleep_color_channel=str(config.get("sleep_color_channel", "") or ""),
            sleep_color_leaf=str(config.get("sleep_color_leaf", "") or ""),
            sleep_bg_image=bool(config.get("sleep_bg_image", False)),
            sleep_bg_image_path=str(config.get("sleep_bg_image_path", "") or ""),
            sleep_bg_opacity=_cfg_int(config, "sleep_bg_opacity", 20, 0, 100),
            sleep_4k_native=bool(config.get("sleep_4k_native", False)),
            sleep_intro=bool(config.get("sleep_intro", True)),
            sleep_intro_use_library=bool(config.get("sleep_intro_use_library", True)),
            sleep_intro_announce=bool(config.get("sleep_intro_announce", True)),
            sleep_outro=bool(config.get("sleep_outro", True)),
            sleep_outro_use_library=bool(config.get("sleep_outro_use_library", True)),
            sleep_outro_announce=bool(config.get("sleep_outro_announce", True)),
            sleep_card_lead=sleep_card_lead,
            sleep_sequence=str(config.get("sleep_sequence", "") or ""),
            sleep_xfade=bool(config.get("sleep_xfade", False)),
            sleep_xfade_sec=sleep_xfade_sec,
            sleep_font_scale=_cfg_int(config, "sleep_font_scale", 100, 60, 160),
            sleep_line_spacing=_cfg_int(config, "sleep_line_spacing", 14, 0, 48),
            sleep_letter_spacing=_cfg_int(config, "sleep_letter_spacing", 0, 0, 24),
            sleep_bg_layer=str(config.get("sleep_bg_layer", "") or "bottom"),
            sleep_logo=bool(config.get("sleep_logo", True)),
            sleep_logo_path=str(config.get("sleep_logo_path", "") or ""),
            sleep_logo_position=str(config.get("sleep_logo_position", "") or "top_right"),
            sleep_logo_size=_cfg_int(config, "sleep_logo_size", 96, 40, 240),
            sleep_logo_opacity=_cfg_int(config, "sleep_logo_opacity", 90, 10, 100),
            sleep_logo_pos_x=_cfg_float(config, "sleep_logo_pos_x", 92.0, 0.0, 100.0),
            sleep_logo_pos_y=_cfg_float(config, "sleep_logo_pos_y", 6.0, 0.0, 100.0),
            sleep_intro_volume_db=_cfg_float(config, "sleep_intro_volume_db", 0),
            sleep_outro_volume_db=_cfg_float(config, "sleep_outro_volume_db", 0),
            ch3_en_repeats=_cfg_int(config, "ch3_en_repeats", 3),
            ch3_zh_repeats=_cfg_int(config, "ch3_zh_repeats", 1),
            ch3_zh_always=bool(config.get("ch3_zh_always", True)),
            dialogue_xfade=bool(config.get("dialogue_xfade", False)),
            pad=pad,
            render_fps=int(config.get("render_fps", 8)),
            workers=int(config.get("workers", 1)),
            subtitle_font_size=int(config.get("subtitle_font_size", 60)),
            subtitle_style=str(config.get("subtitle_style", "") or ""),
            no_zh_subtitle=bool(config.get("no_zh_subtitle", False)),
            no_4k=bool(config.get("no_4k", False)),  # Web 开关：跳过 Step6 4K 产出（等价 CLI --no-4k）
            no_thumbnail=bool(config.get("no_thumbnail", False)),
            quick_test=bool(config.get("quick_test", False)),
            output_dir=config.get("output_dir", "./output"),
            tts_engine=config.get("tts_engine", "kokoro"),
            tts_rate=tts_rate,
            tts_rate_en=config.get("tts_rate_en", "") or None,
            tts_rate_zh=config.get("tts_rate_zh", "") or None,
            tts_rate_narration=config.get("tts_rate_narration", "") or None,
            qwen_model_path=config.get("qwen_model_path", ""),
            qwen_base_model_path=config.get("qwen_base_model_path", ""),
            qwen_voicedesign_model_path=config.get("qwen_voicedesign_model_path", ""),
            qwen_device=config.get("qwen_device", ""),
            moss_model_path=config.get("moss_model_path", ""),
            moss_tokenizer_path=config.get("moss_tokenizer_path", ""),
            moss_device=config.get("moss_device", "cpu"),
            moss_repo_dir=config.get("moss_repo_dir", ""),
            moss_tts_temperature=config.get("moss_tts_temperature", 0.8),
            moss_tts_retry=config.get("moss_tts_retry", 3),
            moss_tts_top_p=config.get("moss_tts_top_p", 0.95),
            moss_tts_top_k=config.get("moss_tts_top_k", 25),
            moss_tts_rep_penalty=config.get("moss_tts_rep_penalty", 1.2),
            moss_tts_text_temperature=config.get("moss_tts_text_temperature", 1.0),
            moss_tts_greedy=bool(config.get("moss_tts_greedy", False)),
            sleep_intro_video=_resolve_sleep_intro_video(config),
            sleep_outro_video=_resolve_sleep_outro_video(config),
            bgm_mix=bool(config.get("bgm_mix", False)),
            bgm_music_dir=str(config.get("bgm_music_dir", "")
                              or Path(__file__).parent.parent / "bgm_music"),
            bgm_start_chapter=int(config.get("bgm_start_chapter", 1) or 1),
            bgm_ducking_mode=str(config.get("bgm_ducking_mode", "sidechain")),
            bgm_base_gain_db=float(config.get("bgm_base_gain_db", -15)),
            bgm_volume_offset_db=float(config.get("bgm_volume_offset_db", -25)),
            bgm_fade_ms=int(config.get("bgm_fade_ms", 3000)),
            bgm_intro_outro_seconds=int(config.get("bgm_intro_outro_seconds", 5)),
            bgm_highpass_freq=int(config.get("bgm_highpass_freq", 150)),
            bgm_min_volume_db=float(config.get("bgm_min_volume_db", -40)),
            bgm_dynamic_volume=bool(config.get("bgm_dynamic_volume", True)),
            bgm_spectral_shaping=bool(config.get("bgm_spectral_shaping", True)),
            bgm_stereo_offset=float(config.get("bgm_stereo_offset", 0.0)),
            bgm_sc_threshold_db=float(config.get("bgm_sc_threshold_db", -30)),
            bgm_sc_threshold_offset_db=float(config.get("bgm_sc_threshold_offset_db", -5)),
            bgm_sc_ratio=int(config.get("bgm_sc_ratio", 8)),
            bgm_sc_attack_ms=int(config.get("bgm_sc_attack_ms", 5)),
            bgm_sc_release_ms=int(config.get("bgm_sc_release_ms", 400)),
            resume=False,
        )

    def _seed_from_script_library(self, script_id: str, args, topic: str,
                                  parent_dir: Path, used_topics_file: str):
        """Step 0 alternative: seed the run dir from a library script (no LLM).

        Writes script.json + subdirs + checkpoint, marks the script USED.
        Returns (script, work_dir, dirs); (None, None, None) after _fail().
        """
        from . import script_library

        doc = script_library.get_script_doc(script_id)
        if not doc:
            self._fail(f"脚本库中未找到脚本: {script_id}")
            return None, None, None
        script = doc.get("script") or {}
        if not script.get("dialogue"):
            self._fail(f"脚本库脚本无对话内容: {script_id}")
            return None, None, None
        if doc.get("structure") and doc["structure"] != args.structure:
            self._fail(
                f"脚本结构不匹配: 脚本为 {doc['structure']}，当前模式为 {args.structure}")
            return None, None, None

        topic = doc.get("topic") or script.get("title") or topic
        args.topic = topic
        if doc.get("cefr"):
            args.cefr = doc["cefr"]

        self._on_log_line("\n" + "=" * 60)
        self._on_log_line("Step 0: 使用脚本库脚本（跳过 LLM 生成）...")
        yt_title = script.get("youtube_title", script.get("title", topic))
        safe_title = _safe_dirname(yt_title, topic)
        work_dir = parent_dir / getattr(args, "mode_name", args.structure) / safe_title
        work_dir.mkdir(parents=True, exist_ok=True)
        dirs = {k: work_dir / k for k in ("images", "clips", "audio", "subtitles", "videos")}
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        script_path = work_dir / "script.json"
        script["structure"] = args.structure
        script["channel_id"] = str(getattr(args, "channel_id", "") or "")
        script_path.write_text(
            json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        _save_checkpoint(work_dir, "step0_script", topic=topic, cefr=args.cefr,
                         structure=args.structure, animation=args.animation,
                         visual_style=str(self.config.get("visual_style", "")),
                         host_character=str(self.config.get("host_character", "") or ""))
        # 各模式独立记录已用主题（不再写全局 used_topics.json，
        # 同一主题仍可在其他模式生成/使用）
        script_library.mark_topic_used_mode(
            args.structure, topic, script_id=script_id, run_name=safe_title)
        script_library.mark_used(script_id, run_name=safe_title)
        review = doc.get("review") or {}
        score_info = (f"，审查 {review.get('score')} 分"
                      if review.get("score") is not None else "")
        self._on_log_line(f"  [ScriptLib] {topic} "
                          f"({len(script.get('dialogue', []))} 行{score_info})")
        self._on_log_line(f"  [ScriptLib] 已标记为「已使用」(run: {safe_title})")
        self._on_log_line(f"  Script saved: {script_path}")
        return script, work_dir, dirs

    def _on_log_line(self, line: str):
        """Called for each stdout line from pipeline."""
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 5000:
                self.log_lines = self.log_lines[-3000:]
            for pattern, step_id, step_label in STEP_PATTERNS:
                if re.search(pattern, line):
                    self.current_step = step_id
                    self.current_step_label = step_label
                    break

    def _run(self, config: dict, resume: bool):
        """Main pipeline execution in background thread."""
        # 用户停止即时生效：注册线程局部停止探测，本线程内所有 LLM 调用
        # 的等待/退避/重试循环逐 0.5s 检查 _stop_flag（Web 线程不受影响）。
        set_llm_stop_hook(self._stop_flag.is_set)
        # 无论 _run 主体在哪一步抛异常（含 try 块之前的 env/args 构建），
        # 都必须释放互斥锁，否则模式测试/下次启动会被永久阻塞
        try:
            self._run_inner(config, resume)
        finally:
            set_llm_stop_hook(None)
            run_mutex.release("pipeline")

    def _run_inner(self, config: dict, resume: bool):
        """Main pipeline execution body (mutex held by _run)."""
        # Import here so heavy imports happen in the worker thread
        from pipeline import (
            _step0_script, _step1_mcp, _step2_images_tts,
            _step3_clips, _step4_timeline, _step45_thumbnail,
            _step5_compose, _step55_bgm, _step6_4k,
            _generate_script_with_retry, _resolve_topic, _resolve_run_dir,
        )

        # Set env vars
        self._set_env(config)

        # Build args namespace
        args = self._build_args(config)
        args.resume = resume

        # Redirect stdout to capture print() output
        old_stdout = sys.stdout
        buf = _LineBuffer(self._on_log_line)
        sys.stdout = buf

        try:
            parent_dir = Path(args.output).resolve()
            parent_dir.mkdir(parents=True, exist_ok=True)
            # 新布局：每种模式一个独立文件夹 output/{mode}/{run_name}/
            # （序列帧新模式按 mode_name 分文件夹，族行为归一后 structure 已是族名）
            mode_dir = parent_dir / getattr(args, "mode_name", args.structure)
            mode_dir.mkdir(parents=True, exist_ok=True)
            # Full raw LLM responses are dumped here when _chat hits errors
            os.environ["LLM_DEBUG_DIR"] = str(parent_dir / "llm_debug")
            used_topics_file = args.used_topics_file or str(parent_dir / "used_topics.json")

            # Load checkpoint for resume
            if resume:
                checkpoint = _load_checkpoint(mode_dir)
                if not checkpoint:
                    checkpoint = {}
            else:
                checkpoint = {}

            # Resolve topic
            topic = _resolve_topic(args, checkpoint)
            if topic is None:
                topic = pick_random_topic(args.topics_file, used_topics_file, mark=False)
                if not topic:
                    self._fail("No topics found. Please specify a topic or provide topics.json.")
                    return
            args.topic = topic

            # Step 0: Script generation — 脚本库脚本直接落盘（跳过 LLM 生成）
            script_id = str(config.get("script_id") or "").strip()
            if script_id and not resume:
                script, work_dir, dirs = self._seed_from_script_library(
                    script_id, args, topic, parent_dir, used_topics_file)
                if script is None:
                    return
            else:
                script, work_dir, dirs = _step0_script(
                    args, checkpoint, topic, mode_dir, used_topics_file)
                # 全新生成也补记「模式已用」（脚本页按模式排除主题用）
                try:
                    from . import script_library as _sl
                    _sl.mark_topic_used_mode(
                        args.structure, topic, run_name=Path(work_dir).name)
                except Exception:
                    pass
            self.work_dir = str(work_dir)

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step0_script")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Reload script in case user edited it during step-mode pause
            _script_path = work_dir / "script.json"
            if _script_path.exists():
                script = json.loads(_script_path.read_text(encoding="utf-8"))
                self._on_log_line("  [Step Mode] Reloaded script.json (edits applied).")

            # Step 1: MCP init —— sleep 恒跳过（主流程零 MCP；缩略图 AI 生成在
            # Step 4.5 按需初始化，空 token 不在此崩与 MCP 无关的运行）
            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step1_mcp")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 2: Images + TTS
            ctx = _step2_images_tts(args, checkpoint, script, work_dir, dirs,
                                    stop_check=self._stop_flag.is_set)

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step2_images_tts")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 3: Video clips
            clip_paths, group_info, line_to_group = _step3_clips(
                args, checkpoint, work_dir, dirs, script, ctx,
                stop_check=self._stop_flag.is_set)

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step3_video")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 4: Timeline
            timeline, narration, normal_paths, zh_paths = _step4_timeline(
                args, checkpoint, script, work_dir, dirs, ctx["tts_results"])

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step4_timeline")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 4.5: Thumbnail
            _step45_thumbnail(args, checkpoint, script, work_dir, dirs, timeline, ctx)

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step45_thumbnail")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 5: Compose
            final_path, safe_vid_name = _step5_compose(
                args, checkpoint, script, work_dir, dirs, clip_paths, timeline,
                narration, normal_paths, zh_paths, ctx["tts_results"],
                group_info, line_to_group, stop_check=self._stop_flag.is_set)
            self.final_path = final_path

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step5_compose")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 5.5: BGM 版权音乐混合（启用时输出 {stem}_bgm.mp4，4K 以其为源）
            final_path = _step55_bgm(args, checkpoint, work_dir, final_path)
            self.final_path = final_path

            if self._stop_flag.is_set():
                self._set_stopped()
                return
            self._wait_for_step_approval("step55_bgm")
            if self._stop_flag.is_set():
                self._set_stopped()
                return

            # Step 6: 4K
            final_4k_path = _step6_4k(args, checkpoint, work_dir, final_path, safe_vid_name)

            # Clear checkpoint on completion
            cp_path = work_dir / "checkpoint.json"
            if cp_path.exists():
                cp_path.unlink()

            with self._lock:
                self.status = "done"
                self.finished_at = time.time()
            self._on_log_line("")
            self._on_log_line("=" * 60)
            self._on_log_line(f"DONE! Final video: {final_path}")
            fsize = os.path.getsize(final_path) / (1024 * 1024)
            self._on_log_line(f"Size: {fsize:.1f}MB")
            if final_4k_path and Path(final_4k_path).exists():
                self._on_log_line(f"4K video: {final_4k_path}")

        except LLMStoppedError:
            # 用户点击「停止运行」→ LLM 等待/重试循环即时中止（BaseException
            # 穿透脚本生成的全部 except Exception 重试层直达此处）。
            # 标记 stopped 而非 error；此时 stdout 仍指向日志缓冲，行可进前端。
            self._set_stopped()
        except SystemExit as e:
            # pipeline 模块用 sys.exit(1) 中止（图片缺失 / MCP token 耗尽 /
            # 质检门禁 exit=2 等）。SystemExit 不是 Exception 子类，
            # except Exception 接不住 — 不显式处理的话 finally 兜底会把
            # running 误标成 done（前端弹"视频生成完成"）。
            self._fail(f"Pipeline aborted (exit code {e.code})")
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}")
            import traceback
            tb = traceback.format_exc()
            for line in tb.split("\n"):
                self._on_log_line(line)
        finally:
            sys.stdout = old_stdout
            buf.flush()
            with self._lock:
                if self.status == "running":
                    # 正常完成路径在上面已显式置 done；走到这里仍 running
                    # 说明线程异常退出 — 一律标记失败，绝不静默显示为完成
                    self.status = "error"
                    self.error = "Pipeline thread exited without a terminal status"
                self.finished_at = time.time()

    def _fail(self, msg: str):
        with self._lock:
            self.status = "error"
            self.error = msg
            self.finished_at = time.time()
        self._on_log_line(f"\nFATAL: {msg}")

    # ------------------------------------------------------------------
    # Recompose: re-burn subtitles with a new style on a finished run
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_fps(path: str) -> int:
        """ffprobe 视频帧率（quest=25 / 其余=24），失败回退 24。"""
        import subprocess as _sp
        try:
            r = _sp.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-of", "default=nw=1:nk=1", path],
                capture_output=True, text=True, timeout=30)
            s = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
            val = 0.0
            if "/" in s:
                num, den = s.split("/", 1)
                val = float(num) / float(den) if float(den) else 0.0
            elif s:
                val = float(s)
            if val > 0:
                return int(round(val))
        except Exception:
            pass
        return 24

    def refresh_youtube_metadata(self, run_name: str, mode: str = "") -> tuple[bool, str]:
        """用 script.json + subtitles/meta.json 的 timeline 重新生成 youtube_metadata.json。

        复用 Step 4.5 的 save_youtube_metadata，零 AI 成本秒级完成，
        用于脚本编辑后刷新标题/简介/章节。返回 (ok, message)。
        """
        if self.is_running:
            return False, "Pipeline 正在运行中，请等待完成后再刷新"

        output_dir = Path(load_config().get("output_dir", "./output"))
        run_dir = find_run_dir(output_dir, run_name, mode)
        if not run_dir:
            return False, f"运行不存在: {run_name}"
        script_path = run_dir / "script.json"
        meta_path = run_dir / "subtitles" / "meta.json"
        if not script_path.exists():
            return False, "缺少 script.json"
        if not meta_path.exists():
            return False, "缺少 subtitles/meta.json（请先跑完 Step 4）"
        try:
            script = json.loads(script_path.read_text(encoding="utf-8"))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            from thumbnail_gen import save_youtube_metadata
            out = save_youtube_metadata(
                script=script,
                timeline=meta.get("timeline", []),
                output_path=str(run_dir / "youtube_metadata.json"),
                structure=script.get("structure", "original"),
            )
            return True, f"已重新生成: {Path(out).name}"
        except Exception as e:
            return False, f"生成失败: {e}"

    def generate_4k(self, run_name: str, mode: str = "") -> tuple[bool, str]:
        """为已完成运行生成（或重新生成）4K 版本（复用 Step 6 超分逻辑，本地渲染零积分）。

        源视频 = 运行目录根部成片；固定 ffmpeg lanczos 引擎（4K 恒定生成，
        超分配置组已移除）。后台线程执行；
        期间与主 pipeline / 模式测试互斥。
        返回 (ok, message)。
        """
        if self.is_running:
            return False, "Pipeline 正在运行中，请等待完成后再生成 4K"

        config = load_config()
        output_dir = Path(config.get("output_dir", "./output"))
        run_dir = find_run_dir(output_dir, run_name, mode)
        if not run_dir:
            return False, f"运行不存在: {run_name}"
        script_path = run_dir / "script.json"
        if not script_path.exists():
            return False, "缺少 script.json"
        try:
            script = json.loads(script_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return False, f"script.json 读取失败: {e}"

        # 成片定位：优先按脚本标题还原文件名，
        # 否则取根部排除中间产物/旧 4K 后最新的 mp4
        safe_vid_name = _safe_dirname(
            script.get("youtube_title", script.get("title", run_name)), run_name)
        final_path = run_dir / f"{safe_vid_name}.mp4"
        if not final_path.exists():
            candidates = [v for v in run_dir.glob("*.mp4")
                          if not v.name.startswith(("final_no_sub", "final_video_norm"))
                          and not v.name.endswith("_4K.mp4")]
            if not candidates:
                return False, "未找到成片视频（运行目录根部无 final mp4）"
            final_path = max(candidates, key=lambda v: v.stat().st_mtime)
            safe_vid_name = final_path.stem

        # 超分配置组已移除：4K 恒定生成，固定 ffmpeg lanczos 引擎与默认超时
        upscale_engine, upscale_timeout = "ffmpeg", 3600

        if not run_mutex.try_acquire("4k_gen"):
            return False, (f"资源被占用：{run_mutex.current_owner()}"
                           f"（主 pipeline / 模式测试运行中请等待完成）")

        self._stop_flag.clear()
        self._step_mode = False
        self._paused_after_step = ""
        with self._lock:
            self.log_lines = []
            self.status = "running"
            self.current_step = ""
            self.current_step_label = "生成 4K 版本（本地渲染）"
            self.started_at = time.time()
            self.finished_at = 0
            self.error = ""
            self.work_dir = str(run_dir)
            self.final_path = ""

        self._thread = threading.Thread(
            target=self._generate_4k_run,
            args=(run_dir, str(final_path), safe_vid_name,
                  upscale_engine, upscale_timeout),
            daemon=True)
        self._thread.start()
        return True, "4K 生成已启动"

    def _generate_4k_run(self, run_dir: Path, final_path: str, safe_vid_name: str,
                         upscale_engine: str, upscale_timeout: int):
        """后台线程：按 Step 6 同款逻辑生成 4K，写临时文件成功后原子替换旧 4K。"""
        import subprocess as _sp
        old_stdout = sys.stdout
        buf = _LineBuffer(self._on_log_line)
        sys.stdout = buf
        four_k_path = run_dir / f"{safe_vid_name}_4K.mp4"
        tmp_path = run_dir / f"{safe_vid_name}_4K_tmp.mp4"
        try:
            print("=" * 60)
            print(f"Generate4K: {run_dir.name}")
            print(f"  源视频: {Path(final_path).name}")
            # 已 4K 守卫（sleep 原生 4K 成片即 4K）：直接链接产出，零重编码
            try:
                from media_utils import probe_resolution
                _w, _h = probe_resolution(str(final_path))
                if _w >= 3800:
                    print(f"  [4K] 源视频已是 {_w}x{_h} —— 链接产出 _4K 文件（跳过放大）")
                    try:
                        os.link(final_path, four_k_path)
                    except OSError:
                        import shutil as _sh
                        _sh.copy2(final_path, four_k_path)
                    with self._lock:
                        self.status = "done"
                        self.finished_at = time.time()
                    print(f"Generate4K DONE! {four_k_path.name} (linked, 0s)")
                    return
            except Exception:
                pass  # 探测失败 → 落回常规放大
            tmp_path.unlink(missing_ok=True)
            r = None
            ai_done = False
            if upscale_engine == "ai":
                from sr_upscale import upscale_video_ai, model_available
                if not model_available():
                    print("  [4K] AI 超分不可用（权重缺失或无 CUDA），回退 ffmpeg lanczos")
                else:
                    print("  [4K] AI 超分引擎：realesr-animevideov3 (torch CUDA fp16)")
                    upscale_video_ai(final_path, str(tmp_path), timeout=upscale_timeout)
                    ai_done = True
            if not ai_done:
                print("  [4K] ffmpeg lanczos 放大中（scale=3840:2160）...")
                r = _sp.run(
                    ["ffmpeg", "-i", final_path,
                     "-vf", "scale=3840:2160:flags=lanczos",
                     "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-threads", "0",
                     "-c:a", "copy",
                     str(tmp_path), "-y"],
                    capture_output=True, timeout=upscale_timeout)
            if ai_done or (r is not None and r.returncode == 0 and tmp_path.exists()):
                os.replace(str(tmp_path), str(four_k_path))
                size_mb = four_k_path.stat().st_size / (1024 * 1024)
                with self._lock:
                    self.status = "done"
                    self.finished_at = time.time()
                print("=" * 60)
                print(f"Generate4K DONE! {four_k_path.name} ({size_mb:.1f}MB)")
            else:
                tmp_path.unlink(missing_ok=True)
                stderr = (r.stderr.decode("utf-8", errors="replace")[-500:]
                          if r is not None and r.stderr else "")
                self._fail("4K 生成失败（720p 版本仍可用）"
                           + (f" ffmpeg stderr: {stderr}" if stderr else ""))
        except _sp.TimeoutExpired:
            tmp_path.unlink(missing_ok=True)
            self._fail(f"4K 生成超时（>{upscale_timeout}s），720p 版本仍可用")
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            self._fail(f"Generate4K {type(e).__name__}: {e}")
            import traceback
            for line in traceback.format_exc().split("\n"):
                self._on_log_line(line)
        finally:
            sys.stdout = old_stdout
            buf.flush()
            with self._lock:
                if self.status == "running":
                    self.status = "done"
                self.finished_at = time.time()
            run_mutex.release("4k_gen")

    # ------------------------------------------------------------------
    # BGM mix: mix copyright BGM into a finished run's audio
    # ------------------------------------------------------------------

    def bgm_mix(self, run_name: str, mode: str = "", target: str = "final") -> tuple[bool, str]:
        """为已完成运行混入版权 BGM（输出 {标题}_bgm.mp4 新文件，原片保留）。

        target="final" 混 1080p 成片；target="4k" 混 4K 版本
        （输出 {标题}_4K_bgm.mp4）。参数读取运行所在模式的当前配置
        （配置页改完即可对旧运行重混）。
        照 generate_4k 模式在后台线程执行；期间与主 pipeline / 模式测试互斥。
        返回 (ok, message)。
        """
        if self.is_running:
            return False, "Pipeline 正在运行中，请等待完成后再混音"

        config = load_mode_config(mode) if mode in MODES else load_config()
        output_dir = Path(config.get("output_dir", "./output"))
        run_dir = find_run_dir(output_dir, run_name, mode)
        if not run_dir:
            return False, f"运行不存在: {run_name}"
        if not (run_dir / "script.json").exists():
            return False, "缺少 script.json"

        # 成片定位：优先按脚本标题还原文件名，否则取根部最新的非中间产物 mp4
        # （排除 _4K/_bgm 自身，避免拿 BGM 版再叠一层 BGM）
        try:
            script = json.loads((run_dir / "script.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            return False, f"script.json 读取失败: {e}"
        safe_vid_name = _safe_dirname(
            script.get("youtube_title", script.get("title", run_name)), run_name)
        if target == "4k":
            # 4K 源定位：优先按脚本标题还原，否则取根部最新的 *_4K.mp4
            # （{标题}_4K_bgm.mp4 以 _bgm.mp4 结尾，不匹配 *_4K.mp4，不会拿混音版再叠一层）
            final_path = run_dir / f"{safe_vid_name}_4K.mp4"
            if not final_path.exists():
                candidates = list(run_dir.glob("*_4K.mp4"))
                if not candidates:
                    return False, "未找到 4K 视频（请先「生成4K」）"
                final_path = max(candidates, key=lambda v: v.stat().st_mtime)
        else:
            # 成片定位：优先按脚本标题还原文件名，否则取根部最新的非中间产物 mp4
            # （排除 _4K/_bgm 自身，避免拿 BGM 版再叠一层 BGM）
            final_path = run_dir / f"{safe_vid_name}.mp4"
            if not final_path.exists():
                candidates = [
                    v for v in run_dir.glob("*.mp4")
                    if not v.name.startswith(("final_no_sub", "final_video_norm"))
                    and not v.name.endswith(("_4K.mp4", "_bgm.mp4"))
                ]
                if not candidates:
                    return False, "未找到成片视频（运行目录根部无 final mp4）"
                final_path = max(candidates, key=lambda v: v.stat().st_mtime)

        music_dir = str(config.get("bgm_music_dir", "") or "").strip() \
            or str(Path(__file__).parent.parent / "bgm_music")
        if not Path(music_dir).is_dir() or not any(Path(music_dir).iterdir()):
            return False, (f"音乐库为空或不存在: {music_dir}\n"
                           f"请放入音乐文件（mp3/wav/flac 等）或在配置页修改「音乐库路径」")

        if not run_mutex.try_acquire("bgm_mix"):
            return False, (f"资源被占用：{run_mutex.current_owner()}"
                           f"（主 pipeline / 模式测试 / 4K 生成中请等待完成）")

        self._stop_flag.clear()
        self._step_mode = False
        self._paused_after_step = ""
        with self._lock:
            self.log_lines = []
            self.status = "running"
            self.current_step = ""
            self.current_step_label = ("BGM 音乐混合 4K（本地渲染）"
                                       if target == "4k" else "BGM 音乐混合（本地渲染）")
            self.started_at = time.time()
            self.finished_at = 0
            self.error = ""
            self.work_dir = str(run_dir)
            self.final_path = ""

        params = dict(
            ducking_mode=str(config.get("bgm_ducking_mode", "sidechain") or "sidechain"),
            bgm_base_gain_db=float(config.get("bgm_base_gain_db", -15)),
            volume_offset_db=float(config.get("bgm_volume_offset_db", -25)),
            fade_duration_ms=int(config.get("bgm_fade_ms", 3000)),
            highpass_freq=int(config.get("bgm_highpass_freq", 150)),
            min_volume_db=float(config.get("bgm_min_volume_db", -40)),
            dyn_vol=bool(config.get("bgm_dynamic_volume", True)),
            spec_shape=bool(config.get("bgm_spectral_shaping", True)),
            stereo_offset=float(config.get("bgm_stereo_offset", 0.0)),
            sc_threshold_db=float(config.get("bgm_sc_threshold_db", -30)),
            sc_threshold_offset_db=float(config.get("bgm_sc_threshold_offset_db", -5)),
            sc_ratio=int(config.get("bgm_sc_ratio", 8)),
            sc_attack_ms=int(config.get("bgm_sc_attack_ms", 5)),
            sc_release_ms=int(config.get("bgm_sc_release_ms", 400)),
            intro_outro_seconds=int(config.get("bgm_intro_outro_seconds", 5)),
            # 4K 带 padding 时整片重编码，600s 默认值会超时
            ffmpeg_timeout=3600 if target == "4k" else 600,
        )
        start_chapter = int(config.get("bgm_start_chapter", 1) or 1)
        out_path = run_dir / f"{final_path.stem}_bgm.mp4"
        self._thread = threading.Thread(
            target=self._bgm_mix_run,
            args=(run_dir, final_path, out_path, music_dir, params, start_chapter),
            daemon=True)
        self._thread.start()
        return True, "BGM 混音已启动"

    def _bgm_mix_run(self, run_dir: Path, src_video: Path, out_path: Path,
                     music_dir: str, params: dict, start_chapter: int = 1):
        """后台线程：mix_bgm_into_video 混音，成功原子替换旧 _bgm 产物。"""
        from bgm_mix import mix_bgm_into_video, chapter_start_seconds
        old_stdout = sys.stdout
        buf = _LineBuffer(self._on_log_line)
        sys.stdout = buf
        tmp_path = out_path.with_name(out_path.stem + "_tmp.mp4")
        try:
            print("=" * 60)
            print(f"BGM Mix: {run_dir.name}")
            print(f"  源视频: {src_video.name}")
            print(f"  音乐库: {music_dir}")
            print(f"  混音模式: {params['ducking_mode']}")
            params = dict(params)
            params["bgm_start_seconds"] = chapter_start_seconds(run_dir, start_chapter)
            tmp_path.unlink(missing_ok=True)
            ok = mix_bgm_into_video(str(src_video), str(tmp_path), music_dir, **params)
            if ok and tmp_path.exists() and tmp_path.stat().st_size > 0:
                os.replace(str(tmp_path), str(out_path))
                size_mb = out_path.stat().st_size / (1024 * 1024)
                with self._lock:
                    self.status = "done"
                    self.finished_at = time.time()
                    self.final_path = str(out_path)
                print("=" * 60)
                print(f"BGM Mix DONE! {out_path.name} ({size_mb:.1f}MB)")
            else:
                tmp_path.unlink(missing_ok=True)
                self._fail("BGM 混音失败（原片未改动）")
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            self._fail(f"BGM Mix {type(e).__name__}: {e}")
            import traceback
            for line in traceback.format_exc().split("\n"):
                self._on_log_line(line)
        finally:
            sys.stdout = old_stdout
            buf.flush()
            with self._lock:
                if self.status == "running":
                    self.status = "done"
                self.finished_at = time.time()
            run_mutex.release("bgm_mix")

    def _set_stopped(self):
        with self._lock:
            self.status = "stopped"
            self.finished_at = time.time()
        self._on_log_line("\n⏹ Pipeline stopped by user")

    def stop(self):
        self._stop_flag.set()

    def get_logs_since(self, since: int = 0) -> list[str]:
        with self._lock:
            if since < len(self.log_lines):
                return self.log_lines[since:]
        return []


# Singleton
_service: PipelineService | None = None


def get_service() -> PipelineService:
    global _service
    if _service is None:
        _service = PipelineService()
    return _service
