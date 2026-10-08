"""sleep 模式合成：组级块构建 + concat 三段式（防 AAC priming 漂移）。

每块 = 一张静态卡片（-loop 1）+ 该组音频链（朗读段 + anullsrc 静音气口，
filter_complex 内统一 aresample/立体声后 concat 单编码 aac）→ 200+ 个均匀
块（libx264/yuv420p/25fps/aac 44100 立体声）走 media_utils.concat_segments。
xfade_sec>0 时相邻块先交叉溶解合并为纯视频流（仅画面、音频仍走网格拼接），
失败自动回退硬切。
文字全部预渲染进卡片 → 无字幕烧录步骤；末尾 apply_final_loudnorm 原地归一。

native_4k=True 时卡片按 3840x2160 原生渲染、块直接编码 4K（文字像素级
清晰，跳过 Step 6 lanczos 放大）；False 输出与历史版本逐字节一致（720p）。
"""
import os
import shutil
import subprocess
from pathlib import Path

from media_utils import (TARGET_H, TARGET_W, apply_final_loudnorm,
                         concat_segments, extra_4k_x264_params, get_duration,
                         merge_blocks_xfade, safe_filename)
from sleep.audio_sleep import COMBO_GAP
from sleep.sleep_cards import (render_intro_card, render_outro_card,
                               render_pair_card)

BLOCK_TIMEOUT = 600


def _output_vf(out_w: int, out_h: int) -> str:
    """按输出分辨率构造 scale/pad（720p 时与 media_utils.VF_NORM 一致）。"""
    return (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2")


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=BLOCK_TIMEOUT)


def _logo_overlay(logo_path: str, position: str, size: int, opacity: int,
                  out_h: int, pos_x: float = 92.0, pos_y: float = 6.0
                  ) -> tuple[list[str], str]:
    """构造频道 Logo 叠加的 ffmpeg 输入段与 filter_complex 视频段。

    logo 作为额外输入（单帧 PNG，overlay 默认 eof_action=repeat 持续显示，
    调用方音频输入索引需顺延 1）；尺寸/边距按 px@720p 定义、随输出分辨率
    等比缩放；透明度经 colorchannelmixer 均匀调 alpha。
    返回 (输入参数段, 视频滤镜段：[bg]+[lg]→overlay→[vout])。
    """
    k = out_h / TARGET_H
    s = max(8, round(size * k))
    m = max(4, round(size * 0.35 * k))
    if position == "top_left":
        xy = (f"{m}", f"{m}")
    elif position == "bottom_left":
        xy = (f"{m}", f"main_h-overlay_h-{m}")
    elif position == "bottom_right":
        xy = (f"main_w-overlay_w-{m}", f"main_h-overlay_h-{m}")
    elif position == "custom":
        # 任意位置：Logo 左上角在可移动空间（main-overlay）中的百分比坐标
        fx = min(100.0, max(0.0, float(pos_x))) / 100.0
        fy = min(100.0, max(0.0, float(pos_y))) / 100.0
        xy = (f"(main_w-overlay_w)*{fx:.4f}", f"(main_h-overlay_h)*{fy:.4f}")
    else:  # top_right（默认）
        xy = (f"main_w-overlay_w-{m}", f"{m}")
    alpha = min(1.0, max(0.1, float(opacity or 90) / 100.0))
    inputs = ["-i", str(logo_path)]
    chain = (f"[1:v]scale={s}:-1,format=rgba,"
             f"colorchannelmixer=aa={alpha:.2f}[lg];"
             f"[bg][lg]overlay={xy[0]}:{xy[1]}[vout]")
    return inputs, chain


def _build_audio_chain(block_segs: list[dict], audio_paths: dict,
                       lead: float = 0.0, intro_db: float = 0.0,
                       outro_db: float = 0.0,
                       audio_start: int = 1) -> tuple[str, list[str]]:
    """块内音频 filter_complex：朗读文件 + 静音气口统一 44100 立体声 concat。

    返回 (filter_complex 字符串, ffmpeg 输入参数列表)。段音频按段类型查：
    pair → pair_paths[(pair, step)]；intro/outro → audio_paths["intro"/"outro"]；
    无音频段（gap）生成等长静音（anullsrc 须以 -f lavfi 输入，否则被当作
    文件名导致整块失败）。lead>0 时每组第一个步骤（序列编排后的首步）
    朗读前先补 lead 秒静音（卡片提前量：画面先出现、稍后出声；该段
    timeline duration 已含 lead）。

    combo（AB 连贯）段内联拼接：a_m + COMBO_GAP 静音 + b_f 直接进 concat 链
    —— 不再有预编码 combo 中间 mp3（省一次有损重编码 + 每组一个子进程），
    段时长即 a_m+COMBO_GAP+b_f（audio_sleep 计算写入 timeline）。
    """
    inputs: list[str] = []
    chains: list[str] = []
    concat_refs: list[str] = []
    # 卡片图占用输入 0（无音频流）；logo 存在时占输入 1，音频从 audio_start 起
    state = {"n": audio_start}
    first_pair = True  # 每块 = 一组（或 intro/outro），首 pair 段承担卡片提前量

    def _push_silence(sec: float) -> None:
        inputs.extend(["-f", "lavfi", "-i",
                       f"anullsrc=r=44100:cl=stereo:d={sec:.3f}"])
        concat_refs.append(f"[{state['n']}:a]")
        state["n"] += 1

    def _push_file(path: str, db: float = 0.0) -> None:
        inputs.extend(["-i", path])
        n = state["n"]
        vol = f"volume={db:.2f}dB," if db else ""
        chains.append(f"[{n}:a]{vol}aresample=44100,aformat=channel_layouts=stereo[a{n}]")
        concat_refs.append(f"[a{n}]")
        state["n"] += 1

    for seg in block_segs:
        seg_type = seg.get("type", "")
        if seg_type == "pair":
            key = str(seg.get("pair", 0)).zfill(4)
            step = seg.get("step", "")
            pmap = audio_paths.get("pair_paths", {}).get(key, {})
            if step == "combo":
                entries = [(pmap.get("a_m", ""), 0.0),
                           (pmap.get("b_f", ""), COMBO_GAP)]
            else:
                entries = [(pmap.get(step, ""), 0.0)]
            if first_pair and lead > 0:
                _push_silence(lead)
            first_pair = False
            if all(p and os.path.exists(p) for p, _ in entries):
                for path, pre_sil in entries:
                    if pre_sil > 0:
                        _push_silence(pre_sil)
                    _push_file(path)
            else:
                _push_silence(max(0.0, float(seg.get("duration", 0))))
        elif seg_type in ("intro", "outro"):
            # 片头/片尾音量偏移（dB）：绑定视频与默认 TTS 播报统一生效；
            # 0dB 不注入 volume 滤镜（默认输出与历史逐比特一致）
            db = intro_db if seg_type == "intro" else outro_db
            path = audio_paths.get(seg_type, "")
            if path and os.path.exists(path):
                _push_file(path, db=db)
            else:
                _push_silence(max(0.0, float(seg.get("duration", 0))))
        else:
            _push_silence(max(0.0, float(seg.get("duration", 0))))
    fg = ";".join(chains) + ";" + "".join(concat_refs) + f"concat=n={len(concat_refs)}:v=0:a=1[aout]"
    return fg, inputs


def _build_video_block(intro_video: str, block_segs: list[dict], out_path: str,
                       vf: str, volume_db: float = 0.0,
                       logo: tuple[list[str], str] | None = None,
                       x264_params: list[str] | None = None) -> None:
    """绑定片头/片尾视频时的 intro/outro 块：整段转码统一规格（音画随视频自带）。

    volume_db≠0 时对视频自带音轨做音量偏移。logo=(输入段, 视频滤镜段) 时
    滤镜全部并入 filter_complex（-af 不能与 -filter_complex 共存，
    volume 一并写入 graph），不再走 -vf。
    x264_params：原生 4K 时由调用方传入（如 rc-lookahead=10），非 4K 传 None
    = 与历史命令逐字节一致。"""
    block_dur = round(sum(float(seg.get("duration", 0.0)) for seg in block_segs), 3)
    lg_inputs, lg_chain = logo or ([], "")
    extra_v = list(x264_params or [])
    if lg_inputs:
        parts = [f"[0:v]{vf}[bg]", lg_chain]
        audio_map = "0:a:0"
        if volume_db:
            parts.append(f"[0:a]volume={volume_db:.2f}dB[aout]")
            audio_map = "[aout]"
        cmd = ["ffmpeg", "-y", "-i", intro_video, *lg_inputs,
               "-filter_complex", ";".join(parts),
               "-map", "[vout]", "-map", audio_map,
               "-t", f"{block_dur:.3f}",
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", *extra_v,
               "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
               out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", intro_video,
               "-vf", vf]
        if volume_db:
            cmd += ["-af", f"volume={volume_db:.2f}dB"]
        cmd += ["-t", f"{block_dur:.3f}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25", *extra_v,
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                out_path]
    r = _run_ffmpeg(cmd)
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError(f"FFmpeg intro video block failed: {(r.stderr or '')[-300:]}")


def _build_block(card_path: str, block_segs: list[dict], audio_paths: dict,
                 out_path: str, vf: str, lead: float = 0.0,
                 intro_db: float = 0.0, outro_db: float = 0.0,
                 logo: tuple[list[str], str] | None = None,
                 x264_params: list[str] | None = None) -> None:
    """构建一个块 mp4（静态卡 + 音频链）。

    logo=(输入段, 视频滤镜段) 时视频流并入 filter_complex：
    [0:v]→vf→[bg] 叠 logo→[vout]，不再走 -vf；音频输入索引顺延 1。
    x264_params：原生 4K 时由调用方传入（4K 的 rc-lookahead 缓冲是最大单块
    内存），非 4K 传 None = 与历史命令逐字节一致。"""
    block_dur = round(sum(float(seg.get("duration", 0.0)) for seg in block_segs), 3)
    lg_inputs, lg_chain = logo or ([], "")
    fg, inputs = _build_audio_chain(block_segs, audio_paths, lead=lead,
                                    intro_db=intro_db, outro_db=outro_db,
                                    audio_start=2 if lg_inputs else 1)
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", card_path]
    cmd += lg_inputs  # logo 输入占用索引 1（无音频流）
    cmd += inputs  # 已含 "-i <file>" 与 "-f lavfi -i anullsrc=..." 完整参数片段
    if lg_inputs:
        # 视频流并入 graph：vf 先产出 [bg]（不再走 -vf），再叠 logo
        cmd += ["-filter_complex", f"[0:v]{vf}[bg];" + lg_chain + ";" + fg,
                "-map", "[vout]"]
    else:
        cmd += ["-filter_complex", fg,
                "-map", "0:v:0", "-vf", vf]
    cmd += ["-map", "[aout]",
            "-t", f"{block_dur:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
            *list(x264_params or []),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            out_path]
    r = _run_ffmpeg(cmd)
    if r.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise RuntimeError(f"FFmpeg block failed ({Path(out_path).name}): "
                           f"{(r.stderr or '')[-300:]}")


def _ensure_cards(timeline: list[dict], script: dict, cards_dir: Path,
                  theme: dict, channel_name: str, badge_text: str,
                  outro_text: str, num_pairs: int,
                  w: int = TARGET_W, h: int = TARGET_H) -> dict:
    """文件级续传渲染卡片。返回 {card_key: path}，key: intro/0001../outro。

    4K 卡片文件名带 _4k 后缀，避免误复用历史 720p 卡片（跨尺寸续传错尺寸）。
    """
    cards_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_4k" if w != TARGET_W else ""
    cards: dict[str, str] = {}
    if any(seg.get("type") == "intro" for seg in timeline):
        intro_path = str(cards_dir / f"intro_card{suffix}.png")
        if not os.path.exists(intro_path):
            render_intro_card(theme, intro_path, channel_name, badge_text, w=w, h=h)
        cards["intro"] = intro_path
    dialogue = script.get("dialogue", [])
    rows_a, rows_b = dialogue[0::2], dialogue[1::2]
    for i in range(1, num_pairs + 1):
        path = str(cards_dir / f"card_{i:04d}{suffix}.png")
        if not os.path.exists(path):
            a = rows_a[i - 1] if i <= len(rows_a) else {}
            b = rows_b[i - 1] if i <= len(rows_b) else {}
            render_pair_card(a, b, i, theme, path, channel_name, badge_text,
                             w=w, h=h)
        cards[str(i).zfill(4)] = path
    outro_path = str(cards_dir / f"outro_card{suffix}.png")
    if not os.path.exists(outro_path):
        render_outro_card(theme, outro_path, outro_text, channel_name, badge_text,
                          w=w, h=h)
    cards["outro"] = outro_path
    return cards


def compose_sleep(work_dir: str, timeline: list[dict], script: dict,
                  audio_results: dict, cards_dir: str, theme: dict,
                  channel_name: str = "English with me", badge_text: str = "EN",
                  outro_text: str = "", num_pairs: int = 0,
                  intro_video: str = "", outro_video: str = "",
                  intro_volume_db: float = 0.0, outro_volume_db: float = 0.0,
                  native_4k: bool = False,
                  card_lead: float = 0.0, xfade_sec: float = 0.0,
                  logo_path: str = "", logo_position: str = "top_right",
                  logo_size: int = 96, logo_opacity: int = 90,
                  logo_pos_x: float = 92.0, logo_pos_y: float = 6.0,
                  progress_cb=None, stop_check=None) -> str:
    """合成 sleep 成片。返回最终 mp4 路径（videos/{safe}.mp4）。

    native_4k=True：卡片原生 3840x2160 渲染 + 块编码 4K（成片即 4K，
    下游 Step 6 检测已 4K 自动硬链接跳过放大）。
    xfade_sec>0：相邻块边界（组间 + 片头/片尾衔接）画面交叉溶解过渡
    （0.2-2.0s；仅画面，音频不动；整片多 1-2 次视频重编码）。0=硬切。
    """
    out_w, out_h = (3840, 2160) if native_4k else (TARGET_W, TARGET_H)
    vf = _output_vf(out_w, out_h)
    # 原生 4K 时「块编码器」本身就是 4K 编码器：把 4K 编码参数（SLEEP_4K_X264_PARAMS，
    # 主要是收 rc-lookahead）交给它 —— 否则每个块都要吃满默认 lookahead 的 ~1.4GB。
    # 非原生（720p）传空列表，命令与历史逐字节一致。
    _x264 = extra_4k_x264_params() if native_4k else []
    if _x264:
        print(f"  [Sleep] 块编码参数(原生4K): {' '.join(_x264)}")
    # 频道 Logo 水印（全片叠加；文件缺失只跳过不报错）
    logo: tuple[list[str], str] | None = None
    if logo_path:
        if os.path.exists(logo_path):
            logo = _logo_overlay(logo_path, logo_position, logo_size,
                                 logo_opacity, out_h,
                                 pos_x=logo_pos_x, pos_y=logo_pos_y)
            print(f"  [Sleep] Logo overlay: {Path(logo_path).name} "
                  f"@{logo_position} size={logo_size} opacity={logo_opacity}%")
        else:
            print(f"  [Sleep] Logo file not found, skip overlay: {logo_path}")
    work = Path(work_dir)
    vid_dir = work / "videos"
    vid_dir.mkdir(parents=True, exist_ok=True)
    # 音量偏移落日志：便于在运行日志里确认「片头/片尾音量偏移(dB)」确实
    # 送到了合成阶段（末级两遍线性归一按整片恒定增益，偏移不会被抹平）
    if intro_volume_db:
        print(f"  [Sleep] 片头音量偏移: {intro_volume_db:+.2f} dB"
              + ("（作用对象：绑定片头库视频自带音轨）" if intro_video
                 else "（作用对象：默认片头卡片 TTS 播报）"))
    if outro_volume_db:
        print(f"  [Sleep] 片尾音量偏移: {outro_volume_db:+.2f} dB"
              + ("（作用对象：绑定片尾库视频自带音轨）" if outro_video
                 else "（作用对象：默认片尾卡片 TTS 播报）"))
    tmp_dir = work / "tmp_sleep_blocks"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    (tmp_dir / "blocks").mkdir(parents=True, exist_ok=True)

    def _cb(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)

    pairs_in_tl = [seg for seg in timeline if seg.get("type") == "pair"]
    max_pair = max((int(seg.get("pair", 0)) for seg in pairs_in_tl), default=0)
    _cb(2, f"Rendering cards (pairs={max_pair}, {out_w}x{out_h})...")
    cards = _ensure_cards(timeline, script, Path(cards_dir), theme,
                          channel_name, badge_text, outro_text, max_pair,
                          w=out_w, h=out_h)

    # --- 时间轴 → 块序列：intro | [5 pair + 5 gap]* | outro ---
    blocks: list[list[dict]] = []
    cur: list[dict] = []
    for seg in timeline:
        t = seg.get("type", "")
        if t in ("intro", "outro"):
            if cur:
                blocks.append(cur)
                cur = []
            blocks.append([seg])
        elif t == "pair":
            cur.append(seg)
        elif t == "gap":
            cur.append(seg)
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)

    block_paths: list[str] = []
    total = len(blocks)
    for bi, block_segs in enumerate(blocks):
        if stop_check and stop_check():
            raise RuntimeError("stopped")
        head = block_segs[0]
        t = head.get("type", "")
        out_path = str(tmp_dir / "blocks" / f"block_{bi:04d}.mp4")
        # 绑定片头/片尾视频：intro/outro 块整段转码该片（音画随视频自带 BGM/播报）
        is_intro_video = (t == "intro" and intro_video
                          and os.path.exists(intro_video))
        is_outro_video = (t == "outro" and outro_video
                          and os.path.exists(outro_video))
        if not (is_intro_video or is_outro_video):
            if t == "intro":
                card = cards["intro"]
            elif t == "outro":
                card = cards["outro"]
            else:
                card = cards[str(head.get("pair", 0)).zfill(4)]
        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 1000):
            try:
                if is_intro_video:
                    _build_video_block(intro_video, block_segs, out_path, vf,
                                       volume_db=intro_volume_db, logo=logo,
                                       x264_params=_x264)
                elif is_outro_video:
                    _build_video_block(outro_video, block_segs, out_path, vf,
                                       volume_db=outro_volume_db, logo=logo,
                                       x264_params=_x264)
                else:
                    _build_block(card, block_segs, audio_results, out_path, vf,
                                 lead=card_lead, intro_db=intro_volume_db,
                                 outro_db=outro_volume_db, logo=logo,
                                 x264_params=_x264)
            except RuntimeError as e:
                if str(e) == "stopped":
                    raise
                print(f"  [Sleep] Block {bi} failed ({e}), retry once...")
                if is_intro_video:
                    _build_video_block(intro_video, block_segs, out_path, vf,
                                       volume_db=intro_volume_db, logo=logo,
                                       x264_params=_x264)
                elif is_outro_video:
                    _build_video_block(outro_video, block_segs, out_path, vf,
                                       volume_db=outro_volume_db, logo=logo,
                                       x264_params=_x264)
                else:
                    _build_block(card, block_segs, audio_results, out_path, vf,
                                 lead=card_lead, intro_db=intro_volume_db,
                                 outro_db=outro_volume_db, logo=logo,
                                 x264_params=_x264)
        block_paths.append(out_path)
        if bi % 10 == 0 or bi == total - 1:
            _cb(int(2 + bi / total * 78),
                f"Block {bi + 1}/{total} ({t}, {head.get('pair', '')})".strip())

    merged_video = None
    if xfade_sec > 0 and len(block_paths) >= 2:
        if native_4k:
            print("  [Sleep] 4K 原生 + 交叉溶解：整片重编码耗时较长，请耐心等待")
        _cb(80, f"Crossfading {len(block_paths)} blocks ({xfade_sec:.2f}s)...")
        merged_video = merge_blocks_xfade(
            block_paths, str(tmp_dir / "xfade_merged.mp4"), xfade_sec)
        if merged_video is None:
            print("  [Sleep] 交叉溶解合并失败 — 回退硬切拼接")
    _cb(82, "Concatenating blocks...")
    no_sub = str(vid_dir / "final_no_sub.mp4")
    concat_segments(block_paths, no_sub, tmp_dir=str(tmp_dir),
                    video_source=merged_video)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    _cb(92, "Final loudnorm...")
    apply_final_loudnorm(no_sub, str(vid_dir))

    yt_title = script.get("youtube_title") or script.get("title") or "sleep_video"
    final_path = vid_dir / f"{safe_filename(yt_title, 'sleep_video')}.mp4"
    os.replace(no_sub, final_path)
    dur = get_duration(str(final_path))
    _cb(100, f"Sleep video done: {final_path.name} ({dur / 60:.1f} min)")
    return str(final_path)
