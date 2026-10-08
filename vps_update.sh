#!/usr/bin/env bash
# ============================================================================
# sleep_english 一键更新 + 重启（在 VPS 上跑：bash /opt/sleep_english/vps_update.sh）
#
# 做什么：
#   1) 安全检查：出片/ffmpeg 正在跑时默认拒绝重启（--force 强制）
#   2) 备份 + 暂存 VPS 上的本地改动（防止被 pull 冲掉）
#   3) git pull --ff-only（只快进，不自动合并）
#   4) requirements.txt 变了才重装依赖
#   5) 本地补丁：上游已含则丢弃 stash，未含则重新应用，冲突则保留并提示
#   6) 清理 .venv_broken，重启 systemd 服务，轮询 /api/health
#   7) 打印摘要与回滚命令
#
# 用法：vps_update.sh [--dry-run] [--force] [--no-restart]
# ============================================================================
set -uo pipefail

REPO=/opt/sleep_english
BRANCH=${SLEEP_UPDATE_BRANCH:-master}
DRY=0; FORCE=0; NO_RESTART=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force) FORCE=1 ;;
    --no-restart) NO_RESTART=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 2 ;;
  esac
done

cd "$REPO" || { echo "FATAL: 进不去 $REPO"; exit 1; }

# 读 .vps_env（拿 SLEEP_AUTH_PASSWORD 之类，供鉴权后的接口调用）
if [ -f "$REPO/.vps_env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.vps_env"
  set +a
fi
AUTH_HDR=()
[ -n "${SLEEP_AUTH_PASSWORD:-}" ] && AUTH_HDR=(-H "X-Sleep-Auth: ${SLEEP_AUTH_PASSWORD}")

OLD=$(git rev-parse --short HEAD)
echo "=============================================================="
echo " sleep_english 更新  $(date -Is)"
echo " 当前 commit: $OLD  ($(git log -1 --pretty=%s | cut -c1-60))"
echo "=============================================================="

# --- 1) 安全检查：有出片/ffmpeg 在跑就别重启 --------------------------------
BUSY=""
if curl -s -m 6 "${AUTH_HDR[@]}" http://127.0.0.1:8766/api/run/status 2>/dev/null | grep -q '"is_running":true'; then
  BUSY="Web 控制台正在出片"
elif pgrep -f 'pipeline.py' >/dev/null 2>&1; then
  BUSY="命令行 pipeline.py 正在跑"
elif pgrep -x ffmpeg >/dev/null 2>&1; then
  BUSY="有 ffmpeg 进程在跑"
fi
if [ -n "$BUSY" ] && [ "$FORCE" != "1" ]; then
  echo "!! 拒绝执行：$BUSY"
  echo "   重启会中断它（音频/卡片按文件续传，之后可 bash run_cli.sh --resume 续跑）"
  echo "   确认要强制：bash vps_update.sh --force"
  exit 3
fi
[ -n "$BUSY" ] && echo "!! --force：忽略「$BUSY」继续"

# --- 2) 备份 + 暂存本地改动 -------------------------------------------------
DIRTY=$(git status --porcelain --untracked-files=no | wc -l)
if [ "$DIRTY" -gt 0 ]; then
  mkdir -p logs
  BACKUP="logs/pre_pull_$(date +%Y%m%d_%H%M%S).patch"
  git diff >"$BACKUP"
  echo "本地有 $DIRTY 个已跟踪文件改动 → 备份到 $BACKUP 并 stash"
  git stash push -m "vps-update-$(date +%s)" >/dev/null || echo "  (stash 失败，继续)"
else
  BACKUP=""
  echo "本地无已跟踪改动"
fi

# --- 3) 拉取（只快进） ------------------------------------------------------
echo "--- git fetch / pull --ff-only origin $BRANCH ---"
git fetch --all --prune || { echo "FATAL: git fetch 失败（网络或仓库权限）"; exit 4; }
if [ "$DRY" = "1" ]; then
  echo "[dry-run] 将要拉取的提交："
  git log --oneline "HEAD..origin/$BRANCH" | head -20
  echo "[dry-run] 变更文件："
  git diff --stat "HEAD..origin/$BRANCH" | tail -25
  echo "[dry-run] 结束（未做任何改动）"
  exit 0
fi
if ! git pull --ff-only "origin" "$BRANCH"; then
  echo "FATAL: 无法快进合并（本地与远端分叉）。人工处理：git log --oneline --graph --all"
  exit 5
fi
NEW=$(git rev-parse --short HEAD)
CHANGED=$(git diff --name-only "$OLD" "$NEW" || true)
echo "新 commit: $NEW"
if [ "$OLD" = "$NEW" ]; then
  echo "（代码没有变化）"
else
  echo "本次变更文件："; echo "$CHANGED" | sed 's/^/  /' | head -30
fi

# --- 4) 依赖 ---------------------------------------------------------------
if echo "$CHANGED" | grep -q '^requirements.txt$'; then
  echo "--- requirements.txt 有变动 → 重装依赖 ---"
  "$REPO/.venv/bin/pip" install -q -r requirements.txt || echo "!! pip 安装有失败项，请检查"
else
  echo "依赖未变（requirements.txt 未出现在变更里）"
fi

# --- 5) 本地补丁去留 -------------------------------------------------------
if git stash list | grep -q 'vps-update-'; then
  STASH=$(git stash list | grep 'vps-update-' | head -1 | cut -d: -f1)
  if git stash show -p "$STASH" | git apply --reverse --check - >/dev/null 2>&1; then
    echo "上游已包含本地补丁 → 丢弃 stash $STASH"
    git stash drop "$STASH" >/dev/null
  elif git stash show -p "$STASH" | git apply --check - >/dev/null 2>&1; then
    echo "上游未包含本地补丁 → 重新应用 $STASH"
    git stash apply "$STASH" || echo "!! 应用失败，patch 在 ${BACKUP:-logs/pre_pull_*.patch}"
  else
    echo "!! 本地补丁与上游冲突：保留 stash $STASH（备份 patch: ${BACKUP:-无}）"
    echo "   人工处理：git stash show -p $STASH | head -50"
  fi
fi

# --- 6) 清理 + 重启 --------------------------------------------------------
if [ -d "$REPO/.venv_broken" ]; then
  echo "清理 .venv_broken（$(du -sh "$REPO/.venv_broken" 2>/dev/null | cut -f1)）"
  rm -rf "$REPO/.venv_broken"
fi

if [ "$NO_RESTART" = "1" ]; then
  echo "--no-restart：跳过重启（代码已更新，服务仍在跑旧代码）"
else
  echo "--- 重启 sleep-english ---"
  systemctl restart sleep-english
  OK=""
  for _ in $(seq 1 30); do
    if curl -sf -m 5 http://127.0.0.1:8766/api/health >/dev/null 2>&1; then
      OK=1; break
    fi
    sleep 1
  done
  if [ -n "$OK" ]; then
    echo "健康检查通过：$(curl -s -m 5 http://127.0.0.1:8766/api/health)"
  else
    echo "!! 健康检查失败，最近日志："
    journalctl -u sleep-english -n 25 --no-pager | tail -25
    echo "回滚：cd $REPO && git checkout $OLD -- . && systemctl restart sleep-english"
    exit 6
  fi
fi

echo "=============================================================="
echo " 完成：$OLD → $NEW   $(date -Is)"
echo " 服务：$(systemctl is-active sleep-english)  端口：$(ss -ltn | grep -c 8766)"
echo " 回滚：cd $REPO && git checkout $OLD -- . && systemctl restart sleep-english"
[ -n "${BACKUP:-}" ] && echo " 本地改动备份：$REPO/$BACKUP"
echo "=============================================================="
