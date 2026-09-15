# CODELY.md — sleep_english

## Project Overview

独立专注 **sleep 模式**（睡觉听·AB 短句循环）的英语听力视频生产工具，2026-09-15 从 `colab_listening_b_web` 分离而来（原项目保留全部模式不动）。结构：intro 频道名播报 → N×[A男常速 → A女慢速 → B男常速 → B女慢速 → AB连贯] → outro；默认 200 组=400 句约 100 分钟；纯 Pillow 卡片 + 本地 TTS + FFmpeg，主流程零 MCP。

**技术栈：** FastAPI + Jinja2 SSR + Tailwind CSS + HTMX + 原生 JS；端口 **8766**（run.bat）。

## Architecture

```
sleep_english/
├── app/                          # Web 层（路由按领域拆分在 routers/）
│   ├── main.py                   # 装配层（pages/config/run/batch_queue/topics/scripts/runs/mcp_tokens/intro_videos/health/voices×3/ai_test）
│   ├── config_manager.py         # MODES=["sleep"]；PARAM_SPEC 已裁剪至 sleep 生效集；resolve_mcp_tokens/sleep 配色
│   ├── pipeline_service.py       # 后台线程跑 pipeline（_set_env/_build_args/_run_inner；sleep 无角色复用链路）
│   ├── intro_library.py          # 片头库索引与绑定解析（sleep_intro_video）
│   ├── script_library.py         # 批量脚本（支持 sleep：DEFAULT_LINES sleep=400）
│   ├── batch_queue_service.py    # 控制台批量队列
│   ├── local_batch_service.py    # 运行历史批量 4K/BGM
│   ├── thumbnail_regen_service.py# 缩略图独立子进程重生成
│   └── routers/                  # 14 个路由文件
├── pipeline/
│   ├── pipeline.py               # sleep-only 精简编排器（_stepN 签名与 pipeline_service 兼容）
│   ├── sleep/                    # audio/llm_client/cards/timeline/video_compose/bg_image/intro_video
│   ├── llm_client.py             # LLM 基础设施（_chat/_extract_json；sensenova/openai/gemini + 代理）
│   ├── tts_engine.py / qwen_tts_engine.py / moss_tts_engine.py
│   ├── media_utils.py / checkpoint.py / topic_manager.py / mcp_client.py / sensenova_image.py
│   ├── thumbnail_gen.py          # sleep 缩略图 + save_youtube_metadata（章节 Intro/Phrase Drills/Outro）
│   ├── bgm_mix.py / sr_upscale.py / seo_pool.py / yt_meta_styles/ / style_manager.py / script_style.py
│   └── fonts/  topics.json
├── configs/                      # mode_sleep.json（含密钥，gitignored）
├── bgm_music_60s/                # BGM 音乐库（gitignored）
└── run.bat                       # 端口 8766
```

### 与原项目的裁剪差异

- 删除模式：original*/quest*/story 全部相关模块与页面（characters/story_family/channel_factory/styles/subtitle_styles 路由与模板）
- config_manager：MODES=["sleep"]、PARAM_SPEC 仅 sleep 生效参数（mcp_tokens/image_provider/visual_style 改为可见）、删 build_cli_args/_SLEEP_UNUSED_KEYS/normalize_animation、structure_family 恒等
- pipeline_service：删角色复用/gender 交换/host_bg 绑定/sprite 自动入库/story env 段
- 2026-09-15 死代码清理（5 commit）：dashboard 死 JS 簇整块删除（曾致「开始生成」按钮 TypeError）；重渲字幕功能链整链删除（sleep 必死）；media_utils 1546→530 行；llm_client 1494→828 行（listening/story 链断码，llm_review/image_gen/script_style 模块本就不存在）；孤儿路由与孤儿 partial 删除；script_library 收敛仅 sleep（VOICE_DEFAULT_MODES/DEFAULT_MODES 均为 sleep）
- 音色页素材库绑定块与 library_chars 传参已彻底移除

## Pipeline Steps

| 步骤 | 说明 |
|------|------|
| Step 0 | LLM 脚本生成（sleep 分批 50 组/批，.sleep_cache 缓存可关） |
| Step 1 | 跳过（缩略图/背景图按需 initialize MCP） |
| Step 2 | 本地 TTS（文件级续传 _usable：exists+非零字节；时长写 .durations.json sidecar）+ 片头库绑定 + 可选 AI 背景图 |
| Step 3 | 跳过 |
| Step 4 | build_sleep_timeline + SRT sidecar（meta.json 记 sleep_pairs，resume 校验一致性） |
| Step 4.5 | assign_sleep_episode 集数递增 + AI 缩略图（兜底 Pillow）+ save_youtube_metadata |
| Step 5 | compose_sleep（组级块 + concat_segments 三段式；combo 段= a_m+0.15s+b_f 块内内联拼接，无 combo 中间 mp3；卡片底图模板+字体缓存；xfade 可选） |
| Step 5.5 | 可选 BGM（bgm_mix） |
| Step 6 | 可选 4K（已 4K 源硬链接产出 _4K 文件） |

## Development Conventions

- Python 3.12+ 类型注解；FastAPI async 路由；pipeline 自包含不 import app
- 中文注释与日志；Windows GBK 控制台注意（临时验证脚本只 print ASCII）
- 时间轴 gap=0.0；TTS loudnorm 归一化；新配置参数照 sleep 组三件套（PARAM_SPEC/_build_args/pipeline.py）接线
- 修改 sleep 结构只动 pipeline/sleep/ 五模块；改卡片渲染保持 sleep_cards scale 系数
- 每次 code change 后立即 commit + push（用户常设规则）
- 验证命令：临时 .py/.cmd 写到 %TEMP% 跑完即删；py_compile 批量校验；零积分 E2E 用 `--quick-test --no-4k`（源 run 已在 output/sleep/）

## 关键事实

- 集数徽章 output/sleep/.thumb_episode.json 按主题计数（未迁移旧计数，首集从 1 起）
- 音色默认按模式分套：VOICE_DEFAULT_MODES=("sleep",)，配置无 modes.sleep 节 → 回退 legacy 平铺键（与原项目行为一致）
- Kokoro 单句失败自动重试 ×2；qwen/moss 失败回退 kokoro
- 卡片渲染有模板缓存（改主题字段即自动失效）；音频时长 sidecar .durations.json（mtime+size 校验）
- 本仓库无 git remote（push 不可用，commit 即可）
- 双项目并行时集数/主题防重各自独立，可能重合（已知权衡）
- 用户反馈记忆（跨会话通用规则）见用户级 CODELY.md；本项目后续沉淀追加到「Codely Structured Memories」Project 节
