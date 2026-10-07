"""把 Kokoro 全部音色与试听缓存落到 Google Drive，开机自动同步回容器本地。

为什么需要它：
    Kokoro 的音色权重（`af_sarah.pt` 这类 ≈0.3-1MB 的小文件）默认下在容器本地
    `~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/voices/`，而 Colab 的本地盘
    随 VM 回收清空 —— 每次开机都要重新联网抓一遍（Colab 网络快时无所谓，但 hf-mirror
    不可达 / 免费档限速时就是出片前的一大段等待，试听页还会显示「⚠ 未缓存」）。
    所以这里把 28 个英语音色 + 中文兜底音色存到 Drive，开机只做「Drive → 本地」的补缺拷贝，
    并把新下载的音色回写 Drive；试听 mp3 则直接生成到 `configs/voice_previews/`
    （Colab 上 `configs/` 已 symlink 到 Drive，控制台「三音色页」因而开机即全部可点播）。

两个目录：
    Drive（事实源）: <DRIVE_ROOT>/kokoro/voices/<name>.pt
    本地（工作缓存）: ~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/voices/<name>.pt
                    （pipeline/tts_engine.py 的 _find_voice_in_cache 扫的就是这里，
                      所以放进去控制台就会显示「✓ 已缓存」）

设计约束：
    * 只用「加法」——只补缺失/更新的文件，从不删除、从不覆盖更新的版本；
    * Drive 未挂载（没跑 drive.mount）时不阻塞：打警告继续，出片链路照常；
    * 音色/试听命名复用仓库既有实现（`tts_engine` / `app.routers.voices_kokoro`），
      避免出现「脚本生成的试听文件控制台不认」这类漂移。

用法（notebook 已封装；单独跑也行）：
    DRIVE_ROOT=/content/drive/MyDrive/sleep_english_colab python3 colab/kokoro_voices.py --all
    DRIVE_ROOT=... python3 colab/kokoro_voices.py --status      # 只看现状
    DRIVE_ROOT=... python3 colab/kokoro_voices.py --sync-back   # 把本次新下载的写回 Drive
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# HF_ENDPOINT 必须在 import tts_engine 之前定好（它在导入期读一次），
# COLAB_HF_ENDPOINT 给国内/镜像场景一个开关：huggingface.co 抓不动时改 hf-mirror.com。
_ENDPOINT_OVERRIDE = (os.environ.get("COLAB_HF_ENDPOINT", "") or "").strip()
if _ENDPOINT_OVERRIDE and not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = _ENDPOINT_OVERRIDE

HERE = Path(__file__).resolve().parent
# REPO_DIR 默认＝本脚本所在目录的上一级（Colab 由 env 传入 /content/sleep_english）；
# 这样本机 Windows 上直接跑也能找到 pipeline/ 与 app/（用于预灌 Drive）。
REPO_DIR = Path(os.environ.get("REPO_DIR") or HERE.parent).resolve()
PIPELINE_DIR = REPO_DIR / "pipeline"

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

# Kokoro-82M 官方音色权重在 HF 仓库里的路径（与 tts_engine._download_voice 一致）
_DEFAULT_LOCAL_VOICES = Path(os.path.expanduser(
    "~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/voices"))
# 中文台词的主链路是 edge-tts，Kokoro 中文只在 edge-tts 全失败时兜底（zf_xiaoxiao），
# zf_xiaobei（男声）一并备着，缺了不影响英语出片。
CHINESE_VOICES = ("zf_xiaoxiao", "zf_xiaobei")

_SKIP_PARTS = {"__pycache__", ".ipynb_checkpoints", ".DS_Store", "Thumbs.db"}


def log(msg: str) -> None:
    print(f"[kokoro_voices] {msg}", flush=True)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or "").strip() or default


# ---------------------------------------------------------------------------
# 仓库内既有实现（延迟导入：--help / --status 不需要 torch/kokoro）
# ---------------------------------------------------------------------------

def _add_paths() -> None:
    for p in (str(REPO_DIR), str(PIPELINE_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def _english_voices() -> list[str]:
    """28 个英语音色的名字（事实源＝pipeline/tts_engine.KOKORO_VOICES，不另存一份）。"""
    _add_paths()
    from tts_engine import KOKORO_VOICES
    return [str(v["name"]) for v in KOKORO_VOICES]


def _expected_voices() -> list[str]:
    return _english_voices() + list(CHINESE_VOICES)


def _preview_path(voice: str):
    """试听缓存路径（复用控制台路由的命名 === 控制台认的就是这个文件）。"""
    _add_paths()
    from app.routers.voices_kokoro import _kokoro_preview_cache_path
    from app.tts_state import PREVIEW_TEXTS
    text = PREVIEW_TEXTS["english"]
    return _kokoro_preview_cache_path(voice, text), text


# ---------------------------------------------------------------------------
# 目录 / 拷贝
# ---------------------------------------------------------------------------

def drive_paths(args) -> tuple[Path, Path]:
    """返回 (drive_root, drive_voices_dir)。"""
    drive_root = Path(args.drive_root or _env(
        "DRIVE_ROOT", "/content/drive/MyDrive/sleep_english_colab")).resolve()
    kokoro_dir = Path(_env("COLAB_KOKORO_DIR", str(drive_root / "kokoro")))
    return drive_root, kokoro_dir / "voices"


def drive_ready(drive_root: Path) -> bool:
    """Drive 挂载判据与 drive_config.py 一致：MyDrive 的父目录存在。"""
    return drive_root.parent.is_dir()


def _copy_if_needed(src: Path, dst: Path) -> bool:
    """src → dst（dst 缺失或尺寸不同才拷）；返回是否真的拷了。"""
    try:
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, dst)
        return True
    except OSError as e:
        log(f"  !! 拷贝失败 {src.name} → {dst}：{e}")
        return False


def _local_pt_files(local_dir: Path) -> list[Path]:
    if not local_dir.is_dir():
        return []
    return [p for p in sorted(local_dir.glob("*.pt"))
            if p.stat().st_size > 1000 and not p.name.startswith(".")]


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------

def fetch_missing(local_dir: Path) -> tuple[int, list[str]]:
    """补齐失踪音色：本地缓存/目标目录里没有的，走 tts_engine._download_voice。"""
    _add_paths()
    from tts_engine import _download_voice

    have = {p.name for p in _local_pt_files(local_dir)}
    got, failed = 0, []
    for voice in _expected_voices():
        fname = f"{voice}.pt"
        if fname in have:
            log(f"  ok   已有 {fname}")
            continue
        t0 = time.time()
        try:
            path = _download_voice(fname)
        except Exception as e:
            failed.append(fname)
            log(f"  warn 下载失败 {fname}：{str(e)[:100]}")
            continue
        # _download_voice 落在标准 HF 缓存；目标目录不同（--hf-voices-dir）时补一份
        if Path(path).parent != local_dir:
            _copy_if_needed(Path(path), local_dir / fname)
        got += 1
        log(f"  down {fname}（{time.time() - t0:.1f}s）")
    return got, failed


def sync_in(drive_voices: Path, local_dir: Path) -> int:
    """Drive → 本地（开机补缺）。"""
    n = 0
    for name in _expected_voices():
        src = drive_voices / f"{name}.pt"
        if not src.is_file() or src.stat().st_size <= 1000:
            continue
        if _copy_if_needed(src, local_dir / f"{name}.pt"):
            n += 1
    return n


def sync_back(drive_voices: Path, local_dir: Path) -> int:
    """本地 → Drive（把本次会话新下载/已有的音色回写，供下回开机免下载）。"""
    n = 0
    for src in _local_pt_files(local_dir):
        if _copy_if_needed(src, drive_voices / src.name):
            n += 1
    return n


def make_previews(limit: int = 0) -> tuple[int, int, list[str]]:
    """为全部英语音色补齐试听 mp3（已存在的跳过）；返回 (新建, 跳过, 失败音色)。"""
    _add_paths()
    from tts_engine import TTSEngine

    created = skipped = 0
    failed: list[str] = []
    engine = TTSEngine()
    names = _english_voices()
    if limit > 0:
        names = names[:limit]
    for voice in names:
        cache_path, text = _preview_path(voice)
        if cache_path.exists() and cache_path.stat().st_size > 1000:
            skipped += 1
            continue
        t0 = time.time()
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            engine.synth_english(text, voice, str(cache_path), rate="+0%")
        except Exception as e:
            failed.append(voice)
            log(f"  warn 试听生成失败 {voice}：{str(e)[:100]}")
            continue
        created += 1
        log(f"  prev {voice}（{time.time() - t0:.1f}s）")
    return created, skipped, failed


def warm_model(device: str | None) -> bool:
    """预热 Kokoro 英文模型（≈330MB，进本地 HF 缓存）。

    先按当前 HF_ENDPOINT 试一次；失败（国内直连 HF 常见）再用 hf-mirror 起子进程重试 ——
    huggingface_hub 在导入期读 HF_ENDPOINT，所以换端点必须换进程。
    """
    _add_paths()
    if device:
        os.environ["KOKORO_DEVICE"] = device
    t0 = time.time()
    try:
        from tts_engine import TTSEngine
        TTSEngine._get_kokoro()
        log(f"  模型就绪（{time.time() - t0:.1f}s，device={os.environ.get('KOKORO_DEVICE') or 'auto'}）")
        return True
    except Exception as e:
        log(f"  warn 模型加载失败（{str(e)[:120]}）—— 改用 hf-mirror 重试")

    env = {**os.environ, "HF_ENDPOINT": "https://hf-mirror.com"}
    code = (f"import sys; sys.path.insert(0, {str(PIPELINE_DIR)!r}); "
            "from tts_engine import TTSEngine; TTSEngine._get_kokoro()")
    r = subprocess.run([sys.executable, "-c", code], env=env, text=True,
                       capture_output=True, timeout=1800)
    if r.returncode == 0:
        log(f"  模型就绪（hf-mirror，{time.time() - t0:.1f}s）")
        return True
    log(f"  !! 模型仍未就绪：{(r.stderr or r.stdout or '').strip()[-200:]}")
    return False


def status(drive_voices: Path, local_dir: Path, drive_ok: bool) -> dict:
    _add_paths()
    from tts_engine import _find_voice_in_cache

    english = _english_voices()
    on_drive = {p.stem for p in _local_pt_files(drive_voices)}
    local = {p.stem for p in _local_pt_files(local_dir)}
    cached = {v for v in english if _find_voice_in_cache(f"{v}.pt")}
    previews = []
    for v in english:
        try:
            p, _ = _preview_path(v)
        except Exception:
            p = None
        previews.append(p is not None and p.exists() and p.stat().st_size > 1000)

    log(f"Drive        : {'已挂载' if drive_ok else '未挂载（本步只影响持久化）'}  {drive_voices}")
    log(f"本地缓存目录 : {local_dir}")
    log(f"英语音色     : Drive {len(on_drive & set(english))}/{len(english)}"
        f" · 本地 {len(local & set(english))}/{len(english)}"
        f" · 控制台识别 {len(cached)}/{len(english)}")
    log(f"试听 mp3     : {sum(previews)}/{len(english)}")
    return {"drive": sorted(on_drive), "local": sorted(local),
            "cached": sorted(cached), "previews": sum(previews),
            "english": len(english)}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Kokoro 全部音色 + 试听缓存落 Google Drive（并同步回本地）")
    ap.add_argument("--drive-root", default="",
                    help="Drive 下的项目目录（默认环境变量 DRIVE_ROOT）")
    ap.add_argument("--hf-voices-dir", default="",
                    help="本地音色目录（默认 HF 缓存，与控制台 cached 判定一致；测试用）")
    ap.add_argument("--device", default="", choices=["", "cpu", "cuda"],
                    help="Kokoro 计算设备（默认环境变量 KOKORO_DEVICE）")
    ap.add_argument("--model", action="store_true", help="预热模型（≈330MB，进本地 HF 缓存）")
    ap.add_argument("--sync-in", action="store_true", help="Drive → 本地（开机补缺）")
    ap.add_argument("--all", action="store_true", help="补齐全部音色权重（缺哪个下哪个）")
    ap.add_argument("--previews", action="store_true", help="为全部英语音色补齐试听 mp3")
    ap.add_argument("--sync-back", action="store_true", help="本地 → Drive（回写新下载的音色）")
    ap.add_argument("--status", action="store_true", help="只打印现状，不下载不生成")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个英语音色（测试用）")
    args = ap.parse_args()

    if not any((args.model, args.sync_in, args.all, args.previews, args.sync_back, args.status)):
        args.model = args.sync_in = args.all = args.previews = args.sync_back = True

    drive_root, drive_voices = drive_paths(args)
    local_dir = Path(args.hf_voices_dir).resolve() if args.hf_voices_dir else _DEFAULT_LOCAL_VOICES
    ok = drive_ready(drive_root)

    log(f"仓库 {REPO_DIR}")
    log(f"Drive {drive_root}{'' if ok else '（父目录不存在 = 未挂载）'}")
    if not ok:
        log("!! 没挂载 Drive：音色只会留在容器本地，下回开机还得重下。")
        log("!! 想让音色持久化，先跑 notebook 的 Drive 挂载步，再重跑本步。")
    else:
        try:
            drive_voices.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            ok = False
            log(f"!! Drive 目录不可写（{e}）—— 降级为仅本地缓存")
    try:
        local_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log(f"!! 本地音色目录不可写：{e}")
        return 1

    if args.status:
        status(drive_voices if ok else Path("/nonexistent"), local_dir, ok)
        return 0

    # 1) 模型：没有模型连音色都试听不了（≈330MB，只在本地缓存，不进 Drive）
    if args.model:
        log("==> [1/5] 预热 Kokoro 英文模型")
        warm_model(args.device or None)

    # 2) Drive → 本地（先补本地，后面 --all 才知道还缺哪些）
    if args.sync_in and ok:
        n = sync_in(drive_voices, local_dir)
        log(f"==> [2/5] Drive → 本地补缺 {n} 个音色")
    else:
        log("==> [2/5] 跳过 Drive → 本地补缺" + ("" if ok else "（Drive 未挂载）"))

    # 3) 补齐缺失音色（联网；单个失败不影响其余，英语音色失败会在末尾点名）
    failures: list[str] = []
    if args.all:
        log("==> [3/5] 补齐缺失音色权重")
        _got, failures = fetch_missing(local_dir)
    else:
        log("==> [3/5] 跳过音色补齐")

    # 4) 试听 mp3（落在 configs/voice_previews/，Colab 上即 Drive）
    if args.previews:
        log("==> [4/5] 生成试听缓存（configs/voice_previews/）")
        created, skipped, prev_failed = make_previews(args.limit)
        failures.extend(prev_failed)
        log(f"    新建 {created} 个 · 已有 {skipped} 个"
            f"{' · 失败 ' + str(len(prev_failed)) + ' 个' if prev_failed else ''}")
    else:
        log("==> [4/5] 跳过试听生成")

    # 5) 本地 → Drive（本次会话新下载的音色回写，下回开机免下载）
    if args.sync_back and ok:
        n = sync_back(drive_voices, local_dir)
        log(f"==> [5/5] 本地 → Drive 回写 {n} 个音色")
    else:
        log("==> [5/5] 跳过回写" + ("" if ok else "（Drive 未挂载）"))

    status(drive_voices if ok else Path("/nonexistent"), local_dir, ok)
    if failures:
        log(f"!! 有 {len(failures)} 个文件没能就绪：{', '.join(sorted(set(failures))[:8])}")
        log("   （英语音色缺失会让该音色出片/试听时才现场下载；中文音色只影响中文兜底）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
