"""成品归档 + 运行状态备份到 Google Drive。

Colab 的本地盘随 VM 回收清空，所以「跑完的期」必须自己搬到 Drive：
  1. 成品归档：<output>/sleep/<run>/ → MyDrive/<DRIVE_DIR>/output/<run>/
     只拷成品（videos/*.mp4、thumbnail.jpg、subtitles/、script.json、youtube_metadata.json…），
     跳过 audio/ cards/ clips/ images/ tmp_sleep_blocks/ 这些体积大、只在断点续传时有用的中间素材
     —— 一期上千个卡片/音频小文件写 Drive FUSE 会慢到不可用，所以渲染固定在本地盘。
  2. 状态备份：used_topics.json / .thumb_episode.json / .sleep_cache/ 反向回写 Drive state/
     （映射表来自 drive_config.py，开机由它恢复，保证主题防重与集数徽章跨会话连续）

Web 控制台里跑完的期次同样用本脚本归档；命令行格跑完也调它。可重复执行（幂等）。
另外 archive_all() 是给 app/pipeline_service.py 的收尾钩子直接调用的入口：Colab 上
（COLAB_AUTO_ARCHIVE=1）控制台跑完一期会自动归档，不用记着再点一格 —— 「忘了归档 →
VM 回收 → 成品没了」是这套流程最常踩的坑。

用法：
    python3 colab/archive_to_drive.py                # 成品 + 状态
    python3 colab/archive_to_drive.py --state-only   # 只备份状态（很快）
    python3 colab/archive_to_drive.py --products-only
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# REPO_DIR 默认＝本脚本的上一级（Colab 由 env 传 /content/sleep_english）；
# 本机 Windows 上直接跑（例如手工补归档）也能 import 到 app/。
REPO_DIR = Path(os.environ.get("REPO_DIR") or HERE.parent).resolve()
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from drive_config import STATE_DIRS, STATE_FILES, drive_paths, log  # noqa: E402
from app.config_manager import iter_run_dirs  # noqa: E402

# 只保留成品：中间素材在 Drive 上又慢又没用（重跑靠本地/Range 下载）
SKIP_DIRS = {"audio", "cards", "clips", "images", "tmp_sleep_blocks"}


def _ignore(_dir, names):
    return [n for n in names if n in SKIP_DIRS or n.startswith(".")]


def _already_archived(src: Path, dst: Path) -> bool:
    """dst 里已有同一批成品 mp4（名字+大小一致）→ 认为这期归档过了，不必再拷几 GB。"""
    src_videos, dst_videos = src / "videos", dst / "videos"
    if not src_videos.is_dir() or not dst_videos.is_dir():
        return False
    mp4s = [f for f in src_videos.iterdir() if f.suffix == ".mp4"]
    if not mp4s:
        return False
    for f in mp4s:
        target = dst_videos / f.name
        if not target.is_file() or target.stat().st_size != f.stat().st_size:
            return False
    return True


def _archive_products(archive_dir: Path, force: bool = False) -> int:
    archive_dir.mkdir(parents=True, exist_ok=True)
    runs = iter_run_dirs(os.environ.get("COLAB_OUTPUT_DIR", "/content/sleep_english_output"))
    if not runs:
        log(f"{os.environ.get('COLAB_OUTPUT_DIR')} 下还没有 run —— 先出一期再归档")
        return 0
    for src in runs:
        videos = src / "videos"
        mp4s = [f for f in videos.iterdir() if f.suffix == ".mp4"] if videos.is_dir() else []
        dst = archive_dir / src.name
        if not force and _already_archived(src, dst):
            log(f"SKIP {src.name[:58]} | 已在 Drive（要强制重拷加 --force）")
            continue
        t0 = time.time()
        shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore)
        flag = "OK " if mp4s else "!! 无成品 mp4（可能还没跑完）"
        log(f"{flag} {src.name[:58]} | mp4 {len(mp4s)} | {time.time() - t0:.0f}s")
    log(f"成品归档目录：{archive_dir}（{len(runs)} 个 run）")
    return len(runs)


def _copy_newer(src: Path, dst: Path) -> bool:
    """src 比 dst 新（或 dst 缺失）才拷；拷成功返回 True。"""
    if not src.is_file():
        return False
    try:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime \
                and dst.stat().st_size == src.stat().st_size:
            return False
    except OSError:
        pass
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _backup_state(drive_state: Path, output_dir: Path) -> int:
    drive_state.mkdir(parents=True, exist_ok=True)
    n = 0
    for drive_name, local_rel in STATE_FILES:
        if _copy_newer(output_dir / local_rel, drive_state / drive_name):
            n += 1
            log(f"  备份 state/{drive_name}")
    for drive_name, local_rel in STATE_DIRS:
        src_dir = output_dir / local_rel
        if not src_dir.is_dir():
            continue
        copied = 0
        for f in sorted(src_dir.rglob("*")):
            if f.is_dir() or f.name.startswith("."):
                continue
            if _copy_newer(f, drive_state / drive_name / f.relative_to(src_dir)):
                copied += 1
        n += copied
        if copied:
            log(f"  备份 state/{drive_name}/：{copied} 个文件")
    log(f"状态备份：{n} 个文件 → {drive_state}")
    return n


def archive_all(force: bool = False, products: bool = True, state: bool = True) -> dict:
    """成品 + 运行状态一起归档，返回统计（供 CLI 与 Web 控制台收尾钩子共用）。

    与 main() 判据完全一致，只是入口不同：驱动它的可能是 notebook 的一个单元格，
    也可能是 app/pipeline_service.py 的出片收尾钩子（COLAB_AUTO_ARCHIVE=1 时）——
    后者解决的是「跑完忘了点归档，VM 一回收成品就没了」这条最常见的数据丢失路径。
    """
    drive_root, _drive_cfg, drive_state = drive_paths()
    output_dir = Path(os.environ.get("COLAB_OUTPUT_DIR", "/content/sleep_english_output"))

    if not drive_root.parent.is_dir():
        log(f"!! Drive 没挂载（{drive_root.parent} 不存在）—— 归档跳过，成品只在容器本地")
        return {"ok": False, "reason": "drive_not_mounted", "runs": 0, "state_files": 0}

    log(f"输出目录 {output_dir}")
    runs = _archive_products(drive_root / "output", force=force) if products else 0
    files = _backup_state(drive_state, output_dir) if state else 0
    log("归档完成。下次开机：成品在 Drive，配置与状态自动恢复。")
    return {"ok": True, "runs": runs, "state_files": files}


def main() -> int:
    ap = argparse.ArgumentParser(description="成品与运行状态归档到 Google Drive")
    ap.add_argument("--products-only", action="store_true", help="只归档成品")
    ap.add_argument("--state-only", action="store_true", help="只备份运行状态")
    ap.add_argument("--force", action="store_true", help="成品已在 Drive 也重拷一遍")
    args = ap.parse_args()

    r = archive_all(force=args.force,
                    products=not args.state_only,
                    state=not args.products_only)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
