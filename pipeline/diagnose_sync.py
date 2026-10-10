"""A/V sync diagnosis for sleep runs (read-only).

For a run dir, answers: **which card is on screen at time t** and **which pair's
speech is playing at time t**, then compares both with the planned timeline.

Outputs (per pair block):
  - 计划边界（meta 累计时间轴）
  - 实际换卡时刻（帧匹配：显示卡片发生变化的时间）
  - 实际语音起点（音频 RMS 分段）
  - 偏差（换卡 − 计划 / 换卡 − 语音）

Usage:
  python pipeline/diagnose_sync.py <run_dir> [--fps 4] [--card-suffix _4k]
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

CARD_W, CARD_H = 64, 36
SR = 8000


def load_cards(run_dir: Path, prefer_suffix: str = "") -> list[tuple[str, np.ndarray]]:
    """返回 [(名字, 64x36 灰度数组)]，按卡片编号排序。"""
    cards = []
    for p in sorted((run_dir / "cards").glob("card_*.png")):
        if prefer_suffix and prefer_suffix not in p.name:
            # 先收 4K 卡；没有的再用非 4K
            continue
        im = Image.open(p).convert("L").resize((CARD_W, CARD_H))
        cards.append((p.name, np.asarray(im, dtype=np.float32)))
    if not cards:
        for p in sorted((run_dir / "cards").glob("card_*.png")):
            im = Image.open(p).convert("L").resize((CARD_W, CARD_H))
            cards.append((p.name, np.asarray(im, dtype=np.float32)))
    return cards


def frames_gray(video: Path, fps: int) -> np.ndarray:
    """一次 ffmpeg 流式导出：每秒 fps 张 64x36 灰度帧 → (N, 36, 64)。"""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={fps}",
           "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{CARD_W}x{CARD_H}", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-300:])
    buf = np.frombuffer(r.stdout, dtype=np.uint8)
    n = buf.size // (CARD_W * CARD_H)
    return buf[: n * CARD_W * CARD_H].reshape(n, CARD_H, CARD_W).astype(np.float32)


def match_cards(frames: np.ndarray, cards: list[tuple[str, np.ndarray]]) -> list[int]:
    """每帧匹配最像的卡片下标。"""
    out = []
    for f in frames:
        best, bi = 1e18, -1
        for i, (_, c) in enumerate(cards):
            d = float(np.mean(np.abs(f - c)))
            if d < best:
                best, bi = d, i
        out.append(bi)
    return out


def speech_segments(video: Path, win_ms: int = 100, thresh_db: float = -45.0):
    """粗粒度语音分段（50ms 窗口 RMS 阈值 + 最短静音 0.4s）。"""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
           "-ar", str(SR), "-f", "s16le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        return []
    audio = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    w = int(SR * win_ms / 1000)
    n = audio.size // w
    if n == 0:
        return []
    rms = np.sqrt(np.mean(audio[: n * w].reshape(n, w) ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-12)
    voiced = db > thresh_db
    segs, start = [], None
    gap_need = int(400 / win_ms)          # 400ms 静音才断句
    silent = 0
    for i, v in enumerate(voiced):
        t = i * win_ms / 1000.0
        if v:
            if start is None:
                start = t
            silent = 0
        elif start is not None:
            silent += 1
            if silent >= gap_need:
                segs.append((round(start, 3), round(t - silent * win_ms / 1000.0, 3)))
                start = None
                silent = 0
    if start is not None:
        segs.append((round(start, 3), round(n * win_ms / 1000.0, 3)))
    return segs


def planned_blocks(meta: dict) -> list[dict]:
    """按 meta 的 sleep_block_grouping 还原块 → (label, 起点, 时长)。"""
    tl = meta.get("timeline", [])
    grp = str(meta.get("sleep_block_grouping", "per_step"))
    blocks, cur = [], []
    if grp == "per_pair":
        for s in tl:
            if s.get("type") in ("intro", "outro"):
                if cur:
                    blocks.append(cur)
                    cur = []
                blocks.append([s])
                continue
            if cur and cur[0].get("pair", 0) != s.get("pair", 0):
                blocks.append(cur)
                cur = []
            cur.append(s)
        if cur:
            blocks.append(cur)
    else:
        for s in tl:
            ty = s.get("type")
            if ty in ("intro", "outro"):
                if cur:
                    blocks.append(cur)
                    cur = []
                blocks.append([s])
            elif ty == "pair":
                cur = [s]
            elif ty == "gap":
                cur.append(s)
                blocks.append(cur)
                cur = []
        if cur:
            blocks.append(cur)
    out, t = [], 0.0
    for b in blocks:
        dur = round(sum(float(x.get("duration", 0) or 0) for x in b), 3)
        head = b[0]
        label = head.get("type") if head.get("type") in ("intro", "outro") else \
            f"pair{head.get('pair')}"
        out.append({"label": label, "start": round(t, 3), "dur": dur})
        t += dur
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--fps", type=int, default=4, help="抽帧密度（帧/秒），默认 4")
    ap.add_argument("--card-suffix", default="_4k")
    ap.add_argument("--video", default="", help="指定成片路径（默认取 videos/ 第一个）")
    args = ap.parse_args()

    run = Path(args.run_dir)
    video = Path(args.video) if args.video else next(iter(sorted((run / "videos").glob("*.mp4"))))
    meta = json.loads((run / "subtitles" / "meta.json").read_text(encoding="utf-8"))
    cards = load_cards(run, args.card_suffix)
    print(f"run      : {run}")
    print(f"video    : {video.name}")
    print(f"cards    : {[c[0] for c in cards]}")
    print(f"meta     : card_fps={meta.get('sleep_card_fps')} grouping={meta.get('sleep_block_grouping')}")

    blocks = planned_blocks(meta)
    print(f"计划块数 : {len(blocks)}  总时长={round(sum(b['dur'] for b in blocks), 3)}s")

    frames = frames_gray(video, args.fps)
    print(f"抽帧     : {frames.shape[0]} 帧（{args.fps} fps → {frames.shape[0]/args.fps:.1f}s）")
    idx = match_cards(frames, cards)
    changes = [(round(i / args.fps, 3), cards[idx[i]][0]) for i in range(1, len(idx))
               if idx[i] != idx[i - 1]]
    segs = speech_segments(video)
    print(f"换卡次数 : {len(changes)}   语音段: {len(segs)}")

    print("\n  {:<8} {:>9} {:>10} {:>10} {:>11} {:>11}".format(
        "块", "计划起点", "换卡时刻", "语音起点", "换卡-计划", "换卡-语音"))
    worst = 0.0
    for b in blocks:
        if b["label"] == "intro":
            continue
        # 该块起点之后最近的换卡 / 语音起点
        ch = next((t for t, _ in changes if t >= b["start"] - 0.6), None)
        sp = next((s for s, _ in segs if s >= b["start"] - 0.6), None)
        d1 = (ch - b["start"]) if ch is not None else float("nan")
        d2 = (ch - sp) if (ch is not None and sp is not None) else float("nan")
        if ch is not None:
            worst = max(worst, abs(d2) if d2 == d2 else 0.0)
        print("  {:<8} {:>9.3f} {:>10} {:>10} {:>11} {:>11}".format(
            b["label"], b["start"],
            f"{ch:.3f}" if ch is not None else "-",
            f"{sp:.3f}" if sp is not None else "-",
            f"{d1:+.3f}" if d1 == d1 else "-",
            f"{d2:+.3f}" if d2 == d2 else "-"))
    print(f"\n最大 |换卡 − 语音起点| = {worst:.3f}s（1/fps = {1.0/(meta.get('sleep_card_fps') or 25):.3f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
