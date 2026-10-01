#!/usr/bin/env bash
# 启动 Web 控制台 + Cloudflare 隧道（免费域名），打印可直接点开的带令牌链接。
#   REPO_DIR=/content/sleep_english bash colab/serve.sh
#
# 两种隧道（都走 Cloudflare 免费额度）：
#   默认             cloudflared quick tunnel → 每次开机一个全新的 *.trycloudflare.com
#                    （无需 CF 账号；域名每次换，令牌不变）
#   CF_TUNNEL_TOKEN  有 CF 账号的话跑 named tunnel → 固定域名（需在 Zero Trust 面板
#                    预先配好 public hostname，并用 CF_DOMAIN 传域名只为打印）
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(cd "$HERE/.." && pwd)}"
PORT="${PORT:-8766}"
LOG_DIR="${COLAB_LOG_DIR:-/content/colab_logs}"
ENV_FILE="${ENV_FILE:-/content/colab_env.sh}"
PY="$(command -v python3 || command -v python)"
mkdir -p "$LOG_DIR"
cd "$REPO_DIR" || { echo "找不到仓库目录 $REPO_DIR"; exit 1; }

# drive_config.py 写下的 token / 目录等变量（文件是我们自己生成的，直接 source）
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

# 访问令牌：env → env 文件 → 现场生成并回写（重启单元格也能拿到同一个）
if [ -z "${COLAB_ACCESS_TOKEN:-}" ]; then
  COLAB_ACCESS_TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(12))')"
  export COLAB_ACCESS_TOKEN
  if grep -q '^export COLAB_ACCESS_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^export COLAB_ACCESS_TOKEN=.*|export COLAB_ACCESS_TOKEN='$COLAB_ACCESS_TOKEN'|" "$ENV_FILE"
  else
    echo "export COLAB_ACCESS_TOKEN='$COLAB_ACCESS_TOKEN'" >>"$ENV_FILE"
  fi
fi
export COLAB_ACCESS_TOKEN

pkill -f "uvicorn secure_gate:app" >/dev/null 2>&1
pkill -f "cloudflared tunnel" >/dev/null 2>&1
sleep 1

echo "==> 启动 Web 服务（127.0.0.1:$PORT，带令牌闸门）"
PYTHONPATH="$REPO_DIR:$HERE" \
  nohup "$PY" -m uvicorn secure_gate:app --host 127.0.0.1 --port "$PORT" \
  >"$LOG_DIR/uvicorn.log" 2>&1 &
UV_PID=$!

READY=""
for _ in $(seq 1 90); do
  if curl -sf -H "X-Colab-Token: $COLAB_ACCESS_TOKEN" "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    READY=1
    break
  fi
  kill -0 "$UV_PID" 2>/dev/null || break
  sleep 1
done
if [ -z "$READY" ]; then
  echo "!! Web 服务没起来，uvicorn.log 最后 40 行："
  tail -n 40 "$LOG_DIR/uvicorn.log"
  exit 1
fi
echo "    /api/health OK (pid $UV_PID)"

PUBLIC_URL=""
if ! command -v cloudflared >/dev/null 2>&1; then
  echo "!! cloudflared 没装上（重跑 setup 格）—— Web 服务只在容器内可用"
elif [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  echo "==> 开 named tunnel（固定域名，需 CF 面板已配好 public hostname）"
  nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" \
    >"$LOG_DIR/cloudflared.log" 2>&1 &
  CF_PID=$!
  [ -n "${CF_DOMAIN:-}" ] && PUBLIC_URL="https://${CF_DOMAIN}"
  for _ in $(seq 1 30); do
    grep -q "Registered tunnel connection" "$LOG_DIR/cloudflared.log" 2>/dev/null && break
    kill -0 "$CF_PID" 2>/dev/null || break
    sleep 1
  done
else
  echo "==> 开公网隧道（cloudflared quick tunnel，CF 免费域名，无需账号）"
  nohup cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
    >"$LOG_DIR/cloudflared.log" 2>&1 &
  CF_PID=$!
  for _ in $(seq 1 60); do
    PUBLIC_URL="$(grep -Eo 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' "$LOG_DIR/cloudflared.log" | head -1)"
    [ -n "$PUBLIC_URL" ] && break
    kill -0 "$CF_PID" 2>/dev/null || break
    sleep 1
  done
fi

echo
echo "=============================================================="
if [ -n "$PUBLIC_URL" ]; then
  echo " Web 控制台已就绪，浏览器打开："
  echo
  echo "   ${PUBLIC_URL}/?ct=${COLAB_ACCESS_TOKEN}"
  echo
else
  echo " 没拿到公网 URL（cloudflared 日志见 $LOG_DIR/cloudflared.log）"
  echo " Web 服务本身在跑：http://127.0.0.1:$PORT"
  echo " 可以改用 notebook 里的「命令行直接出片」单元格，不依赖 Web UI。"
fi
echo "--------------------------------------------------------------"
echo " 访问令牌 : $COLAB_ACCESS_TOKEN   （持久化在 Drive 的 state/access_token.txt）"
echo " 运行日志 : tail -f $LOG_DIR/uvicorn.log"
echo " 停止服务 : pkill -f 'uvicorn secure_gate:app'; pkill -f 'cloudflared tunnel'"
echo " 提醒     : 这条链接等于完整控制权（能读配置里的密钥、能跑视频），别转发"
echo "=============================================================="
