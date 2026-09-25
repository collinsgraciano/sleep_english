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

- **频道矩阵·独立完整配置（2026-09-16 二期；三期多开）**：频道实体 `configs/channels/{cid}.json` 内嵌 **config 节 = 整套 PARAM_SPEC 私有快照**（创建/转正时从全局深拷贝种子 + brand_colors→sleep 色板映射 + 身份键），此后完全独立演化；每频道可用不同 LLM Provider/Key（防关联）。**频道上下文 = URL `?channel=` 参数（三期：URL 唯一事实源，多标签页并行操作不同频道，无服务端全局状态）**：dashboard/config/arrangement/topics 四页面接受 `?channel=` 按频道快照渲染；写 API（/api/config/save·save_all·all·sleep_preview、/api/topics/* 全端点、/api/run/start）显式收 channel 参数（config.py `_ctx/_save_ctx`、topics.py `_ctx_config` helper）；base.html 顶部上下文条为 URL 导航器（切换=增删 channel 参数跳转）并自动改写侧边栏链接保持上下文；矩阵页「⚙ 配置」新标签打开 /config?channel=cid。`sync_channel_config(cid, scope)` 从全局按组回填（all/credentials/content/visual/bgm），**身份键守卫** IDENTITY_SYNC_GUARD（sleep_channel_name/sleep_outro_text/topics_file/used_topics_file/channel_id/structure 永不被同步覆盖）。旧格式频道（仅 overrides 无 config 节）读取时自迁移并惰性写回。队列频道项 `_build_config` 装载频道完整快照再叠 topic/cefr/script_id。一期机制的延续：brand_colors 三色→11 色 sleep_color_* 映射、quick_test 同频道过滤、LLM 缓存键含频道 ctx 哈希、`.thumb_episode.json` 键=cid|topic、频道音色 args.sleep_voice_male/female。quick_config 面板字段清单仍按模式共享（值本身按频道）。并发生成仍受 run_mutex 串行（多频道铺量走矩阵填充+队列）；同频道双标签编辑后写覆盖。YouTube 上传本期不做（youtube_metadata.json 已按频道产出，下期接每频道 OAuth）
- **片头库按频道分库（2026-09-17）**：intro_videos 页面与全部 /api/intro_videos/* 端点接受频道上下文（?channel=/body/Form，URL 唯一事实源，同 config/topics 惯例）——频道读写自己的独立片头库（configs/channels/{cid}/intro_library.json + intro_videos/{id}/intro.mp4），绑定写频道配置快照 sleep_intro_video；生成/上传的产物、主题色、TTS、BGM 目录、MCP tokens、LLM Provider/Key/模型均按频道快照。全局上下文（无 channel）行为不变（全局库 + mode_sleep.json）。运行解析（pipeline_service._resolve_sleep_intro_video）：config.channel_id 非空时频道库优先、全局库回退 —— 存量频道快照可能继承指向全局片头的历史绑定，回退保证不失效；频道页 use 仅允许绑频道库内 id（隔离），列表返回 used_missing 时前端提示「历史全局绑定」。前端 localStorage 设置/频道名历史键按频道后缀隔离（intro_last_settings__{cid}）；生成/提示词单槽 409 守卫保持全局（TTS/MCP 资源互斥）。不迁移存量全局片头到频道库。
- **多频道工作台（2026-09-17）**：`/workspace` 页（templates/workspace.html，独立外壳不 extends base.html）同窗口多频道页签并行操作——每页签一个频道一个**常驻 iframe**（src 带 `?channel=`，URL 仍是唯一事实源），display 显隐切换保留各频道页面状态（表单/滚动/HTMX）；iframe load 时同源回读 `contentWindow.location` 同步页签的 cid/page（页签内换频道/换页页签条自动跟随）；localStorage `se_ws_tabs` 持久化页签布局，`?channel=` 直达预选补开；添加页签同频道去重（降低同频道双开写覆盖）。pages.py 加 /workspace 路由 + base.html 侧边栏「🪟 多频道工作台」入口（iframe 内点该入口 target=_top 顶层跳转防嵌套）；五个频道页面与后端零改动
- **频道名字体智能回退+列表选择+粗细（2026-09-17）**：中文频道名豆腐块根因=手写体回退链在 Ink Free 截胡（无中文字形且 Pillow 不逐字回退）。新增 fontTools 依赖做 cmap 字形检测：pipeline/font_scanner.py 提供 font_covers/covered_count/supports_cjk/scan_fonts（winreg 枚举系统字体→推荐手写/内置/含中文/系统四组下拉数据；configs/font_scan_cache.json 磁盘缓存 gitignored，首扫约 4s 后秒回；TTFont 无 silent 形参会 TypeError，fontNumber=0 通吃 ttf/ttc）。sleep_cards._channel_font_runs 按频道名逐段选字体（custom→Inkfree→segoepr→seguisym→seguiemj→msyhbd→msyh；fontTools 缺失=恒覆盖退旧行为，文件损坏=链上跳过），卡片左上角/片头大字/片尾/缩略图全走它；_handwrite_path(theme,text) 单字体最优解保留给 intro_video 片头文字层零改动受益。PARAM_SPEC 新 type "font"（_config_groups.html 分组 optgroup+〔中〕标记+__custom__ 逃生口+data-font-custom 输入框；collectConfig/_collectSleepFields 哨兵守卫、fillForm 经 _ensureSelectOption upsert、dashboard 快捷面板未知 type 兜底 text）。新增 sleep_handwrite_weight 0-4 描边假粗（_stroke_px 按字号 1/18 封顶防糊死 CJK 计数器：角标 36px 封顶 2px、片头 100px 全档位）；单字体+无描边路径保持历史像素一致。**句子区三字体同日接入列表可选**：sleep_font_en（A/B 句英文+片尾结束语，默认 Nunito→雅黑 Bold 链）/ sleep_font_ph（IPA，默认 Cambria，缺 ə ʃ ʊ 等字形自动回退）/ sleep_font_zh（中文翻译+片头副题+缩略图横条+intro_video 副题，默认雅黑），type 同 "font" 共用下拉组件；sleep_cards._pick_font(custom,fallbacks,text) 泛化单字体最优解（_theme_font 按 theme 键取用），句子区整行覆盖回退不做逐段混排；角标 EN/序号仍固定 FONT_EN（设计元素不跟选）
- **画面页预览增强：Logo 盖章 + 背景示例图（2026-09-17）**：预览三卡合成时按成片参数叠加 Logo（config.py `_stamp_logo_on_card` 复刻 `_logo_overlay` 数学：宽=size、边距=0.35×size、四角、alpha 乘进原 alpha 通道等价 colorchannelmixer）；Logo 解析顺序与成片一致（总开关→显式路径→频道上下文自动探测 `configs/channel_assets/{cid}/logo.png`，POST 端点经 `?channel=` 传上下文）。背景图开关开但固定路径留空/失效 → 预览注入内置示例图 `pipeline/sleep/assets/preview_bg_sample.jpg` 演示混合/层级（成片仍按主题 AI 生图）。**关键约束：build_theme 绝不映射 logo 键**——Logo 盖章只注入预览 theme，成片卡片渲染路径零影响，否则烘焙+FFmpeg overlay 双 Logo。
- **Sleep 画面页并入参数配置（2026-09-19）**：侧边栏「😴 Sleep 睡前短句」/sleep 画面样式专项页整链路删除（pages.py 路由、sleep_config.html、SLEEP_VISUAL_KEYS/SLEEP_PREVIEW_HIDDEN_FIELDS、工作台页签路由同步移除），全部画面参数由「参数配置」页 Sleep 组承载（内嵌实时预览/随机配色/配色组合照旧，能力本就是该组渲染的超集，零功能损失）。配置页分组顺序：GROUP_META sleep order=0 默认置顶；分组标题 ⠿ 把手拖拽排序（_config_groups.html `group_reorder` 开关仅 /config 传入、_config_scripts.html dragstart/dragover 半区插入/dragend 落盘），顺序存 localStorage `se_config_group_order__{mode}`（UI 偏好，多频道上下文共享不按频道隔离；存储未包含的新分组按服务端默认顺序追加尾部）。存量工作台页签 page='sleep' 经 PAGE_ROUTES 回退自动落控制台。
- 集数徽章 output/sleep/.thumb_episode.json 按主题计数（未迁移旧计数，首集从 1 起）
- 音色默认按模式分套：VOICE_DEFAULT_MODES=("sleep",)，配置无 modes.sleep 节 → 回退 legacy 平铺键（与原项目行为一致）
- Kokoro 单句失败自动重试 ×2；qwen/moss 失败回退 kokoro
- 卡片渲染有模板缓存（改主题字段即自动失效）；音频时长 sidecar .durations.json（mtime+size 校验）
- GitHub remote：https://github.com/collinsgraciano/sleep_english（私有，2026-09-16 创建）；每次 code change 后立即 commit + push
- channel_factory 已按用户要求补迁挂载（2026-09-15，router+page 路由+侧边栏+paths 常量，eb0c9c5 的「残留留本地」决定作废）；configs/channel_drafts·favorites·references·assets·channels 为运行时数据保持本地不入库
- 双项目并行时集数/主题防重各自独立，可能重合（已知权衡）
- 用户反馈记忆（跨会话通用规则）见用户级 CODELY.md；本项目后续沉淀追加到「Codely Structured Memories」Project 节

## Codely Structured Memories

### User

### Feedback
- [2026-09-23 18:29:41] [2026-09-23] git diff 显示某文件整文件变更（几百上千行）但实际只改了几十行时，先怀疑行尾翻转：用 `git diff --stat --ignore-cr-at-eol <file>` 对比确认。Why: 两个仓库（sleep_english / colab_listening_b_web）core.autocrlf=true 且 LF blob 进库，但 sleep_english/pipeline/llm_client.py 曾是历史遗留的唯一 CRLF blob——任何编辑后 git add 都触发整文件 diff（真实改动仅 115 行却显示 874/825）。How to apply: 确认是 EOL 后让 git 默认 clean filter 顺带归一化提交（一次整文件 diff 换此后永久干净），不要用 -c core.autocrlf=false 绕过以免把 CRLF blob 继续传下去；sleep_english llm_client.py 已于 2026-09-23 归一化。
### Project
- [2026-09-23 23:16:36] [2026-09-23] sleep_english 与 colab_listening_b_web 的 pipeline/llm_client.py + app/pipeline_service.py 源自 2026-09-15 复制但已漂移：WBK Provider 全套（633b70c）与 wbk 分支流式化（44097e7）仅存在于 sleep_english；colab 的 WBK 走 custom:* → openai 通用通道，2026-09-23 已给该 openai 通道加流式（stream=True + SSE 逐行解析 + 端点回普通 JSON 的兼容回退，_OpenaiStreamError/_parse_plain_chat_json）。LLM 基础设施改动（_chat 重试/退避/停止 hook/限速/gemini_chat/max_tokens）仍需双项目同步，但两文件已非同构（diff 860+/207-）。Why: 同步流式修复时发现 colab 无任何 WBK 代码，硬套 patch 会 py_compile 失败。How to apply: 后续改 llm_client/pipeline_service 时问一句是否需双项目同步；同步前先 diff 确认目标文件存在所引用的符号。

- [2026-09-23 19:39:12] [2026-09-23] 新增 WBK (WorkBuddy) LLM Provider（llm_provider="wbk"，commit 633b70c）：端点 http://45.13.214.22:7864/v1（llm_client.WBK_BASE_URL），OpenAI 兼容协议 + reasoning_effort 思考强度参数。核心：llm_client.WBK_MODEL_SPECS 20 模型规格表（max_out 输出上限/efforts 支持档位/default_effort），wbk_thinking_for() 把请求档位裁剪到模型支持集（不支持或 default=不发送 reasoning_effort），wbk_max_tokens_for() 把 max_tokens 按模型拉满（调用方传小值不生效，auto 32k/glm-5.3-flash 131072 等）。模型 ID 必须带 cn: 前缀（/v1/models 实测返回形式），裸名端点也接受但统一存前缀。配置三件套 wbk_api_key/wbk_model/wbk_thinking 已接 PARAM_SPEC/resolve_provider/_set_env/_build_args/pipeline.py argparse 与全部 Web 消费点（script_library override+review、topics_ai、runs 缩略图 override、voices_qwen、channel_factory/intro/outro _llm_chat、ai_test 流式）。Key 已存 configs/mode_sleep.json（gitignored）。端点行为实测：不支持档位不报错但被忽略，GET /v1/models 有权威规格（context_length/max_output_tokens/reasoning_supported_efforts）。Why: 用户要求思考强度按模型配置、max_tokens 直接拉满。How to apply: 改 WBK 相关逻辑以 WBK_MODEL_SPECS 为唯一事实源；注意 d2a3e26「服务连接」提交已按用户要求回退（force push 丢弃）。
- [2026-09-23 23:05:34] [2026-09-23] WBK 中转（http://45.13.214.22:7864）对非流式 /chat/completions 请求有 ~120s 上游硬超时（表现：状态 502、总耗时恒 120.0x s 零方差、首字 —、不计费），长输出生成必撞墙。llm_client._chat 的 wbk 分支已改为 SSE 流式（"stream": True + 逐行 data: 解析，commit 44097e7），实测 297.6s 长生成一次成功。How to apply: wbk 分支任何改动必须保持 stream=True 与逐行解析；新接 OpenAI 兼容中转 Provider 时默认走流式，避免同类超时。
- [2026-09-24 00:00:17] [2026-09-24] WBK Provider 全套已移植到 colab_listening_b_web（commit 24d644e，14 文件 +353/-30，叠加此前 6a90d07 openai 通道流式）。与 sleep_english 对齐点：config_manager PARAM_SPEC wbk 三件套+llm_provider wbk 选项+resolve_provider wbk 分支；pipeline_service _set_env/_build_args；pipeline.py Step0/argparse/main；script_library/topics_ai/channel_factory/intro_videos 的 _llm_chat·_build_llm_override·_chat_json 三分支（wbk_thinking_for 裁剪档位）；ai_test.py/pages/scripts/voices_qwen 接线；ai_test.html/scripts.html 模型与档位下拉。差异：colab 无 outro_videos.py 与 runs.py 缩略图 LLM override 链路（跳过）；colab wbk 分支流式用 _WbkStreamError。How to apply: 两项目 WBK 相关后续改动仍需双项目同步；colab 侧还有既有脏文件（CODELY.md/configs/*.json）未提交，与 WBK 无关。
- [2026-09-24 00:15:58] [2026-09-24 00:20:00] sleep_english Kokoro「produced no audio」根因=espeak-ng 未安装：KPipeline('a') 初始化 EspeakFallback 失败仅 warning→g2p.fallback=None，misaki 0.7.4 词库 OOV 词（如 hometown）lexicon 返 None 时 join 崩 NoneType+str，_kokoro_synth_chunk 三级降级对短句退化为整句重试必死。修复（pipeline/tts_engine.py）：_ensure_espeak_fallback 在 _get_kokoro 后用 espeakng-loader 内置 DLL 挂 EspeakFallback（british=False），失败仅告警保持旧行为；_PHONETIC_FIXES 加 hometown→home town；requirements.txt 加 espeakng-loader>=0.2.0。Why: espeak 兜底缺失使任意词库外词全灭且重试无效。How to apply: 排查 Kokoro 无声先查 g2p.fallback 是否 None（misaki 初始化 espeak 失败静默）；espeakng-loader 用法=EspeakWrapper.set_library(get_library_path())+set_data_path(get_data_path())，勿用 set_library（不存在）。
- [2026-09-24 16:44:42] [2026-09-24 16:45] sleep_english 已删除 SenseNova 与「OpenAI Compatible (内置)」两个 LLM Provider（用户要求），commit 见 git log：① config_manager PARAM_SPEC 删 sensova_api_key/sensenova_model/openai_base_url/openai_api_key/openai_model 五键，llm_provider 默认改 wbk（选项仅 wbk/gemini + custom:*），resolve_provider 兜底从 openai 改为 wbk（旧配置 llm_provider=sensenova 自动落 wbk），load_mode_config API Key 兜底 allowlist 改为仅 gemini_api_key；② llm_client._chat 删 sensenova 分支与 SENSENOVA_* env 读取，openai 分支保留作为 custom:* 自定义 Provider 的后端通道（OPENAI_MODEL 默认空串）；③ pipeline/pipeline.py 删 --image-provider/--api-key/--model 参数与 SENSENOVA env 注入，--llm-provider choices=[openai,gemini,wbk]；④ pipeline/sensenova_image.py 已 git rm 删除，bg_image.py/thumbnail_gen.py/pipeline.py 的 sensenova 生图分支全删（生图 Provider 只剩 mcp）；⑤ thumbnail_regen_cli.py 删 ref_img/provider/sensenova_api_key 入参，thumbnail_regen_service.py payload 同步；⑥ channel_factory.py 删 _gen_asset_sensenova 与 image_provider 分支（Logo/Banner 恒走 MCP）；⑦ templates ai_test/scripts/_config_scripts/runs/channel_factory 的 SENSENOVA_MODELS/OPENAI_MODEL_OPTIONS/模型联动 JS 全清，自定义 Provider 模型下拉复用 wbk_model 字段；⑧ configs/mode_sleep.json、default.json、channels/*.json 存量 sensenova_*/openai_* 键已清理（运行时数据不入库）。注意：CODELY.md L27/L29 架构描述仍提 sensenova/sensenova_image.py（CODELY.md 受记忆系统保护无法用 replace 修改），后续会话读到时以本记忆为准。**Why:** 用户明确要求「把 OpenAI Compatible (内置) 和 SenseNova 给删除了，相关代码清理干净」并选择「全删 SenseNova，openai 保留（custom 后端）」。**How to apply:** 新增 Provider 时改 config_manager.PARAM_SPEC llm_provider 选项 + resolve_provider 分支 + llm_client._chat 分派 + pipeline.py argparse 四处；不要再引用 sensenova_image 或 SENSENOVA_* env。
- [2026-09-24 19:20:01] [2026-09-24 19:20] sleep_english 新增「一次请求生成完整脚本」开关（llm_single_shot_script，group llm，默认关；commit 69a799d）：single_shot=True 走 llm_client_sleep._generate_single_shot —— 单次请求全部组数+元数据（max_tokens=65536/timeout=600s，WBK 分支自动按模型上限拉更高），输出不足按剩余区间补齐 ≤2 轮，缓存 sleep_{ck}_single.json 与分批文件不冲突。接线四件套（PARAM_SPEC/_build_args/pipeline.py argparse/_step0+script_library._llm_single_shot）已被并行会话的 Provider 清理提交 2d0a18b 一并带入，核心逻辑在 69a799d。Why: 用户要求一次请求出完整脚本（配合 WBK 大输出模型）。How to apply: _generate_batch 现有 allow_short 形参（数量校验改由调用方承担，字段校验内联），分批路径 allow_short=False 行为不变；改 single_shot 逻辑时勿动分批分支。

### Reference

