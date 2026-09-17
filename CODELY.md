# CODELY.md — sleep_english

## Project Overview

独立专注 **sleep 模式**（睡觉听·AB 短句循环）的英语听力视频生产工具，2026-09-15 从 `colab_listening_b_web` 分离而来（原项目保留全部模式不动）。结构：intro 频道名播报 → N×[A男常速 → A女慢速 → B男常速 → B女慢速 → AB连贯] → outro；默认 200 组=400 句约 100 分钟；纯 Pillow 卡片 + 本地 TTS + FFmpeg，主流程零 MCP。

**技术栈：** FastAPI + Jinja2 SSR + Tailwind CSS + HTMX + 原生 JS；端口 **8766**（run.bat）。

## Architecture

```
sleep_english/
├── app/                          # Web 层（路由按领域拆分在 routers/）
│   ├── main.py                   # 装配层（pages/config/run/batch_queue/topics/scripts/runs/mcp_tokens/intro_videos/health/voices×3/ai_test/channels）
│   ├── config_manager.py         # MODES=["sleep"]；PARAM_SPEC 已裁剪至 sleep 生效集；resolve_mcp_tokens/sleep 配色
│   ├── channel_profiles.py       # 频道矩阵核心：频道实体 CRUD + resolve_run_config 配置合成 + 工坊收藏转正/品牌色→sleep 色板映射
│   ├── pipeline_service.py       # 后台线程跑 pipeline（_set_env/_build_args/_run_inner；sleep 无角色复用链路；透传 channel_id/音色覆盖/channel_profile）
│   ├── intro_library.py          # 片头库索引与绑定解析（2026-09-17 起按频道分库互不影响：channel 空=全局库 configs/intro_library.json，非空=configs/channels/{cid}/intro_library.json+intro_videos/；intro_library.py 提供 videos_dir/intro_file/load_library/save_library/resolve_video_path 全带 channel_id，解析频道库优先→全局库回退）
│   ├── script_library.py         # 批量脚本（支持 sleep：DEFAULT_LINES sleep=400）
│   ├── batch_queue_service.py    # 控制台批量队列（队列项含 channel_id/channel_name；_build_config 走 resolve_run_config）
│   ├── local_batch_service.py    # 运行历史批量 4K/BGM
│   ├── thumbnail_regen_service.py# 缩略图独立子进程重生成
│   └── routers/                  # 15 个路由文件（channels.py = 频道矩阵 API：CRUD/收藏转正/频道主题库/fill_queue）
├── pipeline/
│   ├── pipeline.py               # sleep-only 精简编排器（_stepN 签名与 pipeline_service 兼容；script.json 落 channel_id；quick_test 同频道过滤）
│   ├── sleep/                    # audio/llm_client/cards/timeline/video_compose/bg_image/intro_video
│   ├── llm_client.py             # LLM 基础设施（_chat/_extract_json；sensenova/openai/gemini + 代理）
│   ├── tts_engine.py / qwen_tts_engine.py / moss_tts_engine.py
│   ├── media_utils.py / checkpoint.py / topic_manager.py / mcp_client.py / sensenova_image.py
│   ├── thumbnail_gen.py          # sleep 缩略图 + save_youtube_metadata（章节 Intro/Phrase Drills/Outro；assign_sleep_episode 键含频道前缀）
│   ├── bgm_mix.py / sr_upscale.py / seo_pool.py / yt_meta_styles/ / style_manager.py / script_style.py
│   └── fonts/  topics.json
├── configs/                      # mode_sleep.json（含密钥，gitignored）；channels/{cid}.json 频道实体（gitignored 运行时数据）
├── bgm_music_60s/                # BGM 音乐库（gitignored）
└── run.bat                       # 端口 8766
```

### 与原项目的裁剪差异

- 删除模式：original*/quest*/story 全部相关模块与页面（characters/story_family/styles/subtitle_styles 路由与模板；channel_factory 见下）
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

- **频道矩阵·独立完整配置（2026-09-16 二期；三期多开）**：频道实体 `configs/channels/{cid}.json` 内嵌 **config 节 = 整套 PARAM_SPEC 私有快照**（创建/转正时从全局深拷贝种子 + brand_colors→sleep 色板映射 + 身份键），此后完全独立演化；每频道可用不同 LLM Provider/Key（防关联）。**频道上下文 = URL `?channel=` 参数（三期：URL 唯一事实源，多标签页并行操作不同频道，无服务端全局状态）**：dashboard/config/sleep/arrangement/topics 五页面接受 `?channel=` 按频道快照渲染；写 API（/api/config/save·save_all·all·sleep_preview、/api/topics/* 全端点、/api/run/start）显式收 channel 参数（config.py `_ctx/_save_ctx`、topics.py `_ctx_config` helper）；base.html 顶部上下文条为 URL 导航器（切换=增删 channel 参数跳转）并自动改写侧边栏链接保持上下文；矩阵页「⚙ 配置」新标签打开 /config?channel=cid。`sync_channel_config(cid, scope)` 从全局按组回填（all/credentials/content/visual/bgm），**身份键守卫** IDENTITY_SYNC_GUARD（sleep_channel_name/sleep_outro_text/topics_file/used_topics_file/channel_id/structure 永不被同步覆盖）。旧格式频道（仅 overrides 无 config 节）读取时自迁移并惰性写回。队列频道项 `_build_config` 装载频道完整快照再叠 topic/cefr/script_id。一期机制的延续：brand_colors 三色→11 色 sleep_color_* 映射、quick_test 同频道过滤、LLM 缓存键含频道 ctx 哈希、`.thumb_episode.json` 键=cid|topic、频道音色 args.sleep_voice_male/female。quick_config 面板字段清单仍按模式共享（值本身按频道）。并发生成仍受 run_mutex 串行（多频道铺量走矩阵填充+队列）；同频道双标签编辑后写覆盖。YouTube 上传本期不做（youtube_metadata.json 已按频道产出，下期接每频道 OAuth）
- **片头库按频道分库（2026-09-17）**：intro_videos 页面与全部 /api/intro_videos/* 端点接受频道上下文（?channel=/body/Form，URL 唯一事实源，同 config/topics 惯例）——频道读写自己的独立片头库（configs/channels/{cid}/intro_library.json + intro_videos/{id}/intro.mp4），绑定写频道配置快照 sleep_intro_video；生成/上传的产物、主题色、TTS、BGM 目录、MCP tokens、LLM Provider/Key/模型均按频道快照。全局上下文（无 channel）行为不变（全局库 + mode_sleep.json）。运行解析（pipeline_service._resolve_sleep_intro_video）：config.channel_id 非空时频道库优先、全局库回退 —— 存量频道快照可能继承指向全局片头的历史绑定，回退保证不失效；频道页 use 仅允许绑频道库内 id（隔离），列表返回 used_missing 时前端提示「历史全局绑定」。前端 localStorage 设置/频道名历史键按频道后缀隔离（intro_last_settings__{cid}）；生成/提示词单槽 409 守卫保持全局（TTS/MCP 资源互斥）。不迁移存量全局片头到频道库。
- 集数徽章 output/sleep/.thumb_episode.json 按主题计数（未迁移旧计数，首集从 1 起）
- 音色默认按模式分套：VOICE_DEFAULT_MODES=("sleep",)，配置无 modes.sleep 节 → 回退 legacy 平铺键（与原项目行为一致）
- Kokoro 单句失败自动重试 ×2；qwen/moss 失败回退 kokoro
- 卡片渲染有模板缓存（改主题字段即自动失效）；音频时长 sidecar .durations.json（mtime+size 校验）
- GitHub remote：https://github.com/collinsgraciano/sleep_english（私有，2026-09-16 创建）；每次 code change 后立即 commit + push
- channel_factory 已按用户要求补迁挂载（2026-09-15，router+page 路由+侧边栏+paths 常量，eb0c9c5 的「残留留本地」决定作废）；configs/channel_drafts·favorites·references·assets·channels 为运行时数据保持本地不入库
- 双项目并行时集数/主题防重各自独立，可能重合（已知权衡）
- 用户反馈记忆（跨会话通用规则）见用户级 CODELY.md；本项目后续沉淀追加到「Codely Structured Memories」Project 节
