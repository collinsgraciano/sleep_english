"""BGM 版权音乐混合（移植自 yt_aduio_book_one_to_all_v2/pipeline/bgm.py）。

随机连串版权音乐拼接 + 三种 ducking 混音模式：
- amix: 简单叠加（动态音量包络 + 频谱空隙塑形 + 高通滤波）
- sidechain: ffmpeg 侧链压缩（旁白说话时 BGM 自动压低、静默时升高；
  跳过 dyn_vol/spec_shape/高通，保留 BGM 完整频率指纹供 Content ID 匹配）
- sidechain_adaptive: 侧链阈值随旁白 RMS 自适应（BGM/旁白比例恒定）

与参考项目差异：本模块面向成片视频（mp4 → mp4），
视频流 -c:v copy 零重编码，仅音频重编码。
"""

from __future__ import annotations

import gc
import glob
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
from functools import lru_cache

import numpy as np
from pydub import AudioSegment
from scipy.signal import butter, sosfilt, stft, istft


# ============================================================================
# 缓存 BGM 源文件以降低长批处理中重复解码开销
# ============================================================================
@lru_cache(maxsize=4)
def load_music_segment_cached(music_path: str) -> AudioSegment:
    """缓存少量 BGM 源文件，减少重复解码开销（每首约 3-5MB 常驻内存）。"""
    return AudioSegment.from_file(music_path)


# ============================================================================
# 音频分析
# ============================================================================
def analyze_audio(audio_segment: AudioSegment) -> dict:
    duration_ms = len(audio_segment)
    peak_dbfs = audio_segment.max_dBFS

    # 用 500ms 窗口分段，只统计有声音的部分算 RMS
    # pydub 的 dBFS 包含静音段和呼吸声，拉低 RMS 导致 sidechain threshold 偏低
    chunk_size_ms = 500
    chunks = [
        audio_segment[i : i + chunk_size_ms]
        for i in range(0, duration_ms, chunk_size_ms)
        if i + chunk_size_ms <= duration_ms
    ]

    chunk_levels = []
    active_linear_sum = 0.0
    active_count = 0
    for chunk in chunks:
        try:
            level = chunk.dBFS
            if level > -60:
                chunk_levels.append(level)
            if level > -50:
                # 累积有效片段的线性功率，用于计算"有声音部分"的真实 RMS
                active_linear_sum += 10 ** (level / 10.0)
                active_count += 1
        except Exception:
            pass

    if active_count > 0:
        # 有效片段的线性平均 → 转回 dB，排除静音段对 RMS 的拉低效应
        rms_dbfs = 10 * math.log10(active_linear_sum / active_count)
    else:
        rms_dbfs = audio_segment.dBFS

    dynamic_range_db = (max(chunk_levels) - min(chunk_levels)) if len(chunk_levels) >= 2 else 0
    return {
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
        "dynamic_range_db": dynamic_range_db,
        "duration_ms": duration_ms,
        "sample_rate": audio_segment.frame_rate,
        "channels": audio_segment.channels,
    }


def compute_volume_envelope(audio_segment: AudioSegment, window_ms: int = 200):
    duration_ms = len(audio_segment)
    envelope = []
    for i in range(0, duration_ms, window_ms):
        chunk = audio_segment[i : i + window_ms]
        if len(chunk) < 50:
            envelope.append(envelope[-1] if envelope else -60)
            continue
        try:
            level = max(chunk.dBFS, -60)
            envelope.append(level)
        except Exception:
            envelope.append(-60)
    return np.array(envelope), window_ms


def analyze_spectral_gaps(audio_segment: AudioSegment, n_bands: int = 8):
    sample_rate = audio_segment.frame_rate

    # 超长音频（>10 分钟）仅分析前 5 分钟，避免全局 STFT 内存爆炸
    _MAX_ANALYSIS_MS = 5 * 60 * 1000
    if len(audio_segment) > _MAX_ANALYSIS_MS:
        audio_segment = audio_segment[:_MAX_ANALYSIS_MS]

    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)
    if audio_segment.channels > 1:
        samples = samples.reshape((-1, audio_segment.channels)).mean(axis=1)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1)
    samples = samples / max_val
    nperseg = min(4096, len(samples))
    freqs, times, Zxx = stft(samples, fs=sample_rate, nperseg=nperseg)
    power = np.abs(Zxx) ** 2

    nyquist = sample_rate / 2
    max_freq = min(nyquist, 16000)
    band_edges = np.logspace(np.log10(150), np.log10(max_freq), n_bands + 1)

    band_energies = []
    for i in range(n_bands):
        mask = (freqs >= band_edges[i]) & (freqs < band_edges[i + 1])
        band_energies.append(power[mask].mean() if mask.any() else 1e-10)

    band_energies_db = 10 * np.log10(np.array(band_energies) + 1e-10)
    max_energy_db = band_energies_db.max()
    relative_db = band_energies_db - max_energy_db
    band_gains = np.clip(-relative_db * 0.3, 0, 6)
    return band_gains, band_edges


# ============================================================================
# 信号处理
# ============================================================================
def apply_highpass_filter(audio_segment: AudioSegment, cutoff_freq: float = 150, order: int = 4) -> AudioSegment:
    sample_rate = audio_segment.frame_rate
    channels = audio_segment.channels
    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)
    if channels > 1:
        samples = samples.reshape((-1, channels))

    nyquist = sample_rate / 2.0
    sos = butter(order, min(cutoff_freq / nyquist, 0.99), btype="high", output="sos")

    if channels > 1:
        filtered = np.zeros_like(samples)
        for ch in range(channels):
            filtered[:, ch] = sosfilt(sos, samples[:, ch])
        filtered = filtered.flatten()
    else:
        filtered = sosfilt(sos, samples)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    filtered = np.clip(filtered, -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=filtered.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )


def _shape_single_channel(samples, sample_rate, band_gains, band_edges):
    nperseg = min(4096, len(samples))
    freqs, times, Zxx = stft(samples, fs=sample_rate, nperseg=nperseg)

    gain_curve = np.ones(len(freqs))
    for i in range(len(band_gains)):
        mask = (freqs >= band_edges[i]) & (freqs < band_edges[i + 1])
        gain_curve[mask] = 10 ** (band_gains[i] / 20.0)

    Zxx_shaped = Zxx * gain_curve[:, np.newaxis]
    _, result = istft(Zxx_shaped, fs=sample_rate, nperseg=nperseg)

    if len(result) > len(samples):
        result = result[: len(samples)]
    elif len(result) < len(samples):
        result = np.pad(result, (0, len(samples) - len(result)))
    return result


def apply_spectral_shaping(audio_segment: AudioSegment, band_gains, band_edges) -> AudioSegment:
    sample_rate = audio_segment.frame_rate
    channels = audio_segment.channels
    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64)

    if channels > 1:
        samples = samples.reshape((-1, channels))
        result_channels = [
            _shape_single_channel(samples[:, ch], sample_rate, band_gains, band_edges)
            for ch in range(channels)
        ]
        result = np.column_stack(result_channels).flatten()
    else:
        result = _shape_single_channel(samples, sample_rate, band_gains, band_edges)

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    result = np.clip(result, -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=result.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=sample_rate,
        channels=channels,
    )


def apply_dynamic_volume(audio_segment: AudioSegment, volume_envelope, window_ms: int,
                         vol_offset_db: float = -25, min_vol_db: float = -40) -> AudioSegment:
    duration_ms = len(audio_segment)
    envelope_median = np.median(volume_envelope)

    chunks = []
    for i, env_level in enumerate(volume_envelope):
        start_ms = i * window_ms
        end_ms = min(start_ms + window_ms, duration_ms)
        if start_ms >= duration_ms:
            break

        chunk = audio_segment[start_ms:end_ms]
        if len(chunk) < 10:
            continue

        deviation = env_level - envelope_median
        dynamic_adjust = np.clip(deviation * 0.4, -6, 6)
        target_volume = max(env_level + vol_offset_db + dynamic_adjust, min_vol_db)

        try:
            gain = np.clip(target_volume - chunk.dBFS, -40, 10)
            chunk = chunk.apply_gain(gain)
        except Exception:
            pass
        chunks.append(chunk)

    if not chunks:
        return audio_segment

    # 一次性合并基于底层内存序列，无损杜绝 O(N²) OOM 溢出及其引发的极长计算耗时
    raw_data = b"".join([c.raw_data for c in chunks])
    result = audio_segment._spawn(raw_data)

    if len(result) > duration_ms:
        result = result[:duration_ms]
    elif len(result) < duration_ms:
        result += AudioSegment.silent(
            duration=duration_ms - len(result),
            frame_rate=audio_segment.frame_rate,
        )
    return result


def apply_stereo_offset(audio_segment: AudioSegment, offset: float = 0.3) -> AudioSegment:
    if audio_segment.channels < 2:
        audio_segment = audio_segment.set_channels(2)

    samples = np.array(audio_segment.get_array_of_samples(), dtype=np.float64).reshape((-1, 2))
    left_gain = (1.0 - offset * 0.5) if offset > 0 else 1.0
    right_gain = 1.0 if offset > 0 else (1.0 + offset * 0.5)

    samples[:, 0] *= left_gain
    samples[:, 1] *= right_gain

    max_val = 2 ** (audio_segment.sample_width * 8 - 1) - 1
    result = np.clip(samples.flatten(), -max_val, max_val).astype(
        np.int16 if audio_segment.sample_width == 2 else np.int32,
    )

    return AudioSegment(
        data=result.tobytes(),
        sample_width=audio_segment.sample_width,
        frame_rate=audio_segment.frame_rate,
        channels=2,
    )


# ============================================================================
# 音乐文件检索
# ============================================================================
def get_all_music_files(music_folder: str) -> list[str]:
    supported_extensions = ("*.mp3", "*.wav", "*.flac", "*.ogg", "*.m4a", "*.aac", "*.wma")
    music_files = []
    for ext in supported_extensions:
        music_files.extend(glob.glob(os.path.join(music_folder, ext)))
        music_files.extend(glob.glob(os.path.join(music_folder, ext.upper())))
    music_files = list(set(music_files))
    if not music_files:
        raise FileNotFoundError(f"未找到可选的音乐文件: {music_folder}")
    return music_files


# ============================================================================
# BGM 准备（随机连串拼接）
# ============================================================================
def prepare_copyright_music(
    music_files: list[str],
    target_duration_ms: int,
    original_audio: AudioSegment,
    original_analysis: dict,
    vol_offset_db: float,
    hp_freq: int,
    fade_ms: int,
    min_vol_db: float,
    dyn_vol: bool,
    spec_shape: bool,
    st_offset: float,
) -> AudioSegment:
    print("  [BGM] 开启随机连串版权音乐模式")

    # 交叉淡化时长归一：0/负数/None 一律当「不做交叉淡化」。
    # 必须显式判 >0 再调用 pydub 的 fade_in/fade_out：pydub 的 fade() 走
    # `if duration:` 分支，duration=0 时落到 else 去算 `end - start`，而
    # fade_out 只传了 duration+end（start=None）→
    # TypeError: unsupported operand type(s) for -: 'int' and 'NoneType'，
    # 整次混音直接失败。真实触发场景：配置「交叉淡入淡出 ms」= 0。
    fade_ms = max(0, int(fade_ms or 0))
    if fade_ms <= 0:
        print("  [BGM] 交叉淡入淡出已关闭（0ms）")

    # 全局分析原声频谱间隙
    global_bg, global_be = None, None
    if spec_shape:
        print("  [BGM] 全局频谱空隙分析与嵌入检测")
        global_bg, global_be = analyze_spectral_gaps(original_audio)

    # 随机打乱音乐库
    shuffled_files = list(music_files)
    random.shuffle(shuffled_files)

    target_seconds = target_duration_ms // 1000
    print(f"  [BGM] 随机拼接池大小: {len(shuffled_files)} 首 | 目标: {target_seconds}s")

    # 收集分段到列表，最终用 raw_data 一次性 O(N) 拼接
    # （旧实现 looped += segment 每轮复制全部已积累数据 → O(N²)）
    segments: list[AudioSegment] = []
    accumulated_ms = 0
    music_idx = 0
    ref_frame_rate = None
    ref_channels = None
    ref_sample_width = None
    _t0 = time.time()

    while accumulated_ms < target_duration_ms:
        music_path = shuffled_files[music_idx % len(shuffled_files)]
        music_idx += 1

        segment = load_music_segment_cached(music_path)
        segment_duration = len(segment)

        # 首次加载时记录参考格式，后续统一归一化以保证 raw_data 拼接安全
        if ref_frame_rate is None:
            ref_frame_rate = segment.frame_rate
            ref_channels = segment.channels
            ref_sample_width = segment.sample_width
        elif (
            segment.frame_rate != ref_frame_rate
            or segment.channels != ref_channels
            or segment.sample_width != ref_sample_width
        ):
            segment = (
                segment
                .set_frame_rate(ref_frame_rate)
                .set_channels(ref_channels)
                .set_sample_width(ref_sample_width)
            )

        if hp_freq > 0:
            segment = apply_highpass_filter(segment, cutoff_freq=hp_freq)
        if spec_shape and global_bg is not None:
            segment = apply_spectral_shaping(segment, global_bg, global_be)

        remaining = target_duration_ms - accumulated_ms

        # 先算淡出时长、判 >0 再调用（pydub fade_out(0) 会抛 TypeError，
        # 见本函数开头注释：fade_ms=0 或 remaining/片段极短时都会算出 0）
        if remaining < segment_duration:
            segment = segment[:remaining]
            fade_len = min(fade_ms, remaining // 4)
        else:
            fade_len = min(fade_ms, segment_duration // 4)
        if fade_len > 0:
            segment = segment.fade_out(fade_len)

        # 交叉淡入淡出：只对列表末段做 fade_out（等效于对已积累音频尾部 fade_out），
        # 避免每轮复制全量 O(N) 数据
        if segments and fade_ms > 0:
            afade = min(fade_ms, len(segment) // 4)
            if afade > 0:
                segment = segment.fade_in(afade)
                last = segments[-1]
                segments[-1] = last.fade_out(afade)

        segments.append(segment)
        accumulated_ms += len(segment)

        if accumulated_ms % (30 * 1000) < max(segment_duration, 1):
            elapsed = time.time() - _t0
            print(
                f"  [BGM] 拼接进度: {accumulated_ms // 1000}/{target_seconds}s "
                f"({accumulated_ms * 100 // max(target_duration_ms, 1)}%) | 已用 {music_idx} 首 | 耗时 {elapsed:.1f}s"
            )

    # O(N) 高效拼接：直接连接底层 raw_data，杜绝 O(N²) 反复拷贝
    raw_data = b"".join(s.raw_data for s in segments)
    looped = segments[0]._spawn(raw_data)
    looped = looped[:target_duration_ms]
    # 立即释放大对象，避免后处理阶段同时持有 segments + raw_data + looped → OOM
    del raw_data, segments
    gc.collect()

    print("  [BGM] 拼接完成，开始动态音量与淡入淡出后处理...")

    if dyn_vol:
        print("  [BGM] 全局动态音量包络跟踪")
        env, w_ms = compute_volume_envelope(original_audio)
        looped = apply_dynamic_volume(looped, env, w_ms, vol_offset_db, min_vol_db)
    else:
        target_volume = max(original_analysis["rms_dbfs"] + vol_offset_db, min_vol_db)
        looped = looped.apply_gain(target_volume - looped.dBFS)

    final_fade = min(fade_ms, target_duration_ms // 10)
    if final_fade > 100:
        looped = looped.fade_in(final_fade).fade_out(final_fade)

    if st_offset != 0.0:
        print(f"  [BGM] 立体声偏移: {st_offset:.1f}")
        looped = apply_stereo_offset(looped, offset=st_offset)

    print(f"  [BGM] 后处理完成，耗时 {time.time() - _t0:.1f}s")
    return looped


# ============================================================================
# 章节起始时间解析
# ============================================================================
def chapter_start_seconds(run_dir: str | os.PathLike, chapter: int) -> float:
    """从运行目录的 youtube_metadata.json 解析第 N 章的起始秒数。

    chapters 元素格式 "MM:SS Label" / "HH:MM:SS Label"，按数组顺序即 YouTube
    章节顺序（1=第一章=00:00）。chapter<=1、文件缺失、解析失败或越界时
    返回 0.0（BGM 是增强功能，回落从头混音不视为错误）。
    """
    try:
        chapter = int(chapter or 1)
    except (TypeError, ValueError):
        return 0.0
    if chapter <= 1:
        return 0.0
    meta_path = os.path.join(str(run_dir), "youtube_metadata.json")
    try:
        import json
        with open(meta_path, encoding="utf-8") as fh:
            chapters = json.load(fh).get("chapters") or []
    except (OSError, ValueError) as e:
        print(f"  [BGM] 起始章节解析失败（回退从头混音）: {meta_path} - {e}")
        return 0.0
    if not chapters or chapter > len(chapters):
        print(f"  [BGM] 起始章节 {chapter} 超出实际章节数 {len(chapters)}（回退从头混音）")
        return 0.0
    ts = str(chapters[chapter - 1]).split()[0] if str(chapters[chapter - 1]).strip() else ""
    parts = ts.split(":")
    if not all(p.isdigit() for p in parts) or len(parts) > 3:
        print(f"  [BGM] 起始章节 {chapter} 时间戳格式异常: {ts!r}（回退从头混音）")
        return 0.0
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + int(p)
    print(f"  [BGM] 起始章节: 第{chapter}章 @ {ts} ({seconds:.0f}s)")
    return float(seconds)


# ============================================================================
# 顶层混音入口（视频版）
# ============================================================================
def mix_bgm_into_video(
    video_path: str,
    output_path: str,
    music_dir: str,
    *,
    ducking_mode: str = "sidechain",
    bgm_base_gain_db: float = -15,
    volume_offset_db: float = -25,
    highpass_freq: int = 150,
    fade_duration_ms: int = 3000,
    min_volume_db: float = -40,
    dyn_vol: bool = True,
    spec_shape: bool = True,
    stereo_offset: float = 0.0,
    sc_threshold_db: float = -30,
    sc_threshold_offset_db: float = -5,
    sc_ratio: int = 8,
    sc_attack_ms: int = 5,
    sc_release_ms: int = 400,
    ffmpeg_timeout: int = 600,
    bgm_start_seconds: float = 0.0,
) -> bool:
    """把版权 BGM 混入视频音轨（视频流 copy，仅音频重编码）。失败返回 False 不抛异常。

    ffmpeg_timeout: 内部 ffmpeg 进程超时上限（秒）。
    bgm_start_seconds: BGM 起始秒数（通常来自运行目录章节时间戳），
    该点之前 BGM 静音。

    历史：曾有 intro_outro_seconds（默认 5）在旁白首/尾加静音并让视频 tpad 冻结帧
    延展同秒数 —— 那会让整片视频重编码（4K 上要多花 1.5-2 小时），已按需求删除：
    现在输出时长与原片严格一致，视频流恒为 copy（秒级完成）。
    """
    bgm_temp_path: str | None = None
    try:
        # 入口参数归一（0/负数/None = 不做交叉淡化）：下游 min(fade_duration_ms, …)
        # 与 pydub fade_* 都要求正整数，None 会在 min() 就炸、0 会在 pydub 内部炸
        fade_duration_ms = max(0, int(fade_duration_ms or 0))
        music_files = get_all_music_files(music_dir)
        print(f"  [BGM] 加载视频音轨: {os.path.basename(video_path)}")
        orig_audio = AudioSegment.from_file(video_path)
        analysis = analyze_audio(orig_audio)

        # 侧链模式：跳过动态音量、频谱塑形和高通滤波，保留 BGM 原始指纹
        is_sidechain = ducking_mode in ("sidechain", "sidechain_adaptive")
        if is_sidechain:
            dyn_vol = False
            spec_shape = False
            highpass_freq = 0  # 保留完整频率指纹供 Content ID 匹配
            effective_vol_offset = bgm_base_gain_db

            # sidechain_adaptive: 阈值随旁白 RMS 动态计算，保证不同内容 BGM/旁白比例恒定
            if ducking_mode == "sidechain_adaptive":
                sc_threshold_db = analysis["rms_dbfs"] + sc_threshold_offset_db
                print(
                    f"  [BGM] 自适应侧链模式: base_gain={bgm_base_gain_db}dB "
                    f"threshold=旁白RMS({analysis['rms_dbfs']:.1f}dB)+{sc_threshold_offset_db}dB="
                    f"{sc_threshold_db:.1f}dB ratio={sc_ratio}:1"
                )
            else:
                print(
                    f"  [BGM] 侧链压缩模式: base_gain={bgm_base_gain_db}dB "
                    f"threshold={sc_threshold_db}dB ratio={sc_ratio}:1"
                )
        else:
            effective_vol_offset = volume_offset_db

        # BGM 起始点防呆：起点贴近/超出片长时回退从头混音
        if bgm_start_seconds > 0 and bgm_start_seconds >= len(orig_audio) / 1000 - 1:
            print(f"  [BGM] 起始点 {bgm_start_seconds:.0f}s 贴近/超出片长，回退从头混音")
            bgm_start_seconds = 0.0

        # 已删除「首尾独立段」（原 intro_outro_seconds=5）：旁白不再加静音、视频不再
        # tpad 延展，因此视频流恒为 copy、输出时长与原片一致（见函数 docstring）。

        bgm_music = prepare_copyright_music(
            music_files,
            len(orig_audio),
            orig_audio,
            analysis,
            effective_vol_offset,
            highpass_freq,
            fade_duration_ms,
            min_volume_db,
            dyn_vol,
            spec_shape,
            stereo_offset,
        )

        # 格式对齐：采样率/声道/长度与旁白一致
        if orig_audio.frame_rate != bgm_music.frame_rate:
            bgm_music = bgm_music.set_frame_rate(orig_audio.frame_rate)
        if orig_audio.channels != bgm_music.channels:
            bgm_music = bgm_music.set_channels(orig_audio.channels)
        # BGM 起始点：前置静音把音乐右移（下方截齐块自然裁掉尾部超长部分），
        # 起点之前即天然无 BGM（章节时间戳与原片严格对齐）。
        lead_ms = int(bgm_start_seconds * 1000)
        if lead_ms > 0:
            fd_in = min(fade_duration_ms, len(bgm_music) // 4)
            if fd_in > 0:
                bgm_music = bgm_music.fade_in(fd_in)
            bgm_music = AudioSegment.silent(
                duration=lead_ms, frame_rate=bgm_music.frame_rate,
            ) + bgm_music
        if len(bgm_music) > len(orig_audio):
            bgm_music = bgm_music[: len(orig_audio)]
        elif len(bgm_music) < len(orig_audio):
            bgm_music += AudioSegment.silent(
                duration=len(orig_audio) - len(bgm_music),
                frame_rate=orig_audio.frame_rate,
            )
        if lead_ms > 0:
            # prepend+截齐会裁掉 prepare 阶段的尾部淡出，在片尾补一次
            fd_out = min(fade_duration_ms, len(bgm_music) // 10)
            if fd_out > 100:
                bgm_music = bgm_music.fade_out(fd_out)

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # 内存优化：优先 ffmpeg 流式叠加（视频流 copy + 音频 filter_complex 一条命令）
        ok_mix = False
        if shutil.which("ffmpeg"):
            print("  [BGM] 混音叠加（ffmpeg 流式）...")
            ok_mix = _ffmpeg_overlay_video(
                video_path,
                bgm_music,
                output_path,
                ducking_mode=ducking_mode,
                sc_threshold_db=sc_threshold_db,
                sc_ratio=sc_ratio,
                sc_attack_ms=sc_attack_ms,
                sc_release_ms=sc_release_ms,
                timeout=ffmpeg_timeout,
            )
            if ok_mix:
                del orig_audio, bgm_music
                gc.collect()
                load_music_segment_cached.cache_clear()
                size_mb = os.path.getsize(output_path) / (1024 * 1024)
                print(f"  [BGM] 混音已保存: {os.path.basename(output_path)} ({size_mb:.1f}MB)")
                return True
            print("  [BGM] ffmpeg 叠加失败，回退到 pydub overlay")

        # 回退方案：pydub overlay（内存占用较高）+ remux
        # sidechain 模式下 pydub 无法做侧链压缩，补偿增益到安全电平
        if is_sidechain:
            compensate_db = volume_offset_db - bgm_base_gain_db
            print(
                f"  [BGM] pydub 回退：侧链压缩不可用，BGM 增益补偿 {bgm_base_gain_db}dB → {volume_offset_db}dB"
            )
            bgm_music = bgm_music.apply_gain(compensate_db)
        print("  [BGM] 混音叠加（pydub）...")
        mixed = orig_audio.overlay(bgm_music)
        del orig_audio, bgm_music
        gc.collect()
        load_music_segment_cached.cache_clear()

        ok_mix = _remux_video_audio(video_path, mixed, output_path,
                                    timeout=ffmpeg_timeout)
        del mixed
        gc.collect()
        if ok_mix:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"  [BGM] 混音已保存: {os.path.basename(output_path)} ({size_mb:.1f}MB)")
        return ok_mix
    except Exception as e:
        print(f"  [BGM] 音频混入失败: {e}")
        return False


def _cleanup(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _remux_video_audio(video_path: str, mixed_audio: AudioSegment, output_path: str,
                       timeout: int = 600) -> bool:
    """pydub 回退路径：把混音后的音频与原视频合成为新 mp4。

    视频流恒为 copy（音频长度 = 原音频长度，无需 tpad 延展/重编码）。
    """
    mixed_temp_path: str | None = None
    try:
        mixed_temp = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
            dir=os.path.dirname(output_path) or ".",
        )
        mixed_temp_path = mixed_temp.name
        mixed_temp.close()
        mixed_audio.export(mixed_temp_path, format="wav")
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", mixed_temp_path,
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            output_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            stderr = r.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            print("  [BGM] remux 失败:", stderr[-500:])
            return False
        return True
    except subprocess.TimeoutExpired:
        print("  [BGM] remux 超时（>600s）")
        return False
    except Exception as e:
        print(f"  [BGM] remux 异常: {e}")
        return False
    finally:
        _cleanup(mixed_temp_path)


# ============================================================================
# ffmpeg 流式叠加（视频版：视频流恒为 copy，零重编码）
# ============================================================================
def _ffmpeg_overlay_video(
    video_path: str,
    bgm_audio: AudioSegment,
    output_path: str,
    *,
    ducking_mode: str = "amix",
    sc_threshold_db: float = -30,
    sc_ratio: int = 8,
    sc_attack_ms: int = 5,
    sc_release_ms: int = 400,
    timeout: int = 600,
) -> bool:
    """使用 ffmpeg 从磁盘叠加 BGM 到视频音轨。

    ducking_mode:
      - "amix": 简单叠加（amix + volume=2.0）
      - "sidechain" / "sidechain_adaptive": 侧链压缩（旁白说话时BGM自动压低，静默时BGM升高）

    视频流始终 `-c:v copy`（历史上的首尾 padding/tpad 延展已删除，见 mix_bgm_into_video）。
    侧链 key 固定取视频自带音轨 `[0:a]`。
    """
    bgm_temp_path: str | None = None
    try:
        # 写 BGM 到临时 WAV 文件（WAV 无编解码开销，速度最快）
        bgm_temp = tempfile.NamedTemporaryFile(
            suffix=".wav",
            delete=False,
            dir=os.path.dirname(output_path) or ".",
        )
        bgm_temp_path = bgm_temp.name
        bgm_temp.close()
        bgm_audio.export(bgm_temp_path, format="wav")

        if ducking_mode in ("sidechain", "sidechain_adaptive"):
            # 侧链压缩：旁白作为 sidechain key，BGM 被压缩
            # 旁白 RMS 超过 threshold 时 → BGM 被压低 ratio:1
            # 旁白静默时 → BGM 以原始增益播放（Content ID 可检测）
            #
            # ffmpeg sidechaincompress 参数单位：
            #   threshold: 线性振幅 (0.000976563 ~ 1)，需从 dB 转换
            #   attack/release: 毫秒 (0.01 ~ 2000/9000)
            sc_threshold_linear = 10 ** (sc_threshold_db / 20.0)
            # ffmpeg sidechaincompress 的 threshold 是线性振幅，必须在 (0, 1]；
            # 越界（例如误传 +1dB → 1.122）会让 ffmpeg 直接报
            # "Numerical result out of range" → 混音静默退到 pydub（慢、吃内存，
            # 长片有 OOM 风险），所以这里夹紧并告警，保证快的 ffmpeg 路径可用。
            if not (0.0 < sc_threshold_linear <= 1.0):
                clamped = min(1.0, max(0.000976563, sc_threshold_linear))
                print(f"  [BGM] threshold {sc_threshold_db:+.1f}dB → {sc_threshold_linear:.4f} "
                      f"越界，夹紧为 {clamped:.4f}（ffmpeg 要求线性 0~1）")
                sc_threshold_linear = clamped
            narr_src = "[0:a]"
            filter_complex = (
                f"{narr_src}asplit=2[narr][sc_key];"
                f"[1:a][sc_key]sidechaincompress="
                f"threshold={sc_threshold_linear}:ratio={sc_ratio}:"
                f"attack={sc_attack_ms}:release={sc_release_ms}[bgm_ducked];"
                f"[narr][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=0,volume=2.0[a]"
            )
            print(
                f"  [BGM] 侧链压缩: threshold={sc_threshold_db:.0f}dB({sc_threshold_linear:.4f}) "
                f"ratio={sc_ratio}:1 attack={sc_attack_ms}ms release={sc_release_ms}ms"
            )
        else:
            # amix=inputs=2:duration=first → 输出长度匹配第一路输入
            # dropout_transition=0 → 无过渡淡出
            # volume=2.0 → 抵消 amix 默认的除以2归一化，等效于直接相加+裁剪
            filter_complex = (
                "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0,volume=2.0[a]"
            )

        cmd = ["ffmpeg", "-y", "-i", video_path, "-i", bgm_temp_path]
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "0:v:0",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
        ]
        cmd += [output_path]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            print("  [BGM] ffmpeg 叠加失败:", stderr[-500:])
            return False
        return True
    except subprocess.TimeoutExpired:
        print("  [BGM] ffmpeg 叠加超时（>600s）")
        return False
    except FileNotFoundError:
        print("  [BGM] ffmpeg 未安装或不在 PATH 中")
        return False
    except Exception as e:
        print(f"  [BGM] ffmpeg 叠加异常: {e}")
        return False
    finally:
        _cleanup(bgm_temp_path)
