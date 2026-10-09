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

cfg() {  # cfg <key>：读配置；bool true→1 / false→""；数字/字符串原样；缺失→""
  "$REPO/.venv/bin/python" -c "
import json
try:
    d = json.load(open('$CFG', encoding='utf-8'))
except Exception:
    d = {}
v = d.get('$1', '')
if isinstance(v, bool):
    print('1' if v else '')
elif v is None:
    print('')
else:
    print(v)
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
# 统一规则：命令行**显式给过**同名 flag 时不再追加配置值（否则 argparse 取最后一个，
# 配置会静默覆盖用户显式传参 —— 曾致 `--sleep-card-fps 1` 被配置默认 25 覆盖）
has_flag() { case "$JOINED" in *" $1 "*) return 0 ;; *) return 1 ;; esac; }
add_flag() {  # add_flag <flag> [value]
  has_flag "$1" && return 0
  if [ "$#" -ge 2 ]; then ARGS+=("$1" "$2"); else ARGS+=("$1"); fi
}

if [ "$(cfg sleep_4k_native)" = "1" ]; then add_flag --sleep-4k-native; fi
if [ "$(cfg no_4k)" = "1" ]; then add_flag --no-4k; fi
# 卡片块帧率（1/2/5/25）：静态卡降帧提速；未绑定片头/片尾视频时才真正生效
_fps="$(cfg sleep_card_fps)"
[ -n "$_fps" ] && add_flag --sleep-card-fps "$_fps"
# 块划分方式：per_step（默认）/ per_pair（同组合并，省每块固定开销）
_grp="$(cfg sleep_block_grouping)"
[ -n "$_grp" ] && add_flag --sleep-block-grouping "$_grp"

# BGM：配置开着就加 --bgm-mix，并把音乐库/侧链参数按配置传下去（与网页一键生成一致）
if [ "$(cfg bgm_mix)" = "1" ]; then add_flag --bgm-mix; fi
for pair in \
  "bgm_music_dir:--bgm-music-dir" \
  "bgm_ducking_mode:--bgm-ducking-mode" \
  "bgm_start_chapter:--bgm-start-chapter" \
  "bgm_base_gain_db:--bgm-base-gain-db" \
  "bgm_volume_offset_db:--bgm-volume-offset-db" \
  "bgm_fade_ms:--bgm-fade-ms" \
  "bgm_highpass_freq:--bgm-highpass-freq" \
  "bgm_min_volume_db:--bgm-min-volume-db" \
  "bgm_sc_threshold_db:--bgm-sc-threshold-db" \
  "bgm_sc_threshold_offset_db:--bgm-sc-threshold-offset-db" \
  "bgm_sc_ratio:--bgm-sc-ratio" \
  "bgm_sc_attack_ms:--bgm-sc-attack-ms" \
  "bgm_sc_release_ms:--bgm-sc-release-ms" ; do
  _key="${pair%%:*}"
  _flag="${pair##*:}"
  _val="$(cfg "$_key")"
  [ -n "$_val" ] && add_flag "$_flag" "$_val"
done
if [ "$(cfg bgm_mix)" = "1" ]; then
  if [ "$(cfg bgm_dynamic_volume)" = "1" ]; then add_flag --bgm-dynamic-volume; else add_flag --no-bgm-dynamic-volume; fi
  if [ "$(cfg bgm_spectral_shaping)" = "1" ]; then add_flag --bgm-spectral-shaping; else add_flag --no-bgm-spectral-shaping; fi
fi

echo "[run_cli] model=${WBK_MODEL} thinking=${WBK_THINKING} key=$([ -n "$WBK_API_KEY" ] && echo SET || echo EMPTY)"
echo "[run_cli] 4K: native=$(cfg sleep_4k_native) no_4k=$(cfg no_4k) | x264=${SLEEP_4K_X264_PARAMS:-'(默认)'} | unload=${SLEEP_UNLOAD_TTS:-0}"
echo "[run_cli] 参数: ${ARGS[*]}"

cd "$REPO/pipeline" || exit 1
exec "$REPO/.venv/bin/python" pipeline.py --output "$REPO/output" "${ARGS[@]}"
