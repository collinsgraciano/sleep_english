#!/usr/bin/env bash
# ============================================================================
# VPS 命令行出片包装：把「配置里的开关」翻译成 CLI 参数，再跑 pipeline.py。
#
# 为什么需要：Web 控制台的参数来自 configs/mode_sleep.json（pipeline_service 组装），
# 而 pipeline.py 的 CLI 只认命令行开关 —— 直接跑 CLI 会静默用默认值（例如
# sleep_4k_native=False、wbk_model=cn:auto），与网页点“开始生成”结果不一致。
# 本脚本读同一份配置，补齐这些差异（命令行显式传参优先）。
#
# 用法：
#   bash /opt/sleep_english/run_cli.sh --resume --sleep-pairs 50
#   bash /opt/sleep_english/run_cli.sh --resume --upscale-timeout 43200   # 补 4K
#   bash /opt/sleep_english/run_cli.sh --resume --no-4k                  # 显式覆盖配置
# ============================================================================
set -u
REPO=/opt/sleep_english
CFG=$REPO/configs/mode_sleep.json

# .vps_env：线程数、HF、卸载开关、4K 编码参数等
if [ -f "$REPO/.vps_env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.vps_env"
  set +a
fi

cfg() {  # cfg <key>：读配置；bool true→1 / false→""；字符串原样；缺失→""
  "$REPO/.venv/bin/python" -c "
import json
try:
    d = json.load(open('$CFG', encoding='utf-8'))
except Exception:
    d = {}
v = d.get('$1', '')
print(v if isinstance(v, str) else ('1' if v else ''))
" 2>/dev/null
}

# LLM：Web 路径由 pipeline_service 注入这些 env；CLI 路径在这里补上
export WBK_API_KEY="$(cfg wbk_api_key)"
export WBK_MODEL="$(cfg wbk_model)"
export WBK_THINKING="$(cfg wbk_thinking)"
export GEMINI_API_KEY="$(cfg gemini_api_key)"
export LLM_PROVIDER="${LLM_PROVIDER:-wbk}"
[ -z "${WBK_MODEL:-}" ] && export WBK_MODEL="cn:auto"
[ -z "${WBK_THINKING:-}" ] && export WBK_THINKING="default"

# 4K 开关：命令行显式传了就不重复追加
ARGS=("$@")
JOINED=" $* "
if [ "$(cfg sleep_4k_native)" = "1" ] && [[ "$JOINED" != *" --sleep-4k-native "* ]]; then
  ARGS+=(--sleep-4k-native)
fi
if [ "$(cfg no_4k)" = "1" ] && [[ "$JOINED" != *" --no-4k "* ]]; then
  ARGS+=(--no-4k)
fi

echo "[run_cli] model=${WBK_MODEL} thinking=${WBK_THINKING} key=$([ -n "$WBK_API_KEY" ] && echo SET || echo EMPTY)"
echo "[run_cli] 4K: native=$(cfg sleep_4k_native) no_4k=$(cfg no_4k) | x264=${SLEEP_4K_X264_PARAMS:-'(默认)'} | unload=${SLEEP_UNLOAD_TTS:-0}"
echo "[run_cli] 参数: ${ARGS[*]}"

cd "$REPO/pipeline" || exit 1
exec "$REPO/.venv/bin/python" pipeline.py --output "$REPO/output" "${ARGS[@]}"
