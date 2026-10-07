#!/usr/bin/env bash
# Colab 环境准备：系统依赖 → Python 依赖 → Kokoro TTS 栈 → cloudflared → 自检
# 由 colab/sleep_english_colab.ipynb 调用；也可单独跑：
#   REPO_DIR=/content/sleep_english bash colab/setup.sh
#
# 幂等设计：装完写一个标记文件（COLAB_SETUP_FLAG，默认 /content/.colab_setup_done），
# 下次调用先「标记 + 实检」再决定要不要重跑 apt/pip —— 同一个会话里重跑只花 1-2 秒。
# 要强制重装：COLAB_FORCE_INSTALL=1
#
# 重要：**标记只在 `import kokoro` 真的通过后才写**，并且末尾自检发现 kokoro 缺失会
# exit 1。原因：Colab 的 pip 包装在 VM 本地盘，**空闲回收/重连后全部消失**（标记文件也
# 一起没了，但若只重跑「配置/启动」格就会出现「控制台在跑、Step 2 报 No module named
# kokoro」）。宁可这里红着失败，也不要让下游出片时才炸。
set -uo pipefail

REPO_DIR="${REPO_DIR:-/content/sleep_english}"
PY="$(command -v python3 || command -v python)"
SETUP_FLAG="${COLAB_SETUP_FLAG:-/content/.colab_setup_done}"
FORCE_INSTALL="${COLAB_FORCE_INSTALL:-0}"
PIP_LOG="${COLAB_PIP_LOG:-/content/colab_logs/pip.log}"
mkdir -p "$(dirname "$PIP_LOG")" 2>/dev/null || true
cd "$REPO_DIR" || { echo "找不到仓库目录 $REPO_DIR（先跑 notebook 的克隆步）"; exit 1; }

echo "==> Python：$("$PY" -V 2>&1) · $PY"
"$PY" -c 'import sys; print("    解释器：", sys.executable)'

# pip 安装：静默 + 落日志（失败时把日志尾巴打出来，不再"装失败了却没人知道"）
pip_try() {
  local log="$1"; shift
  { "$PY" -m pip install -q "$@"; } >>"$log" 2>&1 && return 0
  { "$PY" -m pip install -q --break-system-packages "$@"; } >>"$log" 2>&1 && return 0
  return 1
}

# 关键模块是否真的可用（标记文件可能来自"上次装了一半"的会话，所以必须实检）
_ready_check() {
  command -v ffmpeg >/dev/null 2>&1 || return 1
  "$PY" - <<'PYCHECK' >/dev/null 2>&1
import importlib
for m in ("fastapi", "uvicorn", "PIL", "numpy", "soundfile", "kokoro", "misaki"):
    importlib.import_module(m)
PYCHECK
}

_tts_ok() { "$PY" -c "import kokoro, misaki" >/dev/null 2>&1; }

SKIP_INSTALL=0
if [ "$FORCE_INSTALL" = "1" ]; then
  echo "==> COLAB_FORCE_INSTALL=1：忽略标记，强制重装"
elif [ -f "$SETUP_FLAG" ] && _ready_check; then
  SKIP_INSTALL=1
  echo "==> 依赖已就绪（$SETUP_FLAG + 实检通过）—— 跳过 apt/pip"
  echo "    要强制重装：把参数区 FORCE_INSTALL 设 True（或 COLAB_FORCE_INSTALL=1）再跑本步"
fi

if [ "$SKIP_INSTALL" != "1" ]; then
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
  pip_try "$PIP_LOG" -r requirements.txt || echo "    warn requirements.txt 有失败项（详见 $PIP_LOG）"

  echo "==> [3/4] Kokoro TTS 栈（本地合成，主流程零积分）"
  # 关于 Python 版本（本机 3.13.11 实测 + PyPI 元数据，别改错）：
  #   * kokoro / misaki 的较新版本元数据写的是 Requires-Python <3.13，Colab（3.13）上
  #     普通安装就是「Ignored ... requires a different python version / 无版本可装」；
  #   * **--no-deps 不能绕过 Requires-Python**（要 --ignore-requires-python）—— 旧写法
  #     就是这样踩的坑；
  #   * 能直接在 3.13 上装的组合：kokoro==0.7.16 + misaki==0.7.4（都声明 >=3.7、纯 Python
  #     轮子），但 kokoro 0.7.16 的依赖写着 misaki>=0.7.16，所以这两个必须配 --no-deps 装，
  #     再由我们把依赖单独补齐（本机就是这套组合在跑）；
  #   * 0.9.4 那套（老 notebook 用过）只能 --ignore-requires-python 强装，作为兜底。
  PY_VER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")"
  echo "    Python $PY_VER"
  : >"$PIP_LOG.kokoro"

  # 英语出片必需（都能在 3.13 上装）
  KOKORO_DEPS="torch transformers huggingface-hub numpy scipy loguru soundfile regex \
num2words spacy phonemizer-fork espeakng-loader"
  # 可选：中文兜底音色 + 更精细的英文 G2P；装不上不影响英语出片
  KOKORO_EXTRA="spacy-curated-transformers cn2an pypinyin pypinyin-dict ordered-set jieba addict"

  _try_kokoro() {
    local label="$1"; shift
    echo "    尝试：$label"
    if pip_try "$PIP_LOG.kokoro" "$@"; then
      if _tts_ok; then
        echo "    ok   $label"
        return 0
      fi
      echo "    warn $label 装上了但 import 失败"
    else
      echo "    warn $label 安装命令失败"
    fi
    return 1
  }

  if _tts_ok; then
    echo "    ok   kokoro/misaki 已可用，跳过"
  else
    # 首选：3.13 上可直接安装、且本机验证过的组合（--no-deps 绕开 kokoro 的 misaki>=0.7.16）
    _try_kokoro "--no-deps kokoro==0.7.16 misaki==0.7.4" \
      --no-deps "kokoro==0.7.16" "misaki==0.7.4" || true
    if ! _tts_ok; then
      # 兜底 1：0.9.4（元数据 <3.13，必须 --ignore-requires-python 才装得上）
      _try_kokoro "--ignore-requires-python --no-deps kokoro==0.9.4 misaki[en]==0.9.4" \
        --ignore-requires-python --no-deps "kokoro==0.9.4" "misaki[en]==0.9.4" || true
    fi
    if ! _tts_ok; then
      # 兜底 2：不带版本约束的最新版（同样强制忽略 Requires-Python）
      _try_kokoro "--ignore-requires-python --no-deps kokoro misaki[en]（最新）" \
        --ignore-requires-python --no-deps kokoro "misaki[en]" || true
    fi
    pip_try "$PIP_LOG.kokoro" --prefer-binary $KOKORO_DEPS ||
      echo "    warn 英语必需依赖有失败项（详见 $PIP_LOG.kokoro）"
    pip_try "$PIP_LOG.kokoro" --prefer-binary $KOKORO_EXTRA ||
      echo "    warn 可选依赖（中文兜底 / 精细 G2P）有失败项 —— 不影响英语出片"
  fi

  if ! _tts_ok; then
    echo "==> FAIL kokoro/misaki 装不上（Step 2 TTS 必失败：No module named 'kokoro'）"
    echo "    常见原因：Colab 空闲回收/重连后 VM 本地盘的 pip 包会丢 —— 重跑本步即可恢复。"
    echo "    pip 日志尾部（$PIP_LOG.kokoro）："
    tail -n 25 "$PIP_LOG.kokoro" 2>/dev/null || true
    echo "    处理建议："
    echo "      1) 重跑本步（网络抖动很常见；本步可反复重跑）"
    echo "      2) 仍失败：看上面日志里哪一步报错；3.13 的关键是 --no-deps（本步已自动做）"
    echo "      3) 标记文件**没写**，所以下次重跑不会误判为已就绪"
    exit 1
  fi

  # misaki 的英文 G2P 用 spacy 词性标注提质，缺模型会退回较糙的回退链
  if ! "$PY" -c "import en_core_web_sm" >/dev/null 2>&1; then
    echo "    下载 spacy en_core_web_sm"
    "$PY" -m spacy download en_core_web_sm >/dev/null 2>&1 ||
      pip_try "$PIP_LOG" "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" ||
      echo "    warn en_core_web_sm 没装上（英文 G2P 会用较糙的回退链，不影响出片）"
  fi

  echo "==> [4/4] cloudflared（把本地 8766 变成公网 URL —— Cloudflare 免费域名）"
  if ! command -v cloudflared >/dev/null 2>&1; then
    case "$(uname -m)" in
      aarch64 | arm64) CF_ARCH=arm64 ;;
      *) CF_ARCH=amd64 ;;
    esac
    curl -sSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}"
    chmod +x /usr/local/bin/cloudflared
  fi

  # 标记只在「实检通过」时才写：这是"跳过安装"的唯一依据
  if _ready_check; then
    touch "$SETUP_FLAG" 2>/dev/null || true
    echo "==> 安装完成，标记已写：$SETUP_FLAG"
  else
    echo "==> WARN 实检仍有缺失（见下面的自检 FAIL 行）—— 标记不写，下次会重新装"
    exit 1
  fi
fi

echo "==> 自检"
if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -version | head -1
else
  echo "  FAIL ffmpeg 没装上（apt 失败？FORCE_INSTALL=True 重跑本步）"
fi
if command -v cloudflared >/dev/null 2>&1; then
  cloudflared --version 2>/dev/null | head -1
else
  echo "  WARN cloudflared 没装上 —— Web 控制台只能本地访问，命令行出片不受影响"
fi
"$PY" - <<'PYCHECK'
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, "pipeline")

for mod in ("fastapi", "uvicorn", "PIL", "numpy", "soundfile", "kokoro", "misaki"):
    try:
        importlib.import_module(mod)
        print(f"  ok   {mod}")
    except Exception as e:
        print(f"  FAIL {mod}: {e}")

try:  # 版本号很关键：3.13 上能装的组合与 3.12 不同，排障时先看这一行
    import kokoro
    import misaki
    print(f"  ok   kokoro {getattr(kokoro, '__version__', '?')}"
          f" · misaki {getattr(misaki, '__version__', '?')}")
except Exception:
    pass

# 英文 G2P 冒烟（不需要下载 330MB 模型就能验证音素化链路）
try:
    from misaki import en as _misaki_en
    _g2p = _misaki_en.G2P(british=False)
    _ps, _ = _g2p("Good night. Sweet dreams.")
    # 只报长度：IPA 字符在某些终端编码下会直接抛 UnicodeEncodeError（假报警）
    print(f"  ok   misaki en G2P（音素 {len(_ps)} 字符）")
except Exception as e:
    print(f"  WARN misaki en G2P 不可用（英语合成会失败）：{str(e)[:120]}")

cuda = False
try:
    import torch
    cuda = bool(torch.cuda.is_available())
    print(f"  torch {torch.__version__} | cuda={cuda}"
          + (f" | {torch.cuda.get_device_name(0)}" if cuda else "（CPU 运行时，正常）"))
except Exception as e:
    print(f"  FAIL torch: {e}")

# Kokoro 设备：Colab 默认强制 CPU（KOKORO_DEVICE=cpu），与机器无关、结果可复现
try:
    import tts_engine
    dev = tts_engine._kokoro_device()
    print(f"  ok   Kokoro device = {dev or 'auto（有 CUDA 就用 CUDA）'}")
    if (dev or "") == "cpu" or (not dev and not cuda):
        print("       预计耗时：10 组 ≈ 5-10 分钟 · 50 组 ≈ 25-45 分钟 · 200 组 ≈ 1.5-3 小时")
        print("       （想用 GPU：参数区 FORCE_CPU=False，或把 KOKORO_DEVICE 改成 cuda）")
except Exception as e:
    print(f"  FAIL tts_engine: {e}")

try:
    from media_utils import FONT_EN, FONT_ZH, FONT_PH
    for label, path in (("FONT_EN", FONT_EN), ("FONT_ZH", FONT_ZH), ("FONT_PH", FONT_PH)):
        print(f"  {'ok  ' if os.path.exists(path) else 'FAIL'} {label} = {path}")
except Exception as e:
    print(f"  FAIL media_utils: {e}")

nunito = os.path.join("pipeline", "fonts", "Nunito-Bold.ttf")
print(f"  {'ok  ' if os.path.exists(nunito) else 'FAIL'} 卡片英文字体 {nunito}")

# Kokoro 音色缓存：本地（本会话工作缓存）与 Drive（跨会话事实源）
local_voices = Path.home() / ".cache/huggingface/hub/models--hexgrad--Kokoro-82M/voices"
n_local = len([p for p in local_voices.glob("*.pt")]) if local_voices.is_dir() else 0
drive_dir = Path(os.environ.get("COLAB_KOKORO_DIR", "")) if os.environ.get("COLAB_KOKORO_DIR") else None
n_drive = len([p for p in (drive_dir / "voices").glob("*.pt")]) if drive_dir and (drive_dir / "voices").is_dir() else 0
print(f"  ok   Kokoro 音色：本地缓存 {n_local} 个 · Drive {n_drive} 个"
      f"（colab/kokoro_voices.py 负责同步；全部落盘后控制台试听不再联网）")

for d in ("ai_scripts", "ai_scripts_hot"):
    n = len([x for x in os.listdir(d) if not x.startswith("_")]) if os.path.isdir(d) else 0
    print(f"  预生成脚本 {d}: {n} 个")
print("  本机新写、还没 push 的批次：colab/scripts_sync.py --push 传到 Drive 的 scripts/，")
print("  Colab 开机（COLAB_SCRIPTS_SYNC=1）自动拉回仓库，不必为了一个批次重新 push 代码。")

cfg = os.path.join("configs", "mode_sleep.json")
if os.path.islink("configs"):
    print(f"  ok   configs/ -> {os.readlink('configs')}（配置持久化在 Drive）")
elif os.path.exists(cfg):
    print("  warn configs/ 不是 symlink —— 本会话的配置改动不会保留到下回开机")
PYCHECK

# 收尾闸门：kokoro 不可用 = 本步失败（否则 Step 2 出片时才报 ModuleNotFoundError）
if ! _tts_ok; then
  echo "==> FAIL 本会话缺少 kokoro/misaki：Step 2 TTS 会报 No module named 'kokoro'。"
  echo "    常见原因：Colab 空闲回收/重连后 VM 本地盘的 pip 包会丢 —— 重跑本步即可恢复。"
  echo "    要强制重装：参数区 FORCE_INSTALL=True（或 COLAB_FORCE_INSTALL=1）。"
  exit 1
fi

echo "==> setup.sh 完成"
