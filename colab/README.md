# colab — 在 Google Colab 上跑完整 Web 控制台（配置存 Drive · CF 免费域名 · 每次拉最新代码）

一条 notebook 就是整套环境：**从 GitHub 拉最新代码 → 挂 Drive → 装依赖 → 配置落 Drive →
起完整 Web 控制台（Cloudflare 免费域名 + 令牌闸门）→ 出片 → 成品归档回 Drive**。

和以往 Colab 版最大的差别：**配置的事实源是 Google Drive**。
`<repo>/configs` 被 symlink 到 `MyDrive/<DRIVE_DIR>/configs`，于是 Web 控制台里改的每一个参数、
填的每一个密钥、建的频道与片头库都直接落在 Drive 上 —— VM 被回收也不丢，下回开机自动恢复。
运行状态（主题防重、集数徽章计数、LLM 批次缓存）也会随归档格备份、开机恢复。

notebook 只是薄引导层：安装 / 配置 / 启动 / 归档逻辑全在 `colab/` 的脚本里，所以仓库一更新，
重跑第 1 格就吃到新版本。

## 打开方式

推到 GitHub 后，任选一种：

- 直接开：`https://colab.research.google.com/github/collinsgraciano/sleep_english/blob/master/colab/sleep_english_colab.ipynb`
  （私有仓库：Colab 会先让你登录 GitHub 授权读仓库）
- 或 Colab 首页 → GitHub 标签页 → 粘贴仓库地址 → 选 `colab/sleep_english_colab.ipynb`
- 或把本文件所在目录里的 notebook 上传到 Colab（`File → Open notebook → Upload`）

## 准备工作

1. `Runtime → Change runtime type` 选 **T4 GPU**（Kokoro TTS 快很多；CPU 也能跑，只是慢）。
2. 左侧 🔑 **Secrets** 建键（键名可在 notebook 参数区改）：

| Secret | 必需 | 说明 |
|---|:---:|---|
| `GITHUB_TOKEN` | ✅（私有仓库） | GitHub → Settings → Developer settings → Fine-grained PAT，本仓库 **Contents: Read**。缺了它 `git clone` 会 404/401 |
| `GEMINI_API_KEY` | 二选一 | Step 0 现场生成脚本走 Gemini |
| `OPENAI_API_KEY` | 二选一 | 中转 / 自建 OpenAI 兼容端点，另需在参数区填 `OPENAI_BASE_URL` / `OPENAI_MODEL` |
| `CF_TUNNEL_TOKEN` | 可选 | 想用**固定域名**才需要，见下面「域名」一节 |

3. 从上往下依次运行。**必须**跑完第 1-3 格（拉码 → 装依赖 → 配置落 Drive）再起控制台。

## Drive 目录布局

```
MyDrive/<DRIVE_DIR>/                 # DRIVE_DIR 默认 sleep_english_colab
├── configs/            ← 仓库 configs/ 的实体（symlink 目标）
│                          mode_sleep.json（含 LLM 密钥）、llm_providers.json、
│                          intro_library.json + intro_videos/、channels/、color presets、
│                          voice 配置、batch_queue.json… 控制台里改的全在这里
├── state/
│   ├── used_topics.json        主题防重（开机恢复，跨会话接着数）
│   ├── thumb_episode.json      缩略图集数徽章计数
│   ├── sleep_cache/            LLM 批次缓存（同主题重跑零 LLM 成本）
│   └── access_token.txt        公网访问令牌（域名每次换，令牌不变）
└── output/<run>/               成品归档：videos/*.mp4、thumbnail.jpg、subtitles/、
                                script.json、youtube_metadata.json…
```

- **渲染固定在容器本地盘**（`/content/sleep_english_output`）：一期 200 组会产生上千个卡片/音频
  小文件与几个 GB 中间素材，写 Drive FUSE 会慢到不可用。所以「本地渲染 + 成品归档」。
- `configs/` 里的**小文件**（JSON/配置）走 Drive 完全没问题，`mode_sleep.json` 每次保存都是小原子写。

## 每次开机发生了什么

| 步骤 | 脚本 | 做了什么 |
|---|---|---|
| 1 | notebook 第 1 格 | `git pull --ff-only` 或浅克隆最新代码（私有仓库用 Secrets 里的 PAT，只活在内存 URL 里、日志打码）；挂 Drive；写 `/content/colab_env.sh` |
| 2 | `colab/setup.sh` | apt（ffmpeg / espeak-ng / Noto CJK 字体软链到 media_utils 期望路径）+ pip（requirements + Kokoro 栈 + spacy 模型）+ cloudflared + 自检 |
| 3 | `colab/drive_config.py` | 播种缺失配置 → `configs` symlink 到 Drive → 恢复运行状态 → 读/生成访问令牌 |
| 4 | `colab/colab_config.py` | 平台归一化（输出目录/主题库/Kokoro/关 4K 原生/关 BGM/清 `H:\` 路径）+ 从 Secrets 刷新 LLM 密钥 |
| 5 | `colab/serve.sh` | uvicorn（`secure_gate:app` 令牌闸门）+ cloudflared 隧道 → 打印带令牌链接 |
| 6 | `colab/archive_to_drive.py` | 成品归档到 Drive + 状态回写 Drive |

## 域名（Cloudflare 免费）

**默认：quick tunnel**（`colab/serve.sh` 不带 `CF_TUNNEL_TOKEN` 时）

- 免费、不需要 Cloudflare 账号，每次开机拿到一个**全新的** `https://xxxx.trycloudflare.com`
- 域名每次都换（这就是「分开域名」），但**访问令牌不变**（存在 Drive 的 state 里），
  所以只需要把新域名接到 `?ct=令牌` 上重新打开/收藏

**可选：named tunnel（固定域名）**

- 需要一个你自己的域名挂在 Cloudflare 免费档（CF 免费提供 DNS 与隧道，域名本身要自己有）
- Zero Trust → Networks → Tunnels 建隧道，public hostname 指向 `http://localhost:8766`
- 把 tunnel token 存进 Secrets 并在参数区填 `SECRET_CF_TUNNEL_TOKEN`，域名填 `CF_DOMAIN`
- 之后每次开机都是同一个域名，不用重新 bind

> 无论哪种，**令牌闸门都必须在**：`GET /api/config` 会原样返回含密钥的整份配置，
> 隧道是公开 URL，不加锁等于把密钥挂到公网。带令牌的链接 = 完整控制权，别转发。

## 日常用法

- **Web 控制台**：控制台 / 参数配置 / 主题管理 / 批量脚本 / 运行历史 / 片头库 / 片尾库 / 三音色页 /
  AI 测试，和本机 `localhost:8766` 一致。改完配置立刻落 Drive。
- **命令行出片**：notebook 第 7 节两格（冒烟 10 组 / 正式一期）。中断重跑同格走文件级续传，也可加 `--resume`。
- **纯命令行（不用 notebook）**：

```bash
git clone --depth 1 https://github.com/collinsgraciano/sleep_english.git /content/sleep_english
cd /content/sleep_english
REPO_DIR=/content/sleep_english bash colab/setup.sh

DRIVE_ROOT=/content/drive/MyDrive/sleep_english_colab \
COLAB_OUTPUT_DIR=/content/sleep_english_output \
ENV_FILE=/content/colab_env.sh \
  python3 colab/drive_config.py

LLM_PROVIDER_TYPE=gemini GEMINI_API_KEY=xxx \
COLAB_OUTPUT_DIR=/content/sleep_english_output \
  python3 colab/colab_config.py

source /content/colab_env_llm.sh
cd pipeline
python3 pipeline.py --topic "Ordering Coffee" --cefr A2 --sleep-pairs 10 --no-4k \
  --output /content/sleep_english_output
cd .. && python3 colab/archive_to_drive.py
```

## 改参数的正确姿势

- **Web 控制台里改**（推荐）：写进 Drive 上的配置，跨会话保留。
- **参数区（notebook 第 1 格）**：只在**首次生成配置**时决定落盘值。之后它不再覆盖 Drive 上的配置 ——
  否则每次开机都会把你在控制台里调好的参数盖回去。
- 想让参数区重新生效：在环境里设 `COLAB_CONFIG_RESET=1` 再重跑第 4 格（配置格）。
- 想彻底重来：删掉 Drive 上 `configs/mode_sleep.json`（或整个 `configs/`）后重跑第 3、4 格。

## 改了代码之后

| 改了什么 | 要做什么 |
|---|---|
| `pipeline/`、`app/` | `git push` → Colab 重跑第 1 格（会自行 pull / 重克隆），再重启 Web 服务格 |
| `colab/` 下的脚本 | 同上（notebook 调的就是仓库里这份，已克隆过才需要重跑第 1 格） |
| notebook 本身 | 重新从 GitHub 打开（notebook 也在仓库里） |
| `requirements.txt` | 重跑第 2 格（setup 格） |

## 出问题对照

| 现象 | 大概率原因 / 处理 |
|---|---|
| 第 1 格 `git clone` 404/401 | 私有仓库没建 `GITHUB_TOKEN` Secrets，或 PAT 没有本仓库 Contents: Read |
| 第 3 格警告「没挂载 Drive」 | Drive 授权被跳过 → 重跑第 1 格允许挂载；否则配置只在本次会话有效 |
| 配置改了但下回开机没保留 | 第 3 格没跑成功（configs 没 symlink）→ 看它打印的警告，重跑该格 |
| 链接打不开 / 401 | 必须用带 `?ct=令牌` 的完整链接；域名换了要用新链接；令牌换了重新点新链接 |
| 没打印公网 URL | 看 `/content/colab_logs/cloudflared.log`，重跑第 6 格即可（本地服务本身在跑） |
| 中文显示豆腐块 | setup 自检里 `FONT_ZH` 是否 `ok`；apt 装字体失败就重跑第 2 格 |
| `pip` 报 externally-managed-environment | setup.sh 已自动回退 `--break-system-packages` |
| 自检里 `cuda=False` | Runtime 没选 GPU；CPU 也能出片，只是慢 |
| 跑一半断线 | Colab 空闲约 90 分钟回收 VM → 重开 notebook 跑第 1-3 格，再命令行出片格（音频/卡片按文件续传，可加 `--resume`） |
| 成品不见了 | 只在本地盘、没跑第 8 格归档 → 归档要在 VM 回收前做 |
| Step 0 失败且没配 LLM 通道 | 要么配 Secrets（Gemini / OpenAI 兼容），要么用控制台里「预生成脚本」入库脚本出片（零 API 调用） |
| `configs/` symlink 让 `git status` 很脏 | 正常现象（4 个被跟踪配置文件在 symlink 后面）；pull 失败时第 1 格会自动整目录重克隆 |

## 已知边界

- **域名每次换**：quick tunnel 免费域名的固有特性；要固定域名走 named tunnel（需自有域名）。
- **Colab 免费档无 SLA**：VM 会被回收，「运行历史」页只认本次会话的本地输出；持久品是 Drive 上的
  配置 + 归档成品。
- **Qwen3-TTS / MOSS-TTS**：依赖本机 `H:\models\` 的大模型文件，Colab 上没有 → 一律 Kokoro。
- **BGM 混音**：`bgm_music_60s/` 未进 Git；想用就把音乐上传到 Drive 再在控制台里把
  `bgm_music_dir` 指向 Drive 路径（混音本身在本地算，Drive 只读输入，可接受）。
- **片头/片尾库**：`configs/intro_videos/` 现在随 configs 落 Drive，Colab 上现场生成的片头会
  持久保留；本机生成的片头要手动拷进 Drive 对应目录才会出现在 Colab 的片头库里。
- **预生成脚本**：以 Git 为准；本机新写、还没 push 的批次 Colab 看不到（push 后重跑第 1 格即可）。
- **AI 缩略图 / AI 背景图**：默认走 MCP（本机 Codely 登录态），Colab 没有 → 自动回落 Pillow 卡片；
  想用 AI 就在控制台「参数配置」页填 aixoras / sensenova 的 HTTP 生图密钥。
- **一次只开一个会话**：两个 Colab 同时挂同一个 `DRIVE_DIR` 会互相覆盖配置；要多开就给
  `DRIVE_DIR` 用不同名字（参数区可改）。

## 对本机 Windows 的影响

无。`colab/` 是纯新增目录，`app/`、`pipeline/`、`configs/` 一行没改；本机照旧 `run.bat`
（`http://localhost:8766`）。两边各自独立运行，互不干扰；本机的 API 密钥不会因为 Colab 而外泄
（Drive 里的配置只属于你自己的 Google 账号）。
