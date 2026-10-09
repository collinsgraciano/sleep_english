"""卡片块帧率（sleep_card_fps）回归测试：零成本，不跑真实合成。

覆盖：
  1. `_build_block(fps=25)` 生成的 ffmpeg 命令与历史命令**逐字符一致**（golden）；
  2. `fps=1` 的命令除 `-r` 值外与 25fps 完全相同（改动面最小、只降帧率）；
  3. `_resolve_card_fps()` 安全闸门：绑定片头/片尾视频 / 交叉溶解 / 非法值 → 回退 25；
  4. `_existing_block_ok()`：换过帧率后旧块必须重建（否则混合帧率拼接时长漂移）。

跑法：python pipeline/test_card_fps.py（纯 stdlib + 本机 ffmpeg）
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(PIPE.parent))

import sleep.video_compose_sleep as vc  # noqa: E402
from pipeline import _resolve_card_fps  # noqa: E402

FAILS: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


SEGS = [{"type": "pair", "pair": 1, "step": "a_m", "duration": 2.5},
        {"type": "gap", "duration": 1.0}]
AUDIO = {"pair_paths": {"0001": {"a_m": "/nonexistent/a_m.mp3"}}}
TMP = Path(tempfile.gettempdir()) / "sleep_card_fps_test"
TMP.mkdir(exist_ok=True)
VF = vc._output_vf(1280, 720)


def capture_block_cmd(fps: int) -> list[str]:
    """用假 _run_ffmpeg 捕获 _build_block 生成的命令（同时造出输出文件过校验）。"""
    cap: dict = {}

    def fake_run(cmd, **kw):
        cap["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"0" * 2000)

        class R:
            returncode = 0
            stderr = b""
        return R()

    orig = vc._run_ffmpeg
    vc._run_ffmpeg = fake_run
    try:
        vc._build_block("cards/card_0001.png", SEGS, AUDIO,
                        str(TMP / f"block_{fps}.mp4"), VF, lead=0.3, fps=fps)
    finally:
        vc._run_ffmpeg = orig
    return cap["cmd"]


def main() -> int:
    print("=== 1) fps=25 与历史命令逐字符一致（golden）===")
    out25 = str(TMP / "block_25.mp4")
    expected = [
        "ffmpeg", "-y", "-loop", "1", "-i", "cards/card_0001.png",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=0.300",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=2.500",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=1.000",
        "-filter_complex", ";[1:a][2:a][3:a]concat=n=3:v=0:a=1[aout]",
        "-map", "0:v:0",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
               "pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-map", "[aout]", "-t", "3.500",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        out25,
    ]
    got25 = capture_block_cmd(25)
    check(got25 == expected, "fps=25 命令逐字符一致",
          "" if got25 == expected else f"got={got25}")

    print("=== 2) fps=1 命令 = golden + (-framerate 1) + (-r 1) ===")
    out1 = str(TMP / "block_1.mp4")
    got1 = capture_block_cmd(1)
    # fps<25 时在图片输入前插入 -framerate N（否则 image2 demuxer 仍按默认 25fps
    # 产帧、-r 只在滤镜链末尾丢弃 → 4K 帧的内存搬运成本照旧：实测 26s 块
    # @1fps 11.5s → 2.1s（5.5×），相对 25fps 基线 13.2s 达 9.4×）
    expected_1 = expected[:2] + ["-framerate", "1"] + expected[2:]
    i_r = expected_1.index("-r")
    expected_1[i_r + 1] = "1"
    expected_1[-1] = out1
    check(got1 == expected_1, "fps=1 命令逐字符符合预期",
          "" if got1 == expected_1 else f"got={got1}")
    check(got1[got1.index("-r") + 1] == "1" and got25[got25.index("-r") + 1] == "25",
          "-r 值分别为 1 / 25")
    check(len(got1) == len(got25) + 2, "参数个数 = 25fps + 2",
          f"{len(got1)} vs {len(got25)}")

    print("=== 3) _resolve_card_fps 安全闸门 ===")
    fps, why = _resolve_card_fps(_Args(), {})
    check(fps == 25, "无配置 → 25fps", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=1), {})
    check(fps == 1, "1fps + 无绑定视频 + 无 xfade → 1fps", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=2), {})
    check(fps == 2, "2fps 生效", why)
    dummy = TMP / "intro_video.mp4"
    dummy.write_bytes(b"0" * 2000)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=1), {"intro_video": str(dummy)})
    check(fps == 25 and "绑定" in why, "绑定片头视频 → 回退 25fps", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=1),
                                 {"outro_video": "/nonexistent/outro.mp4"})
    check(fps == 1, "绑定路径不存在（未真正绑定）→ 仍可降帧", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=1, sleep_xfade=True), {})
    check(fps == 25 and "交叉溶解" in why, "交叉溶解开启 → 回退 25fps", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=3), {})
    check(fps == 25 and "非法" in why, "非法值 3 → 回退 25fps", why)
    fps, why = _resolve_card_fps(_Args(sleep_card_fps=25), {})
    check(fps == 25, "显式 25 → 25fps", why)

    print("=== 4) _existing_block_ok 帧率校验（resume 守卫）===")
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        check(True, "（无 ffmpeg，跳过）", "skip")
    else:
        f25 = TMP / "probe_25.mp4"
        f1 = TMP / "probe_1.mp4"
        for path, fps in ((f25, 25), (f1, 1)):
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"color=c=black:s=128x72:d=2:r={fps}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                           capture_output=True)
        check(vc._existing_block_ok(str(f25), 25), "25fps 块匹配 25 → 可复用")
        check(not vc._existing_block_ok(str(f25), 1), "25fps 块 vs 期望 1 → 重建")
        check(vc._existing_block_ok(str(f1), 1), "1fps 块匹配 1 → 可复用")
        check(not vc._existing_block_ok(str(TMP / "nope.mp4"), 25), "文件不存在 → 重建")

    print()
    if FAILS:
        print(f"===== 失败 {len(FAILS)} 项：{FAILS} =====")
        return 1
    print("===== 卡片帧率测试全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
