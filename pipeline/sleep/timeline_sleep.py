"""sleep 模式时间轴：intro → N×[序列朗读段 + 序列静音段] → outro，gap 恒 0.0。

段 dict 与 listening 时间轴同构（type/duration/subtitle_en/subtitle_zh +
sleep 专属键 pair/step），供 save_youtube_metadata 章节与 compose 块构建消费。
pair 段 duration = 音频实际时长（pad=0，气口全部由显式 gap 段承担），
保证时间轴累计时长 == 合成视频时长。

组内步骤序列（sleep_sequence 配置）：可自由增删/调序/重复 SEQUENCE_STEPS 中
的步骤，每步可带专属停顿（gap）；留空/解析失败回落默认步序（PAIR_STEPS +
三档全局停顿参数，与历史行为一致）。
"""
import json

from media_utils import build_srt

# 每组内的默认步骤顺序（与 audio_sleep 文件后缀一致）
PAIR_STEPS = ("a_m", "a_slow", "b_m", "b_slow", "combo")
# 序列编排可选步骤（b_f=B句女声常速，默认结构不用，供 sleep_sequence 独立成步）
SEQUENCE_STEPS = ("a_m", "a_slow", "b_m", "b_slow", "b_f", "combo")

# 序列步骤 gap 留空（null）时沿用全局停顿参数的映射
# （a_m/b_m/b_f=常速→gap_short；a_slow/b_slow=慢速→gap_long；combo→pair_gap）
_STEP_DEFAULT_GAP = {"a_m": "short", "b_m": "short", "b_f": "short",
                     "a_slow": "long", "b_slow": "long", "combo": "pair"}


def parse_sleep_sequence(raw) -> list[dict] | None:
    """解析 sleep_sequence 配置（JSON 数组 [{step, gap}]）。

    返回规范化步骤列表（gap=None 表示沿用全局停顿参数映射）；
    留空 / JSON 损坏 / 无有效步骤时返回 None（=默认结构）。
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        data = json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    steps: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        step = str(item.get("step", "")).strip()
        if step not in SEQUENCE_STEPS:
            continue
        gap = item.get("gap")
        if gap is None:
            gap_val = None
        else:
            try:
                gap_val = round(min(30.0, max(0.0, float(gap))), 3)
            except (TypeError, ValueError):
                gap_val = None
        steps.append({"step": step, "gap": gap_val})
    return steps or None


def sequence_signature(sequence: list[dict] | None) -> str:
    """序列规范化签名（meta.json 重建守卫用；默认结构恒为空串）。"""
    if not sequence:
        return ""
    return json.dumps(sequence, ensure_ascii=False, separators=(",", ":"))


def build_sleep_timeline(script: dict, audio: dict, num_pairs: int,
                         gap_short: float = 1.0, gap_long: float = 2.0,
                         pair_gap: float = 3.0, include_intro: bool = True,
                         include_outro: bool = True,
                         card_lead: float = 0.0,
                         sequence: list[dict] | None = None,
                         quantize_sec: float = 0.0,
                         grouping: str = "per_step",
                         tail_margin: float = 0.0) -> list[dict]:
    """由 prepare_sleep_audio 结果构建线性时间轴。

    quantize_sec>0：把每个块的总时长向上取整到该步长（帧长 `1/fps`）的整数倍
    （仅低帧率卡片编码需要，见 _align_block_durations）；0=保持各段精确时长。
    tail_margin>0：每组音频读完后再多留这么多秒静音才换画面（"严格换卡"保证）；
    可与 quantize_sec 同时使用 ⇒ 目标 `ceil((A+margin)/step)*step`。
    grouping：块划分方式（"per_step"/"per_pair"），影响对齐/补齐的单位与块数。

    sequence=None：默认步序 a_m → g_short → a_slow → g_long → b_m → g_short
    → b_slow → g_long → combo → pair_gap（与历史行为一致）。

    传入 parse_sleep_sequence 结果：按序列编排，每步后停顿取该步 gap
    （None 回落 _STEP_DEFAULT_GAP 映射的全局参数）；card_lead 加到每组
    第一个步骤的 duration（画面先于朗读出现，compose 在该段音频链前插
    等长静音）。

    include_intro=False 时不生成 intro 段（片头开关）；include_outro=False
    时不生成 outro 段（片尾开关，intro/outro 绑定守卫与时间轴重建见
    pipeline._step2/_step4）。
    """
    timeline: list[dict] = []
    if include_intro:
        intro_dur = float(audio.get("intro_dur", 0.0))
        timeline.append({"type": "intro", "duration": round(intro_dur, 3),
                         "subtitle_en": "", "subtitle_zh": "", "pair": 0, "step": ""})

    pair_durs = audio.get("pair_durs", {})
    dialogue = script.get("dialogue", [])
    rows_a, rows_b = dialogue[0::2], dialogue[1::2]
    total = min(num_pairs, len(pair_durs), len(rows_a), len(rows_b))

    def _gap_after(step: str, gap_val) -> float:
        if gap_val is not None:
            return float(gap_val)
        key = _STEP_DEFAULT_GAP.get(step, "short")
        return float({"short": gap_short, "long": gap_long, "pair": pair_gap}[key])

    steps = sequence if sequence else [{"step": s, "gap": None} for s in PAIR_STEPS]

    for i in range(1, total + 1):
        key = str(i).zfill(4)
        durs = pair_durs[key]
        text_a = rows_a[i - 1].get("text", "")
        text_b = rows_b[i - 1].get("text", "")
        zh_a = rows_a[i - 1].get("zh", "")
        zh_b = rows_b[i - 1].get("zh", "")
        step_texts = {"a_m": (text_a, zh_a), "a_slow": (text_a, zh_a),
                      "b_m": (text_b, zh_b), "b_slow": (text_b, zh_b),
                      "b_f": (text_b, zh_b),
                      "combo": (f"{text_a} {text_b}", f"{zh_a} {zh_b}")}
        for si, entry in enumerate(steps):
            step = entry["step"]
            dur = float(durs[step])
            if si == 0 and card_lead > 0:
                dur += float(card_lead)
            timeline.append({
                "type": "pair", "step": step, "pair": i,
                "duration": round(dur, 3),
                "subtitle_en": step_texts[step][0],
                "subtitle_zh": step_texts[step][1],
            })
            timeline.append({"type": "gap", "step": "", "pair": i,
                             "duration": round(_gap_after(step, entry.get("gap")), 3),
                             "subtitle_en": "", "subtitle_zh": ""})

    if include_outro:
        outro_dur = float(audio.get("outro_dur", 0.0))
        timeline.append({"type": "outro", "duration": round(outro_dur, 3),
                         "subtitle_en": "", "subtitle_zh": "", "pair": 0, "step": ""})
    if (quantize_sec and quantize_sec > 0) or tail_margin > 0:
        _align_block_durations(timeline, float(quantize_sec or 0.0),
                               margin=float(tail_margin or 0.0), grouping=grouping)
    return timeline


def _align_block_durations(timeline: list[dict], step: float, margin: float = 0.0,
                           grouping: str = "per_step") -> None:
    """把**每个块**的总时长对齐到 step 的整数倍，并保证 ≥ 本组音频 + margin。

    就地修改。两个用途：

    1. **帧长对齐**（step = `1/fps`，仅低帧率需要）：此时一帧 = 1/fps 秒，块时长
       不是帧长整数倍的话编码器会向上取整 → 视频比音频网格长（实测 52 块累计
       +26.8s，画面与旁白逐步错位）。取整后音频网格、SRT、YouTube 章节与视频
       四者严格同源。
    2. **换卡尾巴余量**（margin > 0，任意帧率都可用）：每组音频读完后画面再多停留
       margin 秒才切换到下一组 —— 即「本组音频读完（含停顿）才换画面」的显式保证。
       目标时长 `V_k = ceil((A_k + margin) / step) * step ≥ A_k + margin`。

    取整方式：延长块内**最后一段**——pair 块的末段是 gap（纯静音，听感无影响），
    intro/outro 块延长自身。块边界识别与 compose_sleep 的分块规则一致：
    intro/outro 各自成块；`grouping="per_step"` 时 [pair + gap] 成块，
    `"per_pair"` 时同组全部步骤成块（此时需对齐的块数只有 1/5）。
    `step<=0` 时只按 margin 补（不量化到帧长）。
    """
    n = len(timeline)
    per_pair = str(grouping or "").strip().lower() == "per_pair"
    i = 0
    while i < n:
        seg = timeline[i]
        t = seg.get("type", "")
        if t in ("intro", "outro"):
            _pad_last([seg], step, margin)
            i += 1
        elif t == "pair":
            if per_pair:
                pid = seg.get("pair", 0)
                block = []
                while (i < n and timeline[i].get("type") not in ("intro", "outro")
                       and timeline[i].get("pair", 0) == pid):
                    block.append(timeline[i])
                    i += 1
                _pad_last(block, step, margin)
            else:
                block = [seg]
                if i + 1 < n and timeline[i + 1].get("type") == "gap":
                    block.append(timeline[i + 1])
                    i += 2
                else:
                    i += 1
                _pad_last(block, step, margin)
        else:
            i += 1


def _pad_last(block: list[dict], step: float, margin: float = 0.0) -> None:
    """把块补齐到 `ceil((总时长 + margin)/step)*step`（step<=0 时只补 margin）。

    注意必须保证**结果本身**是 step 的整数倍（`V = ceil((A+margin)/step)*step`，
    补 `V - A`），而不是把 (A+margin) 对齐后再加到 A 上（那样结果会带零头）。
    """
    total = sum(float(s.get("duration", 0.0) or 0.0) for s in block)
    target = total + max(0.0, float(margin or 0.0))
    if step and step > 0:
        # 避免浮点误差导致 3.0000000001 被 ceil 成 4
        n = int((target - 1e-9) // step) + 1
        add = n * step - total
    else:
        add = target - total
    if add > 1e-9:
        block[-1]["duration"] = round(
            float(block[-1].get("duration", 0.0) or 0.0) + add, 3)


def build_sleep_srt(timeline: list[dict]) -> str:
    """sidecar 字幕（不上屏烧录）：pair 段的 EN/ZH 进 SRT，其余跳过。"""
    return build_srt(timeline, skip_types={"intro", "gap", "outro"}, gap=0.0)
