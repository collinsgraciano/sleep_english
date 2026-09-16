"""sleep 模式音频准备：整组音频直接按目标速率经引擎 rate 机制合成。

每组生成：a_m/b_m（男声，male_rate 速率）、b_f（女声常速，供 AB 连贯内联）、
a_slow/b_slow（女声慢速——Kokoro 用原生 speed 参数变速不变调，
Qwen/MOSS 引擎内部 atempo）；AB 连贯（combo）不再预编码中间 mp3 ——
时长按 a_m + COMBO_GAP + b_f 计算，块合成 filter_complex 内直接拼接
（少 200 个子进程与一次有损重编码）。另有 intro（频道名播报）/ outro
（结束语），均男声（male_rate 速率）。
跨引擎（kokoro/qwen/moss）复用 tts_pipeline 同款引擎装配 + kokoro 单句回退。
音频时长写 .durations.json sidecar（mtime+size 校验），resume 免逐文件
ffprobe 子进程；音频文件保持引擎原生格式，块合成时统一 aresample/立体声。
"""
import json
import os
import time
from pathlib import Path

from media_utils import get_duration

# AB 连贯段内 A→B 的气口秒数（原 _combo_file 的拼接间隔，语义不变）
COMBO_GAP = 0.15

# 组内音频文件全集（b_f 仅 combo 内联消费；序列编排可独立成步）
PAIR_FILES = ("a_m", "b_m", "b_f", "a_slow", "b_slow")
# 默认结构消费的全部步骤（5 个真实文件 + combo 计算时长段）
DEFAULT_NEEDED_STEPS = frozenset(PAIR_FILES) | {"combo"}

# 时长 sidecar：{文件名: [mtime_ns, size, 时长秒]}
_DUR_SIDECAR = ".durations.json"


def needed_audio_steps(sequence: list[dict] | None) -> set[str] | None:
    """序列编排消费的步骤集合（含 combo 伪步骤，其时长 = a_m+COMBO_GAP+b_f）。

    combo 消费 a_m + b_f 两个文件，其余步骤各对应同名音频文件；
    空序列 / 默认结构 / 解析失败返回 None（=全集，prepare/load 据此回退）。
    """
    if not sequence:
        return None
    need: set[str] = set()
    for entry in sequence:
        step = entry.get("step")
        if step == "combo":
            need.update(("a_m", "b_f", "combo"))
        elif step in PAIR_FILES:
            need.add(step)
    return need or None


def _pair_durs(paths: dict, needed: set[str], durs: dict) -> dict:
    """组内各步骤时长（仅 needed 键；combo = a_m + COMBO_GAP + b_f）。"""
    out = {s: _dur(paths[s], durs) for s in PAIR_FILES if s in needed}
    if "combo" in needed and "a_m" in out and "b_f" in out:
        out["combo"] = out["a_m"] + COMBO_GAP + out["b_f"]
    return out


def _rate_str(mult: float) -> str:
    """速率倍率 → 引擎 rate 字符串（1.0→"+0%"，0.8→"-20%"）。"""
    return f"{(float(mult) - 1.0) * 100:+.0f}%"


def _sleep_meta_sig(tts_engine: str, slow_rate: float, male_rate: float) -> str:
    return f"sleep|{tts_engine}|{slow_rate:.3f}|{male_rate:.3f}"


def _check_sleep_cache(audio_dir: Path, sig: str) -> None:
    """引擎/慢速参数变化时清除旧 sleep 音频缓存（.tts_meta.json 签名机制）。"""
    meta_path = audio_dir / ".tts_meta.json"
    current = meta_path.read_text(encoding="utf-8") if meta_path.exists() else ""
    if current == sig:
        return
    if current.startswith("sleep|"):
        removed = 0
        for f in audio_dir.glob("pair_*.mp3"):
            f.unlink()
            removed += 1
        for name in ("intro_sleep.mp3", "outro_sleep.mp3"):
            p = audio_dir / name
            if p.exists():
                p.unlink()
                removed += 1
        if removed:
            print(f"  [Sleep] TTS 参数变化（{current} → {sig}），清除 {removed} 个旧音频缓存")
    sidecar = audio_dir / _DUR_SIDECAR
    if sidecar.exists():
        sidecar.unlink()  # 旧时长条目对应已删除文件，一并清理
    meta_path.write_text(sig, encoding="utf-8")


# ---------------------------------------------------------------------------
# 时长 sidecar：resume 路径免逐文件 ffprobe（200 组 ≈ 1000+ 次子进程）
# ---------------------------------------------------------------------------

def _load_dur_sidecar(audio_dir: Path) -> dict:
    p = audio_dir / _DUR_SIDECAR
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_dur_sidecar(audio_dir: Path, durs: dict) -> None:
    try:
        (audio_dir / _DUR_SIDECAR).write_text(
            json.dumps(durs, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # sidecar 是加速缓存，写失败不影响正确性


def _dur(path: str, durs: dict) -> float:
    """时长解析：sidecar 命中（mtime+size 校验一致）→ 直接用；否则 ffprobe 并写回。"""
    key = os.path.basename(path)
    try:
        st = os.stat(path)
    except OSError:
        return get_duration(path)
    ent = durs.get(key)
    if isinstance(ent, list) and len(ent) == 3 \
            and ent[0] == st.st_mtime_ns and ent[1] == st.st_size:
        return float(ent[2])
    d = get_duration(path)
    durs[key] = [st.st_mtime_ns, st.st_size, d]
    return d


def _usable(path: str) -> bool:
    """文件级续传判定：存在且非零字节（损坏/空文件交给重生成，而非拖到块合成才炸）。"""
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def pair_steps(audio_dir: Path, i: int) -> dict:
    """第 i 组（1-based）各步骤音频路径表（5 个真实文件；combo 为计算时长段）。"""
    base = f"pair_{i:04d}"
    return {step: str(audio_dir / f"{base}_{step}.mp3")
            for step in ("a_m", "b_m", "b_f", "a_slow", "b_slow")}


def load_sleep_audio_results(audio_dir: Path, num_pairs: int,
                             needed_steps: set[str] | None = None) -> dict | None:
    """从已存在文件重建 sleep 音频结果（resume / 完整性校验）。

    needed_steps 内全部文件 + intro/outro 齐全才返回，否则 None（交给
    正常生成流程按文件续传）。needed_steps=None 时按全集校验（默认结构）。
    combo 时长 = a_m+COMBO_GAP+b_f（块合成内联拼接，无 combo 中间文件）。
    """
    audio_dir = Path(audio_dir)
    needed = needed_steps or set(DEFAULT_NEEDED_STEPS)
    intro = audio_dir / "intro_sleep.mp3"
    outro = audio_dir / "outro_sleep.mp3"
    if not (_usable(str(intro)) and _usable(str(outro))):
        return None
    durs = _load_dur_sidecar(audio_dir)
    pair_paths, pair_durs = {}, {}
    for i in range(1, num_pairs + 1):
        paths = pair_steps(audio_dir, i)
        if not all(_usable(paths[s]) for s in PAIR_FILES if s in needed):
            return None
        key = str(i).zfill(4)
        pair_paths[key] = dict(paths)
        pair_durs[key] = _pair_durs(paths, needed, durs)
    intro_dur = _dur(str(intro), durs)
    outro_dur = _dur(str(outro), durs)
    _save_dur_sidecar(audio_dir, durs)
    return {
        "intro": str(intro), "intro_dur": intro_dur,
        "outro": str(outro), "outro_dur": outro_dur,
        "pair_paths": pair_paths, "pair_durs": pair_durs,
    }


def build_engine_and_voice_map(tts_engine: str, script: dict):
    """三引擎装配（照抄 tts_pipeline 三分支）。返回 (tts, voice_map)。

    供 prepare_sleep_audio 与片头频道名播报合成（Web 层）共用。
    """
    if tts_engine == "qwen":
        from qwen_tts_engine import QwenTTSEngine, build_qwen_voice_map
        tts = QwenTTSEngine(
            os.environ.get("QWEN_MODEL_PATH",
                           r"H:\models\Qwen3-TTS-12Hz-0.6B-CustomVoice"),
            os.environ.get("QWEN_DEVICE", "cuda:0"),
            os.environ.get("QWEN_BASE_MODEL_PATH",
                           r"H:\models\Qwen3-TTS-12Hz-1.7B-Base"),
            os.environ.get("QWEN_VOICEDSIGN_MODEL_PATH",
                           r"H:\models\Qwen3-TTS-12Hz-1.7B-VoiceDesign"))
        voice_map = build_qwen_voice_map(script, "sleep")
    elif tts_engine == "moss":
        from moss_tts_engine import MossTTSEngine, build_moss_voice_map
        tts = MossTTSEngine(
            os.environ.get("MOSS_MODEL_PATH") or r"H:\models\MOSS-TTS-Nano-Model",
            os.environ.get("MOSS_DEVICE") or "cpu",
            os.environ.get("MOSS_TOKENIZER_PATH") or r"H:\models\MOSS-Audio-Tokenizer-Nano",
            os.environ.get("MOSS_REPO_DIR") or r"H:\models\MOSS-TTS-Nano")
        voice_map = build_moss_voice_map(script, "sleep")
    else:
        from tts_engine import TTSEngine, build_voice_map
        tts = TTSEngine()
        voice_map = build_voice_map(script, "sleep")
    return tts, voice_map


def prepare_sleep_audio(script: dict, audio_dir: Path, num_pairs: int,
                        tts_engine: str = "kokoro", slow_rate: float = 0.8,
                        male_rate: float = 1.0,
                        channel_name: str = "", outro_text: str = "",
                        voice_male: str = "", voice_female: str = "",
                        stop_check=None,
                        needed_steps: set[str] | None = None) -> dict:
    """生成全部 sleep 音频（文件级续传）。返回 results dict（见 load_*）。

    slow_rate/male_rate 为速率倍率（0.8=八成速），经各引擎 synth_english
    的 rate 参数实现（Kokoro 原生 speed 变速不变调；Qwen/MOSS 引擎内部处理）。
    voice_male/voice_female 为频道级音色覆盖（空=按性别默认映射），
    qwen/moss 引擎同样按引擎音色 id 生效。
    needed_steps（needed_audio_steps 结果）：仅合成序列编排消费的步骤文件，
    None=默认结构全集。
    """
    audio_dir = Path(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    needed = needed_steps or set(DEFAULT_NEEDED_STEPS)
    slow_rate = float(slow_rate or 0.8)
    male_rate = min(1.5, max(0.5, float(male_rate or 1.0)))
    male_rate_str = _rate_str(male_rate)
    slow_rate_str = _rate_str(slow_rate)
    _check_sleep_cache(audio_dir, _sleep_meta_sig(tts_engine, slow_rate, male_rate))

    # --- 引擎装配（三分支抽至 build_engine_and_voice_map，片头播报共用）---
    tts, voice_map = build_engine_and_voice_map(tts_engine, script)
    male_voice = voice_map.get("char_a", "am_adam")
    female_voice = voice_map.get("char_b", "af_sarah")
    # 频道级音色覆盖（空=维持按性别映射；qwen/moss 引擎同样生效）
    if str(voice_male or "").strip():
        male_voice = str(voice_male).strip()
    if str(voice_female or "").strip():
        female_voice = str(voice_female).strip()

    _fb = {"eng": None, "map": None}

    def _kokoro():
        from tts_engine import TTSEngine, build_voice_map
        if _fb["eng"] is None:
            _fb["eng"] = TTSEngine()
        if _fb["map"] is None:
            _fb["map"] = build_voice_map(script, "sleep")
        return _fb["eng"], _fb["map"]

    def _synth(text: str, voice: str, path: str, rate: str = "+0%") -> float:
        attempts = 3 if tts_engine == "kokoro" else 1
        for attempt in range(attempts):
            try:
                return tts.synth_english(text, voice, path, rate=rate)
            except Exception as e:
                if tts_engine == "kokoro" and attempt < attempts - 1:
                    # Kokoro 无兜底引擎：单句偶发失败自动重试（本地推理零成本）
                    print(f"  [Sleep][Retry {attempt + 1}/{attempts - 1}] "
                          f"{Path(path).name} {type(e).__name__}: {e}")
                    time.sleep(1)
                    continue
                if tts_engine == "kokoro":
                    raise
                print(f"  [Sleep][Fallback] {Path(path).name} {type(e).__name__}: {e}")
                eng, kmap = _kokoro()
                fb_voice = kmap.get("char_a") if voice == male_voice else kmap.get("char_b")
                return eng.synth_english(text, fb_voice or "af_sarah", path, rate=rate)
        raise RuntimeError(f"unreachable: {path}")

    narration_voice = male_voice
    intro = audio_dir / "intro_sleep.mp3"
    outro = audio_dir / "outro_sleep.mp3"
    if not _usable(str(intro)):
        _synth(channel_name or "English with me", narration_voice, str(intro),
               rate=male_rate_str)
    if not _usable(str(outro)):
        _synth(outro_text or "Thanks for listening. See you next time!",
               narration_voice, str(outro), rate=male_rate_str)

    dialogue = script.get("dialogue", [])
    rows_a = dialogue[0::2]
    rows_b = dialogue[1::2]
    total = min(num_pairs, len(rows_a), len(rows_b))

    pair_paths, pair_durs = {}, {}
    durs = _load_dur_sidecar(audio_dir)
    for i in range(1, total + 1):
        if stop_check and stop_check():
            print("  [Sleep] Stop requested, aborting audio prep.", flush=True)
            raise RuntimeError("stopped")
        paths = pair_steps(audio_dir, i)
        text_a = rows_a[i - 1].get("text", "")
        text_b = rows_b[i - 1].get("text", "")
        # needed 步骤文件齐全 → 整组跳过（时长走 sidecar，免 ffprobe）
        if all(_usable(paths[s]) for s in PAIR_FILES if s in needed):
            key = str(i).zfill(4)
            pair_paths[key] = dict(paths)
            pair_durs[key] = _pair_durs(paths, needed, durs)
            continue
        for s in PAIR_FILES:
            if s not in needed:
                continue
            if not _usable(paths[s]):
                if s == "a_m":
                    _synth(text_a, male_voice, paths["a_m"], rate=male_rate_str)
                elif s == "b_m":
                    _synth(text_b, male_voice, paths["b_m"], rate=male_rate_str)
                elif s == "b_f":
                    _synth(text_b, female_voice, paths["b_f"])
                elif s == "a_slow":
                    _synth(text_a, female_voice, paths["a_slow"], rate=slow_rate_str)
                else:  # b_slow
                    _synth(text_b, female_voice, paths["b_slow"], rate=slow_rate_str)
        key = str(i).zfill(4)
        pair_paths[key] = dict(paths)
        pair_durs[key] = _pair_durs(paths, needed, durs)
        if i % 10 == 0 or i == total:
            print(f"  [Sleep] Audio pairs {i}/{total} done")

    _save_dur_sidecar(audio_dir, durs)
    return {
        "intro": str(intro), "intro_dur": _dur(str(intro), durs),
        "outro": str(outro), "outro_dur": _dur(str(outro), durs),
        "pair_paths": pair_paths, "pair_durs": pair_durs,
    }
