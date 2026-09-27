#!/usr/bin/env bash
# Colab 环境准备：系统依赖 → Python 依赖 → Kokoro TTS 栈 → cloudflared → 自检
# 由 colab/sleep_english_colab.ipynb 调用；也可单独跑：
#   REPO_DIR=/content/sleep_english bash colab/setup.sh
set -uo pipefail

REPO_DIR="${REPO_DIR:-/content/sleep_english}"
PY="$(command -v python3 || command -v python)"
cd "$REPO_DIR" || { echo "找不到仓库目录 $REPO_DIR（先跑 notebook 的克隆单元格）"; exit 1; }

pip_install() {
  "$PY" -m pip install -q "$@" || "$PY" -m pip install -q --break-system-packages "$@"
}

echo "==> [1/4] 系统依赖：ffmpeg + espeak-ng + 字体"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  ffmpeg espeak-ng libsndfile1 \
  fonts-noto-cjk fonts-dejavu-core fonts-liberation >/dev/null
fc-cache -f >/dev/null 2>&1

# media_utils.py 的 Linux 字体常量写死 /usr/share/fonts/truetype/noto/*.ttc，
# 而 Ubuntu 的 fonts-noto-cjk 实际装在 opentype/ 下 —— 补软链，否则中文全是豆腐块
NOTO_LINK_DIR=/usr/share/fonts/truetype/noto
mkdir -p "$NOTO_LINK_DIR"
for variant in Bold Regular; do
  want="$NOTO_LINK_DIR/NotoSansCJK-$variant.ttc"
  [ -e "$want" ] && continue
  found="$(find /usr/share/fonts -iname "NotoSansCJK-*${variant}*.ttc" 2>/dev/null | head -1)"
  [ -z "$found" ] && found="$(find /usr/share/fonts -iname 'NotoSansCJK-*.ttc' 2>/dev/null | head -1)"
  if [ -n "$found" ]; then
    ln -sf "$found" "$want"
    echo "    字体软链 $want -> $found"
  fi
done

echo "==> [2/4] Python 依赖：requirements.txt"
pip_install -r requirements.txt

echo "==> [3/4] Kokoro TTS 栈（本地合成，主流程零积分）"
# 版本对齐本机验证过的组合；torch 用 Colab 预装的（含 CUDA），不加版本约束以免被换掉
pip_install "kokoro==0.7.16" "misaki==0.7.4" "phonemizer-fork==3.3.2" \
  espeakng-loader soundfile jieba einops regex loguru \
  spacy transformers huggingface-hub

# misaki 的英文 G2P 用 spacy 词性标注提质，缺模型会退回较糙的回退链
if ! "$PY" -c "import en_core_web_sm" >/dev/null 2>&1; then
  echo "    下载 spacy en_core_web_sm"
  "$PY" -m spacy download en_core_web_sm >/dev/null 2>&1 ||
    pip_install "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
fi

echo "==> [4/4] cloudflared（把本地 8766 变成公网 URL）"
if ! command -v cloudflared >/dev/null 2>&1; then
  case "$(uname -m)" in
    aarch64 | arm64) CF_ARCH=arm64 ;;
    *) CF_ARCH=amd64 ;;
  esac
  curl -sSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
  chmod +x /usr/local/bin/cloudflared
fi

echo "==> 自检"
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -version | head -1
else
  echo "  FAIL ffmpeg 没装上（apt 失败？重跑本格试试）"
fi
if command -v cloudflared >/dev/null 2>&1; then
  cloudflared --version 2>/dev/null | head -1
else
  echo "  WARN cloudflared 没装上 —— Web 控制台只能本地访问，命令行出片不受影响"
fi
"$PY" - <<'PYCHECK'
import importlib, os, sys
sys.path.insert(0, "pipeline")

for mod in ("fastapi", "uvicorn", "PIL", "numpy", "soundfile", "kokoro", "misaki"):
    try:
        importlib.import_module(mod)
        print(f"  ok   {mod}")
    except Exception as e:
        print(f"  FAIL {mod}: {e}")

try:
    import torch
    print(f"  torch {torch.__version__} | cuda={torch.cuda.is_available()}"
          + (f" | {torch.cuda.get_device_name(0)}" if torch.cuda.is_available() else "（CPU 也能跑，只是慢）"))
except Exception as e:
    print(f"  FAIL torch: {e}")

try:
    from media_utils import FONT_EN, FONT_ZH, FONT_PH
    for label, path in (("FONT_EN", FONT_EN), ("FONT_ZH", FONT_ZH), ("FONT_PH", FONT_PH)):
        print(f"  {'ok  ' if os.path.exists(path) else 'FAIL'} {label} = {path}")
except Exception as e:
    print(f"  FAIL media_utils: {e}")

nunito = os.path.join("pipeline", "fonts", "Nunito-Bold.ttf")
print(f"  {'ok  ' if os.path.exists(nunito) else 'FAIL'} 卡片英文字体 {nunito}")

for d in ("ai_scripts", "ai_scripts_hot"):
    n = len([x for x in os.listdir(d) if not x.startswith("_")]) if os.path.isdir(d) else 0
    hint = "" if n else "  ← 不在 Git 里，需要从 Drive 同步（见 notebook 对应单元格）"
    print(f"  预生成脚本 {d}: {n} 个{hint}")
PYCHECK

echo "==> setup.sh 完成"
