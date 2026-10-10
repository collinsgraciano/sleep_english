"""回归测试：段网格时长 + `concat_segments` 的音频网格通道。

背景（2026-10-09 实测定位）：`_probe_audio_duration()` 在「分离独立项目」时丢失，
`_segment_grid_duration()` 因此每次抛 NameError，被 `concat_segments` 的
`except Exception: audio_ok = False` 静默吞掉 ⇒ 音频「按块网格裁尾/补齐」从未生效，
一直走整轨拼接兜底，累积 AAC priming：per_step ≈0.05s/块、per_pair ≈0.4s/块
（12 块 ≈4.5s）⇒ **音频逐渐早于画面**（用户报告"音画内容不同步"）。

本测试守护：
  1. `_probe_audio_duration` 存在且能读出音频流时长（音频短于视频的容器）；
  2. `_segment_grid_duration` 取 max(视频, 音频) 且不抛异常；
  3. `concat_segments` 用**网格通道**（不打印回退告警），且成片音频时长 == 视频时长
     —— 即使每个块的音频流都短于其视频流。

跑法：python pipeline/test_concat_grid.py
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(PIPE.parent))

import media_utils as mu  # noqa: E402

FAILS: list[str] = []
TMP = Path(tempfile.gettempdir()) / "sleep_concat_grid_test"
TMP.mkdir(exist_ok=True)


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def make(name: str, vdur: float, adur: float) -> Path:
    """视频 vdur 秒、音频 adur 秒（音频更短=真实块的情况）。"""
    out = TMP / name
    out.unlink(missing_ok=True)
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c=black:s=160x90:r=1:d={vdur}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={adur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "1",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                    "-t", str(vdur), str(out)], capture_output=True)
    return out


def main() -> int:
    b1 = make("blk_a.mp4", 5.0, 3.0)      # 视频 5s / 音频 3s
    b2 = make("blk_b.mp4", 4.0, 2.0)      # 视频 4s / 音频 2s

    print("=== 1) 探针可用性（丢失函数即为本 bug 的根因）===")
    check(hasattr(mu, "_probe_audio_duration"), "_probe_audio_duration 已定义")
    av = mu._probe_audio_duration(str(b1)) if hasattr(mu, "_probe_audio_duration") else None
    vv = mu._probe_video_duration(str(b1))
    check(av is not None and abs(av - 3.0) < 0.1, "音频流时长探测 ≈3.0s", f"got {av}")
    check(vv is not None and abs(vv - 5.0) < 0.1, "视频流时长探测 ≈5.0s", f"got {vv}")

    print("=== 2) _segment_grid_duration = max(视频, 音频) 且不抛异常 ===")
    try:
        g1 = mu._segment_grid_duration(str(b1))
        g2 = mu._segment_grid_duration(str(b2))
        check(abs(g1 - 5.0) < 0.1 and abs(g2 - 4.0) < 0.1,
              "网格时长 = 视频流时长（音频更短时）", f"{g1} / {g2}")
    except Exception as e:  # noqa: BLE001
        check(False, "_segment_grid_duration 不应抛异常", f"{type(e).__name__}: {e}")

    print("=== 3) concat_segments：走网格通道，成片音频==视频 ===")
    out = str(TMP / "merged.mp4")
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            mu.concat_segments([str(b1), str(b2)], out, tmp_dir=str(TMP / "ct"))
    except Exception as e:  # noqa: BLE001
        check(False, "concat_segments 执行成功", f"{type(e).__name__}: {e}")
        print(buf.getvalue())
        return 1
    logged = buf.getvalue()
    check("音频网格通道失败" not in logged and "兜底整轨拼接" not in logged,
          "未触发回退（走按块网格通道）", logged.strip()[:80] or "（无告警）")
    vd, ad = mu._probe_video_duration(out), mu._probe_audio_duration(out)
    check(vd is not None and abs(vd - 9.0) < 0.15, "成片视频 ≈9.0s（5+4）", f"got {vd}")
    check(ad is not None and abs(ad - 9.0) < 0.15,
          "成片音频 ≈9.0s（短音频被补齐到网格）", f"got {ad}")
    check(vd and ad and abs(vd - ad) <= 0.05, "音视频同格（差 ≤0.05s）",
          f"差 {None if not (vd and ad) else round(vd - ad, 3)}s")

    print()
    if FAILS:
        print(f"===== 失败 {len(FAILS)} 项：{FAILS} =====")
        return 1
    print("===== 段网格/拼接测试全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
