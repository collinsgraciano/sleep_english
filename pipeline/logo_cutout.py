"""logo_cutout.py — 频道 Logo 上画面（成片/预览）前的本地智能抠图。

消费端（video_compose_sleep._logo_overlay 的 FFmpeg overlay 与
config._stamp_logo_on_card 的 Pillow alpha_composite）都是整图直接叠加，
Logo 自带的纯色底会一起贴上画面。本模块在 Logo 进入叠加环节前把它抠成
透明底：纯色/浅色底检测 + 连通域过滤（只删与边缘连通的背景，Logo 内部
同色区域不误删）+ 边缘羽化去晕边。

设计约束：
- 保守策略——不适合抠（已透明/非纯色底/前景占比异常/任何异常）一律返回
  原路径，行为退化为现状整图叠加，绝不影响成片流程。
- sidecar 缓存（<同目录>/.logo_cutout/），源文件 mtime+size+算法版本
  任一变化即重算（沿用 .durations.json sidecar 校验惯例）。
- pipeline 自包含：不 import app；scipy 惰性导入（首次约 1s，只在真正
  需要抠图时付出）。"""

import json
import os
from pathlib import Path

import numpy as np

ALGO_VERSION = 1
_MAX_SIDE = 2048           # 处理上限（性能保护；成片叠加只用到 ~96px@720p）
_BORDER_ALPHA_RATIO = 0.30  # 边缘一圈 alpha<10 占比超过该值 → 视为已透明
_BG_UNIFORM_RATIO = 0.90    # 边缘环内与底色相近占比低于该值 → 非纯色底
_TOL_LOW = 26.0             # 与底色 RGB 距离低于该值 → 完全背景
_TOL_HIGH = 60.0            # 软过渡带上限（[TOL_LOW, TOL_HIGH) 内按距离渐变）
_FG_MIN_RATIO = 0.02        # 前景占比下限（全被抠掉=误判保护）
_FG_MAX_RATIO = 0.98        # 前景占比上限（几乎没抠掉=误判保护）


def _edge_alpha_transparent_ratio(arr) -> float:
    """图像最外圈像素中 alpha<10 的占比（arr: HxWx4 uint8）。"""
    a = arr[..., 3]
    edge = np.concatenate([a[0, :], a[-1, :], a[:, 0], a[:, -1]])
    return float((edge < 10).mean())


def _detect_bg_color(rgb) -> np.ndarray | None:
    """从边缘环采样判定纯色底色；非纯色底返回 None。

    采样环宽 2px，按 16 级量化取众数 bin 的均值为底色候选；
    环内与底色距离 < TOL_HIGH 的占比不足 → 判非纯色底（保守不抠）。"""
    ring = np.concatenate([rgb[:2, :].reshape(-1, 3), rgb[-2:, :].reshape(-1, 3),
                           rgb[:, :2].reshape(-1, 3), rgb[:, -2:].reshape(-1, 3)])
    q = (ring // 16).astype(np.int64)
    keys = q[:, 0] * 4096 + q[:, 1] * 64 + q[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    bg = ring[keys == values[counts.argmax()]].mean(axis=0)
    dist = np.linalg.norm(ring.astype(float) - bg, axis=1)
    if float((dist < _TOL_HIGH).mean()) < _BG_UNIFORM_RATIO:
        return None
    return bg


def cutout_logo_image(img):
    """核心抠图：PIL Image → 透明底 RGBA Image；不适合抠返回 None。

    步骤：已透明检测 → 纯色底判定 → 距离阈值两级（完全背景/软过渡带）→
    连通域只保留与边缘连通的背景（Logo 内部同色区域不误删）→ 外扩 1px
    吃晕边 + 过渡带渐变 + 轻羽化 → 前景占比保护。"""
    from PIL import Image
    from scipy import ndimage

    img = img.convert("RGBA")
    if img.width > _MAX_SIDE or img.height > _MAX_SIDE:
        img.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)
    arr = np.asarray(img)
    if _edge_alpha_transparent_ratio(arr) > _BORDER_ALPHA_RATIO:
        return None  # 已是透明底（或半透明素材），保持原样
    rgb = arr[..., :3].astype(float)
    bg = _detect_bg_color(rgb)
    if bg is None:
        return None
    dist = np.linalg.norm(rgb - bg, axis=2)

    # 完全背景候选 → 连通域标记 → 只保留与图像边缘连通的域
    full_bg = dist < _TOL_LOW
    labels, n = ndimage.label(full_bg)
    if n == 0:
        return None
    border_labels = np.unique(np.concatenate(
        [labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]]))
    border_labels = border_labels[border_labels != 0]
    if border_labels.size == 0:
        return None
    bg_core = np.isin(labels, border_labels)

    # 背景外扩 1px 吃掉抗锯齿浅色晕边；过渡带（贴背景的 band 像素）
    # 按颜色距离软过渡，避免硬锯齿；最后轻羽化平滑边缘
    bg_grow = ndimage.binary_dilation(bg_core, iterations=1)
    alpha = np.where(bg_grow, 0.0, 255.0)
    near_bg = ndimage.binary_dilation(bg_grow, iterations=1)
    band_soft = (dist >= _TOL_LOW) & (dist < _TOL_HIGH) & ~bg_grow & near_bg
    if band_soft.any():
        t = np.clip((dist - _TOL_LOW) / (_TOL_HIGH - _TOL_LOW), 0.0, 1.0)
        alpha = np.where(band_soft, t * 255.0, alpha)
    alpha = ndimage.gaussian_filter(alpha / 255.0, sigma=0.6) * 255.0
    alpha = np.minimum(alpha, arr[..., 3].astype(float))  # 不放大原 alpha

    fg_ratio = float((alpha > 32).mean())
    if fg_ratio < _FG_MIN_RATIO or fg_ratio > _FG_MAX_RATIO:
        return None  # 抠过头/没抠到 → 误判，保守放弃

    out = np.empty((*arr.shape[:2], 4), dtype=np.uint8)
    out[..., :3] = arr[..., :3]
    out[..., 3] = alpha.astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _cache_paths(src: Path) -> tuple[Path, Path]:
    """sidecar 缓存路径：<同目录>/.logo_cutout/<stem>_cutout.png + .json。"""
    cache_dir = src.parent / ".logo_cutout"
    return (cache_dir / f"{src.stem}_cutout{src.suffix or '.png'}",
            cache_dir / f"{src.stem}_cutout.json")


def cutout_path_for(logo_path: str | Path) -> Path:
    """给定源 Logo 路径 → 抠图缓存路径（文件可能不存在，调用方自判）。"""
    return _cache_paths(Path(logo_path))[0]


def _cache_valid(meta_path: Path, src: Path) -> bool:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        st = src.stat()
        return (meta.get("algo") == ALGO_VERSION
                and meta.get("mtime") == st.st_mtime
                and meta.get("size") == st.st_size)
    except Exception:
        return False


def ensure_logo_cutout(logo_path: str | Path, force: bool = False) -> str:
    """确保 Logo 为透明底版本，返回可直接用于叠加的路径。

    缓存命中（源 mtime+size+算法版本一致）→ 直接返回缓存路径；
    现抠成功 → 写 sidecar 缓存并返回缓存路径；
    不适合抠 / 任何异常 → 返回原路径（调用方零风险）。"""
    src = Path(logo_path)
    if not src.is_file():
        return str(logo_path)
    cache_png, cache_meta = _cache_paths(src)
    if not force and _cache_valid(cache_meta, src):
        return str(cache_png)
    try:
        from PIL import Image

        with Image.open(src) as img:
            img.load()
        cut = cutout_logo_image(img)
        if cut is None:
            return str(logo_path)
        cache_png.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_png.with_name(cache_png.name + ".tmp")
        cut.save(tmp, "PNG")
        os.replace(tmp, cache_png)
        st = src.stat()
        cache_meta.write_text(json.dumps(
            {"algo": ALGO_VERSION, "mtime": st.st_mtime,
             "size": st.st_size, "src": src.name},
            ensure_ascii=False), encoding="utf-8")
        fg = float((np.asarray(cut)[..., 3] > 32).mean())
        print(f"  [LogoCutout] {src.name}: OK (fg {fg:.0%}) -> {cache_png.parent.name}/")
        return str(cache_png)
    except Exception as e:  # noqa: BLE001 — 抠图失败退化为原图叠加，不阻断流程
        print(f"  [LogoCutout] {src.name}: FAILED, fallback to raw: {e}")
        return str(logo_path)
