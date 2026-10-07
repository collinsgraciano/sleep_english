"""Colab 配置持久化：把仓库 configs/ 整个落到 Google Drive，开机再恢复运行状态。

为什么必须有它（这一版和以往 Colab 版的关键差异）：
    以往每次开机都用 Secrets 重新生成 `configs/mode_sleep.json`，于是 Web 控制台里
    调过的参数（组数 / 配色 / 字体 / 频道名 / 片头绑定 / 频道矩阵…）随 VM 回收一起
    消失。现在把 `configs/` 整目录搬进 Drive 再 symlink 回克隆目录：app 的一切配置
    读写都经过 `app/config_manager.py` 的 `CONFIGS_DIR = WEB_ROOT/"configs"`，落到
    symlink 上就等于直接写 Drive —— 零代码改动拿到全量持久化。

六件事：
    1. 播种：仓库 `configs/` 里「Drive 上还没有」的文件逐项拷过去（绝不覆盖用户改过的）
    2. 链接：`rm -rf <repo>/configs && ln -s <Drive>/configs <repo>/configs`
    3. 恢复：Drive `state/` 里的 used_topics.json / .thumb_episode.json / .sleep_cache
             拷回本地输出目录（本地盘每次开机都是空的，集数编号与主题防重要接着数）
    4. 令牌：读或生成访问令牌写进 env 文件 —— 隧道域名每次开机都会换，令牌保持不变
    5. 目录：建好 `<Drive>/kokoro/voices/`（Kokoro 音色权重持久化）与 `<Drive>/scripts/`
    6. 脚本：`COLAB_SCRIPTS_SYNC=1`（默认）时把 Drive 上更新的预生成脚本补进仓库

Drive 没挂 / 没授权时**不阻塞**：打一条醒目警告，降级为「配置只在本次会话有效」，
出片链路照常。

用法（notebook 的「配置落 Drive」格已封装）：
    REPO_DIR=/content/sleep_english \
    DRIVE_ROOT=/content/drive/MyDrive/sleep_english_colab \
    COLAB_OUTPUT_DIR=/content/sleep_english_output \
    ENV_FILE=/content/colab_env.sh \
        python3 colab/drive_config.py

顺带（同一次调用里做完，避免 notebook 再多一步）：
    * 建好 `<Drive>/kokoro/voices/`（Kokoro 全音色权重的持久化目录，
      由 colab/kokoro_voices.py 负责同步与试听生成）；
    * `COLAB_SCRIPTS_SYNC=1`（默认）时把 `<Drive>/scripts/{ai_scripts,ai_scripts_hot}/`
      里更新的预生成脚本补进仓库（colab/scripts_sync.py）；
    * 把 COLAB_KOKORO_DIR / COLAB_AUTO_ARCHIVE / COLAB_SCRIPTS_SYNC 写进 env 文件，
      供 serve.sh 起来的 Web 控制台与命令行出片共用。
"""
import os
import secrets
import shlex
import shutil
import sys
from pathlib import Path

REPO_DIR = Path(os.environ.get("REPO_DIR", "/content/sleep_english")).resolve()
HERE = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("COLAB_OUTPUT_DIR", "/content/sleep_english_output")).resolve()
DRIVE_ROOT = Path(os.environ.get(
    "DRIVE_ROOT", "/content/drive/MyDrive/sleep_english_colab")).resolve()
ENV_FILE = Path(os.environ.get("ENV_FILE", "/content/colab_env.sh"))

# Drive 上 state/ 与本地输出目录的对应关系（archive_to_drive.py 反向复用本表）：
#   state/used_topics.json      ↔ <output>/used_topics.json          （主题防重）
#   state/thumb_episode.json    ↔ <output>/sleep/.thumb_episode.json （集数徽章计数）
#   state/sleep_cache/          ↔ <output>/sleep/.sleep_cache/       （LLM 批次缓存）
STATE_FILES = (
    ("used_topics.json", "used_topics.json"),
    ("thumb_episode.json", "sleep/.thumb_episode.json"),
)
STATE_DIRS = (
    ("sleep_cache", "sleep/.sleep_cache"),
)

_SKIP_PARTS = {"__pycache__", ".ipynb_checkpoints", ".DS_Store", "Thumbs.db"}


def log(msg: str) -> None:
    print(f"[drive_config] {msg}", flush=True)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or "").strip() or default


def _flag(name: str, default: bool = True) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def drive_paths() -> tuple[Path, Path, Path]:
    """返回 (drive_root, drive_configs, drive_state)。"""
    return DRIVE_ROOT, DRIVE_ROOT / "configs", DRIVE_ROOT / "state"


def kokoro_dir() -> Path:
    """Drive 上的 Kokoro 音色权重目录（colab/kokoro_voices.py 的事实源）。"""
    return Path(_env("COLAB_KOKORO_DIR") or (DRIVE_ROOT / "kokoro"))


def scripts_root() -> Path:
    """Drive 上的预生成脚本镜像根（colab/scripts_sync.py 的事实源）。"""
    return Path(_env("COLAB_SCRIPTS_ROOT") or (DRIVE_ROOT / "scripts"))


def _skippable(p: Path) -> bool:
    return any(part in _SKIP_PARTS or part.startswith(".") and part.endswith(".tmp")
               for part in p.parts)


def seed_missing(src_root: Path, dst_root: Path) -> int:
    """把 src_root 下 dst_root 里还不存在的文件拷过去（存在即跳过，绝不覆盖）。

    只补缺：Drive 是用户配置的事实源，仓库升级带来的「新默认文件」能补进去，
    用户在 Drive 上改过的内容不会被仓库里的同名旧文件顶掉。
    """
    if not src_root.is_dir():
        return 0
    copied = 0
    for src in sorted(src_root.rglob("*")):
        if _skippable(src):
            continue
        dst = dst_root / src.relative_to(src_root)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)   # 空目录也补上
            continue
        if dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        except OSError as e:
            log(f"  !! 播种失败 {src} → {dst}：{e}")
    return copied


def restore_missing(src: Path, dst: Path, label: str) -> int:
    """Drive → 本地（只补缺，本地已有的更新版本不动）。文件或目录都吃。"""
    if src.is_dir():
        if not src.exists():
            return 0
        n = 0
        for f in sorted(src.rglob("*")):
            if _skippable(f) or f.is_dir():
                continue
            target = dst / f.relative_to(src)
            if target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, target)
                n += 1
            except OSError as e:
                log(f"  !! 恢复失败 {f} → {target}：{e}")
        if n:
            log(f"  恢复 {label}：{n} 个文件")
        return n
    if not src.is_file():
        return 0
    if dst.exists():
        return 0
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        log(f"  恢复 {label} → {dst}")
        return 1
    except OSError as e:
        log(f"  !! 恢复失败 {src} → {dst}：{e}")
        return 0


def set_env_var(env_file: Path, key: str, value: str) -> None:
    """把 export KEY=value 写进 env 文件（同键覆盖，不重复追加）。"""
    lines: list[str] = []
    if env_file.exists():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    lines = [ln for ln in lines if not ln.startswith(f"export {key}=")]
    lines.append(f"export {key}={shlex.quote(str(value))}")
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_or_create_token(token_file: Path) -> tuple[str, bool]:
    """返回 (令牌, 是否新生成)。令牌持久化在 Drive 上，跨会话不变。"""
    try:
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token, False
    except OSError:
        pass
    token = secrets.token_urlsafe(12)
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token + "\n", encoding="utf-8")
        try:
            os.chmod(token_file, 0o600)
        except OSError:
            pass
    except OSError as e:
        log(f"  !! 令牌写 Drive 失败（本次会话仍可用）：{e}")
    return token, True


def main() -> int:
    repo_cfg = REPO_DIR / "configs"
    drive_root, drive_cfg, drive_state = drive_paths()
    mounted = drive_root.parent.is_dir()

    log(f"仓库 {REPO_DIR}")
    log(f"Drive {drive_root}{'' if mounted else '（父目录不存在 = 未挂载）'}")

    if not mounted:
        log("!! 没挂载 Drive：本次会话的配置改动**不会**保留到下回开机。")
        log("!! 想让配置持久化，先跑 notebook 的 Drive 挂载格（drive.mount），再重跑本格。")
        # 令牌仍然生成，保证 Web 控制台可用（只是每次换域名也换令牌）；
        # 落点与 ENV_FILE 同目录（Colab 上就是 /content/colab_access_token.txt）
        token, _ = load_or_create_token(ENV_FILE.parent / "colab_access_token.txt")
        set_env_var(ENV_FILE, "COLAB_ACCESS_TOKEN", token)
        set_env_var(ENV_FILE, "COLAB_CONFIG_PERSISTED", "0")
        # 音色/脚本目录仍然指过去：Drive 真在的话 kokoro_voices.py 会自己写进去，
        # 只是本步没法确认挂载，所以标 0 让上层自己判断。
        set_env_var(ENV_FILE, "COLAB_KOKORO_DIR", str(kokoro_dir()))
        set_env_var(ENV_FILE, "COLAB_AUTO_ARCHIVE", "0")
        set_env_var(ENV_FILE, "COLAB_SCRIPTS_SYNC", "0")
        log(f"访问令牌（临时）…{token[-4:]}")
        return 0

    drive_cfg.mkdir(parents=True, exist_ok=True)
    drive_state.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- 1/6 播种
    link_configs = _flag("DRIVE_LINK_CONFIGS", True) and os.name != "nt"
    if repo_cfg.is_symlink():
        log("==> [1/6] configs 已指向 Drive，跳过播种")
    else:
        repo_cfg.mkdir(parents=True, exist_ok=True)
        n = seed_missing(repo_cfg, drive_cfg)
        log(f"==> [1/6] 播种 {n} 个文件到 Drive 配置目录（已存在的一律不动）")

        # ------------------------------------------------------------ 2/6 链接
        if link_configs:
            backup = None
            if repo_cfg.exists():
                backup = repo_cfg.with_name("configs.repo_defaults")
                if backup.is_symlink() or backup.exists():
                    if backup.is_symlink() or backup.is_file():
                        backup.unlink()
                    else:
                        shutil.rmtree(backup, ignore_errors=True)
                shutil.move(str(repo_cfg), str(backup))
            os.symlink(str(drive_cfg), str(repo_cfg))
            log(f"==> [2/6] configs → {drive_cfg}（仓库原文件留在 {backup}）")
        else:
            log("==> [2/6] 跳过 symlink（DRIVE_LINK_CONFIGS=0 或非 Linux 环境）")
            log("    注意：此时代码读写的仍是仓库内 configs/，本步只做 Drive 备份播种")

    # ---------------------------------------------------------------- 3/6 恢复
    restored = 0
    for drive_name, local_rel in STATE_FILES:
        restored += restore_missing(drive_state / drive_name,
                                    OUTPUT_DIR / local_rel, f"state/{drive_name}")
    for drive_name, local_rel in STATE_DIRS:
        restored += restore_missing(drive_state / drive_name,
                                    OUTPUT_DIR / local_rel, f"state/{drive_name}/")
    log(f"==> [3/6] 运行状态恢复 {restored} 个文件"
        f"（主题防重 / 集数徽章 / LLM 批次缓存）")

    # ---------------------------------------------------------------- 4/6 令牌
    token, fresh = load_or_create_token(drive_state / "access_token.txt")
    set_env_var(ENV_FILE, "COLAB_ACCESS_TOKEN", token)
    set_env_var(ENV_FILE, "COLAB_DRIVE_ROOT", str(drive_root))
    set_env_var(ENV_FILE, "COLAB_STATE_DIR", str(drive_state))
    set_env_var(ENV_FILE, "COLAB_CONFIG_PERSISTED", "1")
    log(f"==> [4/6] 访问令牌{'已生成' if fresh else '已恢复'}（存 {drive_state / 'access_token.txt'}）…{token[-4:]}")

    # ---------------------------------------------------------------- 5/6 目录
    kokoro = kokoro_dir()
    scripts = scripts_root()
    for d in (kokoro / "voices", scripts):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"  !! 建目录失败 {d}：{e}")
    set_env_var(ENV_FILE, "COLAB_KOKORO_DIR", str(kokoro))
    auto_archive = _flag("COLAB_AUTO_ARCHIVE", True)
    scripts_sync_on = _flag("COLAB_SCRIPTS_SYNC", True)
    set_env_var(ENV_FILE, "COLAB_AUTO_ARCHIVE", "1" if auto_archive else "0")
    set_env_var(ENV_FILE, "COLAB_SCRIPTS_SYNC", "1" if scripts_sync_on else "0")
    log(f"==> [5/6] 目录就绪：Kokoro 音色 → {kokoro / 'voices'}"
        f" · 预生成脚本 → {scripts}")

    # ---------------------------------------------------------------- 6/6 脚本
    if scripts_sync_on:
        try:
            if str(HERE) not in sys.path:
                sys.path.insert(0, str(HERE))
            from scripts_sync import drive_scripts_root, pull_all
            n = pull_all(drive_root, REPO_DIR)
            log(f"==> [6/6] 预生成脚本 Drive → 仓库补缺 {n} 个文件"
                f"（{drive_scripts_root(drive_root)}）")
        except Exception as e:  # 同步失败绝不能拖垮配置持久化
            log(f"!! [6/6] 预生成脚本同步失败（不影响出片）：{type(e).__name__}: {e}")
    else:
        log("==> [6/6] 跳过预生成脚本同步（COLAB_SCRIPTS_SYNC=0）")

    print()
    log("配置持久化就绪：")
    log(f"  配置（含 LLM 密钥）→ {drive_cfg}")
    log(f"  运行状态           → {drive_state}")
    log(f"  Kokoro 音色        → {kokoro / 'voices'}"
        f"（由 colab/kokoro_voices.py 同步 / 生成试听）")
    log(f"  令牌               → {ENV_FILE} 里的 COLAB_ACCESS_TOKEN")
    log(f"  出片后自动归档     → {'开' if auto_archive else '关'}（COLAB_AUTO_ARCHIVE）")
    log("  改配置去 Web 控制台改，改完直接落 Drive，下回开机自动恢复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
