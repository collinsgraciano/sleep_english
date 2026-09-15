#!/usr/bin/env python3
"""Standalone SLEEP-mode video generation pipeline (sleep_english).

Structure: sleep — 睡觉听·AB 短句循环（纯 Pillow 卡片 + TTS + FFmpeg，主流程零 MCP）。

Usage:
    python pipeline.py --topic "Asking the Teacher a Question" --cefr A2 --output ./output

Steps (each an independently resumable function):
  step0  LLM script generation (sleep batch generator, 分批落盘缓存)
  step1  (skipped — sleep 主流程零 MCP；缩略图/背景图在 Step2/4.5 按需初始化)
  step2  Sleep TTS audio (+ optional AI background image / intro video binding)
  step3  (skipped — sleep mode has no video clips)
  step4  Timeline + SRT building
  step4.5 YouTube metadata + thumbnail (AI 生成，失败兜底 Pillow 卡片)
  step5  Final video composition (FFmpeg + Pillow cards)
  step5.5 Optional BGM copyright music mix
  step6  Optional 4K upscale (ffmpeg lanczos / AI engine)

main() is a thin orchestrator; all per-step logic lives in _stepN_* functions
so each can be read, tested, or re-run in isolation.

Domain modules:
  checkpoint.py        — save/load resume state
  tts_engine.py        — Kokoro TTS（默认引擎；qwen/moss 引擎同目录另两文件）
  media_utils.py       — shared FFmpeg/Pillow helpers (concat, loudnorm)
  sleep/               — sleep 专属：audio/cards/timeline/compose/bg_image/intro_video
  thumbnail_gen.py     — YouTube metadata + thumbnail (sleep 分支)
  bgm_mix.py           — BGM 版权音乐混合 (Step 5.5)
  sr_upscale.py        — AI 4K 超分引擎 (Step 6 可选)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add scripts directory to path
SCRIPTS_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPTS_DIR))

from mcp_client import initialize, call_tool, parse_task_id, poll_task, download_file
from topic_manager import pick_random_topic, mark_topic_used
from media_utils import get_duration as _get_audio_duration, safe_filename as _safe_dirname
from checkpoint import save_checkpoint as _save_checkpoint, load_checkpoint as _load_checkpoint, step_done as _step_done
from style_manager import resolve_style_prompt as _resolve_style_prompt


def _get_style_prompt(args) -> str:
    """当前画面风格 prompt 片段（env 注入优先，CLI --visual-style 兜底）。

    pipeline_service 直接调用 _stepN_* 时不经过 main()，因此通过
    VISUAL_STYLE_PROMPT 环境变量传递风格（sleep 仅用于背景图 prompt）。
    """
    return os.environ.get("VISUAL_STYLE_PROMPT") or _resolve_style_prompt(
        getattr(args, "visual_style", None))


# ---------------------------------------------------------------------------
# Script validation + generation
# ---------------------------------------------------------------------------

def _validate_script(script: dict, num_lines: int,
                     quest: bool = False, story: bool = False) -> tuple[bool, str]:
    """Validate a generated sleep script. Returns (is_valid, error_message)."""
    dialogue = script.get("dialogue", [])
    if len(dialogue) < num_lines:
        return False, f"Dialogue count {len(dialogue)} < required {num_lines}"
    for i, line in enumerate(dialogue):
        text = line.get("text", "").strip()
        if not text:
            return False, f"Dialogue line {i} has empty 'text'"
        if not line.get("zh", "").strip():
            return False, f"Dialogue line {i} has empty 'zh'"
        if not line.get("phonetic", "").strip():
            return False, f"Dialogue line {i} has empty 'phonetic'"
        if not line.get("speaker", ""):
            return False, f"Dialogue line {i} has empty 'speaker'"
    return True, ""


def _generate_script_with_retry(topic, cefr, lessons_dir, num_lines,
                                sleep_pairs=0, sleep_batch=50,
                                sleep_cache_dir=None, sleep_use_cache=True,
                                max_attempts=5) -> dict:
    """Generate and validate sleep script, retrying on failure."""
    for attempt in range(max_attempts):
        try:
            print(f"  [Script] Attempt {attempt+1}/{max_attempts}...")
            from sleep.llm_client_sleep import generate_sleep_script
            script = generate_sleep_script(topic, cefr,
                                           num_pairs=int(sleep_pairs or num_lines // 2),
                                           batch_pairs=int(sleep_batch),
                                           lessons_dir=lessons_dir,
                                           cache_dir=sleep_cache_dir,
                                           use_cache=bool(sleep_use_cache))
            valid, msg = _validate_script(script, num_lines)
            if valid:
                print(f"  [Script] Valid: {len(script['dialogue'])} lines")
                return script
            print(f"  [Script] Invalid: {msg}")
        except Exception as e:
            print(f"  [Script] Error: {e}")
        if attempt < max_attempts - 1:
            time.sleep(3)
    raise RuntimeError(f"Script generation failed after {max_attempts} attempts")


# ---------------------------------------------------------------------------
# CLI + orchestration helpers
# ---------------------------------------------------------------------------

def _resolve_topic(args, checkpoint: dict) -> str:
    """Resolve topic (priority): checkpoint topic > --topic > random pick."""
    if checkpoint.get("topic"):
        topic = checkpoint["topic"]
        print(f"  [Topic] Resuming topic from checkpoint: '{topic}'")
        return topic
    if args.topic:
        print(f"  [Topic] Using specified topic: '{args.topic}'")
        return args.topic
    print("  [Topic] No topic specified, picking randomly from topics.json...")
    return None


def _resolve_run_dir(parent_dir: Path, checkpoint: dict) -> Path:
    """On resume: use the checkpoint's recorded run dir (or newest subfolder
    containing a checkpoint). On fresh start: parent_dir (temporary)."""
    if checkpoint and checkpoint.get("_run_dir") and Path(checkpoint["_run_dir"]).exists():
        return Path(checkpoint["_run_dir"])
    if checkpoint:
        cands = [(p.stat().st_mtime, p) for p in parent_dir.iterdir()
                 if p.is_dir() and (p / "checkpoint.json").exists()]
        if cands:
            cands.sort(reverse=True)
            return cands[0][1]
    return parent_dir


# ---------------------------------------------------------------------------
# Quick test（快速测试）：复用上次脚本与素材，零积分
# ---------------------------------------------------------------------------

def _find_quick_test_source(args, exclude_dir=None):
    """快速测试素材来源：sleep 模式最近一次含 script.json 的运行。
    排除回收站/隐藏目录/当前运行。找不到返回 None。"""

    def _collect(base: Path, want_structure):
        runs = []
        if not base.is_dir():
            return runs
        for d in base.iterdir():
            if not d.is_dir() or d.name.startswith((".", "_")):
                continue
            if exclude_dir is not None and d.resolve() == Path(exclude_dir).resolve():
                continue
            sp = d / "script.json"
            if not sp.exists():
                continue
            if want_structure is not None:
                try:
                    st = json.loads(sp.read_text(encoding="utf-8")).get("structure", "")
                except (json.JSONDecodeError, OSError):
                    continue
                if st != want_structure:
                    continue
            runs.append(d)
        return runs

    root = Path(getattr(args, "output_dir", None)
                or getattr(args, "output", "./output"))
    mode_name = getattr(args, "mode_name", "") or ""
    cands = _collect(root / mode_name, None) if mode_name else []
    if not cands:
        if root.is_dir():
            for md in root.iterdir():
                if md.is_dir() and not md.name.startswith((".", "_")):
                    cands += _collect(md, args.structure)
    if not cands:
        return None
    cands.sort(key=lambda d: (d / "script.json").stat().st_mtime, reverse=True)
    return cands[0]


def _quick_test_copy_materials(src: Path, work_dir: Path, args) -> list:
    """复制源运行现成素材：sleep 模式为 images/ audio/。"""
    import shutil
    copied = []
    for name in ("images", "audio"):
        src_dir = src / name
        if not src_dir.is_dir():
            continue
        dst_dir = work_dir / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in src_dir.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(dst_dir / f.name))
        copied.append(f"{name}×{sum(1 for _ in dst_dir.iterdir())}")
    return copied

# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def _step0_script(args, checkpoint: dict, topic: str, parent_dir: Path,
                  used_topics_file: str) -> tuple[dict, Path, dict]:
    """Step 0: generate (or resume) the script and create the run directory."""
    print("=" * 60)
    _llm_provider = os.environ.get("LLM_PROVIDER", "sensenova")
    if _llm_provider == "openai":
        _llm_model = os.environ.get("OPENAI_MODEL", "grok-4.6")
        print(f"Step 0: Generating script via LLM (OpenAI-compatible: {_llm_model})...")
    elif _llm_provider == "gemini":
        _llm_model = os.environ.get("GEMINI_MODEL", "models/gemini-3.8-flash")
        print(f"Step 0: Generating script via LLM (Gemini {_llm_model})...")
    else:
        _llm_model = os.environ.get("SENSENOVA_MODEL", "deepseek-v4-flash")
        print(f"Step 0: Generating script via LLM (SenseNova {_llm_model})...")

    work_dir = _resolve_run_dir(parent_dir, checkpoint)

    def _dirs(base: Path) -> dict:
        return {
            "images": base / "images",
            "clips": base / "clips",
            "audio": base / "audio",
            "subtitles": base / "subtitles",
            "videos": base / "videos",
        }

    dirs = _dirs(work_dir)
    script_path = work_dir / "script.json"
    structure_match = (checkpoint.get("structure") == args.structure)
    if (_step_done(checkpoint, "step0_script") and script_path.exists()
            and structure_match):
        print("  [Resume] Loading existing script...")
        script = json.loads(script_path.read_text(encoding="utf-8"))
        mark_topic_used(used_topics_file, topic)
    else:
        qt_src = (_find_quick_test_source(args)
                  if getattr(args, "quick_test", False) else None)
        if qt_src is not None:
            # 快速测试：脚本复用源运行，运行目录沿用其标题（冲突加 _qt 后缀）
            script = json.loads((qt_src / "script.json").read_text(encoding="utf-8"))
            script["structure"] = args.structure
            yt_title = script.get("youtube_title", script.get("title", topic))
            safe_title = _safe_dirname(yt_title, topic)
            work_dir = parent_dir / safe_title
            if work_dir.exists():
                work_dir = parent_dir / f"{safe_title}_qt{time.strftime('%H%M%S')}"
            work_dir.mkdir(parents=True, exist_ok=True)
            dirs = _dirs(work_dir)
            script_path = work_dir / "script.json"
            for d in dirs.values():
                d.mkdir(parents=True, exist_ok=True)
            script["structure"] = args.structure
            script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            _save_checkpoint(work_dir, "step0_script", topic=topic, cefr=args.cefr,
                             structure=args.structure)
            print(f"  [QuickTest] 脚本复用自上次运行: {qt_src.name}")
        else:
            if getattr(args, "quick_test", False):
                print("  [QuickTest] 找不到可复用脚本的运行 —— 本次正常生成脚本")
            script = _generate_script_with_retry(
                topic, args.cefr, args.lessons_dir, args.num_lines,
                sleep_pairs=int(getattr(args, "sleep_pairs", 200)),
                sleep_batch=int(getattr(args, "sleep_batch_pairs", 50)),
                sleep_cache_dir=str(parent_dir / ".sleep_cache"),
                sleep_use_cache=bool(getattr(args, "sleep_use_cache", True)))
            yt_title = script.get("youtube_title", script.get("title", topic))
            safe_title = _safe_dirname(yt_title, topic)
            work_dir = parent_dir / safe_title
            work_dir.mkdir(parents=True, exist_ok=True)
            dirs = _dirs(work_dir)
            script_path = work_dir / "script.json"
            for d in dirs.values():
                d.mkdir(parents=True, exist_ok=True)
            qa_report = script.pop("_qa", None)
            script["structure"] = args.structure
            script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            if qa_report:
                qa_path = work_dir / "qa_report.json"
                qa_path.write_text(json.dumps(qa_report, indent=2), encoding="utf-8")
                print(f"  QA report saved: {qa_path}")
            _save_checkpoint(work_dir, "step0_script", topic=topic, cefr=args.cefr,
                             structure=args.structure)
            mark_topic_used(used_topics_file, topic)
    print(f"  Script saved: {script_path}")
    print(f"  Title: {script.get('title', '')}")
    print(f"  Dialogue lines: {len(script.get('dialogue', []))}")
    return script, work_dir, dirs


def _step1_mcp(args):
    """Step 1: sleep 主流程零 MCP —— 跳过初始化（零积分模式）。

    缩略图 AI 生成（Step 4.5）与 AI 背景图（Step 2）需要 MCP 时按需 initialize。
    """
    print("\n" + "=" * 60)
    if getattr(args, "quick_test", False):
        print("Step 1: 快速测试 —— 跳过 MCP 初始化")
        return
    print("Step 1: sleep 模式 —— 跳过 MCP 初始化（零 MCP 消耗）")


def _step2_images_tts(args, checkpoint: dict, script: dict, work_dir: Path, dirs: dict,
                      stop_check=None) -> dict:
    """Step 2: sleep 音频生成（TTS 本地零积分，文件级续传）+ 可选 AI 背景图/片头绑定。"""
    print("\n" + "=" * 60)
    print("Step 2: Sleep audio generation (TTS only, zero credits)...")

    img_dir, audio_dir = dirs["images"], dirs["audio"]
    scene = script.get("scene") or args.topic

    style_prompt = _get_style_prompt(args)
    if not os.environ.get("VISUAL_STYLE_PROMPT"):
        os.environ["VISUAL_STYLE_PROMPT"] = style_prompt
    print(f"  [Style] Using style_prompt: {style_prompt[:80]}...")

    tts_results = {}
    # --- Quick test：复用上次素材（零积分）---
    quick_test = getattr(args, "quick_test", False)
    if quick_test:
        qt_src = _find_quick_test_source(args, exclude_dir=work_dir)
        if qt_src is not None:
            _quick_test_copy_materials(qt_src, work_dir, args)
            print(f"  [QuickTest] 复用素材 ← {qt_src.name}")
        else:
            print("  [QuickTest] 未找到源运行 —— sleep 音频正常生成（本地零积分）")

    # --- Sleep TTS（文件级续传）---
    from sleep.audio_sleep import load_sleep_audio_results, prepare_sleep_audio
    loaded = load_sleep_audio_results(audio_dir, int(getattr(args, "sleep_pairs", 200)))
    if loaded is not None:
        tts_results, image_urls = loaded, {}
        print("  [Resume] sleep 音频已完整，跳过 TTS。")
    else:
        image_urls = {}
        try:
            tts_results.update(prepare_sleep_audio(
                script, audio_dir, int(getattr(args, "sleep_pairs", 200)),
                tts_engine=getattr(args, "tts_engine", "kokoro"),
                slow_rate=float(getattr(args, "sleep_slow_rate", 0.8)),
                male_rate=float(getattr(args, "sleep_male_rate", 1.0) or 1.0),
                channel_name=str(getattr(args, "sleep_channel_name", "") or ""),
                outro_text=str(getattr(args, "sleep_outro_text", "") or ""),
                stop_check=stop_check))
        except RuntimeError as e:
            if str(e) == "stopped":
                tts_results["fatal_error"] = "stopped"
                print("  [Sleep] Generation stopped by user.", flush=True)
            else:
                raise

    # 片头库绑定：intro 视频拷入运行目录，timeline intro 段时长随视频
    # （intro TTS 照旧生成，音频完整性校验不变；绑定后 compose 不再消费它）
    intro_src = str(getattr(args, "sleep_intro_video", "") or "").strip()
    if intro_src and not getattr(args, "sleep_intro", True):
        print("  [Sleep] 片头已关闭（sleep_intro=False）—— 跳过片头视频绑定")
    elif intro_src and not tts_results.get("fatal_error"):
        if os.path.exists(intro_src):
            import shutil
            intro_dst = work_dir / "intro_video.mp4"
            if not intro_dst.exists():
                shutil.copy2(intro_src, intro_dst)
            tts_results["intro_video"] = str(intro_dst)
            tts_results["intro_dur"] = _get_audio_duration(str(intro_dst))
            print(f"  [Sleep] Intro video bound: {intro_dst.name} "
                  f"({tts_results['intro_dur']:.1f}s)")
        else:
            print(f"  [Sleep] WARNING: 片头视频不存在: {intro_src} —— 回退默认片头")

    # 背景图（增强功能，失败不中断运行）：固定路径优先，留空按主题 AI 生成；
    # 产物 images/sleep_bg.png，Step 5 build_theme 注入后卡片低透明度混入
    if getattr(args, "sleep_bg_image", False) and not quick_test \
            and not tts_results.get("fatal_error"):
        _fixed_bg = str(getattr(args, "sleep_bg_image_path", "") or "").strip()
        if _fixed_bg and os.path.exists(_fixed_bg):
            print(f"  [SleepBG] 使用固定背景图: {_fixed_bg}")
        else:
            if _fixed_bg:
                print(f"  [SleepBG] WARNING: 固定背景图不存在: {_fixed_bg}"
                      " —— 回退按主题 AI 生成")
            import sensenova_image
            if sensenova_image.get_image_provider() != "sensenova":
                # Step 1 对 sleep 跳过了 MCP 初始化 —— 按需初始化（照缩略图先例）
                raw_tokens = args.mcp_tokens or args.mcp_token or ""
                _toks = [t.strip() for t in raw_tokens.split(",") if t.strip()]
                if _toks:
                    try:
                        initialize(tokens=_toks)
                    except Exception as e:
                        print(f"  [SleepBG] MCP 初始化失败: {e} —— 回退纯渐变")
                else:
                    print("  [SleepBG] 未配置 MCP Token —— 跳过 AI 背景图"
                          "（配置页填 mcp_tokens 或切 sensenova 后可用）")
            from sleep.bg_image import ensure_sleep_bg_image
            ensure_sleep_bg_image(
                str(img_dir), scene, style_prompt,
                mcp_call_tool=call_tool, mcp_parse_task_id=parse_task_id,
                mcp_poll_task=poll_task, mcp_download_file=download_file)

    print("  [Sleep] Skipping clip generation (no video clips)")

    if stop_check and stop_check():
        return {
            "scene": scene,
            "image_urls": image_urls,
            "scene_url": "",
            "char_scene_url": "",
            "tts_results": tts_results,
            "scene_clip_task": None,
            "scene_clip_thread": None,
            "clip_paths": [],
        }
    _tts_err = tts_results.get("fatal_error")
    if _tts_err:
        if _tts_err == "stopped":
            print("  [TTS] Generation stopped by user.")
        else:
            print("  [TTS] FATAL: TTS generation crashed:")
            print(f"    {_tts_err}")
            raise RuntimeError(f"TTS generation failed: {_tts_err}. "
                               f"Fix the issue and re-run with --resume.")
    _got_pairs = len(tts_results.get("pair_durs", {}))
    _need_pairs = int(getattr(args, "sleep_pairs", 200))
    if _got_pairs < _need_pairs:
        raise RuntimeError(
            f"Sleep TTS incomplete: got {_got_pairs}/{_need_pairs} pairs. "
            f"Re-run with --resume to continue.")

    _save_checkpoint(work_dir, "step2_images_tts")

    return {
        "scene": scene,
        "image_urls": image_urls,
        "scene_url": "",
        "char_scene_url": "",
        "tts_results": tts_results,
        "scene_clip_task": None,
        "scene_clip_thread": None,
        "clip_paths": [],
    }

def _step3_clips(args, checkpoint: dict, work_dir: Path, dirs: dict, script: dict,
                 ctx: dict, stop_check=None) -> tuple[list, list, dict]:
    """Step 3: sleep 模式无视频片段 —— 恒跳过。"""
    print("\n" + "=" * 60)
    print(f"Step 3: Skipped ({args.structure} mode — no video generation)")
    _save_checkpoint(work_dir, "step3_video")
    return [], [], {}


def _step4_timeline(args, checkpoint: dict, script: dict, work_dir: Path,
                    dirs: dict, tts_results: dict) -> tuple[list, dict, list, list]:
    """Step 4: build timeline + SRT (or resume from meta.json)."""
    print("\n" + "=" * 60)
    print("Step 4: Building timeline + SRT...")
    sub_dir = dirs["subtitles"]
    srt_path = sub_dir / "output.srt"
    meta_path = sub_dir / "meta.json"

    if _step_done(checkpoint, "step4_timeline") and srt_path.exists() and meta_path.exists():
        print("  [Resume] Loading existing timeline + SRT...")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return (meta["timeline"], meta.get("narration", {}),
                meta.get("normal_paths", []), meta.get("zh_paths", []))

    from sleep.timeline_sleep import build_sleep_srt, build_sleep_timeline
    timeline = build_sleep_timeline(
        script, tts_results, int(getattr(args, "sleep_pairs", 200)),
        gap_short=float(getattr(args, "sleep_gap_short", 1.0)),
        gap_long=float(getattr(args, "sleep_gap_long", 2.0)),
        pair_gap=float(getattr(args, "sleep_pair_gap", 3.0)),
        include_intro=bool(getattr(args, "sleep_intro", True)),
        card_lead=float(getattr(args, "sleep_card_lead", 0.3) or 0.0))
    # 字幕不上屏（文字预渲染进卡片）；SRT 仅作 sidecar 闭源字幕文件
    srt = build_sleep_srt(timeline)

    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt, encoding="utf-8")
    print(f"  SRT saved: {srt_path}")

    meta = {
        "timeline": timeline,
        "script": script,
        "pad": getattr(args, "pad", 0.4),
        "narration": tts_results.get("narration", {}),
        "normal_paths": tts_results.get("normal_paths", []),
        "zh_paths": tts_results.get("zh_paths", []),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Meta saved: {meta_path}")
    _save_checkpoint(work_dir, "step4_timeline")
    return timeline, meta["narration"], meta["normal_paths"], meta["zh_paths"]


def _step45_thumbnail(args, checkpoint: dict, script: dict, work_dir: Path,
                      dirs: dict, timeline: list, ctx: dict) -> None:
    """Step 4.5: generate YouTube thumbnail + metadata."""
    print("\n" + "-" * 60)
    print("Step 4.5: Generating YouTube metadata + thumbnail...")
    from thumbnail_gen import generate_thumbnail, save_youtube_metadata

    thumb_path = str(work_dir / "thumbnail.jpg")
    yt_meta_path = str(work_dir / "youtube_metadata.json")
    if (_step_done(checkpoint, "step4.5_thumbnail") and os.path.exists(yt_meta_path)
            and (args.no_thumbnail or os.path.exists(thumb_path))):
        print("  [Resume] Thumbnail + YouTube metadata already exist, skipping...")
        return

    if args.no_thumbnail or getattr(args, "quick_test", False):
        _skip_why = ("no_thumbnail" if args.no_thumbnail else "quick_test")
        print(f"  [Thumbnail] 跳过缩略图生成（{_skip_why}）——仅生成 YouTube 元数据")
    else:
        # sleep：参考同款 AI 缩略图（3D 角色+大字标题），失败兜底 Pillow 卡片；
        # 集数按主题系列自动递增（重生成读 script.thumb_episode 不再递增）
        from thumbnail_gen import assign_sleep_episode
        from sleep.sleep_cards import build_theme
        assign_sleep_episode(script, str(work_dir.parent))
        (work_dir / "script.json").write_text(
            json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        # Step 1 对 sleep 跳过了 MCP 初始化 —— AI 缩略图需要会话，按需初始化；
        # 无 token / 初始化失败只告警（generate_thumbnail 内部回退 Pillow 卡片，
        # 不崩掉与 MCP 无关的 sleep 运行）
        import sensenova_image
        if sensenova_image.get_image_provider() != "sensenova":
            raw_tokens = args.mcp_tokens or args.mcp_token or ""
            _toks = [t.strip() for t in raw_tokens.split(",") if t.strip()]
            if _toks:
                print("  [Thumbnail] sleep 缩略图 AI 生成 —— 初始化 MCP 会话...")
                try:
                    initialize(tokens=_toks)
                except Exception as e:
                    print(f"  [Thumbnail] MCP 初始化失败: {e} —— 回退 Pillow 卡片")
            else:
                print("  [Thumbnail] 未配置 MCP Token —— sleep 缩略图回退 Pillow 卡片"
                      "（配置页填 mcp_tokens 或切 sensenova 后可用 AI 生成）")
        generate_thumbnail(
            script=script,
            scene_img="",
            output_path=thumb_path,
            mcp_call_tool=call_tool,
            mcp_parse_task_id=parse_task_id,
            mcp_poll_task=poll_task,
            mcp_download_file=download_file,
            structure="sleep",
            sleep_theme=build_theme(vars(args)),
            sleep_channel=str(getattr(args, "sleep_channel_name", "")
                              or "English with me"),
        )
        print(f"  [Thumbnail] sleep thumbnail saved: {thumb_path}")
    save_youtube_metadata(
        script=script,
        timeline=timeline,
        output_path=yt_meta_path,
        structure=args.structure,
    )
    _save_checkpoint(work_dir, "step4.5_thumbnail")

def _step5_compose(args, checkpoint: dict, script: dict, work_dir: Path, dirs: dict,
                   clip_paths: list, timeline: list, narration: dict,
                   normal_paths: list, zh_paths: list, tts_results: dict,
                   group_info: list, line_to_group: dict,
                   stop_check=None) -> tuple[str, str]:
    """Step 5: compose the final video (sleep = Pillow 卡片组级块合成)."""
    print("\n" + "=" * 60)
    print("Step 5: Composing final video...")

    def progress_cb(pct, msg):
        print(f"  [{pct}%] {msg}")

    yt_title = script.get("youtube_title", script.get("title", "final"))
    safe_vid_name = _safe_dirname(yt_title, "final_video")
    final_video_path = work_dir / f"{safe_vid_name}.mp4"
    if _step_done(checkpoint, "step5_compose") and final_video_path.exists():
        print("  [Resume] Final video already exists, skipping compose...")
        return str(final_video_path), safe_vid_name

    from sleep.sleep_cards import build_theme
    from sleep.video_compose_sleep import compose_sleep
    _theme = build_theme(vars(args))
    if getattr(args, "sleep_bg_image", False) and not _theme.get("bg_image_path"):
        # 自动模式：Step 2 生成的 images/sleep_bg.png 注入主题（存在才生效）
        _auto_bg = str(work_dir / "images" / "sleep_bg.png")
        if os.path.exists(_auto_bg):
            _theme["bg_image_path"] = _auto_bg
    final_path = compose_sleep(
        work_dir=str(work_dir),
        timeline=timeline,
        script=script,
        audio_results=tts_results,
        cards_dir=str(work_dir / "cards"),
        theme=_theme,
        channel_name=str(getattr(args, "sleep_channel_name", "")
                         or "English with me"),
        badge_text="EN",
        outro_text=str(getattr(args, "sleep_outro_text", "") or ""),
        num_pairs=int(getattr(args, "sleep_pairs", 200)),
        intro_video=str(tts_results.get("intro_video", "") or ""),
        native_4k=bool(getattr(args, "sleep_4k_native", False)),
        card_lead=float(getattr(args, "sleep_card_lead", 0.3) or 0.0),
        xfade_sec=(float(getattr(args, "sleep_xfade_sec", 0.5) or 0.5)
                   if getattr(args, "sleep_xfade", False) else 0.0),
        progress_cb=progress_cb,
        stop_check=stop_check,
    )
    if not final_path:
        print("  [Compose] Interrupted or no output, skipping checkpoint save.")
        return "", safe_vid_name
    _save_checkpoint(work_dir, "step5_compose")
    return final_path, safe_vid_name


def _step55_bgm(args, checkpoint: dict, work_dir: Path, final_path: str) -> str:
    """Step 5.5: mix random copyright BGM into the final video audio.

    输出新文件 {stem}_bgm.mp4（原片保留）；未启用 / 音乐库缺失 / 混音失败时
    返回原片路径继续（BGM 是增强功能，失败不中止 run）。
    4K 步骤以本函数返回的路径为源（BGM 音轨自动继承到 4K）。
    """
    if not getattr(args, "bgm_mix", False):
        return final_path
    if not final_path or not Path(final_path).exists():
        return final_path

    print("\n" + "=" * 60)
    print("Step 5.5: BGM copyright music mix...")
    music_dir = str(getattr(args, "bgm_music_dir", "") or "").strip()
    if not music_dir:
        music_dir = str(Path(__file__).resolve().parent.parent / "bgm_music")
    if not os.path.isdir(music_dir):
        print(f"  [BGM] 音乐库不存在: {music_dir} — 跳过混音（配置 bgm_music_dir 或放入音乐文件）")
        return final_path

    # Resume: checkpoint 记录的 _bgm 产物仍存在时直接复用
    cached = str(checkpoint.get("bgm_output", "") or "")
    if _step_done(checkpoint, "step55_bgm") and cached and Path(cached).exists():
        print(f"  [Resume] BGM video already exists, skipping: {Path(cached).name}")
        return cached

    final_p = Path(final_path)
    bgm_output = str(work_dir / f"{final_p.stem}_bgm.mp4")

    from bgm_mix import mix_bgm_into_video, chapter_start_seconds
    bgm_start_seconds = chapter_start_seconds(
        work_dir, int(getattr(args, "bgm_start_chapter", 1) or 1))
    ok = mix_bgm_into_video(
        final_path,
        bgm_output,
        music_dir,
        bgm_start_seconds=bgm_start_seconds,
        ducking_mode=str(getattr(args, "bgm_ducking_mode", "sidechain") or "sidechain"),
        bgm_base_gain_db=float(getattr(args, "bgm_base_gain_db", -15)),
        volume_offset_db=float(getattr(args, "bgm_volume_offset_db", -25)),
        highpass_freq=int(getattr(args, "bgm_highpass_freq", 150)),
        fade_duration_ms=int(getattr(args, "bgm_fade_ms", 3000)),
        min_volume_db=float(getattr(args, "bgm_min_volume_db", -40)),
        dyn_vol=bool(getattr(args, "bgm_dynamic_volume", True)),
        spec_shape=bool(getattr(args, "bgm_spectral_shaping", True)),
        stereo_offset=float(getattr(args, "bgm_stereo_offset", 0.0)),
        sc_threshold_db=float(getattr(args, "bgm_sc_threshold_db", -30)),
        sc_threshold_offset_db=float(getattr(args, "bgm_sc_threshold_offset_db", -5)),
        sc_ratio=int(getattr(args, "bgm_sc_ratio", 8)),
        sc_attack_ms=int(getattr(args, "bgm_sc_attack_ms", 5)),
        sc_release_ms=int(getattr(args, "bgm_sc_release_ms", 400)),
        intro_outro_seconds=int(getattr(args, "bgm_intro_outro_seconds", 5)),
    )
    if ok and os.path.exists(bgm_output) and os.path.getsize(bgm_output) > 0:
        size_mb = os.path.getsize(bgm_output) / (1024 * 1024)
        print(f"  [BGM] 完成: {Path(bgm_output).name} ({size_mb:.1f}MB)")
        _save_checkpoint(work_dir, "step55_bgm", bgm_output=bgm_output)
        return bgm_output
    print("  [BGM] 混音失败 — 继续使用原片音频（不中止流程）")
    return final_path


def _step6_4k(args, checkpoint: dict, work_dir: Path, final_path: str,
              safe_vid_name: str) -> Path | None:
    """Step 6: upscale the final video to 4K. Returns the 4K path or None."""
    print("\n" + "=" * 60)
    print("Step 6: Upscaling to 4K...")
    final_4k_path = work_dir / f"{safe_vid_name}_4K.mp4"
    if args.no_4k:
        print("  [4K] Skipped (--no-4k).")
        return None
    if _step_done(checkpoint, "step6_4k") and final_4k_path.exists():
        print("  [Resume] 4K video already exists, skipping...")
        return final_4k_path
    # 已 4K 守卫（sleep 原生 4K 模式成片即 4K）：硬链接产出 _4K 文件，
    # 零重编码且保持运行页「复制 4K 路径 / 混BGM 4K」等下游语义不变
    try:
        from media_utils import probe_resolution
        _w, _h = probe_resolution(str(final_path))
        if _w >= 3800:
            print(f"  [4K] 源视频已是 {_w}x{_h} —— 链接产出 _4K 文件（跳过放大）")
            try:
                os.link(final_path, final_4k_path)
            except OSError:
                import shutil as _sh
                _sh.copy2(final_path, final_4k_path)
            _save_checkpoint(work_dir, "step6_4k")
            return final_4k_path
    except Exception:
        pass  # 探测/链接失败 → 落回常规放大路径
    try:
        # 新增引擎选项：ai = Real-ESRGAN animevideov3 本地超分（默认 ffmpeg 原路径不变）
        engine = str(getattr(args, "upscale_engine", "ffmpeg") or "ffmpeg")
        r = None
        ai_done = False
        if engine == "ai":
            from sr_upscale import upscale_video_ai, model_available
            if not model_available():
                print("  [4K] AI 超分不可用（权重缺失或无 CUDA），回退 ffmpeg lanczos")
            else:
                print("  [4K] AI 超分引擎：realesr-animevideov3 (torch CUDA fp16)")
                upscale_video_ai(
                    final_path, str(final_4k_path),
                    timeout=max(60, int(getattr(args, "upscale_timeout", 3600) or 3600)))
                ai_done = True
        if not ai_done:
            r = subprocess.run(
                ["ffmpeg", "-i", final_path,
                 "-vf", "scale=3840:2160:flags=lanczos",
                 "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-threads", "0",
                 "-c:a", "copy",
                 str(final_4k_path), "-y"],
                capture_output=True,
                timeout=max(60, int(getattr(args, "upscale_timeout", 3600) or 3600)))
    except subprocess.TimeoutExpired:
        print(f"  [4K] Upscaling timed out (>{getattr(args, 'upscale_timeout', 3600)}s) — 720p version is still available.")
        r = None
        final_4k_path.unlink(missing_ok=True)
        raise RuntimeError("STEP6_4K_FAILED") from None
    except Exception as e:
        print(f"  [4K] Upscaling error: {e}")
        r = None
        final_4k_path.unlink(missing_ok=True)
        raise RuntimeError("STEP6_4K_FAILED") from None
    if r is not None and r.returncode == 0 and final_4k_path.exists():
        size_4k = os.path.getsize(final_4k_path) / (1024 * 1024)
        print(f"  4K video saved: {final_4k_path} ({size_4k:.1f}MB)")
        _save_checkpoint(work_dir, "step6_4k")
        return final_4k_path
    if r is not None:
        print(f"  [4K] Upscaling failed, 720p version is still available.")
        stderr = r.stderr.decode("utf-8", errors="replace")[-500:] if r.stderr else ""
        if stderr:
            print(f"  [4K] FFmpeg stderr: {stderr}")
        # 失败必须抛出：否则调用方会误以为全部完成而清除 checkpoint，
        # 导致无法用 --resume 直接重试 4K 步骤
        raise RuntimeError("STEP6_4K_FAILED")
    # AI 引擎成功路径：r 为 None 且文件已生成
    if final_4k_path.exists() and final_4k_path.stat().st_size > 0:
        size_4k = os.path.getsize(final_4k_path) / (1024 * 1024)
        print(f"  4K video saved: {final_4k_path} ({size_4k:.1f}MB)")
        _save_checkpoint(work_dir, "step6_4k")
        return final_4k_path
    return None

# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate sleep listening practice video (睡觉听·AB短句循环)")
    parser.add_argument("--topic", default=None, help="Topic (e.g. 'At the Pharmacy'). If not specified, picks randomly from topics.json")
    parser.add_argument("--cefr", default="A2", choices=["A1", "A2", "B1", "B2", "C1", "C2"], help="CEFR level (default A2)")
    parser.add_argument("--structure", default="sleep", choices=["sleep"],
                        help="Video structure (本项目仅 sleep：睡觉听·AB短句循环)")
    parser.add_argument("--output", default="./output", help="Output directory")
    parser.add_argument("--topics-file", default=str(Path(__file__).parent / "topics.json"), help="Path to topics.json")
    parser.add_argument("--used-topics-file", default=None, help="Path to used_topics.json (default: <output>/used_topics.json)")
    parser.add_argument("--lessons-dir", default=None, help="Lessons dir for anti-duplicate check")
    # --- sleep 参数 ---
    parser.add_argument("--sleep-pairs", type=int, default=200, help="A/B 短对话组数（行数=组数×2，默认 200 组=400 行，clamp 10-400）")
    parser.add_argument("--sleep-slow-rate", type=float, default=0.8, help="女声慢速版速率倍率（0.6-0.95，默认 0.8；经引擎 rate 机制调速）")
    parser.add_argument("--sleep-male-rate", type=float, default=1.0, help="男声速率倍率（0.5-1.5，默认 1.0；<1 更慢更催眠）")
    parser.add_argument("--sleep-gap-short", type=float, default=1.0, help="常速朗读后停顿秒数（默认 1.0）")
    parser.add_argument("--sleep-gap-long", type=float, default=2.0, help="慢速跟读后停顿秒数（默认 2.0）")
    parser.add_argument("--sleep-pair-gap", type=float, default=3.0, help="AB 连贯后切组停顿秒数（默认 3.0）")
    parser.add_argument("--sleep-channel-name", default="English with me", help="卡片/片头频道名（同步作 TTS 播报）")
    parser.add_argument("--sleep-intro-video", default="", help="片头视频 mp4 路径（片头库生成后绑定；空=默认静态卡片+频道名播报）")
    parser.add_argument("--sleep-intro", action=argparse.BooleanOptionalAction, default=True,
                        help="是否生成片头（关闭=无 intro 段直接从第一组开始；intro TTS 仍生成以保持缓存完整性）")
    parser.add_argument("--sleep-card-lead", type=float, default=0.3,
                        help="卡片提前量秒数（每组画面先出现 N 秒再开始朗读，0=关闭，默认 0.3，clamp 0-2）")
    parser.add_argument("--sleep-xfade", action="store_true",
                        help="相邻块边界画面交叉溶解过渡；仅画面、音频不动（默认关=硬切）")
    parser.add_argument("--sleep-xfade-sec", type=float, default=0.5,
                        help="交叉溶解时长秒（0.2-2.0，默认 0.5；需 --sleep-xfade）")
    parser.add_argument("--sleep-font-scale", type=int, default=100, help="句子区字号缩放百分比（60-160，默认 100）")
    parser.add_argument("--sleep-line-spacing", type=int, default=14, help="句子区英文行距像素@720p（0-48，默认 14）")
    parser.add_argument("--sleep-letter-spacing", type=int, default=0, help="句子区字距像素@720p（0-24，默认 0）")
    parser.add_argument("--sleep-bg-layer", default="bottom", help="背景图层级 bottom=底层衬底（默认）/ top=第二级（盖过白卡/边框/叶片）")
    parser.add_argument("--sleep-outro-text", default="Thanks for listening. See you next time!", help="片尾结束语（TTS+卡片）")
    parser.add_argument("--sleep-batch-pairs", type=int, default=50, help="LLM 分批生成每批组数（默认 50）")
    parser.add_argument("--sleep-use-cache", action=argparse.BooleanOptionalAction, default=True, help="复用已落盘批次缓存（默认开；关闭=每次现场重新生成）")
    parser.add_argument("--sleep-show-leaves", action=argparse.BooleanOptionalAction, default=True, help="卡片叶片装饰（默认开）")
    parser.add_argument("--sleep-handwrite-font", default="", help="手写体字体文件路径（缺省 Inkfree→Segoe Script→msyhbd）")
    parser.add_argument("--sleep-color-bg-top", default="", help="背景渐变顶部 hex（空=内置默认）")
    parser.add_argument("--sleep-color-bg-bottom", default="", help="背景渐变底部 hex")
    parser.add_argument("--sleep-color-card", default="", help="卡片底色 hex")
    parser.add_argument("--sleep-color-card-border", default="", help="卡片描边 hex")
    parser.add_argument("--sleep-color-en-a", default="", help="A 句英文颜色 hex")
    parser.add_argument("--sleep-color-en-b", default="", help="B 句英文颜色 hex")
    parser.add_argument("--sleep-color-phonetic", default="", help="IPA 音标颜色 hex")
    parser.add_argument("--sleep-color-zh", default="", help="中文翻译颜色 hex")
    parser.add_argument("--sleep-color-num", default="", help="序号/横条颜色 hex")
    parser.add_argument("--sleep-color-badge-bg", default="", help="角标底色 hex")
    parser.add_argument("--sleep-color-badge-text", default="", help="角标文字色 hex")
    parser.add_argument("--sleep-color-channel", default="", help="频道名颜色 hex")
    parser.add_argument("--sleep-color-leaf", default="", help="叶片颜色 hex")
    parser.add_argument("--sleep-bg-image", action="store_true", help="开启背景图片（低透明度叠加在渐变背景上）")
    parser.add_argument("--sleep-bg-image-path", default="", help="背景图固定本地路径（填了共用；空=按本期主题 AI 生成）")
    parser.add_argument("--sleep-bg-opacity", type=int, default=20, help="背景图不透明度百分比（0-100，默认 20）")
    parser.add_argument("--sleep-4k-native", action="store_true", help="卡片原生 3840x2160 渲染（成片即 4K；默认关=720p 合成后 Step6 放大）")
    # --- LLM ---
    parser.add_argument("--mcp-tokens", default=None, help="TJGenerators MCP OAuth tokens（仅 AI 缩略图/背景图消费，逗号分隔多 token 轮换）")
    parser.add_argument("--mcp-token", default=None, help="(Deprecated) Single MCP token. Use --mcp-tokens instead.")
    parser.add_argument("--image-provider", default="mcp", choices=["mcp", "sensenova"],
                        help="Image generation provider for bg image/thumbnail: 'mcp' (default) or 'sensenova' (U1.5 Lite)")
    parser.add_argument("--api-key", default=None, help="SenseNova API key (or set SENSENOVA_API_KEY env var)")
    parser.add_argument("--model", default=None, help="SenseNova model name (default deepseek-v4-flash)")
    parser.add_argument("--llm-provider", default="sensenova", choices=["sensenova", "openai", "gemini"],
                        help="LLM provider: 'sensenova' (default), 'openai' (OpenAI-compatible endpoint) or 'gemini' (google-genai SDK)")
    parser.add_argument("--openai-base-url", default=None, help="OpenAI-compatible API base URL (default: https://x666.me/v1)")
    parser.add_argument("--openai-api-key", default=None, help="OpenAI-compatible API key (or set OPENAI_API_KEY env var)")
    parser.add_argument("--openai-model", default=None, help="OpenAI-compatible model name (default: grok-4.6)")
    parser.add_argument("--gemini-api-key", default=None, help="Gemini API key (or set GEMINI_API_KEY env var)")
    parser.add_argument("--gemini-model", default=None, help="Gemini model name (default: models/gemini-3.8-flash)")
    parser.add_argument("--llm-proxy-url", default=None, help="HTTP(S)/SOCKS5 proxy URL for ALL LLM API calls (e.g. socks5://127.0.0.1:10308). Only affects LLM traffic.")
    parser.add_argument("--llm-retries", type=int, default=10, help="Max retries per LLM round (default 10)")
    parser.add_argument("--visual-style", default="pixar3d", help="Visual art style id from style_manager.py（sleep 仅影响背景图 prompt）")
    # --- TTS 引擎 ---
    parser.add_argument("--tts-engine", default="kokoro", choices=["kokoro", "qwen", "moss"],
                        help="TTS engine: 'kokoro' (default, local) or 'qwen' (Qwen3-TTS local GPU) or 'moss' (MOSS-TTS-Nano local CPU)")
    parser.add_argument("--qwen-model-path", default=r"H:\models\Qwen3-TTS-12Hz-0.6B-CustomVoice", help="Qwen3-TTS CustomVoice model path")
    parser.add_argument("--qwen-base-model-path", default=r"H:\models\Qwen3-TTS-12Hz-1.7B-Base", help="Qwen3-TTS Base model path")
    parser.add_argument("--qwen-voicedesign-model-path", default=r"H:\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign", help="Qwen3-TTS VoiceDesign model path")
    parser.add_argument("--qwen-device", default="cuda:0", help="Device for Qwen3-TTS (e.g. cuda:0, cpu)")
    parser.add_argument("--moss-model-path", default=r"H:\models\MOSS-TTS-Nano-Model", help="MOSS-TTS-Nano model checkpoint path")
    parser.add_argument("--moss-tokenizer-path", default=r"H:\models\MOSS-Audio-Tokenizer-Nano", help="MOSS Audio Tokenizer path")
    parser.add_argument("--moss-device", default="cpu", help="Device for MOSS-TTS (default: cpu)")
    parser.add_argument("--moss-repo-dir", default=r"H:\models\MOSS-TTS-Nano", help="MOSS-TTS-Nano repo dir")
    parser.add_argument("--moss-tts-temperature", type=float, default=0.8, help="MOSS-TTS audio sampling temperature (default 0.8)")
    parser.add_argument("--moss-tts-retry", type=int, default=3, help="MOSS-TTS per-sentence retry attempts (default 3)")
    parser.add_argument("--moss-tts-top-p", type=float, default=0.95, help="MOSS-TTS audio top-p (default 0.95)")
    parser.add_argument("--moss-tts-top-k", type=int, default=25, help="MOSS-TTS audio top-k (default 25)")
    parser.add_argument("--moss-tts-rep-penalty", type=float, default=1.2, help="MOSS-TTS repetition penalty (default 1.2)")
    parser.add_argument("--moss-tts-text-temperature", type=float, default=1.0, help="MOSS-TTS text sampling temperature (default 1.0)")
    parser.add_argument("--moss-tts-greedy", dest="moss_tts_greedy", action="store_true", help="MOSS-TTS greedy decoding (do_sample=False)")
    # --- 4K / BGM ---
    parser.add_argument("--upscale-timeout", type=int, default=3600, help="Timeout in seconds for 4K upscale (default 3600)")
    parser.add_argument("--upscale-engine", default="ffmpeg", choices=["ffmpeg", "ai"],
                        help="4K upscale engine: ffmpeg (lanczos, default) or ai (realesr-animevideov3, torch CUDA)")
    parser.add_argument("--bgm-mix", dest="bgm_mix", action="store_true",
                        help="After compose, mix random copyright BGM into the final video audio ({name}_bgm.mp4)")
    parser.add_argument("--bgm-music-dir", default="", help="Music library folder. Default: <project>/bgm_music/")
    parser.add_argument("--bgm-ducking-mode", default="sidechain", choices=["amix", "sidechain", "sidechain_adaptive"],
                        help="BGM mixing mode: amix / sidechain (Content-ID friendly) / sidechain_adaptive")
    parser.add_argument("--bgm-start-chapter", type=int, default=1, help="Mix BGM starting from chapter N (1=from beginning)")
    parser.add_argument("--bgm-base-gain-db", type=int, default=-15, help="BGM base gain dB in sidechain mode (default -15)")
    parser.add_argument("--bgm-volume-offset-db", type=int, default=-25, help="BGM volume offset dB in amix mode (default -25)")
    parser.add_argument("--bgm-fade-ms", type=int, default=3000, help="Crossfade duration ms between music segments (default 3000)")
    parser.add_argument("--bgm-intro-outro-seconds", type=int, default=5, help="Silence padding seconds before/after narration (sidechain only, default 5)")
    parser.add_argument("--bgm-highpass-freq", type=int, default=150, help="Highpass filter Hz applied to BGM in amix mode (default 150)")
    parser.add_argument("--bgm-min-volume-db", type=int, default=-40, help="Minimum BGM volume dB (default -40)")
    parser.add_argument("--bgm-dynamic-volume", action=argparse.BooleanOptionalAction, default=True, help="Dynamic volume envelope tracking in amix mode (default on)")
    parser.add_argument("--bgm-spectral-shaping", action=argparse.BooleanOptionalAction, default=True, help="Spectral gap shaping in amix mode (default on)")
    parser.add_argument("--bgm-stereo-offset", type=float, default=0.0, help="BGM stereo offset -1..1 (default 0)")
    parser.add_argument("--bgm-sc-threshold-db", type=int, default=-30, help="Sidechain threshold dB (default -30)")
    parser.add_argument("--bgm-sc-threshold-offset-db", type=int, default=-5, help="sidechain_adaptive threshold offset dB (default -5)")
    parser.add_argument("--bgm-sc-ratio", type=int, default=8, help="Sidechain compression ratio (default 8)")
    parser.add_argument("--bgm-sc-attack-ms", type=int, default=5, help="Sidechain attack ms (default 5)")
    parser.add_argument("--bgm-sc-release-ms", type=int, default=400, help="Sidechain release ms (default 400)")
    # --- 运行控制 ---
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint in output dir")
    parser.add_argument("--no-4k", dest="no_4k", action="store_true", help="Skip the final 4K upscaling step")
    parser.add_argument("--no-thumbnail", dest="no_thumbnail", action="store_true",
                        help="Skip thumbnail image generation (step 4.5 still saves YouTube metadata)")
    parser.add_argument("--quick-test", dest="quick_test", action="store_true",
                        help="Quick test: reuse last run's script/materials; zero credits")
    return parser.parse_args()


def main():
    args = _parse_args()

    # sleep 派生：行数 = 组数×2（组数 clamp 10-400）
    args.sleep_pairs = max(10, min(400, int(args.sleep_pairs)))
    args.num_lines = args.sleep_pairs * 2
    args.mode_name = args.structure

    # 画面风格：注入 env 供 bg_image 读取
    os.environ["VISUAL_STYLE_ID"] = str(getattr(args, "visual_style", "pixar3d"))
    os.environ["VISUAL_STYLE_PROMPT"] = _resolve_style_prompt(args.visual_style)
    style_name = ""
    try:
        from style_manager import get_style
        _s = get_style(args.visual_style)
        style_name = f" ({_s['name']})" if _s else ""
    except Exception:
        pass
    print(f"  [Style] Visual style: {args.visual_style}{style_name}")
    # Full raw LLM responses are dumped here when _chat hits errors
    os.environ.setdefault("LLM_DEBUG_DIR", str(Path(args.output).resolve() / "llm_debug"))

    # TTS engine config (qwen)
    if args.qwen_model_path:
        os.environ["QWEN_MODEL_PATH"] = args.qwen_model_path
    if args.qwen_base_model_path:
        os.environ["QWEN_BASE_MODEL_PATH"] = args.qwen_base_model_path
    if args.qwen_voicedesign_model_path:
        os.environ["QWEN_VOICEDSIGN_MODEL_PATH"] = args.qwen_voicedesign_model_path
    if args.qwen_device:
        os.environ["QWEN_DEVICE"] = args.qwen_device

    # MOSS-TTS env
    if args.moss_model_path:
        os.environ["MOSS_MODEL_PATH"] = args.moss_model_path
    if args.moss_tokenizer_path:
        os.environ["MOSS_TOKENIZER_PATH"] = args.moss_tokenizer_path
    if args.moss_device:
        os.environ["MOSS_DEVICE"] = args.moss_device
    if args.moss_repo_dir:
        os.environ["MOSS_REPO_DIR"] = args.moss_repo_dir
    if args.moss_tts_temperature is not None:
        os.environ["MOSS_TTS_TEMPERATURE"] = str(args.moss_tts_temperature)
    if args.moss_tts_retry:
        os.environ["MOSS_TTS_RETRY"] = str(args.moss_tts_retry)
    if args.moss_tts_top_p:
        os.environ["MOSS_TTS_TOP_P"] = str(args.moss_tts_top_p)
    if args.moss_tts_top_k:
        os.environ["MOSS_TTS_TOP_K"] = str(args.moss_tts_top_k)
    if args.moss_tts_rep_penalty:
        os.environ["MOSS_TTS_REP_PENALTY"] = str(args.moss_tts_rep_penalty)
    if args.moss_tts_text_temperature:
        os.environ["MOSS_TTS_TEXT_TEMPERATURE"] = str(args.moss_tts_text_temperature)
    if args.moss_tts_greedy:
        os.environ["MOSS_TTS_GREEDY"] = "1"

    # SenseNova key：LLM(sensenova) 与 生图 provider=sensenova 共用；提前注入
    if args.api_key:
        os.environ["SENSENOVA_API_KEY"] = args.api_key

    if args.llm_provider == "openai":
        os.environ["LLM_PROVIDER"] = "openai"
        if args.openai_base_url:
            os.environ["OPENAI_BASE_URL"] = args.openai_base_url
        if args.openai_api_key:
            os.environ["OPENAI_API_KEY"] = args.openai_api_key
        if args.openai_model:
            os.environ["OPENAI_MODEL"] = args.openai_model
        elif args.model:
            os.environ["OPENAI_MODEL"] = args.model
        os.environ.setdefault("OPENAI_BASE_URL", "https://x666.me/v1")
        os.environ.setdefault("OPENAI_MODEL", "grok-4.6")
        if not os.environ.get("OPENAI_API_KEY"):
            print("ERROR: OPENAI_API_KEY not set. Pass --openai-api-key or set env var.")
            sys.exit(1)
    elif args.llm_provider == "gemini":
        os.environ["LLM_PROVIDER"] = "gemini"
        if args.gemini_api_key:
            os.environ["GEMINI_API_KEY"] = args.gemini_api_key
        if args.gemini_model:
            os.environ["GEMINI_MODEL"] = args.gemini_model
        os.environ.setdefault("GEMINI_MODEL", "models/gemini-3.8-flash")
        if not os.environ.get("GEMINI_API_KEY"):
            print("ERROR: GEMINI_API_KEY not set. Pass --gemini-api-key or set env var.")
            sys.exit(1)
    else:
        os.environ["LLM_PROVIDER"] = "sensenova"
        if args.model:
            os.environ["SENSENOVA_MODEL"] = args.model
        if not os.environ.get("SENSENOVA_API_KEY"):
            print("ERROR: SENSENOVA_API_KEY not set. Pass --api-key or set env var.")
            sys.exit(1)

    # LLM 代理（全部 Provider 生效；--llm-proxy-url 传入即启用；仅 LLM 流量，
    # 不影响 MCP/生图/TTS——代理窗口在 llm_client 内按调用实现）
    if args.llm_proxy_url:
        os.environ["LLM_PROXY_ENABLED"] = "1"
        os.environ["LLM_PROXY_URL"] = args.llm_proxy_url

    # 生图 Provider：mcp（默认）或 sensenova（U1.5 Lite，读 IMAGE_PROVIDER env）
    os.environ["IMAGE_PROVIDER"] = args.image_provider
    if args.image_provider == "sensenova" and not os.environ.get("SENSENOVA_API_KEY"):
        print("ERROR: image_provider=sensenova requires SENSENOVA_API_KEY.")
        sys.exit(1)

    parent_dir = Path(args.output).resolve()
    parent_dir.mkdir(parents=True, exist_ok=True)
    # 新布局：output/sleep/{run_name}/
    mode_dir = parent_dir / "sleep"
    mode_dir.mkdir(parents=True, exist_ok=True)

    used_topics_file = args.used_topics_file or str(parent_dir / "used_topics.json")

    if args.resume:
        checkpoint = _load_checkpoint(mode_dir)
        if checkpoint:
            cp_struct = checkpoint.get("structure")
            if cp_struct and cp_struct != args.structure:
                print(f"  [Resume] Structure changed ({cp_struct} → {args.structure}), starting fresh.")
                checkpoint = {}
            else:
                print(f"  [Resume] Found checkpoint: {checkpoint.get('completed_steps', [])}")
        else:
            print("  [Resume] No incomplete checkpoint, starting fresh.")
            checkpoint = {}
    else:
        checkpoint = {}

    topic = _resolve_topic(args, checkpoint)
    if topic is None:
        topic = pick_random_topic(args.topics_file, used_topics_file, mark=False)
        if not topic:
            print("  [Topic] ERROR: No topics found. Please specify --topic or provide topics.json.")
            sys.exit(1)
    args.topic = topic

    script, work_dir, dirs = _step0_script(args, checkpoint, topic, mode_dir, used_topics_file)
    _step1_mcp(args)
    ctx = _step2_images_tts(args, checkpoint, script, work_dir, dirs)
    clip_paths, group_info, line_to_group = _step3_clips(args, checkpoint, work_dir, dirs, script, ctx)
    timeline, narration, normal_paths, zh_paths = _step4_timeline(
        args, checkpoint, script, work_dir, dirs, ctx["tts_results"])
    _step45_thumbnail(args, checkpoint, script, work_dir, dirs, timeline, ctx)
    final_path, safe_vid_name = _step5_compose(
        args, checkpoint, script, work_dir, dirs, clip_paths, timeline,
        narration, normal_paths, zh_paths, ctx["tts_results"], group_info, line_to_group)
    # Step 5.5: BGM 版权音乐混合（启用时输出 {stem}_bgm.mp4，4K 以其为源）
    final_path = _step55_bgm(args, checkpoint, work_dir, final_path)
    final_4k_path = _step6_4k(args, checkpoint, work_dir, final_path, safe_vid_name)

    cp_path = work_dir / "checkpoint.json"
    if cp_path.exists():
        cp_path.unlink()
        print("  [Checkpoint] Cleared — next run will start fresh.")

    print("\n" + "=" * 60)
    print(f"DONE! Final video: {final_path}")
    print(f"Size: {os.path.getsize(final_path) / (1024*1024):.1f}MB")
    if final_4k_path is not None and final_4k_path.exists():
        print(f"4K video: {final_4k_path}")
        print(f"4K Size: {os.path.getsize(final_4k_path) / (1024*1024):.1f}MB")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        if "ALL_MCP_TOKENS_EXHAUSTED" in str(e):
            print("\n" + "=" * 60)
            print("FATAL: 所有 MCP Token 积分已耗尽！")
            print("请充值积分后重新运行（加 --resume 参数）继续上次未完成的视频。")
            print("=" * 60)
            sys.exit(1)
        if "STEP6_4K_FAILED" in str(e):
            print("\n[4K] 上采样失败 — 720p 成片已完成且可用。")
            print("checkpoint 已保留：加 --resume 可直接重试 4K 步骤；"
                  "或加 --no-4k --resume 直接完成本次运行。")
            sys.exit(1)
        raise
    except KeyboardInterrupt:
        print("\n\n用户中断。下次运行加 --resume 可继续。")
        sys.exit(0)
