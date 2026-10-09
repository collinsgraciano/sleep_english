"""块划分方式（sleep_block_grouping）回归测试：零成本，不跑合成。

覆盖：
  1. per_step 与历史分块逐块一致（golden：intro | (pair+gap)×N | outro）；
  2. per_pair 把同组多步合并（10 组 → 12 块），且**不丢段、不重段、顺序不变**；
  3. 非法取值回退 per_step；
  4. 时间轴 1/fps 对齐：两种分块下每个块都是 step 的整数倍，per_pair 的补齐量更小；
  5. resume 守卫 `_existing_block_ok` 除帧率外还校验时长（换分块方式必须重建）。

跑法：python pipeline/test_block_grouping.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
sys.path.insert(0, str(PIPE.parent))

import sleep.video_compose_sleep as vc  # noqa: E402
from pipeline import _resolve_block_grouping  # noqa: E402
from sleep.timeline_sleep import _quantize_block_durations  # noqa: E402

FAILS: list[str] = []
TMP = Path(tempfile.gettempdir()) / "sleep_block_grouping"
TMP.mkdir(exist_ok=True)


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


class _Args:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def make_timeline(pairs: int = 2, steps: int = 5) -> list[dict]:
    tl = [{"type": "intro", "duration": 3.016, "pair": 0, "step": ""}]
    for i in range(1, pairs + 1):
        for s in range(steps):
            tl.append({"type": "pair", "step": f"s{s}", "pair": i,
                       "duration": round(2.0 + 0.1 * s + 0.007 * i, 3)})
            tl.append({"type": "gap", "step": "", "pair": i,
                       "duration": round(1.0 + 0.2 * s, 3)})
    tl.append({"type": "outro", "duration": 4.013, "pair": 0, "step": ""})
    return tl


def main() -> int:
    tl = make_timeline(pairs=2, steps=5)
    n_pairs, n_steps = 2, 5

    print("=== 1) per_step 与历史分块一致 ===")
    b_step = vc.group_timeline_blocks(tl, "per_step")
    check(len(b_step) == 1 + n_pairs * n_steps + 1,
          f"块数 = 1 intro + {n_pairs}×{n_steps} + 1 outro", f"got {len(b_step)}")
    check([len(b) for b in b_step] == [1] + [2] * (n_pairs * n_steps) + [1],
          "每块构成：intro | (pair,gap)×N | outro", f"got {[len(b) for b in b_step]}")
    check(b_step[0][0]["type"] == "intro" and b_step[-1][0]["type"] == "outro",
          "首块=intro、末块=outro")

    print("=== 2) per_pair 合并同组 ===")
    b_pair = vc.group_timeline_blocks(tl, "per_pair")
    check(len(b_pair) == 1 + n_pairs + 1, f"块数 = 1 + {n_pairs} + 1",
          f"got {len(b_pair)}")
    check([len(b) for b in b_pair] == [1] + [n_steps * 2] * n_pairs + [1],
          "每组块含全部步骤+停顿", f"got {[len(b) for b in b_pair]}")
    flat_step = [seg for b in b_step for seg in b]
    flat_pair = [seg for b in b_pair for seg in b]
    check(flat_pair == flat_step == tl, "段不丢/不重/顺序不变（两种分块展平后相同）")
    pids = [b[0].get("pair") for b in b_pair[1:-1]]
    check(pids == list(range(1, n_pairs + 1)), "每组块按 pair 顺序", f"{pids}")

    print("=== 3) 非法取值回退 per_step ===")
    check(vc.group_timeline_blocks(tl, "bogus") == b_step, "未知 grouping → per_step")
    check(vc.group_timeline_blocks(tl, "") == b_step, "空 grouping → per_step")
    check(_resolve_block_grouping(_Args()) == "per_step", "无配置 → per_step")
    check(_resolve_block_grouping(_Args(sleep_block_grouping="per_pair")) == "per_pair",
          "配置 per_pair 生效")
    check(_resolve_block_grouping(_Args(sleep_block_grouping="weird")) == "per_step",
          "非法配置 → per_step")
    check(_resolve_block_grouping(_Args(sleep_block_grouping="PER_PAIR")) == "per_pair",
          "大小写不敏感")

    print("=== 4) 时间轴 1/fps 对齐（step=1.0s）===")
    tl_a = json.loads(json.dumps(tl))
    tl_b = json.loads(json.dumps(tl))
    _quantize_block_durations(tl_a, 1.0, "per_step")
    _quantize_block_durations(tl_b, 1.0, "per_pair")
    for name, tl_x, group in (("per_step", tl_a, "per_step"), ("per_pair", tl_b, "per_pair")):
        blocks = vc.group_timeline_blocks(tl_x, group)
        totals = [round(sum(float(s.get("duration", 0) or 0) for s in b), 3) for b in blocks]
        bad = [t for t in totals if abs(t - round(t)) > 1e-3]
        check(not bad, f"{name}: 每块都是 1s 整数倍", f"不合规 {bad[:3]}")
    add_a = round(sum(float(s["duration"]) for s in tl_a) - sum(float(s["duration"]) for s in tl), 3)
    add_b = round(sum(float(s["duration"]) for s in tl_b) - sum(float(s["duration"]) for s in tl), 3)
    check(add_b < add_a, "per_pair 的补齐量小于 per_step（块少了 5 倍）",
          f"per_step +{add_a}s vs per_pair +{add_b}s")
    check(len(vc.group_timeline_blocks(tl_b, "per_pair")) == 1 + n_pairs + 1,
          "对齐不改变块数")

    print("=== 5) resume 守卫：帧率 + 时长 ===")
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        check(True, "（无 ffmpeg，跳过）", "skip")
    else:
        def clip(name, fps, dur):
            p = TMP / name
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", f"color=c=black:s=128x72:d={dur}:r={fps}",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(fps),
                            str(p)], capture_output=True)
            return p
        short = clip("step_block.mp4", 1, 6)      # per_step 的块（~5s→对齐 6s）
        merged = clip("pair_block.mp4", 1, 26)    # per_pair 的块（~26s）
        check(vc._existing_block_ok(str(merged), 1, 26), "1fps/26s 块匹配 → 复用")
        check(not vc._existing_block_ok(str(merged), 1, 6),
              "同帧率但时长不符（per_pair 块 vs per_step 期望）→ 重建")
        check(not vc._existing_block_ok(str(short), 25, 6),
              "时长对但帧率不符 → 重建")
        check(not vc._existing_block_ok(str(TMP / "none.mp4"), 1, 6),
              "文件不存在 → 重建")

    print()
    if FAILS:
        print(f"===== 失败 {len(FAILS)} 项：{FAILS} =====")
        return 1
    print("===== 块划分测试全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
