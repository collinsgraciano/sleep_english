# colab — 在 Google Colab 上跑完整 Web 控制台（默认 CPU · 配置/音色/成品全落 Drive）

一条 notebook 就是整套环境：**拉最新代码 → 装依赖 → 挂 Drive → 配置落 Drive →
Kokoro 全部音色与试听落 Drive → 起完整 Web 控制台（Cloudflare 免费域名 + 令牌闸门）→
出片 → 成品归档回 Drive**。

- **默认 CPU**（不需要抢 GPU，结果与机器无关）；想用 GPU 改一行参数即可。
- **配置的事实源是 Google Drive**：`<repo>/configs` 被 symlink 到
  `MyDrive/<DRIVE_DIR>/configs`，于是控制台里改的每一个参数、填的每一个密钥、建的频道与
  片头库都直接落在 Drive 上 —— VM 被回收也不丢，下回开机自动恢复。
- **音色的事实源也是 Drive**：28 个 Kokoro 英语音色权重（+中文兜底）与全部试听 mp3 都存
  Drive，开机只做补缺同步，控制台「三音色页」立刻显示「✓ 已缓存」且全部可点播。
- notebook 只是薄引导层：安装 / 配置 / 启动 / 归档 / 音色同步的逻辑全在 `colab/` 的脚本里，
  所以仓库一更新，重跑那格就吃到新版本。

## 两版 notebook

| notebook | 形态 | 什么时候用 |
|---|---|---|
| `colab/sleep_english_colab.ipynb` | **只有一个单元格**，一键跑完全部步骤 | 日常默认。参数区在最上面，`STEPS` 可指定只跑某几步 |
| `colab/sleep_english_colab_steps.ipynb` | 拆成多格 | 想逐步执行、单独重跑某一步、逐步看每一步输出 |

两版调用的都是同一批 `colab/` 脚本，行为等价。

## 打开方式

仓库是**公开**的，直接开（无需 GitHub 授权、无需 PAT）：

- `https://colab.research.google.com/github/collinsgraciano/sleep_english/blob/master/colab/sleep_english_colab.ipynb`
- 或 Colab 首页 → GitHub 标签页 → 粘贴 `https://github.com/collinsgraciano/sleep_english` → 选上面那个 notebook
- 或把 notebook 下载下来，`File → Open notebook → Upload`（之后重跑第 1 格仍会从 GitHub 拉最新代码）

## 准备工作

1. `Runtime → Change runtime type` **保持默认 CPU 即可**（Kokoro 82M 在 CPU 上完全跑得动；
   本项目的 Colab 版默认 `KOKORO_DEVICE=cpu`，即使分配到 GPU 也走 CPU —— 结果可复现。
   想用 GPU：参数区 `FORCE_CPU = False`，并把运行时切成 T4）。
2. 左侧 🔑 **Secrets**（键名可在参数区改）：

| Secret | 必需 | 说明 |
|---|:---:|---|
| `GEMINI_API_KEY` | 二选一 | Step 0 现场生成脚本走 Gemini |
| `OPENAI_API_KEY` | 二选一 | 中转 / 自建 OpenAI 兼容端点，另需在参数区填 `OPENAI_BASE_URL` / `OPENAI_MODEL` |
| `CF_TUNNEL_TOKEN` | 可选 | 想用**固定域名**才需要，见「域名」一节 |
| `GITHUB_TOKEN` | 不需要 | 仓库公开，匿名克隆即可；只有私有 fork 才填 `SECRET_GITHUB_TOKEN` |

3. 从上往下运行。单格版直接运行那一格；分格版**必须**跑完「拉码 / 装依赖 / 配置」三步再起控制台。

> 只用预生成脚本出片（控制台「预生成脚本」页）时**不需要任何 LLM 密钥**，零 API 调用。

> **会话生命周期（最容易踩的坑）**：Colab 空闲约 90 分钟会回收 VM，**`/content` 下的东西
> 全都没了 —— 包括 pip 装好的依赖**（Drive 上的配置/音色/成品不受影响）。
> 重连后从 `code` 步重跑一遍（`setup` → `drive` → `config` → `voices` → `console`）即可；
> 单格版默认就是这个顺序。如果只重跑了「配置/启动」而没跑 `setup`，控制台能起来，
> 但一出片就会报 `ModuleNotFoundError: No module named 'kokoro'` —— 现在 `setup` 步会实检
> `import kokoro` 并在缺失时**直接失败**（不会假装装好），单格版的 TTS 相关步骤也会先自动补装。

## Drive 目录布局

```
MyDrive/<DRIVE_DIR>/                 # DRIVE_DIR 默认 sleep_english_colab
├── configs/            ← 仓库 configs/ 的实体（symlink 目标）
│                          mode_sleep.json（含 LLM 密钥）、llm_providers.json、
│                          intro_library.json + intro_videos/、channels/、配色预设、
│                          voice_previews/（全部音色试听 mp3）、脚本库… 控制台里改的全在这里
├── kokoro/voices/      ← 28 个 Kokoro 英语音色权重 + 中文兜底音色（.pt，约 30MB）
│                          开机由 colab/kokoro_voices.py 同步回本地 HF 缓存
├── scripts/            ← 预生成脚本镜像：ai_scripts/、ai_scripts_hot/
│                          （本机用 scripts_sync.py --push 传上来，Colab 开机自动拉回仓库）
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
- `configs/` 与 `kokoro/` 里的都是**小文件**（JSON/配置/几百 KB 的音色），走 Drive 完全没问题。
- **330MB 的 Kokoro 模型不进 Drive**：Drive FUSE 不支持 symlink，HF 的 snapshot 布局会被迫退化成
  整份复制，收益低于每次开机重新下载（Colab 拉 HF 很快；拉不动时用 `COLAB_HF_ENDPOINT=https://hf-mirror.com`）。

## 每次开机发生了什么（单格版的步骤顺序）

| 步骤 | 脚本 | 做了什么 |
|---|---|---|
| `code` | notebook | `git pull --ff-only`，失败（工作树被改过）就整目录浅克隆最新代码 |
| `setup` | `colab/setup.sh` | apt（ffmpeg / espeak-ng / Noto CJK 字体软链）+ pip（requirements + Kokoro 栈 + spacy 模型）+ cloudflared + 自检。**装过一次就秒过**（标记文件 + 实检；`FORCE_INSTALL=True` 强制重装）；`import kokoro` 实检不过就**不写标记并 exit 1**（pip 细节见 `/content/colab_logs/pip.log*`） |
| `drive` | notebook | 挂 Drive，写 `/content/colab_env.sh` |
| `config` | `colab/drive_config.py` + `colab/colab_config.py` | 配置播种/链接到 Drive、恢复运行状态、访问令牌、建 `kokoro/`+`scripts/`、从 Drive 拉预生成脚本；平台归一化（输出目录/主题库/Kokoro/关 4K/关 BGM/清 `H:\` 路径）+ 从 Secrets 刷新 LLM 密钥 |
| `voices` | `colab/kokoro_voices.py` | 预热模型 + Drive→本地补缺音色 + 补齐缺失音色 + 生成全部试听 mp3 + 本地→Drive 回写（幂等） |
| `console` | `colab/serve.sh` | uvicorn（`secure_gate:app` 令牌闸门）+ cloudflared 隧道 → 渲染**可点击**的带令牌链接（并落 `public_url.txt` / Drive `state/last_public_url.txt`） |
| `smoke` / `full` | notebook | 可选的命令行出片（10 组冒烟 / 参数区组数），中断重跑走文件级续传 |
| `archive` | `colab/archive_to_drive.py` | 成品归档到 Drive + 状态回写（`COLAB_AUTO_ARCHIVE=1` 时控制台跑完也会自动归档） |
| `ops` | notebook | 日志尾巴、成品清单、可选停服务 |

只跑其中几步：把参数区 `STEPS` 改成 `["voices"]`、`["config","console"]` 之类即可（每步幂等）。

## 仓库结构：为什么是「公开单仓库」

第 1 点问的是「把 GitHub 仓库拆开会不会更好」。结论：**当前选择公开单仓库**（已按此落地），
理由与替代方案如下 ——

| 方案 | 好处 | 代价 |
|---|---|---|
| **公开单仓库**（现状） | 免 PAT、免 Colab 授权直开 notebook；代码与 Colab 脚本永远同一版本；只推一次 | 仓库内容（含教学脚本、开发用 skill/笔记）对所有人可见 |
| 独立公开「启动器」仓库（只放 `colab/` + notebook，运行时再克隆私有主仓库） | Colab 打开 notebook 不用授权 | 仍要 PAT 才能拉主仓库，收益基本只剩「少一次 OAuth」；两处脚本要同步 |
| 拆成「公开 pipeline+colab」+「私有 web app」 | Colab 端零密钥 | `pipeline/` 与 `app/` 共享模块会出现两份（参考项目 `colab_listening_b` / `colab_listening_b_web` 就是这个形态，代价是长期同步） |

真要改成拆分形态，改动面很小：参数区 `REPO_URL` 指到那个仓库、`SECRET_GITHUB_TOKEN` 填上即可，
notebook 其余部分不用动。

## 音色与试听缓存（全部落 Drive）

- **权重**：`<Drive>/kokoro/voices/*.pt`（28 个英语音色 + `zf_xiaoxiao` / `zf_xiaobei` 中文兜底）。
  开机 `--sync-in` 把 Drive 上的补进本地 HF 缓存（`~/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/voices/`）
  —— 控制台的「已缓存」判定扫的就是这里，所以补进去就变「✓ 已缓存」。
- **试听**：为每个英语音色生成 mp3 到 `configs/voice_previews/`（在 Colab 上就是 Drive）。
  文件名/试听文本与控制台路由共用同一套实现（`app/routers/voices_kokoro.py` + `app/tts_state.py`），
  因此生成完控制台直接认、不用重新合成。
- **单跑/重跑**：

```bash
DRIVE_ROOT=/content/drive/MyDrive/sleep_english_colab python3 colab/kokoro_voices.py --status
DRIVE_ROOT=... python3 colab/kokoro_voices.py --all --previews --sync-back
```

- **本机预灌**（不用等 Colab）：Windows 上直接跑同一个脚本，加上 Drive 桌面版的挂载路径：
  `python colab/kokoro_voices.py --sync-back --drive-root "G:\我的云端硬盘\sleep_english_colab"`
- 首次全量约 5-10 分钟（模型 330MB + 28 个音色 + 28 段试听，CPU 合成）；之后每次开机只做
  补缺拷贝（几十秒内），试听文件已存在则跳过。

## 预生成脚本从 Drive 同步

本机用 `.qoder` / `.workbuddy` 技能新写的批次，**不必为了它重新 push 代码**：

```bash
# 本机（Windows，Drive 桌面版路径）
python colab/scripts_sync.py --push --drive-root "G:\我的云端硬盘\sleep_english_colab"
# Colab 开机自动执行（COLAB_SCRIPTS_SYNC=1）
python3 colab/scripts_sync.py --pull
```

只补「目标缺失 / 源更新的」文件，跳过 `_`/`.` 前缀与 `checkpoint.json`，从不删除。
想只用 Git 管脚本：参数区 / 环境变量把 `COLAB_SCRIPTS_SYNC` 设 0。

## CPU 耗时预期（量级参考）

| 组数 | 时长（量级） | 建议 |
|---|---|---|
| 10 组（冒烟） | 5-10 分钟 | 第一次必跑，验证全链路 |
| 50 组 | 25-60 分钟 | **CPU 上推荐的第一期正式量** |
| 200 组（默认） | 1.5-4 小时 | 长跑；注意 Colab 空闲回收（见下） |

实际取决于 Colab 当次分到几个 vCPU，setup 自检会打印按设备给出的预估。
长跑建议：`KEEP_ALIVE=True`（best-effort：**浏览器标签页要一直开着**），
或者先跑 50 组、靠「归档」把成品落 Drive 再继续。

## 域名（Cloudflare 免费）

**默认：quick tunnel**（不带 `CF_TUNNEL_TOKEN` 时）

- 免费、不需要 Cloudflare 账号，每次开机拿到一个**全新的** `https://xxxx.trycloudflare.com`
- 域名每次都换，但**访问令牌不变**（存在 Drive 的 `state/access_token.txt`），
  所以只需要把新域名接到 `?ct=令牌` 上重新打开/收藏
- **notebook 会把链接渲染成可点击入口**（蓝色「▶ 点击打开 Web 控制台」，下面同时给一条
  可复制的纯文本链接）；`serve.sh` 也会把完整链接落到
  `/content/colab_logs/public_url.txt` 与 Drive 的 `<Drive>/state/last_public_url.txt`，
  所以出片跑完的收尾（`ops` 步）会**再显示一次**入口，不用担心往上翻找；
  分格版在启动格后面有一格专门用来渲染这个链接

**可选：named tunnel（固定域名）**

- 需要一个你自己的域名挂在 Cloudflare 免费档（CF 免费提供 DNS 与隧道，域名本身要自己有）
- Zero Trust → Networks → Tunnels 建隧道，public hostname 指向 `http://localhost:8766`
- 把 tunnel token 存进 Secrets 并在参数区填 `SECRET_CF_TUNNEL_TOKEN`，域名填 `CF_DOMAIN`
- 之后每次开机都是同一个域名，不用重新 bind

> 无论哪种，**令牌闸门都必须在**：`GET /api/config` 会原样返回含密钥的整份配置，
> 隧道是公开 URL，不加锁等于把密钥挂到公网。带令牌的链接 = 完整控制权，别转发。
> 链接里的令牌只在 URL 上出现一次（首次访问后种 cookie，24 小时内免带）；
> 万一泄漏：删掉 Drive 上的 `state/access_token.txt` 再重跑配置步即可换令牌。

## 日常用法

- **Web 控制台**：控制台 / 参数配置 / 主题管理 / 批量脚本 / 运行历史 / 片头库 / 片尾库 / 三音色页 /
  AI 测试，和本机 `localhost:8766` 一致。改完配置立刻落 Drive；出片跑完自动归档（`COLAB_AUTO_ARCHIVE=1`）。
- **命令行出片**：单格版把 `RUN_SMOKE` / `RUN_FULL` 设 True；分格版跑第 7 节两格。
  中断后重跑走文件级续传（音频/卡片按文件缓存），也可加 `--resume`。
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

KOKORO_DEVICE=cpu python3 colab/kokoro_voices.py --all --previews --sync-back
source /content/colab_env_llm.sh
cd pipeline
python3 pipeline.py --topic "Ordering Coffee" --cefr A2 --sleep-pairs 10 --no-4k \
  --output /content/sleep_english_output
cd .. && python3 colab/archive_to_drive.py
```

## 环境变量一览（脚本层）

| 变量 | 默认 | 含义 |
|---|---|---|
| `KOKORO_DEVICE` | 空=自动 | `cpu` / `cuda`；Colab 默认写 `cpu`（强制 CPU） |
| `COLAB_KOKORO_DIR` | `<Drive>/kokoro` | Drive 上的音色权重目录 |
| `COLAB_SCRIPTS_SYNC` | `1` | 开机是否从 Drive 拉预生成脚本 |
| `COLAB_AUTO_ARCHIVE` | Colab 上 `1` | 控制台出片完成后自动归档（本机不设=不变） |
| `COLAB_SETUP_FLAG` | `/content/.colab_setup_done` | 安装标记（幂等跳过用） |
| `COLAB_FORCE_INSTALL` | `0` | `1` = 忽略标记强制重装依赖 |
| `COLAB_PIP_LOG` | `/content/colab_logs/pip.log` | pip 安装日志（失败时脚本会把尾部打出来） |
| `COLAB_HF_ENDPOINT` | 空 | 设了就先按它作为 `HF_ENDPOINT`（例：`https://hf-mirror.com`） |
| `COLAB_LOG_DIR` | `/content/colab_logs` | uvicorn / cloudflared 日志目录 |

## 改参数的正确姿势

- **Web 控制台里改**（推荐）：写进 Drive 上的配置，跨会话保留。
- **参数区（notebook）**：只在**首次生成配置**时决定落盘值。之后它不再覆盖 Drive 上的配置 ——
  否则每次开机都会把你在控制台里调好的参数盖回去。
- 想让参数区重新生效：在环境里设 `COLAB_CONFIG_RESET=1` 再重跑配置步。
- 想彻底重来：删掉 Drive 上 `configs/mode_sleep.json`（或整个 `configs/`）后重跑配置步。

## 改了代码之后

| 改了什么 | 要做什么 |
|---|---|
| `pipeline/`、`app/` | `git push` → Colab 重跑 `code` 步（会自行 pull / 重克隆），再重启 Web 服务步 |
| `colab/` 下的脚本 | 同上（notebook 调的就是仓库里这份） |
| notebook 本身 | 重新从 GitHub 打开 |
| `requirements.txt` | 重跑 `setup` 步（想强制重装把 `FORCE_INSTALL=True`） |

## 出问题对照

| 现象 | 大概率原因 / 处理 |
|---|---|
| **出片报 `ModuleNotFoundError: No module named 'kokoro'`** | 本会话没装依赖：Colab 回收/重连后 VM 本地盘的 pip 包会丢 → 重跑 `setup` 步（要强制重装：`FORCE_INSTALL=True`）。自检/依赖实检会先报出来；单格版的 TTS 步骤也会自动补装 |
| `setup` 步失败并打印 pip 日志 | 看 `/content/colab_logs/pip.log.kokoro` 尾部；网络抖动直接重跑本步。Python 3.13 上 kokoro/misaki 的 PyPI 元数据是 `<3.13`，脚本已自动改用 `--no-deps` 安装 |
| `code` 步 404/401 | 私有 fork 没填 `SECRET_GITHUB_TOKEN`；公开仓库则检查参数区 `REPO_URL` / `BRANCH` |
| `drive` 步警告「没挂载 Drive」 | Drive 授权被跳过 → 重跑该步允许挂载；否则配置/音色/成品只在本次会话有效 |
| 配置改了但下回开机没保留 | `config` 步没跑成功（configs 没 symlink）→ 看它打印的警告，重跑该步 |
| 三音色页显示「⚠ 未缓存」 | 跑 `voices` 步（或 `--sync-in --all`）；确认 Drive 里有 `<Drive>/kokoro/voices/*.pt` |
| 试听按钮转圈很久 | 该音色试听 mp3 还没生成 → 跑 `voices` 步的 `--previews`（首次 5-10 分钟，之后即时） |
| 模型下载失败 | 设 `COLAB_HF_ENDPOINT=https://hf-mirror.com` 重跑 `voices` 步（脚本也会自动用镜像重试一次） |
| 链接打不开 / 401 | 必须用带 `?ct=令牌` 的完整链接；域名换了要用新链接 |
| 没打印公网 URL | 看 `/content/colab_logs/cloudflared.log`，重跑 `console` 步即可（本地服务本身在跑） |
| 中文显示豆腐块 | `setup` 自检里 `FONT_ZH` 是否 `ok`；apt 装字体失败就 `FORCE_INSTALL=True` 重跑 |
| `pip` 报 externally-managed-environment | setup.sh 已自动回退 `--break-system-packages` |
| 自检里 `cuda=False` | 正常（CPU 运行时；本项目默认就强制 CPU） |
| 跑一半断线 | Colab 空闲约 90 分钟回收 VM → 重开 notebook 重跑 `code`/`setup`/`drive`/`config`，再继续出片（音频/卡片按文件续传，可加 `--resume`） |
| 成品不见了 | 归档没跑（`ARCHIVE=True` / `COLAB_AUTO_ARCHIVE=1` 应已自动归档）→ 重跑 `archive` 步要在 VM 回收前做 |
| 预生成脚本页少了本机新批次 | 本机 `python colab/scripts_sync.py --push --drive-root ...` 传到 Drive，Colab 重跑 `config` 步 |
| Step 0 失败且没配 LLM 通道 | 要么配 Secrets（Gemini / OpenAI 兼容），要么用控制台里「预生成脚本」入库脚本出片（零 API 调用） |
| `configs/` symlink 让 `git status` 很脏 / pull 失败 | 正常现象（4 个被跟踪配置文件在 symlink 后面）；pull 失败时 `code` 步会自动整目录重克隆，持久物都在 Drive |

## 已知边界

- **强制 CPU 是刻意的**：结果与机器无关、不必抢 GPU；代价是慢。要快就 `FORCE_CPU=False` + T4。
- **模型（330MB）每次开机重新下载**：Drive 上只持久化音色权重与试听（见上）。
- **域名每次换**：quick tunnel 免费域名的固有特性；要固定域名走 named tunnel（需自有域名）。
- **Colab 免费档无 SLA**：VM 会被回收，「运行历史」页只认本次会话的本地输出；持久品是 Drive 上的
  配置 + 音色 + 预生成脚本 + 归档成品。
- **Qwen3-TTS / MOSS-TTS**：依赖本机 `H:\models\` 的大模型文件，Colab 上没有 → 一律 Kokoro。
- **BGM 混音**：`bgm_music_60s/` 未进 Git；想用就把音乐上传到 Drive 再在控制台里把
  `bgm_music_dir` 指向 Drive 路径（混音本身在本地算，Drive 只读输入，可接受）。
- **片头/片尾库**：`configs/intro_videos/` 随 configs 落 Drive，Colab 上现场生成的片头会持久保留；
  本机生成的片头要手动拷进 Drive 对应目录才会出现在 Colab 的片头库里。
- **预生成脚本**：Git 与 Drive 两条路都通（Git 优先、Drive 补缺），但两边的 `manifest.json`
  按「谁新用谁」覆盖 —— 别在 Drive 上手改 manifest。
- **AI 缩略图 / AI 背景图**：默认走 MCP（本机 Codely 登录态），Colab 没有 → 自动回落 Pillow 卡片；
  想用 AI 就在控制台「参数配置」页填 aixoras / sensenova 的 HTTP 生图密钥。
- **一次只开一个会话**：两个 Colab 同时挂同一个 `DRIVE_DIR` 会互相覆盖配置；要多开就给
  `DRIVE_DIR` 用不同名字（参数区可改）。
- **公开仓库**：仓库里的教学脚本、开发用 skill/笔记都可见；`configs/mode_*.json`、`llm_providers.json`、
  `page_mcp_tokens.json` 被 `.gitignore` 挡住，**密钥不进仓库**；提交历史已做过凭据扫描。

## 对本机 Windows 的影响

几乎为零：`colab/` 是纯新增目录；`pipeline/`、`app/`、`configs/` 只有两处**加法式**改动，且
默认值下行为与本机原样一致：

- `pipeline/tts_engine.py`：新增 `KOKORO_DEVICE` 环境变量支持（不设=原来的自动 cuda/cpu）；
- `app/pipeline_service.py`：出片完成后新增自动归档钩子，**只在 `COLAB_AUTO_ARCHIVE=1` 时动作**
  （本机不设该变量，等于没这段代码）。

本机照旧 `run.bat`（`http://localhost:8766`）；两边各自独立运行，互不干扰；本机的 API 密钥不会因为
Colab 而外泄（Drive 里的配置只属于你自己的 Google 账号）。
