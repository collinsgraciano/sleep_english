# sleep_english @ VPS 45.13.214.22 — 部署与运维说明

> 部署时间：2026-10-08（UTC） · 机器：Ubuntu 24.04 LTS / 1 vCPU (Xeon E5-2683 v4 @2.1GHz) / 2 GB RAM
> 代码基线：`git clone https://github.com/collinsgraciano/sleep_english.git` @ `a2e3244`（与本机 HEAD 一致）+ 本说明末尾列出的补丁

---

## 1. 访问

| 项 | 值 |
|---|---|
| Web 控制台 | **http://45.13.214.22:8766** |
| 监听 | `0.0.0.0:8766`（systemd 服务 `sleep-english`） |
| 认证 | **无**（按部署要求直连）。⚠️ 控制台无鉴权：任何扫到该端口的人都能读取 `configs/` 里的 API Key 并用你的额度跑视频 |

**想加一层令牌闸门（60 秒，仓库自带中间件）**：
```bash
# /etc/systemd/system/sleep-english.service 里把 ExecStart 改成：
#   /opt/sleep_english/.venv/bin/python -m uvicorn secure_gate:app --host 0.0.0.0 --port 8766
# 并在 .vps_env 增加 COLAB_ACCESS_TOKEN=<随机串>；PYTHONPATH 需包含 repo 根与 colab/
#   Environment=PYTHONPATH=/opt/sleep_english:/opt/sleep_english/colab
systemctl daemon-reload && systemctl restart sleep-english
# 之后用 http://45.13.214.22:8766/?ct=<令牌> 打开（首次种 cookie，24h 内免带）
```

---

## 2. 目录

```
/opt/sleep_english/           仓库根（.venv 虚拟环境、configs 运行配置、output 产物）
  .venv/                      Python 3.12 虚拟环境（torch 2.14.1+cpu / kokoro 0.9.4 / misaki 0.9.4 / spacy 3.8.16）
  .vps_env                    systemd EnvironmentFile（线程数/HF/卸载开关/4K 编码参数）
  configs/                    从本机迁移的配置（路径已改写为 Linux；含 API Key，权限 600）
  bgm_music_60s/              版权 BGM 音乐库：35 首 60s mp3（44.1kHz 立体声，86MB）
  bgm_music -> bgm_music_60s  兜底软链（防止回落到 <repo>/bgm_music 的代码路径找不到音乐）
  output/sleep/<run>/         每次运行：audio/ cards/ subtitles/ videos/ + script.json
  logs/                       安装与运行日志（pip.log、setup_sh.log、run_*.log、mem_*.log）
  run_cli.sh                  命令行出片包装（自动注入 WBK_API_KEY）
  run_with_mem.sh             带内存采样的运行包装
  monitor_run.sh              后台内存/进度采样
  verify_deploy.py            部署自查
  measure_kokoro.py           内存/耗时实测
  smoke_pass1.sh / monitor_4k.sh / ab_4k.sh   冒烟与 4K 实测脚本
/root/.cache/huggingface/hub/models--hexgrad--Kokoro-82M/   Kokoro-82M 权重(312MB) + 音色
/etc/systemd/system/sleep-english.service
/swapfile(1G 原有) + /swapfile2(3G 新增) = 4 GB swap
```

---

## 3. 常用命令

```bash
systemctl status sleep-english        # 状态（含内存/CPU）
systemctl restart sleep-english       # 重启（改配置后必须重启才生效）
journalctl -u sleep-english -f         # 跟随日志
tail -f /opt/sleep_english/logs/mem_run_*.log   # 内存采样

# 命令行出片（等价 Web 一键生成；参数与 CLI 一致）
bash /opt/sleep_english/run_cli.sh --resume --sleep-pairs 50
bash /opt/sleep_english/run_cli.sh --resume --upscale-timeout 43200   # 只补 4K

# 部署自查
cd /opt/sleep_english && .venv/bin/python verify_deploy.py
```

---

## 4. 迁移时改过的配置（`configs/`）

| 键 | 原值（Windows） | VPS 值 | 原因 |
|---|---|---|---|
| `output_dir` | `H:/2026_main_project/sleep_english/output` | `/opt/sleep_english/output` | Linux 路径 |
| `topics_file` | `H:/.../colab_listening_b/topics.json` | `/opt/sleep_english/pipeline/topics.json` | 仓库自带主题池 |
| `bgm_music_dir` | `H:\...\bgm_music_60s` | `/opt/sleep_english/bgm_music_60s` | 目录不存在亦可（默认不混 BGM） |
| **`upscale_timeout`** | 3600 | **43200** | 1 核 4K 重编码远超 1 小时；不放大必超时失败 |
| **`sleep_batch_pairs`** | 50 | **25** | 模型一次要恰好 50 组太容易翻车（实测连出 55/53/51 组 + JSON 截断）；25 组/批更稳，配合下面的超量容忍 |
| `sleep_pairs` | 200 | **50** | 首期规模；跑通后再调大 |
| `no_4k` | false | false | 保留 4K 产出 |
| `wbk_api_key` | 空 | **已填**（取自本机 `%USERPROFILE%\.dsh\.credentials.yaml` 的 WBK_API_KEY） | Step 0 需要；见 §10 |
| **`wbk_model` / `wbk_thinking`** | `cn:auto` / default | **`cn:glm-5.3-flash` / `low`** | `cn:auto` 实际路由到 glm-5.3 且开思考，长输出会撞 7864 upstream 330s 上限被掐断；实测换模型后 25 组脚本 8–15s 完成 |
| 频道档案 `configs/channels/*.json` | 各自含 Windows 路径 | 递归改写为 Linux 路径 + 注入 `upscale_timeout` | 频道维度出片同样可用；**保留各频道自己的 `no_4k`/`sleep_pairs` 不动** |

未复制：`font_scan_cache.json`（本机字体路径缓存，无意义）、`output/`（历史产物）、`bgm_music_60s/`（无 BGM 素材）。

---

## 5. 实测数据（本机 + 本 VPS，均为真实测量）

### 5.1 内存（RSS）

| 阶段 | 进程 | 实测峰值 |
|---|---|---|
| 空载 Web 服务 | uvicorn | **59–72 MB** |
| `import torch`（CPU 版） | — | 219 MB |
| 加载 Kokoro 管线后 | — | 1102–1239 MB |
| **TTS 阶段（真实 pipeline，无 arena 调优）** | python | **1907 MB**（avail 一度仅 137 MB） |
| **TTS 阶段（Web 路径，`MALLOC_ARENA_MAX=2`，单跑）** | python | **1194 MB**（同参数 CLI 路径约 1595 MB） |
| **Step5 合成（TTS 已卸载）** | python + ffmpeg | **125 MB + 229 MB = 354 MB** |
| 卸载后常驻 | python | **382–507 MB**（`unload()`+`malloc_trim` 共归还约 1.0–1.3 GB） |
| **Step6 4K（默认参数）** | ffmpeg | **1384–1389 MB**（+ web 28 MB） |
| **Step6 4K（`rc-lookahead=10`）** | ffmpeg | **647 MB**（输出体积几乎不变：39414 vs 39507 KB） |
| ⚠️ 两路并发（Web + CLI 同时跑） | 两个 python | **2.7 GB**：swap 吃到 2.1 GB，进程进 D 状态，速度降到 1/3 —— **绝对不要并发**（`run_mutex` 只在同一进程内生效，跨进程没有互斥） |

> 全程 `dmesg` **零 OOM 记录**；4 GB swap 实际只用到 100–270 MB。
> 结论：**2 GB 内存能跑通（含 4K）**，但 TTS 阶段是唯一贴边的地方 —— 依赖 4 GB swap + 卸载钩子 + 4K 收 lookahead 这三项才安全。

### 5.2 耗时（1 vCPU）

| 项目 | 实测 | 换算 |
|---|---|---|
| Kokoro 单条音频（含 loudnorm+mp3） | **8.6–9.8 s/条**（内存压力大/并发时实测 20–25 s/条） | 1 组 = 4 条 |
| TTS | 10 组(42条) 约 7 min · 50 组(202条) 约 33 min · 200 组(802条) 约 2.2 h | |
| Step5 合成（720p） | 4.5 min 成片用 6.2 min ≈ **1.4× 实时** | 50 组≈35 min · 200 组≈2.3 h |
| Step6 4K（271 s 成片） | **22.5 min ≈ 5× 实时** | 50 组≈2.1 h · 200 组≈8.5 h |
| **单期合计** | 50 组 ≈ **3.0–3.5 h** · 200 组 ≈ **12–14 h** | 4K 占一半以上 |

### 5.3 磁盘

- 每次 50 组运行约 1–2 GB（音频 ~25 MB / 卡片 ~5 MB / 块 ~200 MB / PCM 中间文件 ~270 MB 跑完即删 / 720p 成片 ~100 MB / 4K 成片 ~300 MB–1 GB）
- 200 组运行 PCM 中间文件约 1.06 GB（跑完自动删除）
- 当前根分区 45 GB、已用 39%（约 26 GB 可用）→ 约可存 15–20 期，注意清理 `output/sleep/` 旧运行

---

## 6. 混 BGM（版权音乐）

- **音乐库**：`/opt/sleep_english/bgm_music_60s/` —— 35 首、每首 60.00 s、44.1 kHz 立体声 mp3，合计 86 MB（≈35 分钟素材）。混音时按**随机打乱后循环**取用（`music_idx % len(files)`），所以任何片长都够，不需要手动拼。
- 该目录在 `.gitignore` 里（音乐是用户资产、不入库）→ `git pull` / `git reset --hard` / `vps_update.sh` **都不会删**它。
- 配置：`bgm_music_dir` 已指向 `/opt/sleep_english/bgm_music_60s`；`bgm_mix` 默认 **false**（不混）。`bgm_music` 是指向它的软链兜底。
- **两个使用入口**：
  1. 参数配置页勾「混 BGM」→ 该次运行 Step 5.5 预混，输出 `{标题}_bgm.mp4`（4K 以它为源）；
  2. 运行历史页对**已完成**成片点「混BGM」/「混BGM 4K」→ 只重编码音频、视频流 `-c:v copy`，最省内存（推荐对 4K 成片用这个）。

> 报错对照：音乐库为空时，运行中的 Step 5.5 只会打印「音乐库不存在 … 跳过混音」，而运行历史页的按钮会抛 `FileNotFoundError: 未找到可选的音乐文件`。现在库已就位，两者都能正常工作。

### 混 BGM 的内存代价（选看）

`mix_bgm_into_video` 用 pydub 把**整条音轨读进内存**，再生成等长 BGM，再 `overlay` → 峰值约 3 份 PCM ≈ **每视频分钟 30–40 MB**：

| 片长 | 峰值估算 | 2 GB 机器上的判断 |
|---|---|---|
| 4.5 分钟（冒烟片实测） | ≈ 0.15 GB | 安全 |
| 25 分钟（50 组） | ≈ 0.8–1.0 GB | 可跑（叠加"TTS 已卸载后 ~560 MB" ≈ 1.4–1.6 GB，会用到 swap） |
| 100 分钟（200 组） | ≈ 3 GB+ | **必然 OOM ⇒ 200 组不要开 `bgm_mix`**；改 50 组分批，或先出无 BGM 成片再用按钮单独混 |

侧链参数沿用配置：`sidechain`、base −15 dB、ratio 8:1、attack 5 ms、release 400 ms、起始章节 1（章节时间戳取自运行目录的 `youtube_metadata.json`）。

## 7. 推荐工作流

1. **首期先 50 组**（约 3 小时），确认出片质量与内存表现后再上 200 组。
2. 多期连续出片用控制台「**批量生成队列**」：队列天然串行（`run_mutex` 全局互斥），不会并发抢内存。
3. **4K 是耗时大头**（占 2/3）：要求快就把「跳过 4K 产出」勾上，之后在「运行历史」页对某期单独点「生成 4K」，或 `run_cli.sh --resume --upscale-timeout 43200`。
4. 夜里挂机时建议先 `systemctl restart sleep-english`（把 Web 进程内存清干净），再启动大期数任务。

---

## 8. 内存偏紧时的处置（按优先级）

| 手段 | 效果 |
|---|---|
| 已是默认：`SLEEP_UNLOAD_TTS=1` + `malloc_trim` | TTS 后归还 ~1.0 GB，让合成/4K 阶段峰值只由 ffmpeg 决定 |
| 已是默认：`SLEEP_4K_X264_PARAMS=rc-lookahead=10` | 4K ffmpeg 峰值 1384 → 647 MB |
| 已是默认：`MALLOC_ARENA_MAX=2` | TTS 峰值 −270 MB 量级 |
| 已是默认：swap 4 GB、`OOMScoreAdjust=500` | 万一爆内存，内核优先杀本项目（有断点续传），不会带走 workbuddy2api 等既有服务 |
| 降规模：`sleep_pairs` 改 25/50 | 不降峰值（峰值与组数无关），但缩短高占用窗口 |
| 换引擎参数：4K 用 `-preset fast` | 可再省一些内存/时间，画质略降（改 `SLEEP_4K_X264_PARAMS` 即可，无需改代码） |
| 极端情况：先 `--no-4k` 出 720p，另起 CLI 补 4K | 4K 在独立进程里跑，python 侧只有 ~125 MB |

**别做的事**：
- **不要同时跑两个出片任务**（Web 控制台一个 + 命令行一个）。`run_mutex` 只在同一进程内生效，跨进程没有互斥；实测两路并发时两个 Kokoro 进程合计 2.7 GB，swap 吃到 2.1 GB，速度降到 1/3（进程长时间处于 D 状态）。批量队列是串行的，可以放心用。
- 不要在 TTS 阶段（约前 35–60 分钟）同时跑第二个重任务。
- 不要用「原生 4K 渲染」（`sleep_4k_native`）——那会把 4K 内存压力提前到合成阶段，1 核机器不划算。

---

## 9. 本次为跑通/省内存改的项目代码（9 个文件，均向后兼容）

| 文件 | 改动 | 默认行为 |
|---|---|---|
| `pipeline/tts_engine.py` | 新增 `TTSEngine.unload()`（清管线 + `gc.collect` + `torch.cuda.empty_cache` + glibc `malloc_trim(0)`） | 无调用则不生效，本机不变 |
| `pipeline/pipeline.py` | Step2 末按 `SLEEP_UNLOAD_TTS=1` 调 `unload()`；Step6 4K 命令支持 `SLEEP_4K_X264_PARAMS` | 两个 env 都不设 = 原行为 |
| `pipeline/media_utils.py` | 新增 `extra_4k_x264_params()` | 无 env = 返回空 |
| `app/pipeline_service.py` | args 补 `upscale_timeout`/`upscale_engine` 接线（主流程与「生成 4K」按钮都读配置）；4K 命令支持同一 env | 配置缺省=3600/ffmpeg，同旧默认 |
| `app/config_manager.py` | `PARAM_SPEC` 增加 `upscale_timeout`（配置页可见可保存，不再被表单覆盖丢掉） | 默认 3600 |
| `pipeline/llm_client.py` | `WBK_BASE_URL` 支持环境变量覆盖 | 不设=原硬编码地址 |
| `pipeline/font_scanner.py` + `pipeline/sleep/sleep_cards.py` | **修复 Linux 崩溃**：字体候选链只保留真实存在的文件；`font_covers` 对不存在的文件恒 False | Windows 上链不变（候选都存在） |
| `pipeline/sleep/llm_client_sleep.py` | **LLM 超量容忍**：`SLEEP_ALLOW_PAIR_OVERSHOOT=1` 时，批次"多给几组"截断到需要数量（少给仍失败并重试） | 不设 env = 与原「必须恰好 N 组」一致 |

> 本机仓库同样打了这些补丁（未提交）。建议在本机 `git add -A && git commit` 后推送，VPS 侧再 `git pull` 才不会冲突；VPS 上的补丁文件已是最终版本，**不要**在该目录直接 `git checkout` 覆盖。

---

## 10. 已知限制与注意

1. **LLM 通道**：`llm_provider=wbk` → `WBK_BASE_URL=http://127.0.0.1:7864/v1`（本机 WorkBuddy Manager）。`wbk_api_key` 用的是本机 DSH 凭据里那把 `wbk_Y834AdzM…`（manager 库里 id=5、name=`dsh`、realm=cn 的 key，**明文只存在客户端，服务端只存 hash**）。若要与本机 DSH 用量隔离，建议在 Manager 面板另建一把 key 再填到配置页。
2. **Qwen3-TTS / MOSS-TTS 引擎不可用**（模型权重不在 VPS，且 0.6B/1.7B 在 2 GB 内存上不可行）；`tts_engine` 保持 `kokoro`。
3. **AI 超分不可用**（需 CUDA）：`upscale_engine` 保持 `ffmpeg`，4K 走 lanczos。
4. **片头/片尾手写体**：Windows 专属手写字体在 Linux 不存在，已自动回退 Noto CJK（不崩、外观略不同）。
5. **MCP/生图类步骤**（AI 缩略图、AI 背景图）依赖外部服务与积分；失败会回退 Pillow 卡片，不影响出片。
6. **LLM 分批必须"恰好 N 组"**是原实现的硬校验，模型（cn:auto → glm-5.3）实测经常多给 1–5 组，会让整次运行失败。VPS 上已开 `SLEEP_ALLOW_PAIR_OVERSHOOT=1`（超量截断）+ 把 `sleep_batch_pairs` 降到 25。
   另外 7864 端点对单次生成有 **330s upstream 上限**：`cn:auto`（glm-5.3 + 思考）跑长 JSON 会被掐断（日志 `WBK stream ended without [DONE]`）。实测三个候选都能在 8–15s 内产出合法 25 组 JSON：
   | 模型 | 耗时 | 结果 |
   |---|---|---|
   | `cn:fast-model` | 8.1 s | [DONE] ✓ / 25 组 ✓ |
   | `cn:glm-5.3-flash` + `reasoning_effort=low` | 13.3 s | [DONE] ✓ / 25 组 ✓ |
   | `cn:deepseek-v4.1-flash` + `low` | 15.1 s | [DONE] ✓ / 25 组 ✓ |
   现配置用 `cn:glm-5.3-flash` + `wbk_thinking: low`。换别的模型前请先跑 `bash /opt/sleep_english/test_llm.sh` 复测。
7. root 密码已在聊天中出现过，建议 `passwd` 更换或改用 SSH 公钥登录。

---

## 11. 排障速查

| 现象 | 处理 |
|---|---|
| 控制台打不开 | `ss -ltnp \| grep 8766`；`systemctl restart sleep-english`；看 `journalctl -u sleep-english -n 50` |
| Step 0 报 `WBK_API_KEY not set` | CLI 用 `run_cli.sh`（会自动注入）；Web 路径在配置页确认 `wbk_api_key` 非空 |
| Step 2 报 `No module named 'kokoro'` | `.venv` 被破坏 → 重装（注意 spacy/thinc 版本互斥，见安装日志 `logs/rebuild_venv.sh` 思路） |
| 4K 报「4K 生成超时」 | 确认配置页 `4K 放大超时(秒)` ≥ 43200（或 `run_cli.sh --upscale-timeout 43200`） |
| 出片中途被 OOM kill | `dmesg -T \| grep -i oom`；`run_cli.sh --resume` 续跑（音频/卡片按文件续传）；或降 `sleep-pairs` |
| 字体相关报错 | `verify_deploy.py` 的字体段；Noto CJK 软链由 `colab/setup.sh` 建立 |
| 点「混BGM」报 `未找到可选的音乐文件` | `ls /opt/sleep_english/bgm_music_60s/*.mp3 \| wc -l` 应为 35；`ls -l /opt/sleep_english/bgm_music` 应是指向它的软链（本库在 .gitignore 里，重新 clone 后需要再上传一次） |
| 混 BGM 时进程被杀 | 片长过长（见 §6 的内存表）：50 组以内混、200 组分批或用按钮对已完成成片混 |
