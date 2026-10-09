# sleep_english @ VPS 45.13.214.22 — 部署与运维说明

> 部署时间：2026-10-08（UTC） · 机器：Ubuntu 24.04 LTS / 1 vCPU (Xeon E5-2683 v4 @2.1GHz) / 2 GB RAM
> 代码基线：`git clone https://github.com/collinsgraciano/sleep_english.git` @ `a2e3244`（与本机 HEAD 一致）+ 本说明末尾列出的补丁

---

## 1. 访问（带密码闸门）

| 项 | 值 |
|---|---|
| Web 控制台 | **http://45.13.214.22:8766** |
| 监听 | `0.0.0.0:8766`（systemd 服务 `sleep-english`，入口 `serve_auth:application`） |
| **认证** | **密码闸门已启用**：密码 `inriynisse`（`.vps_env` 的 `SLEEP_AUTH_PASSWORD`） |
| cookie | `sleep_auth`，**365 天**（`Max-Age=31536000`，HttpOnly + SameSite=Lax），首次输密码后免输 |

三种登录方式（都种同一个 365 天 cookie）：
1. 浏览器打开 `http://45.13.214.22:8766/` → 输入密码的登录页；
2. 直接访问 `http://45.13.214.22:8766/?ct=inriynisse` → 免输密码，种 cookie 后自动跳回（密码不会留在地址栏）；
3. 脚本/curl：请求头 `X-Sleep-Auth: <密码>`（也兼容 `X-Colab-Token`）。

细节：cookie 值是 `HMAC-SHA256(随机盐, 密码)`，**不含明文密码**；盐值在 `configs/.auth_salt`（0600），
所以换密码会让所有旧 cookie 立即失效。失败限流：同 IP 5 分钟 10 次 → 429。免鉴权：`/login`、`/api/health`、`/favicon.ico`。

```bash
# 改密码（改完重启生效）
sed -i 's/^SLEEP_AUTH_PASSWORD=.*/SLEEP_AUTH_PASSWORD=你的新密码/' /opt/sleep_english/.vps_env
systemctl restart sleep-english

# 临时关闭闸门（置空 = 直通，任何扫到端口的人都能用，慎用）
sed -i 's/^SLEEP_AUTH_PASSWORD=.*/SLEEP_AUTH_PASSWORD=/' /opt/sleep_english/.vps_env && systemctl restart sleep-english

# 忘密码：直接看配置文件
grep SLEEP_AUTH_PASSWORD /opt/sleep_english/.vps_env
```

> ⚠️ 安全边界：① 默认密码是弱口令且写在仓库/文档里，**建议改成自己的**；
> ② 目前是明文 HTTP，cookie 与密码在链路上可被嗅探，要真正安全需上 TLS
> （可后续加 Caddy 或 Cloudflare 隧道，仓库自带 `colab/secure_gate.py` 那套也可复用）；
> ③ 登录后仍能看到配置里的 API Key —— 闸门挡的是"扫到端口就能用"，不是内网级隔离。

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

# ★ 更新代码（本机 push 之后，VPS 上一条命令；约 10-20 秒）
cd /opt/sleep_english && git pull --ff-only origin master && systemctl restart sleep-english
bash /opt/sleep_english/vps_update.sh              # 等价但更稳：安全检查+备份+依赖+重启+自检
bash /opt/sleep_english/vps_update.sh --dry-run    # 只看会拉什么（不改工作区）
bash /opt/sleep_english/vps_update.sh --force      # 出片正在跑时强制更新（会中断它）

# 命令行出片（读同一份配置：模型/4K 开关与网页一致；参数与 CLI 相同）
bash /opt/sleep_english/run_cli.sh --resume --sleep-pairs 50
bash /opt/sleep_english/run_cli.sh --resume --upscale-timeout 43200   # 只补 4K
bash /opt/sleep_english/run_cli.sh --resume --no-4k                   # 显式覆盖配置里的开关

# 部署自查 / 内存实测 / 4K 参数 A/B
cd /opt/sleep_english && .venv/bin/python verify_deploy.py
.venv/bin/python measure_kokoro.py
bash ab_4k.sh
```

### 3.1 代码更新流程（git push + VPS 一键 pull）

```powershell
# ① 本机：改完代码
git add <改动的文件>; git commit -m "说明"; git push origin master
```
```bash
# ② VPS：一条命令
bash /opt/sleep_english/vps_update.sh
```

`vps_update.sh` 内部顺序（每一步都有兜底，失败会明确报错并给回滚命令）：

1. **安全检查**：Web 出片 / `pipeline.py` / `ffmpeg` 正在跑 → 拒绝重启（`--force` 才继续，并提示 `run_cli.sh --resume` 续跑）；
2. `git fetch`（只取不合）；
3. **未跟踪文件冲突预检**：即将拉入的新文件若本地已有同名未跟踪文件 → 自动挪成 `*.pre-pull.bak`（避免 `git pull` 因 "untracked working tree files would be overwritten" 直接失败）；
4. 备份本地已跟踪改动到 `logs/pre_pull_<时间戳>.patch` 并 `git stash`；
5. `git pull --ff-only`（只快进，分叉就停下报错，不自动合并）；
6. `requirements.txt` 变了才 `pip install -r`；
7. **精确恢复本地改动**：上游本次改过的路径直接丢弃（视为已被上游包含），其余路径从 stash 恢复，然后清理 stash；
8. 清理 `.venv_broken` → `systemctl restart sleep-english` → 轮询 `/api/health`；
9. 打印旧/新 commit、回滚命令、备份 patch 路径。

| 改动类型 | 需要重启？ | 备注 |
|---|---|---|
| `app/**` `pipeline/**` 的 .py | **要** | 脚本已含；web 进程启动时导入 |
| `app/templates/*.html`、`app/static/*` | 要 | 重启只花 ~3 秒 |
| `requirements.txt` | 要 + 重装依赖 | 脚本按 diff 自动判断 |
| 系统包/字体 | 要 | 脚本只提示，不自动 apt |
| `configs/*.json`、音乐库、出片产物 | 不用 | pull 不碰它们（音乐库在 .gitignore 里） |

回滚：`git log --oneline` 找旧 commit → `git checkout <旧commit> -- . && systemctl restart sleep-english`；或用步骤 4 的 `logs/pre_pull_*.patch`。

### 3.2 画廊页：下载视频 / 缩略图

- 入口：运行历史 → 某期的「画廊」按钮（`/runs/{name}/gallery`）
- 头部按钮「**⬇️ 下载视频**」（主成片）与「**⬇️ 下载缩略图**」（主缩略图）；「📹 最终视频」列表里每一行还有「下载」，可分别下 720p / 4K / 4K BGM / BGM 版
- 后端两个只读端点，响应都是 `Content-Disposition: attachment`（中文名走 RFC 5987 `filename*`，支持 Range 续传）：
  - `GET /api/runs/{name}/download/video[?file=文件名]` —— 缺省=主成片（与列表/播放器同口径）；`file` 只接受**纯文件名**（禁 `..`/分隔符），按 运行目录根 → `clips/` → `videos/` 依次查找
  - `GET /api/runs/{name}/download/thumbnail[?file=thumbnail_N.jpg]` —— 缺省=主缩略图；指定名走 `thumbnail(_N)?.jpg` 白名单
- 鉴权：`/api/*` 在密码闸门后面 —— 浏览器点按钮自动带 cookie；`curl` 要加 `-H 'X-Sleep-Auth: <密码>'`
- 实测：`download/video` 200 + `attachment; filename*=utf-8''…`（20.3 MB）；`download/thumbnail` 200 + `attachment; filename="thumbnail.jpg"`（290 KB）；路径穿越 → 400、不存在 → 404、非白名单缩略图名 → 400

---

## 4. 迁移时改过的配置（`configs/`）

| 键 | 原值（Windows） | VPS 值 | 原因 |
|---|---|---|---|
| `output_dir` | `H:/2026_main_project/sleep_english/output` | `/opt/sleep_english/output` | Linux 路径 |
| `topics_file` | `H:/.../colab_listening_b/topics.json` | `/opt/sleep_english/pipeline/topics.json` | 仓库自带主题池 |
| `bgm_music_dir` | `H:\...\bgm_music_60s` | `/opt/sleep_english/bgm_music_60s` | 目录不存在亦可（默认不混 BGM） |
| `bgm_intro_outro_seconds` | 5 | **已删除** | 首尾独立段会让整片视频重编码，按需求删除；BGM 混合现在恒为纯音频 remux（§6.2） |
| **`upscale_timeout`** | 3600 | **43200** | 1 核 4K 重编码远超 1 小时；不放大必超时失败 |
| **`sleep_batch_pairs`** | 50 | **25** | 模型一次要恰好 50 组太容易翻车（实测连出 55/53/51 组 + JSON 截断）；25 组/批更稳，配合下面的超量容忍 |
| `sleep_pairs` | 200 | **50** | 首期规模；跑通后再调大 |
| **`sleep_4k_native`** | false | **true** | 原生 4K 渲染（卡片直接 3840×2160、块直接编 4K、Step6 只硬链）：文字更锐，详见 §5.4 |
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

### 5.4 原生 4K vs 超分（同脚本同音频实测，271 s 成片）

| 指标 | **原生 4K**（当前默认） | 超分（旧路径） |
|---|---|---|
| 卡片渲染 | 3840×2160 **原生** | 1280×720 → lanczos 插值放大 |
| 块编码 | 直接编 4K | 先编 720p，Step6 再整片编 4K |
| Step6 | **硬链产出，0 秒**（`links=2`、同 inode 已校验） | 真重编码 22.6 min |
| 4K 成片体积 / 码率 | 10.1 MB / 298 kbps | 13.3 MB / 393 kbps |
| **文字锐度**（1:1 裁切区边缘能量） | **6.20** | 4.52（原生 **+37% 更锐**） |
| **合成+4K 墙钟**（1 核） | **49.7 min**（Step5 44.5 + 拼接归一 5） | **30.6 min**（Step5 ~8 + Step6 22.6） |
| 峰值内存 | 897 MB（python 218 + ffmpeg 678） | 354 MB（Step5）/ 647 MB（Step6 调优后） |
| 折算 50 组（25 min 片，仅合成+4K） | ≈ 4.7 h | ≈ 2.9 h |
| 折算 200 组（100 min 片，仅合成+4K） | ≈ 18.5 h | ≈ 11.5 h |

**结论**：原生 4K 的**文字明显更锐**（原生渲染，不是插值），且 Step6 归零；代价是这块 1 核机器上
**合成+4K 慢约 60%**（每个块各起一个 x264 进程，块级预热摊不掉），内存峰值 +250 MB（897 MB，仍安全）。

选择建议：
- **看重文字清晰度、能接受慢** → 保持 `sleep_4k_native=true`（现状）；
- **批量赶量（如 200 组）** → 切回超分更省时间：

```bash
# 切回超分（Step6 lanczos）
cd /opt/sleep_english && .venv/bin/python - <<'PY'
import json, pathlib
for p in [pathlib.Path("configs/mode_sleep.json")] + sorted(pathlib.Path("configs/channels").glob("*.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    for c in ([d] + ([d["config"]] if isinstance(d.get("config"), dict) else [])):
        if "sleep_4k_native" in c:
            c["sleep_4k_native"] = False
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
PY
# 命令行出片记得加/去对应开关（run_cli.sh 会读配置自动补：原生=true 时自动加 --sleep-4k-native）
```

对照图（第 60 秒同位置 1:1 裁切）：本机 `_vps_evidence/crop_native.png`（原生）与 `crop_upscale.png`（超分）；
整帧缩略图 `full_native.png` / `full_upscale.png`。VPS 上原图在 `/opt/sleep_english/_compare/`。

## 6. 混 BGM（版权音乐）

- **音乐库**：`/opt/sleep_english/bgm_music_60s/` —— 35 首、每首 60.00 s、44.1 kHz 立体声 mp3，合计 86 MB（≈35 分钟素材）。混音时按**随机打乱后循环**取用（`music_idx % len(files)`），所以任何片长都够，不需要手动拼。
- 该目录在 `.gitignore` 里（音乐是用户资产、不入库）→ `git pull` / `git reset --hard` / `vps_update.sh` **都不会删**它。
- 配置：`bgm_music_dir` 已指向 `/opt/sleep_english/bgm_music_60s`；`bgm_mix` 默认 **false**（不混）。`bgm_music` 是指向它的软链兜底。
- **两个使用入口**：
  1. 参数配置页勾「混 BGM」→ 该次运行 Step 5.5 预混；
  2. 运行历史页对**已完成**成片点「混BGM」/「混BGM 4K」→ 只重编码音频、视频流 `-c:v copy`。

### 6.1 产物命名（原生 4K + BGM 全开时）

| 文件 | 内容 | 运行页/画廊 kind | 说明 |
|---|---|---|---|
| `videos/{标题}.mp4` | **4K，无 BGM** | final | 主成片（原生 4K 渲染下它本身就是 4K） |
| `{标题}_4K.mp4` | 4K，无 BGM（硬链，同 inode） | 4k | 「复制 4K 路径」「混BGM 4K」的干净源 |
| `{标题}_4K_bgm.mp4` | 4K + BGM | 4k_bgm | 混好 BGM 的 4K 版（**不会再被二次混音**） |
| `{标题}_bgm.mp4` | 720p + BGM | bgm | 仅**非**原生 4K 运行才有（Step6 会放大成 `_4K.mp4`） |

> 历史缺陷已修：此前原生 4K 运行里 `_bgm.mp4` 会被 Step6 硬链成 `_4K.mp4`，于是「4K」文件其实带 BGM、而 `_4K_bgm.mp4` 不存在，「混BGM 4K」还会对已带 BGM 的文件再混一次。

### 6.2 性能：BGM 混合已是秒级（原先可能整片重编码）

- 原实现有个 `bgm_intro_outro_seconds`（默认 5）配置：在旁白首/尾加静音段，并让**视频用 tpad 冻结帧延展**同秒数 —— 代价是**整片视频重编码**（4K 21 分钟片要多花 1.5–2 小时）。
- 该配置与行为**已按需求删除**。现在 BGM 混合恒为「**视频流 copy + 音频重编码**」，输出时长与原片严格一致（章节时间戳也对得更齐）。
- 实测（本 VPS，271 s 的 4K 成片，运行页「混BGM」按钮）：**27–29 秒完成**（其中音乐库准备 ~6 s），日志确认走的是 `ffmpeg 流式` 侧链路径：
  `[BGM] 侧链压缩模式: base_gain=-15.0dB threshold=-30.0dB ratio=8:1` → `[BGM] 混音叠加（ffmpeg 流式）` → 产物 **3840×2160、时长 271.3 s（与原片一致）、视频流 md5 与原片完全相同**（证明零重编码）。
- CLI 走同一条路径：`run_cli.sh` 会把配置里的 BGM 参数按**原值**下发（此前 `cfg()` 把数字读成 `1` 的 bug 已修，表现为 ffmpeg 报 `Numerical result out of range` 后静默退回 pydub）。
- 防御：`sidechaincompress` 的 threshold 要求线性 (0,1]；越界时自动夹紧并告警，避免异常配置把混音打回 pydub（长片下 pydub 回退有 OOM 风险，见 §6.3）。
- 保留参数：`bgm_fade_ms`（曲目间交叉淡化）、`bgm_ducking_mode`、`bgm_start_chapter`、`bgm_base_gain_db`/`bgm_volume_offset_db`、`bgm_sc_*` 侧链参数。

### 6.3 内存代价（选看）

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

## 9. 本次为跑通/省内存改的项目代码（均向后兼容）

| 文件 | 改动 | 默认行为 |
|---|---|---|
| `pipeline/tts_engine.py` | 新增 `TTSEngine.unload()`（清管线 + `gc.collect` + `torch.cuda.empty_cache` + glibc `malloc_trim(0)`） | 无调用则不生效，本机不变 |
| `pipeline/pipeline.py` | Step2 末按 `SLEEP_UNLOAD_TTS=1` 调 `unload()`；Step6 4K 命令支持 `SLEEP_4K_X264_PARAMS`；**4K 失败（含被 kill）时删除截断的半成品** | 两个 env 都不设 = 原行为 |
| `pipeline/media_utils.py` | 新增 `extra_4k_x264_params()` | 无 env = 返回空 |
| `app/pipeline_service.py` | args 补 `upscale_timeout`/`upscale_engine` 接线（主流程与「生成 4K」按钮都读配置）；4K 命令支持同一 env | 配置缺省=3600/ffmpeg，同旧默认 |
| `app/config_manager.py` | `PARAM_SPEC` 增加 `upscale_timeout`（配置页可见可保存，不再被表单覆盖丢掉） | 默认 3600 |
| `pipeline/llm_client.py` | `WBK_BASE_URL` 支持环境变量覆盖 | 不设=原硬编码地址 |
| `pipeline/font_scanner.py` + `pipeline/sleep/sleep_cards.py` | **修复 Linux 崩溃**：字体候选链只保留真实存在的文件；`font_covers` 对不存在的文件恒 False | Windows 上链不变（候选都存在） |
| `pipeline/sleep/llm_client_sleep.py` | **LLM 超量容忍**：`SLEEP_ALLOW_PAIR_OVERSHOOT=1` 时，批次"多给几组"截断到需要数量（少给仍失败并重试） | 不设 env = 与原「必须恰好 N 组」一致 |
| `pipeline/sleep/video_compose_sleep.py` | **原生 4K**：`_build_block`/`_build_video_block` 接受 `x264_params`，`compose_sleep` 在 `native_4k` 时下发 `extra_4k_x264_params()`（4K 的 rc-lookahead 是最大单块内存） | 非原生传空 → 命令与历史逐字节一致 |
| `app/auth_gate.py` + `serve_auth.py`（新） | **密码闸门**（默认密码 `inriynisse`、cookie 365 天、`?ct=`/header/登录页三种入口、失败限流）+ 受保护入口 | 只有把 systemd 指向 `serve_auth:application` 才生效；`run.bat` 与 Colab 仍走 `app.main:app` |
| `run_cli.sh`（新） | 命令行出片读**同一份配置**（4K/BGM 开关、模型、key 及全部 BGM 参数），与网页行为对齐 | 命令行显式传参优先；此前 CLI 会静默用默认值 |
| `vps_update.sh`（新） | **一键更新**：安全检查 → fetch → 未跟踪冲突预检 → stash → `pull --ff-only` → 依赖 → 精确恢复本地改动 → 重启 → 自检 | 纯手动执行，不参与运行 |
| `pipeline/bgm_mix.py` | **删除首尾独立段**（`intro_outro_seconds`）：删参数/pad 逻辑/临时 WAV/`tpad`+libx264 分支，只留 `-c:v copy` 音频重编码；`_remux_video_audio` 同步简化 | 未传参数时输出时长与原片一致、秒级完成（原先默认 5 s 延展 + 整片重编码） |
| `pipeline/pipeline.py` + `app/pipeline_service.py` | **原生 4K + BGM 命名**：4K 源混出 `{标题}_4K_bgm.mp4`；Step6 用**干净的 4K** 硬链出 `{标题}_4K.mp4`；运行页「混BGM」按钮按源分辨率命名 | 非原生路径不变 |
| `app/config_manager.py` | 删除 `bgm_intro_outro_seconds`（参数页不再出现）；`sleep_sequence` help 文案改指向参数配置页 | 其余 BGM 参数不变 |
| `app/routers/runs.py` | 新增 `GET /api/runs/{name}/download/video`、`/download/thumbnail`（attachment + 纯文件名校验 + 缩略图白名单） | 新增只读端点 |
| `app/routers/pages.py` + `app/templates/gallery.html` | 画廊头部「⬇️ 下载视频 / ⬇️ 下载缩略图」按钮 + 成片行「下载」改走附件端点；上下文补 `main_video_rel` | 只在画廊页可见 |
| `app/templates/base.html` + `workspace.html` + `app/routers/pages.py` + `app/config_manager.py`（删 `SLEEP_ORCHESTRATION_KEYS`）+ 删 `templates/arrangement.html` | **删除「📋 内容编排」页**（导航/路由/模板/专用参数字典/工作台页签映射） | `sleep_sequence` 键与 timeline 消费逻辑保留，仍在「参数配置」页可编辑 |

> 这些改动**已 commit 并 push 到 GitHub master**（`7773666 → 646886f → 99a75b1 → 8309290 → 7f1c8b5 → fdd7f66`），VPS 上 `bash vps_update.sh` 即可同步；不再有"本机未提交补丁"的问题。

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
| 运行页 4K 时长比成片短 | 旧版本残留：ffmpeg 被中断时会先写完 moov，留下「能播放但截断」的 `_4K.mp4`。已在 `_step6_4k` 修掉（非零退出即删）；历史残留可手动删掉该 `_4K.mp4` 再重跑 4K |
| 出片中途被 OOM kill | `dmesg -T \| grep -i oom`；`run_cli.sh --resume` 续跑（音频/卡片按文件续传）；或降 `sleep-pairs` |
| 字体相关报错 | `verify_deploy.py` 的字体段；Noto CJK 软链由 `colab/setup.sh` 建立 |
| 点「混BGM」报 `未找到可选的音乐文件` | `ls /opt/sleep_english/bgm_music_60s/*.mp3 \| wc -l` 应为 35；`ls -l /opt/sleep_english/bgm_music` 应是指向它的软链（本库在 .gitignore 里，重新 clone 后需要再上传一次） |
| 混 BGM 时进程被杀 | 片长过长（见 §6.3 的内存表）：50 组以内混、200 组分批或用按钮对已完成成片混 |
| 画廊点「下载视频/缩略图」返回 401 | 未登录：浏览器先登录（cookie 365 天）；`curl` 加 `-H 'X-Sleep-Auth: <密码>'` |
| 下载报 400 `Invalid file` | `file=` 只接受**纯文件名**（不能带 `videos/`、`..`）；缩略图须匹配 `thumbnail(_N)?.jpg` |
| 访问 /arrangement 返回 404 | 该页（📋 内容编排）已按需求删除，不是故障；`sleep_sequence` 改在「参数配置」页用 JSON 编辑 |
| 4K 文件里其实有 BGM / 点「混BGM 4K」像混了两次 | 旧产物命名缺陷（已修，见 §6.1）：重跑该期 4K 或手动删掉旧的 `_4K.mp4` 再生成 |
| 混 BGM 想更慢/更"干净指纹" | 首尾独立段已删除（那是整片重编码的根因）；如需恢复可从 git 历史取回 `intro_outro_seconds` 实现 |
| 背景图看着都像"睡觉场景"、认不出主题 | 旧 prompt 写死了 `night lighting` + `sleep atmosphere` + `no people`，且主体取自常为泛化标签的 `scene`。已重构（见 §12）：`sleep_bg_style_mode=topic_first`（默认）让主体跟随主题；必要时用配置页调整 |

---

## 12. sleep 背景图（`images/sleep_bg.png`）—— prompt 与主题贴合

### 12.1 旧实现的问题（已修）

旧 prompt 由 `pipeline/sleep/bg_image.py::build_bg_prompt()` 拼成：

```
Calm dreamy pastel scene related to: {scene}.
Soft muted colors, gentle diffused night lighting, peaceful relaxing sleep atmosphere, ...
no text, ..., no people, {style_prompt}
```

三个致命点：
1. **主体取自 `script["scene"]`**（`pipeline.py` 旧代码 `scene = script.get("scene") or args.topic`），而 LLM schema 只定义 `scene_zh`，`scene` 是额外字段 → 实测出现 `客服 · 情境對話`、`情境對話` 这类**零信息泛化标签**，甚至空串；
2. 氛围段**写死夜晚与睡前**（`night lighting` / `sleep atmosphere`）→ 不管主题是钢琴课还是领养狗，模型都往昏暗柔和靠；
3. **`no people`** → 你的主题几乎都是人的活动，去掉人物后只剩"昏暗空景"。

辅因：混入不透明度偏低（默认 20；你的频道设 30）、风格片段里的 `vibrant saturated colors` 与我们的 `Soft muted colors` 打架。

### 12.2 现在的 prompt 组成（`topic_first`，默认）

```
Wide establishing shot of: {subject}.
The main subject must be clearly recognizable: {subject}.
Soft diffused ambient lighting, low contrast, calm pastel palette suitable for a bedtime video,
clean simple composition, wide shot,
no text, no words, no letters, no watermark            ← 允许人物（可关）
Follow the art style below strictly: {style_prompt}
```

**主体 `{subject}` 解析优先级**（`pick_bg_subject()`）：
1. **本期主题 `topic`**（英文，最贴内容；非泛化时直接用）
2. 非泛化的 `scene`
3. 标题（自动去掉频道固定前缀 `Everyday Phrases — `）
4. `everyday life`

**泛化标签过滤**：空串、`情境對話/日常/對話/短句…` 词表命中、以及「X · Y」形式的 **scene 标签**（末段是泛化词，如 `客服 · 情境對話`、`居家打掃 · 短句`）都会被跳过；`情境 · 取快遞` 这种末段具体的仍会用（主体=取快遞）。

### 12.3 新增/调整的配置（`sleep_visual` 组）

| 键 | 默认 | 说明 |
|---|---|---|
| `sleep_bg_style_mode` | `topic_first` | 新增。`mood_first` = **回到旧版睡前氛围**（与重构前 prompt 逐字符一致） |
| `sleep_bg_allow_people` | `true` | 新增。关掉则 prompt 加 `no people`（旧取向） |
| `sleep_bg_opacity` | 20 → **35** | 只改默认值；**已保存的配置值不会被覆盖**（你的频道是 30，可自行调高） |

> 注意：`sleep_bg_image` 全局默认 false，但你的频道 `ch_..._0` / `ch_..._3` 都覆盖为 **true**（不透明度 30）——所以频道运行的背景图是开着的。

### 12.4 可观测性

- 生成前打印：`[SleepBG] 主体='...'（来源=topic/scene/title/fallback，style_mode=...，allow_people=...）` 与 `[SleepBG] prompt: ...`（截断 300 字）
- 生成后落盘 `images/sleep_bg.prompt.txt`（含主体来源 + 完整 prompt + 时间戳）→ 复盘/重生成时能知道当时到底问了什么

### 12.5 回归保护

`pipeline/sleep/test_bg_image.py`（纯 stdlib，`python pipeline/sleep/test_bg_image.py`）：28 项断言，含
- 泛化标签判定（`客服 · 情境對話`→泛化、`大學宿舍`/`Berry Picking at a Farm`→不泛化）
- 主体优先级（topic > scene > title > fallback）
- `topic_first` 不再出现 `night lighting`/`sleep atmosphere`/`Soft muted colors`/`no people`
- **`mood_first` 与重构前输出逐字符一致**（另用 `git show HEAD:pipeline/sleep/bg_image.py` 做过 6 组输入的实测对比，全部一致）

### 12.6 实测背景图统计（判断"是否像睡觉场景"的量化参考）

| 来源 | 尺寸 | 平均亮度 | 平均饱和(HSV) | 暗像素(<80) |
|---|---|---|---|---|
| 收快遞（旧 prompt） | 2720×1536 | 99.6 | 126.6 | **41.6%** |
| 睡不著 | 1536×1024 | 102.9 | 106.5 | 31.4% |
| 鋼琴課 | 2720×1536 | 144.2 | 88.3 | 14.7% |
| 領養毛小孩 | 2720×1536 | 153.6 | 88.4 | 11.5% |
| 戴運動手環 | 2720×1536 | 126.3 | 105.0 | 26.3% |
| **本期「選串流方案」**（旧 prompt，`scene=串流方案`） | 2720×1536 | 124.2 | 112.2 | 22.3% |

参考：明亮白天室内照片通常 平均亮度 >140、暗像素 <15%。多数旧图偏暗/偏灰（`night lighting` + `muted` 的直接结果），符合"不管主题都像睡觉"的观感。

> ⚠️ 视觉终检需人工确认：本机无法读图。开启背景图后生成一张，直接看 `images/sleep_bg.png` 以及 `sleep_bg.prompt.txt` 里的主体是否就是本期话题。

---

## 13. 卡片块帧率（`sleep_card_fps`）—— 静态卡降帧提速

### 13.1 原理与实测收益

sleep 的块 =「一张静态卡片（`-loop 1`）+ 该组音频链」，`-r 25` 会把**同一张图编码 25 次/秒**。降帧只减少「重复帧的编码次数」，画面零损失（已确认卡片块没有任何动画滤镜）。

本机真实命令形状实测（12s 静态卡 → 4K 输出，含 aac 音频）：

| fps | 耗时 | 加速 | 体积 |
|---|---|---|---|
| 25 | 10.4 s | 1.00× | 0.69 MB |
| 5 | 3.7 s | 2.83× | 0.47 MB |
| 2 | 2.5 s | 4.15× | 0.66 MB |
| 1 | 2.1 s | **5.02×** | 0.87 MB |

不是 25×：每块有固定开销（ffmpeg 启动 + 4K PNG 解码 + aac + 封装 ≈ 1.5–2 s），随帧数线性增长的部分约占 8 s/12s 卡。

### 13.2 生效条件（安全闸门，自动判定并打印原因）

| 情况 | 结果 |
|---|---|
| 未绑定片头/片尾**库视频** 且 `sleep_xfade=false` | ✅ 用配置值（1/2/5/25） |
| 绑定片头/片尾**库视频** | ⛔ 回退 25fps（该块是 25fps 运动画面） |
| 开启交叉溶解 | ⛔ 回退 25fps（0.5s 过渡在低帧率下不足 1 帧） |
| 非法取值 | ⛔ 回退 25fps |

**为什么必须同帧率**：块拼接走 `media_utils.concat_segments` 的 `-c:v copy`（concat demuxer）。实测混合 25fps + 1fps：时长 11.0s → **11.600s**、帧时间戳 **39 处负跳变**；试了 8 种修参数（`+genpts`/`igndts`/`-vsync vfr`/`-fps_mode vfr`/`-copyts`/`avoid_negative_ts`/`video_track_timescale`/重编码）**全部无效**。

### 13.3 帧长对齐（否则视频会比音频长）

`-r 1` 会把每个块向上取整到整秒：52 块累计 **+26.8s**，视频比音频网格长 → 画面与旁白逐步错位。
现在在**时间轴层**（`timeline_sleep._quantize_block_durations`）把每块补齐到 `1/fps` 的整数倍（延长块内 gap 静音），因此音频网格、SRT、YouTube 章节与视频完全一致。

本地 A/B（10 组、720p、复用缓存音频）：

| | 25fps | 1fps |
|---|---|---|
| 容器时长 vs 时间轴 | 267.174s vs 266.956s（+0.218s，历史 CFR 取整） | 290.000s vs 290.000s（**0.000s**） |
| 每块时长 | — | 全部为 1s 整数倍（0/52 不合规） |
| `avg_frame_rate` | 28467200/1139943 | **1/1** |
| Step 5 窗口 | 82.3 s | 59.8 s（1.38×，含固定开销） |
| 体积 | 6.61 MB | 8.49 MB（+28%） |
| 时间轴总长 | 267.0 s | 290.0 s（对齐净增 +23.0s 静音，+8.6%） |

### 13.4 代价与建议

- **时长净增** = 每块 ≤ `1/fps` 的静音补齐：1fps ≈ **+8.6%**，2fps ≈ +4%，5fps ≈ **+1.7%**（都是加在组间停顿里，听感无影响）
- **体积 +25~30%**（低帧率下每帧都是关键帧；25fps 时后续帧几乎是空 P 帧）
- **拖动精度** = `1/fps` 秒（睡前内容无影响）
- 建议：**5fps 是性价比点**（+1.7% 时长换 ~2.8×）；追极限用 1fps（+8.6% 时长换 ~5×）
- VPS 换算（原生 4K、块 ≈24s、25fps 基线 98 s/块）：1fps 预计 ~8–12 s/块 → Step 5 由 ~85 min 降到 ~10–15 min

### 13.5 VPS 实测（同种子 `_smoke`、原生 4K、52 块，2026-10-09 补测）

| | 25fps（`_smoke_bgm`） | 1fps（`_fps1`） |
|---|---|---|
| Step 5 窗口 | 06:56:09 → 07:38:43 = **42m34s**（49.1 s/块） | 11:17:26 → 11:39:45 = **22m19s**（25.8 s/块） |
| Step 5 峰值内存 | sum 897 MB（py 279 + ff 678） | sum 792 MB（py 245 + ff 574） |
| 成片 | 10.12 MB | 10.6 MB（`_4K.mp4` 同尺寸=硬链） |
| 速度 | 1.00× | **1.91×** |

**为什么不是 5–12×**：瓶颈不是帧数，而是**每块的固定开销**（≈24 s/块，与帧数无关）：4K PNG 解码 + 滤波图构建 + x264 4K 初始化/收尾 + mp4 finalize + 进程启动，在 1 vCPU 上被放大。
本机同构实验（4K 源卡、12 s 块）也印证了这个地板：25fps **13.8 s** → 1fps **5.6 s**（2.49×），而把 fps 过滤前置（`fps=1,scale,pad`）只再快 13%（5.6→4.9 s）——**不是主要矛盾**。

⇒ **建议改用 5fps**：实测 5fps 与 1fps 的块耗时几乎一样（26 s vs 29 s 量级），但对齐补齐只有 1/5（+1.7% vs +8.6% 时长）。

### 13.6 更大的杠杆：块合并（待评估）

固定开销是**按块**收的，而 52 个块里有 50 个是同组 5 个步骤——**它们共用同一张卡片**，所以本可合并成 1 块（intro + 10 组 + outro = **12 块**）：

| 方案 | 固定开销 | 帧成本 | Step 5 估算 |
|---|---|---|---|
| 现状 25fps / 52 块 | 52 × 24 s ≈ 21 min | ~21 min | ~42 min（实测 42.6） |
| 仅低帧率 1fps / 52 块 | 52 × 24 s ≈ 21 min | ~1 min | ~22 min（实测 22.3） |
| **块合并 25fps / 12 块** | 12 × 24 s ≈ 5 min | ~21 min | ~26 min |
| **块合并 + 1fps / 12 块** | 12 × 24 s ≈ 5 min | ~1 min | **~6 min（≈7×）** |

块合并需要改 `compose_sleep` 的分块循环（按 `pair` 分组，而不是按 step），并复核 `card_lead`、resume 粒度与 xfade 行为。

