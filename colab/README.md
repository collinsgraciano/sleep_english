# Colab 运行版

在 Google Colab 上跑本项目：**每次开机从 GitHub 拉最新代码**，装依赖，起完整 Web 控制台（公网链接），出片后把成品归档到 Google Drive。

notebook 只是薄引导层 —— 安装/配置/启动逻辑全在本目录的脚本里，所以仓库一更新，重跑 notebook 第 1 格就自动生效，notebook 本身几乎不用改。

## 打开方式

推到 GitHub 后，任选一种：

- 直接开：`https://colab.research.google.com/github/collinsgraciano/sleep_english/blob/master/colab/sleep_english_colab.ipynb`
- 或 Colab 首页 → GitHub 标签页 → 粘贴仓库地址 → 选 `colab/sleep_english_colab.ipynb`
- 或把本文件上传到 Colab（`File → Open notebook → Upload`）

## 准备工作

1. `Runtime → Change runtime type` 选 **T4 GPU**（Kokoro TTS 快很多；CPU 也能跑）
2. 左侧 🔑 **Secrets** 建一个键放 LLM 密钥，二选一：
   - `GEMINI_API_KEY` = 你的 Gemini key（最简单）
   - `OPENAI_API_KEY` = 中转/自建 OpenAI 兼容端点的 key，并在 notebook 参数区填 `OPENAI_BASE_URL` / `OPENAI_MODEL`
3. （可选）想直接用预生成脚本、跳过 Step 0 的 LLM 生成：把本机 `ai_scripts/`、`ai_scripts_hot/` 上传到 `MyDrive/sleep_english_colab/`，再跑 notebook 第 8 节那格

## 本目录文件

| 文件 | 作用 |
|------|------|
| `sleep_english_colab.ipynb` | 主 notebook：拉代码 → 装依赖 → 写配置 → 预热 TTS → 起 Web UI → 出片 → 归档 |
| `setup.sh` | apt（ffmpeg / espeak-ng / Noto CJK 字体）+ pip（requirements + Kokoro 栈）+ cloudflared + 自检 |
| `colab_config.py` | 把 `configs/mode_sleep.json` 改写成 Colab 可用版本；写 LLM 通道；导出 CLI 用的 env 片段 |
| `secure_gate.py` | ASGI 令牌闸门，包住 `app.main:app`（不改 app/ 任何代码） |
| `serve.sh` | 起 uvicorn + cloudflared 快速隧道，打印带令牌的公网链接 |

## Colab 与本机 Windows 的差异

| 能力 | Colab | 说明 |
|------|:---:|------|
| Web 控制台（全部页面） | ✅ | 隧道 + 令牌闸门，功能与本机 `localhost:8766` 一致 |
| Step 0 LLM 脚本生成 | ✅ | 需自备 Gemini / OpenAI 兼容端点密钥 |
| Kokoro TTS | ✅ | 首次下载模型 ≈300MB；GPU 上明显更快 |
| Qwen3-TTS / MOSS-TTS | ❌ | 依赖本机 `H:\models\` 下的大模型，Colab 没有；配置里已清掉这些路径 |
| 预生成脚本库 | ⚠️ | `ai_scripts/`、`ai_scripts_hot/` 未进 Git → 克隆后为空，按上面第 3 步从 Drive 同步 |
| 片头库 / 片尾库 | ⚠️ | `configs/intro_videos/` 未进 Git → 库为空，自动回落默认 Pillow 片头；也可在「片头库」页现场生成 |
| AI 缩略图 / AI 背景图 | ⚠️ | 默认走 MCP（本机 Codely 登录态），Colab 没有 → 自动回落 Pillow 卡片；想用 AI 就在「参数配置」页填 aixoras / sensenova 密钥 |
| BGM 混音 | ❌ | `bgm_music_60s/` 未进 Git，且 `colab_config.py` 强制关闭（自备音乐后可在页面里打开） |
| 4K 放大 | ⚠️ | 默认关（`--no-4k`），开了在 Colab 上很慢 |
| 频道矩阵 / 频道素材 | ⚠️ | `configs/channels/` 未进 Git → 空白开始，可在页面里重建 |
| 运行历史 | ⚠️ | 只认本次会话的本地渲染目录；重启 Runtime 后清空（成品已归档到 Drive） |

## 目录与持久化

- **渲染目录**：`/content/sleep_english_output`（容器本地）。一期 200 组会产生上千个卡片/音频小文件和几个 GB 中间素材，写 Drive FUSE 会慢到不可用，所以渲染一律在本地。
- **归档目录**：`MyDrive/sleep_english_colab/output/<run 名>/`。notebook 第 7 节那格只拷成品：`videos/*.mp4`、`thumbnail.jpg`、`subtitles/`、`script.json`、`youtube_metadata.json`。
- LLM 分批缓存（`output/sleep/.sleep_cache/`）留在本地：同一会话内重跑同主题零 LLM 成本，重启后失效。

## 安全（重要）

`GET /api/config` 会原样返回整份配置，**里面含 API Key**；cloudflared 快速隧道给的是公开 URL。
所以 `secure_gate.py` 强制校验令牌：

- 首次用 serve.sh 打印的 `<url>/?ct=<令牌>` 打开，闸门种下 cookie，之后页面里的 `/api` 请求自动带 cookie，前端零改动
- 命令行探测走请求头 `X-Colab-Token`
- `COLAB_ACCESS_TOKEN` 为空时闸门放行（等于只在本机 127.0.0.1 用，不挂隧道）

**那条带令牌的链接等于完整控制权（能读密钥、能跑视频消耗额度），不要转发、不要贴到公开 issue。**
怀疑泄漏就换令牌：`export COLAB_ACCESS_TOKEN=新令牌` 后重跑 serve 格。

密钥只从 Colab Secrets / 环境变量读，`colab_config.py` 写进 Colab 容器内的 `configs/`（已被 `.gitignore` 排除），日志里只打印脱敏值。

## 不开 notebook 也能跑（纯命令行）

```bash
git clone --depth 1 https://github.com/collinsgraciano/sleep_english.git /content/sleep_english
cd /content/sleep_english
REPO_DIR=/content/sleep_english bash colab/setup.sh

LLM_PROVIDER_TYPE=gemini GEMINI_API_KEY=xxx \
COLAB_OUTPUT_DIR=/content/sleep_english_output \
  python3 colab/colab_config.py

source /content/colab_env_llm.sh
cd pipeline
python3 pipeline.py --topic "Ordering Coffee" --cefr A2 --sleep-pairs 10 --no-4k \
  --output /content/sleep_english_output
```

自定义 OpenAI 兼容端点则改成：
`LLM_PROVIDER_TYPE=openai OPENAI_API_KEY=xxx OPENAI_BASE_URL=https://your-endpoint/v1 OPENAI_MODEL=your-model`。

## 常见问题

| 现象 | 处理 |
|------|------|
| serve 格没打印公网 URL | 看 `/content/colab_logs/cloudflared.log`；重跑 serve 格即可（Web 服务本身在跑，命令行出片不受影响） |
| 链接打不开 / 401 | 必须用带 `?ct=令牌` 的完整链接；或令牌已换、cookie 过期，重新用新链接打开 |
| 跑一半断线 | Colab 空闲约 90 分钟回收 VM。重开 notebook 依次跑第 1-3 格，再跑命令行出片格 —— 音频/卡片按文件续传，也可加 `--resume` |
| 中文显示豆腐块 | setup.sh 已把 Noto CJK 软链到 `media_utils.py` 期望的 `truetype/noto/` 路径；看自检里 `FONT_ZH` 是否 `ok` |
| pip 报 externally-managed-environment | setup.sh 已自动回退 `--break-system-packages` |
| 自检 `cuda=False` | Runtime 没选 GPU；CPU 也能出片，只是 TTS 和合成都慢 |
| 想改参数 | Web 控制台「参数配置」页改完直接生效（写回 Colab 容器内的 `configs/mode_sleep.json`）；notebook 参数区只决定首次落盘值 |

## 对本机 Windows 的影响

无。`colab/` 是纯新增目录，`app/`、`pipeline/`、`configs/` 一行没改；本机照旧 `run.bat`。
