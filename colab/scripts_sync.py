"""预生成脚本库（ai_scripts / ai_scripts_hot）与 Google Drive 之间的补缺同步。

背景：
    仓库里 `ai_scripts/`、`ai_scripts_hot/` 存的是「Step 0 已经生成的 sleep 脚本」
    （每个主题一个目录，内含 script.json + 各目录一个 manifest.json 索引），
    控制台「预生成脚本」页据此列出主题、选中即跳过 LLM 直接出片。
    本机新写的批次没 push 之前，Colab 上的克隆里看不到 —— 这就是 colab/setup.sh
    自检里那句提示所指的机制（原实现只写了提示、没有实现同步，本脚本把它补上）。

同步规则（只做加法，永不删除）：
    * 双向：`--pull` Drive→仓库（Colab 开机用）、`--push` 仓库→Drive（本机用）；
    * 只拷「目标缺失 / 源更新（mtime 新 >1s）/ 尺寸不同且源不旧」的文件；
    * 跳过 `_` 与 `.` 前缀目录（`_recycle_bin`、`.llm_cache`、`_parts` 等）与
      `checkpoint.json`（记的是本机 run 路径，没有任何代码读它）——与 .gitignore、
      app/ai_scripts.py 的约定一致；
    * manifest.json 同样按「谁新用谁」，因此本机新加主题 push 后，Colab pull 到的是
      仓库里那份更新的索引。

用法：
    # Colab（notebook 已封装，DRIVE_ROOT 由 env 传入）
    python3 colab/scripts_sync.py --pull
    # 本机 Windows（Google Drive 桌面版挂载盘符自行替换）
    python colab/scripts_sync.py --push --drive-root "G:\\我的云端硬盘\\sleep_english_colab"
    python colab/scripts_sync.py --status --drive-root "..."
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_DIR_DEFAULT = HERE.parent
DRIVE_SUBDIR = "scripts"
SOURCE_DIRS = ("ai_scripts", "ai_scripts_hot")
SKIP_NAMES = {"checkpoint.json"}
SKIP_PREFIXES = ("_", ".")
MAX_FILE_MB = 16

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


def log(msg: str) -> None:
    print(f"[scripts_sync] {msg}", flush=True)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or "").strip() or default


def drive_scripts_root(drive_root: Path) -> Path:
    return Path(drive_root) / DRIVE_SUBDIR


def _skippable(rel: Path) -> bool:
    if any(part.startswith(SKIP_PREFIXES) for part in rel.parts):
        return True
    return rel.name in SKIP_NAMES


def _should_copy(src: Path, dst: Path) -> bool:
    """目标缺失 / 源更新 / 尺寸不同且源不旧 → 需要拷。"""
    try:
        if not dst.is_file():
            return True
        s, d = src.stat(), dst.stat()
        if s.st_size == d.st_size and s.st_mtime <= d.st_mtime + 1:
            return False
        return s.st_mtime > d.st_mtime + 1 or s.st_size != d.st_size
    except OSError:
        return True


def sync_tree(src_root: Path, dst_root: Path, dry_run: bool = False) -> dict:
    """把 src_root 下的文件按补缺规则拷进 dst_root；返回统计。"""
    stats = {"copied": 0, "kept": 0, "failed": 0, "skipped_big": 0}
    if not src_root.is_dir():
        return stats
    for src in sorted(src_root.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(src_root)
        if _skippable(rel):
            continue
        try:
            size_mb = src.stat().st_size / 1024 / 1024
        except OSError:
            continue
        if size_mb > MAX_FILE_MB:
            stats["skipped_big"] += 1
            log(f"  skip 超大文件 {rel}（{size_mb:.1f}MB > {MAX_FILE_MB}MB）")
            continue
        dst = dst_root / rel
        if not _should_copy(src, dst):
            stats["kept"] += 1
            continue
        if dry_run:
            stats["copied"] += 1
            log(f"  (dry) {rel}")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            stats["copied"] += 1
        except OSError as e:
            stats["failed"] += 1
            log(f"  !! 拷贝失败 {rel}：{e}")
    return stats


def pull_all(drive_root: Path, repo_dir: Path | None = None,
             dry_run: bool = False) -> int:
    """Drive → 仓库（Colab 开机补缺）；返回拷贝文件数。"""
    repo = Path(repo_dir or _env("REPO_DIR") or REPO_DIR_DEFAULT).resolve()
    root = drive_scripts_root(drive_root)
    total = 0
    for name in SOURCE_DIRS:
        src = root / name
        if not src.is_dir():
            continue
        st = sync_tree(src, repo / name, dry_run=dry_run)
        total += st["copied"]
        if st["copied"] or st["failed"]:
            log(f"  拉取 {name}：新增/更新 {st['copied']} · 已是最新 {st['kept']}"
                f"{' · 失败 ' + str(st['failed']) if st['failed'] else ''}")
    return total


def push_all(drive_root: Path, repo_dir: Path | None = None,
             dry_run: bool = False) -> int:
    """仓库 → Drive（本机用）；返回拷贝文件数。"""
    repo = Path(repo_dir or _env("REPO_DIR") or REPO_DIR_DEFAULT).resolve()
    root = drive_scripts_root(drive_root)
    total = 0
    for name in SOURCE_DIRS:
        src = repo / name
        if not src.is_dir():
            continue
        st = sync_tree(src, root / name, dry_run=dry_run)
        total += st["copied"]
        log(f"  推送 {name}：新增/更新 {st['copied']} · 已是最新 {st['kept']}"
            f"{' · 失败 ' + str(st['failed']) if st['failed'] else ''}")
    return total


def _count_scripts(root: Path) -> dict:
    out = {}
    for name in SOURCE_DIRS:
        base = root / name
        n = 0
        if base.is_dir():
            n = sum(1 for p in base.glob("*/script.json")
                    if not p.parent.name.startswith(SKIP_PREFIXES))
        out[name] = n
    return out


def status(drive_root: Path, repo_dir: Path | None = None) -> None:
    repo = Path(repo_dir or _env("REPO_DIR") or REPO_DIR_DEFAULT).resolve()
    root = drive_scripts_root(drive_root)
    log(f"仓库 {repo}")
    log(f"Drive {root}{'' if root.parent.is_dir() else '（父目录不存在 = 未挂载）'}")
    repo_counts = _count_scripts(repo)
    drive_counts = _count_scripts(root) if root.is_dir() else {}
    for name in SOURCE_DIRS:
        log(f"  {name}: 仓库 {repo_counts.get(name, 0)} 个脚本"
            f" · Drive {drive_counts.get(name, 0) if drive_counts else '—'} 个")


def main() -> int:
    ap = argparse.ArgumentParser(description="预生成脚本库 ⇄ Google Drive 补缺同步")
    ap.add_argument("--pull", action="store_true", help="Drive → 仓库（Colab 开机用）")
    ap.add_argument("--push", action="store_true", help="仓库 → Drive（本机用）")
    ap.add_argument("--status", action="store_true", help="只打印两边脚本数量")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要拷贝的文件")
    ap.add_argument("--drive-root", default="", help="Drive 上的项目目录（默认 env DRIVE_ROOT）")
    ap.add_argument("--repo-dir", default="", help="仓库目录（默认 env REPO_DIR 或脚本上一级）")
    args = ap.parse_args()

    drive_root = Path(args.drive_root or _env(
        "DRIVE_ROOT", "/content/drive/MyDrive/sleep_english_colab")).resolve()
    repo = Path(args.repo_dir or _env("REPO_DIR") or REPO_DIR_DEFAULT).resolve()

    if not any((args.pull, args.push, args.status)):
        args.pull = True

    if args.status:
        status(drive_root, repo)
        return 0
    if not drive_root.parent.is_dir():
        log(f"!! Drive 没挂载（{drive_root.parent} 不存在）—— 跳过同步")
        return 0

    if args.pull:
        n = pull_all(drive_root, repo, dry_run=args.dry_run)
        log(f"拉取完成：新增/更新 {n} 个文件 → {repo}")
    if args.push:
        n = push_all(drive_root, repo, dry_run=args.dry_run)
        log(f"推送完成：新增/更新 {n} 个文件 → {drive_scripts_root(drive_root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
