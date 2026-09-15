"""Shared media utilities — FFmpeg helpers, font constants, and common
video composition building blocks used by all structure variants.

Consolidates previously duplicated code from:
  - pipeline._get_audio_duration
  - video_compose._get_duration / _probe_resolution / _has_audio
  - tts_engine.TTSEngine.get_duration
  - video_compose + quest: concat, loudnorm
  - pipeline._safe_dirname + video_compose inline _re.sub
"""
import os
import re
import sys
import subprocess
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass  # stdout may be redirected/captured (web/pytest) — reconfigure unavailable

# ---------------------------------------------------------------------------
# Font paths (auto-detect Windows vs Linux/Colab)
# ---------------------------------------------------------------------------
import platform

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    FONT_EN = r"C:\Windows\Fonts\msyhbd.ttc"
    FONT_ZH = r"C:\Windows\Fonts\msyh.ttc"
    FONT_PH = r"C:\Windows\Fonts\cambria.ttc"
else:
    FONT_EN = "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"
    FONT_ZH = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
    # DejaVu has complete IPA glyph coverage — Noto CJK renders many IPA
    # characters (ɪ ə ʃ ʒ ɡ...) as tofu boxes
    FONT_PH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    if not os.path.exists(FONT_EN):
        FONT_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not os.path.exists(FONT_ZH):
        FONT_ZH = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"
    if not os.path.exists(FONT_PH):
        FONT_PH = FONT_EN


# ---------------------------------------------------------------------------
# Target canvas — every segment is normalized to this size for concat safety
# ---------------------------------------------------------------------------
TARGET_W, TARGET_H = 1280, 720
VF_NORM = (
    f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
    f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2"
)


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------

def get_duration(path: str) -> float:
    """Get media duration in seconds via ffprobe.

    Consolidates pipeline._get_audio_duration, video_compose._get_duration,
    and tts_engine.TTSEngine.get_duration.
    """
    if not path or not os.path.exists(path):
        return 0.0
    try:
        return float(subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            text=True, encoding="utf-8", errors="replace",
        ).strip())
    except Exception:
        return 0.0


def probe_resolution(video_path: str) -> tuple[int, int]:
    """Get video stream resolution via ffprobe (fallback TARGET_W x TARGET_H).

    Overlays are rendered at this size so they match the video canvas exactly.
    """
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0",
             str(video_path)], text=True, encoding="utf-8", errors="replace").strip()
        # csv writer prints "width,height" (comma-separated)
        parts = out.replace("x", ",").split(",")
        w, h = int(parts[0]), int(parts[1])
        if w > 0 and h > 0:
            return w, h
    except Exception:
        pass
    return TARGET_W, TARGET_H


def has_audio(video_path: str) -> bool:
    """Check if a video file has an audio stream."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(video_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return "audio" in r.stdout.strip()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def safe_filename(yt_title: str, fallback: str = "final_video") -> str:
    """Sanitize a YouTube title into a filesystem-safe name.

    Consolidates pipeline._safe_dirname and video_compose inline _re.sub.
    """
    name = re.sub(r'[\U0001F000-\U0001FFFF]', '', yt_title)  # remove emoji
    name = re.sub(r"[\\/:*?\"'<>|]", '', name).strip()  # ' breaks FFmpeg concat demuxer
    name = re.sub(r'\s+', '_', name)[:80]
    if not name:
        name = re.sub(r'[^\w\s-]', '', fallback).strip().replace(' ', '_')
    return name or "final_video"


# ---------------------------------------------------------------------------

def _probe_video_duration(path: str) -> float | None:
    """探测视频流容器时长（秒），供 xfade 偏移计算。失败返回 None。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None

def _segment_grid_duration(path: str) -> float | None:
    """段网格时长 = max(视频流时长, 音频流时长)——concat demuxer 的推进依据。

    demuxer 按每文件各流时长的最大值推进下一段起点（实测视频流帧量化向上
    时切点=max 流时长）。音频 PCM 必须按同一网格裁尾/补齐，否则：视频流
    （ceil 到帧边界）> 音频流（mp3 解码短于容器时长 − AAC priming），每段
    音频网格比视频网格短数十 ms，数百段累积秒级（sleep 静态卡片块 1000 块
    实测音频比视频短 55s，语音大幅先于卡片出现）。
    """
    durs = [d for d in (_probe_video_duration(path), _probe_audio_duration(path))
            if d is not None and d > 0]
    if durs:
        return max(durs)
    return get_duration(path)


# sleep 块交叉溶解：单次 ffmpeg 最多 128 输入（实测稳定规模），
# 超出按 128 一组宽分块先合并为中间块再合并（块间边界同样叠化，语义与
# 单链等价），402 块（400 组 + 片头片尾）= 2 遍整片编码。
_XFADE_MAX_INPUTS = 128
_XFADE_MERGE_TIMEOUT = 7200


def _blocks_xfade_graph(n: int, video_durs: list[float],
                        grid_durs: list[float], xfade: float) -> tuple[str, str]:
    """构建块 xfade 链的纯视频 filter 图。返回 (图文本, 末视频流标签)。

    偏移与 tpad 冻结量按网格时长（max 视频流/音频
    流，与 concat demuxer 推进一致）计算——卡片块视频流帧量化向上时若用
    视频流时长，输出总长会短于音频网格总和、累积漂移（e14708a 同源问题）。
    每块 tpad = 网格差（补齐音>视的块）+（非本链末块再加 xfade 供叠化）；
    末块补齐到网格保证分块中间产物时长 = Σ成员网格（下一层偏移正确）。
    """
    parts = []
    labels = []
    for k in range(n):
        stop = grid_durs[k] - video_durs[k] + (xfade if k < n - 1 else 0.0)
        if stop > 0.0005:
            parts.append(
                f"[{k}:v]tpad=stop_mode=clone:stop_duration={stop:.3f}[v{k}]")
            labels.append(f"v{k}")
        else:
            labels.append(f"{k}:v")
    prev = labels[0]
    offset = 0.0
    for k in range(1, n):
        offset += grid_durs[k - 1]
        label = f"x{k}"
        parts.append(
            f"[{prev}][{labels[k]}]xfade=transition=fade:duration={xfade:.3f}:"
            f"offset={offset:.3f}[{label}]")
        prev = label
    return ";".join(parts), prev


def _run_xfade_merge_video(seg_paths: list[str], out_path: str,
                           xfade: float, fps: int, timeout: int) -> bool:
    """单次 ffmpeg 调用完成纯视频 xfade 合并（音频由调用方网格拼接）。

    图写脚本文件经 -filter_complex_script 传入（不占命令行）；输出 -an。
    失败/超时删产物返回 False。
    """
    n = len(seg_paths)
    video_durs, grid_durs = [], []
    for p in seg_paths:
        vd = _probe_video_duration(p)
        if not vd or vd <= 0:
            return False
        gd = _segment_grid_duration(p)
        video_durs.append(vd)
        grid_durs.append(max(gd, vd) if gd and gd > 0 else vd)
    if min(grid_durs) < xfade * 2:
        return False
    graph, vlabel = _blocks_xfade_graph(n, video_durs, grid_durs, xfade)
    script_path = out_path + ".filter.txt"
    cmd = ["ffmpeg", "-y"]
    for p in seg_paths:
        cmd += ["-i", p]
    cmd += [
        "-filter_complex_script", script_path,
        "-map", f"[{vlabel}]", "-an",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
        "-t", f"{sum(grid_durs):.3f}",
        out_path,
    ]
    ok = False
    try:
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(graph)
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print("  [Xfade] 块交叉溶解合并超时 — 保持硬切")
            return False
        if r.returncode == 0 and os.path.exists(out_path) \
                and os.path.getsize(out_path) > 1000:
            ok = True
        else:
            print(f"  [Xfade] 块交叉溶解合并失败 — 保持硬切 "
                  f"({r.stderr.decode(errors='replace')[-300:] if r.stderr else ''})")
            try:
                os.remove(out_path)
            except OSError:
                pass
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass
    return ok


def merge_blocks_xfade(block_paths: list[str], out_path: str,
                       xfade: float, fps: int = 25,
                       max_inputs: int = _XFADE_MAX_INPUTS,
                       timeout: int = _XFADE_MERGE_TIMEOUT) -> str | None:
    """把相邻块合并为带交叉溶解（crossfade）的单条纯视频流。

    sleep 组间/片头片尾边界硬切观感生硬；处理方式：
    - 除最后一块外每块视频尾帧 tpad=stop_mode=clone 冻结延展（补齐
      video→网格差值 + xfade 秒）；
    - xfade 链 offset_k = 前面各块网格时长之和 → 输出总长 = Σ网格时长，
      与 concat_segments 音频 PCM 网格拼接严格同格，各块内容起点不变；
    - 仅画面过渡：输出 -an 纯视频，音频由调用方按原网格拼接（块内语音
      完全不受影响）。

    块数超 max_inputs 时宽分块合并。任一步失败返回 None（fail-open，
    调用方回退硬切 concat）。
    """
    paths = [p for p in block_paths if p and os.path.exists(p)]
    if len(paths) < 2 or xfade <= 0 or len(paths) != len(block_paths):
        return None
    parent = Path(out_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    stem = Path(out_path).stem
    cur = list(paths)
    try:
        level = 0
        while len(cur) > max_inputs:
            nxt = []
            for ci in range(0, len(cur), max_inputs):
                chunk = cur[ci:ci + max_inputs]
                if len(chunk) < 2:
                    nxt.append(chunk[0])
                    continue
                mid = str(parent / f"{stem}_m{level}_{ci // max_inputs:03d}.mp4")
                print(f"  [Xfade] 分组合并 {ci // max_inputs + 1} "
                      f"（{len(chunk)} 块，第 {level + 1} 轮）...")
                if not _run_xfade_merge_video(chunk, mid, xfade, fps, timeout):
                    return None
                nxt.append(mid)
            cur = nxt
            level += 1
        if len(cur) < 2:
            return None
        if not _run_xfade_merge_video(cur, out_path, xfade, fps, timeout):
            return None
        return out_path
    finally:
        # 清理分块中间产物（out_path 本身不带 _m 不误删）
        for f in parent.glob(f"{stem}_m*.mp4"):
            try:
                f.unlink()
            except OSError:
                pass


def concat_segments(segment_paths: list[str], output_path: str,
                    tmp_dir: str | Path = None,
                    video_source: str | None = None) -> str:
    """Concatenate segment files (video stream copy + audio filter re-encode).

    All segments must have uniform format (libx264/yuv420p/25fps/aac/44100Hz/stereo).
    video_source：外部预合成纯视频流（如 merge_blocks_xfade 产物）。提供且
    存在时跳过视频 concat 通道、直接以其为 mux 视频源（不删除该文件）；
    音频仍按段网格拼接，与视频总长严格同格。

    为什么不能直接 concat demuxer + -c copy：段的 AAC 轨道含 encoder priming
    （约 46ms），MP4 edit list 把展示时长裁剪到内容时长，但 demuxer copy 拼接
    推进下一段时按轨道原始时长计算 → 每段边界音频多推进约 40ms，几百段后累积
    2 秒以上，字幕（按 timeline 计划时间烧录）相对音频越来越提前。
    因此音频走逐段解码路径（起点 priming 由 edit list 生效裁掉），再按段网格
    时长（max 视频流/音频流，与 demuxer 推进一致）字节级裁尾/补静音后拼接
    PCM 一次编码；视频流无此问题，仍 demuxer 纯 copy 零重编码。注意仅解码
    重编码不够：edit list 不裁尾部，解码会保留编码器补齐到 AAC 帧边界的
    静音样本，且 atrim 等 filter 裁剪是整帧粒度裁不掉，必须在 PCM 字节层
    裁剪/补齐。

    Returns the output path on success, raises RuntimeError on failure.
    """
    tmp_dir = Path(tmp_dir).resolve() if tmp_dir else Path(output_path).resolve().parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    concat_list = tmp_dir / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in segment_paths:
            p = str(Path(s).resolve()).replace("'", "'\\''")
            f.write(f"file '{p}'\n")

    def _run(args: list, what: str) -> None:
        r = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"Concat {what} failed: {r.stderr.decode(errors='replace')[-2000:]}")

    v_only = tmp_dir / "concat_video_only.mp4"
    a_only = tmp_dir / "concat_audio_only.m4a"
    own_video = not (video_source and os.path.exists(video_source))
    if own_video:
        _run(["-f", "concat", "-safe", "0", "-i", str(concat_list),
              "-c:v", "copy", "-an", v_only], "video pass")
    else:
        # 外部预合成的纯视频流（如 sleep 块 xfade 合并）：跳过 concat
        # 视频通道，音频仍按原段网格拼接（与视频总长严格同格）
        v_only = Path(video_source)

    # 音频：逐段解码 → 按容器时长裁掉 AAC 帧尾补齐 → 拼 PCM → 一次编码
    _SR, _CH, _SW = 44100, 2, 2  # s16le stereo 每样本帧 4 字节
    pcm_path = tmp_dir / "concat_audio.pcm"
    audio_ok = True
    try:
        with open(pcm_path, "wb") as out_f:
            for s in segment_paths:
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(Path(s).resolve()),
                     "-map", "0:a:0", "-ar", str(_SR), "-ac", str(_CH),
                     "-f", "s16le", "-"],
                    capture_output=True)
                if r.returncode != 0:
                    audio_ok = False
                    break
                data = r.stdout
                seg_dur = _segment_grid_duration(str(s))
                if seg_dur is not None and seg_dur > 0:
                    want = int(round(seg_dur * _SR)) * _CH * _SW
                    if want < len(data):
                        data = data[:want]
                    elif want > len(data):
                        # 解码短于网格时长（常态：mp3 解码差 + 帧量化）：
                        # 补静音保持音频网格与视频网格严格同格推进
                        data += b"\x00" * (want - len(data))
                out_f.write(data)
        if audio_ok:
            _run(["-f", "s16le", "-ar", str(_SR), "-ac", str(_CH),
                  "-i", str(pcm_path),
                  "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                  a_only], "audio pass")
    except Exception:
        audio_ok = False
    finally:
        try:
            os.remove(pcm_path)
        except OSError:
            pass
    if not audio_ok:
        # 兜底：整轨解码重编码（保留每段 ~11ms 补齐误差，但保证产出）
        _run(["-f", "concat", "-safe", "0", "-i", str(concat_list),
              "-vn", "-af", "asetpts=N/SR/TB",
              "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
              a_only], "audio fallback pass")

    _run(["-i", v_only, "-i", a_only,
          "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
          output_path], "mux pass")
    for t in ((v_only,) if own_video else ()) + (a_only,):
        try:
            os.remove(t)
        except OSError:
            pass

    return output_path


# ---------------------------------------------------------------------------
# Subtitle rendering + overlay burn
# Final loudnorm pass
# ---------------------------------------------------------------------------

def apply_final_loudnorm(video_path: str, vid_dir: str,
                        progress_cb=None) -> str:
    """Apply final loudnorm normalization to the composed video.

    Tries loudnorm first; if it fails, falls back to volume boost.
    Returns the path to the normalized video (may be the same as input).
    """
    def _cb(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)

    norm_path = str(Path(vid_dir) / "final_video_norm.mp4")
    norm_result = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-c:v", "copy",  # video passthrough — fast, no re-encode
         "-c:a", "aac", "-b:a", "128k",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
         norm_path],
        capture_output=True, timeout=600,
    )
    if (norm_result.returncode == 0 and os.path.exists(norm_path)
            and os.path.getsize(norm_path) > 1000):
        os.replace(norm_path, video_path)
        return video_path

    # Fallback: simple volume boost
    if os.path.exists(norm_path):
        try:
            os.remove(norm_path)
        except OSError:
            pass

    vol_path = str(Path(vid_dir) / "final_video_vol.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k",
             "-af", "volume=6dB",
             vol_path],
            capture_output=True, timeout=600,
        )
        if os.path.exists(vol_path) and os.path.getsize(vol_path) > 1000:
            os.replace(vol_path, video_path)
    except Exception:
        pass  # Keep original if both fail

    return video_path


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# SRT building (shared by all timeline variants)
# ---------------------------------------------------------------------------

def _format_srt_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(timeline: list[dict], skip_types: set[str] | None = None,
              gap: float = 0.0) -> str:
    """Build SRT from a timeline list. Timestamps match video exactly.

    Segments whose type is in skip_types produce no SRT entry (text is on
    static images, not subtitles). Segments with empty subtitle_en are also
    skipped.

    Consolidates timeline.build_srt_from_timeline,
    quest.build_srt_from_timeline_quest.
    """
    if skip_types is None:
        skip_types = {"listen_en", "listen_zh", "practice",
                      "practice_intro", "vocab", "quiz",
                      "dialogue_slow"}

    srt_lines = []
    idx = 1
    current_time = 0.0

    for seg in timeline:
        dur = seg["duration"]
        start = current_time
        end = start + dur

        text_en = seg.get("subtitle_en", "")
        text_zh = seg.get("subtitle_zh", "")
        seg_type = seg.get("type", "")

        if seg_type in skip_types:
            current_time = end + gap
            continue

        if not text_en:
            current_time = end + gap
            continue

        audio_dur = seg.get("audio_dur", dur)
        srt_end = start + audio_dur

        srt_lines.append(str(idx))
        srt_lines.append(f"{_format_srt_time(start)} --> {_format_srt_time(srt_end)}")
        srt_lines.append(text_en)
        if text_zh:
            srt_lines.append(text_zh)
        srt_lines.append("")
        idx += 1
        current_time = end + gap

    return "\n".join(srt_lines)
