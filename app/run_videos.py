"""运行目录「成片清单」与「源视频定位」的唯一事实源。

sleep 成片落盘位置历史上不统一：
  - compose 产出 1080p 成片   → {run}/videos/{标题}.mp4
    （pipeline/sleep/video_compose_sleep.py 的 final_path）
  - Step 5.5 混 BGM           → {run}/{标题}_bgm.mp4
  - Step 6 / 「生成4K」        → {run}/{标题}_4K.mp4、{run}/{标题}_4K_bgm.mp4
  - 片头/片尾库绑定素材副本     → {run}/videos/intro_video.mp4
    （旧版写在运行目录根部 {run}/intro_video.mp4，见 pipeline/pipeline.py）

因此各处「成片列表」口径必须一致，否则会出现
「运行目录里只有 1 个成片、画廊页却显示 2 个」这类自相矛盾；
而「源视频定位」若只扫根部，会把片头素材副本 intro_video.mp4
误当成成片（「混BGM」/「生成4K」的历史缺陷）。

本模块集中三件事，供 app/routers/runs.py 与 app/routers/pages.py 共用：
  1. iter_run_video_files   —— 什么算成片（根部 + videos/，剔除中间产物/素材副本）
  2. resolve_run_source_video —— 源视频怎么定位（先 videos/ 再根部，最后按类型取最新）
  3. classify_run_video / list_run_videos —— 条目怎么标注（类型 + 所在子目录 + 体积）
"""
from __future__ import annotations

import json
from pathlib import Path

# 非成片文件名前缀：中间产物 + 片头/片尾库素材副本（不是成片，不出现在列表里，
# 也绝不作为「混BGM / 生成4K」的源视频）
MATERIAL_PREFIXES = ("final_no_sub", "final_video_norm",
                     "intro_video", "outro_video")

# 类型 → (展示顺序, 中文标签)。顺序即列表展示顺序：原始成片 → BGM 版 → 4K → 4K BGM
VIDEO_KINDS: tuple[tuple[str, str], ...] = (
    ("final", "原始成片"),
    ("bgm", "BGM 版"),
    ("4k", "4K"),
    ("4k_bgm", "4K BGM"),
)
_KIND_ORDER = {key: i for i, (key, _label) in enumerate(VIDEO_KINDS)}


def is_material_copy(name: str) -> bool:
    """片头/片尾库素材副本或中间产物（不是成片）。"""
    return str(name).startswith(MATERIAL_PREFIXES)


def classify_run_video(name: str) -> tuple[str, str]:
    """文件名 → (类型 key, 中文标签)。

    依赖仓库既有的命名约定（同 runs.py::_resolve_video_copy_paths 的
    ``*_4K.mp4`` / ``*_4K_bgm.mp4`` 判定）：标题本身以 ``_bgm`` / ``_4K``
    结尾时会被归入派生类型，属已知限制。
    """
    n = str(name)
    if n.endswith("_4K_bgm.mp4"):
        return "4k_bgm", "4K BGM"
    if n.endswith("_4K.mp4"):
        return "4k", "4K"
    if n.endswith("_bgm.mp4"):
        return "bgm", "BGM 版"
    return "final", "原始成片"


def run_script(run_dir: Path | str | None) -> dict:
    """读取运行的 script.json（缺失/损坏返回空 dict，调用方按空值兜底）。"""
    if run_dir is None:
        return {}
    try:
        data = json.loads((Path(run_dir) / "script.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_mode_hint(run_dir: Path | str | None) -> str:
    """运行目录所属模式（新布局 output/{mode}/{run}；旧扁平布局返回空）。

    用作 find_run_dir / 视频 URL 的 mode 提示，消歧同名跨模式运行。
    """
    if run_dir is None:
        return ""
    try:
        from .config_manager import MODES
        parent = Path(run_dir).parent.name
        return parent if parent in MODES else ""
    except Exception:
        return ""


def iter_run_video_files(run_dir: Path | str | None) -> list[Path]:
    """运行目录内的成片候选（根部 + videos/ 子目录，按文件名去重）。

    同名同时存在于根部与 videos/ 时**优先根部**：与
    ``GET /api/runs/{name}/video/{filename}`` 的解析顺序一致
    （根部 → clips/ → videos/），避免「列表能点但播的不是同一个文件」。
    """
    if run_dir is None:
        return []
    base = Path(run_dir)
    out: list[Path] = []
    seen: set[str] = set()
    for folder in (base, base / "videos"):
        if not folder.is_dir():
            continue
        try:
            files = sorted(folder.glob("*.mp4"))
        except OSError:
            continue
        for f in files:
            if not f.is_file() or is_material_copy(f.name) or f.name in seen:
                continue
            seen.add(f.name)
            out.append(f)
    return out


def resolve_run_source_video(run_dir: Path | str | None,
                             safe_name: str = "",
                             target: str = "final",
                             include_derived: bool = False) -> Path | None:
    """定位成片源文件。

    target="final" → 1080p 原始成片（``videos/{标题}.mp4`` 优先）；
    target="4k"    → 4K 成片（``{标题}_4K.mp4``）。

    顺序：
      1. ``videos/{name}`` → ``{name}``（compose 权威输出在 videos/，旧版在根部）
      2. 全部成片候选中该类最新修改的一个（文件名无标题可用时兜底）
    ``include_derived=True`` 时，final 分支允许退化到「BGM 版」（供「生成4K」的
    旧行为兼容）；「混BGM」必须保持 False —— 否则会拿 BGM 版再叠一层 BGM。
    """
    if run_dir is None:
        return None
    base = Path(run_dir)
    want = "4k" if str(target).lower() == "4k" else "final"
    fname = f"{safe_name}_4K.mp4" if (safe_name and want == "4k") else \
        (f"{safe_name}.mp4" if safe_name else "")
    if fname:
        for folder in (base / "videos", base):
            cand = folder / fname
            if cand.is_file():
                return cand
    allowed = {want}
    if want == "final" and include_derived:
        allowed.add("bgm")
    pool = [p for p in iter_run_video_files(base)
            if classify_run_video(p.name)[0] in allowed]
    if not pool:
        return None
    try:
        return max(pool, key=lambda p: p.stat().st_mtime)
    except OSError:
        return pool[0]


def list_run_videos(run_dir: Path | str | None,
                    run_name: str = "",
                    mode: str = "") -> list[dict]:
    """成片清单（供 /api/runs、/runs 卡片、画廊页、dashboard 弹窗共用）。

    返回条目：``{name, kind, label, rel, url, size_mb}``。
    ``rel`` 为相对运行目录的路径（``videos/xxx.mp4`` / ``xxx.mp4``），
    用于界面显示「这个视频到底在哪」，消除「目录里 1 个、列表里 2 个」的困惑。
    """
    if run_dir is None:
        return []
    base = Path(run_dir)
    q = f"?mode={mode}" if mode else ""
    items: list[dict] = []
    for f in iter_run_video_files(base):
        kind, label = classify_run_video(f.name)
        try:
            size_mb = round(f.stat().st_size / (1024 * 1024), 1)
        except OSError:
            continue
        rel = f"videos/{f.name}" if f.parent.name == "videos" else f.name
        items.append({
            "name": f.name,
            "kind": kind,
            "label": label,
            "rel": rel,
            "size_mb": size_mb,
            "url": (f"/api/runs/{run_name}/video/{f.name}{q}"
                    if run_name else f"/api/runs/video/{f.name}{q}"),
        })
    items.sort(key=lambda it: (_KIND_ORDER.get(it["kind"], 99), it["name"]))
    return items
