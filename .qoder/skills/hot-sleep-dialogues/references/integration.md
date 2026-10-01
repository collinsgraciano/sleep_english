# 字段 ↔ 管线消费对照手册

写作方法看 `references/craft.md`，字段格式看 `references/script-format.md`；本文件是两者的
**下游**：把「写出来的 JSON」和「跑起来的音频/卡片/成片」对起来。结论来自对 `pipeline/`、
`app/` 实际代码的核对（**行号会随重构变动，字段名与行为是稳定的**）。

> **一句话结论：只有三类问题会让整次运行中止**——① `dialogue` 缺失或为空
> （`app/pipeline_service.py:827-829`，`_seed_from_ai_scripts`）；② 某行 `text` 为空、
> 或**不含任何拉丁字母**（纯标点/纯数字），走到 TTS 时抛 `Kokoro produced no audio`
> （`pipeline/tts_engine.py:521`）；③ 顶层被处理的中文字段写成 `null`（缺键安全，`null` 会
> 在 `.strip()`/目录命名处崩）。其余字段写空了**都不报错**，只会静默降级成默认值或整段消失
> ——这才是最容易丢分的地方。
> 一条命令把三级列出来：`python .qoder/skills/hot-sleep-dialogues/scripts/preflight.py`。

---

## 一、九个步骤各读哪些字段

步骤清单的唯一事实来源是 `app/pipeline_service.py:40-50` 的 `STEP_DEFS`（执行顺序 = 列表顺序）：

| # | step id | 短标签 | 读哪些脚本字段 |
|---|---|---|---|
| 1 | `step0_script` | 脚本 | 预生成路径（`_seed_from_ai_scripts`）：`dialogue`（**只查非空**）、`topic`/`title`、`cefr`、`youtube_title`（**决定输出目录名**）；组数按 `len(dialogue)//2` 同步。LLM 路径才会跑 `_validate_script` 逐行校验 |
| 2 | `step1_mcp` | MCP | 无（sleep 模式按需初始化，只服务缩略图与可选背景图） |
| 3 | `step2_images_tts` | 音频 | `dialogue[].text`、`char_a_gender`/`char_b_gender`（`pipeline/tts_engine.py:770 build_voice_map`）、`scene`（可选 AI 背景图提示，空则回落 `topic`，`pipeline/pipeline.py:407`） |
| 4 | `step3_video` | 片段 | 无（sleep 模式 no-op） |
| 5 | `step4_timeline` | 时间轴 | `dialogue`（**A/B 由行序决定**——偶数行 A、奇数行 B，不看 `speaker` 值）、`sleep_pairs`；输出 `subtitles/output.srt` + `subtitles/meta.json` |
| 6 | `step45_thumbnail` | 缩略图 | `title_zh`、`thumb_badge`/`thumb_main`/`thumb_hook`（空则回落内置默认，`pipeline/thumbnail_gen.py:78-81`）、`youtube_*` |
| 7 | `step5_compose` | 合成 | `dialogue[].text`/`phonetic`/`zh`（文字预渲染进卡片）、`youtube_title`（产出文件名） |
| 8 | `step55_bgm` | BGM | 无脚本字段（读 `youtube_metadata.json` 章节） |
| 9 | `step6_4k` | 4K | 无脚本字段 |

---

## 二、字段三级分类：空了会怎样

### A. 会崩（中止整次运行）

| 字段 | 空了/写坏会怎样 | 证据（行号会随重构变动，字段名与行为是稳定的） |
|---|---|---|
| `dialogue` | 预生成路径直接 `_fail("预生成脚本无对话内容")`，该篇根本起不来 | `app/pipeline_service.py:827-829` |
| `dialogue[i].text` | 空 → 抛 `Kokoro produced no audio`；**纯标点/纯数字也算**（没有拉丁字母就合成不出音频） | `pipeline/tts_engine.py:521` |
| 任何会被 `.strip()` 的中文字段写成 `null` | 缺键安全，`null` 会崩（标题、目录命名、`thumb_*`、`youtube_*`） | `pipeline/media_utils.py:118-128` 调用点 |

> ⚠️ **最容易踩的坑**：预生成脚本这条「零 LLM」路径**不跑**逐行校验
> （`_seed_from_ai_scripts` 只查 `dialogue` 非空），空台词能一路走到 TTS 才炸——
> 只能在写稿/预检阶段拦住（`preflight.py` 标成 🔴）。

### B. 会降级（能出片，但内容/画面明显变差）

| 字段 | 空了会怎样 |
|---|---|
| `scene` | 背景图 prompt 退化成 `topic`（`pipeline/pipeline.py:407`） |
| `thumb_badge` / `thumb_main` / `thumb_hook` | 缩略图文案**三连回落**：徽章 → `不用背！`；主标题 → `title_zh` → `睡覺聽`；钩子 → `thumbnail_subtitle` → `零基礎自然開口說`（`pipeline/thumbnail_gen.py:78-82`）——写空了完全不报错 |
| `char_a_gender` / `char_b_gender` | `build_voice_map` 按性别默认兜底；性别缺失/写错会让 A/B 音色选择退化（`pipeline/tts_engine.py:770`） |
| `phonetic` / `zh`（某行） | 卡片上该行没有注音/中文；预生成路径只查非空（写了 `//` 也照样落盘） |
| `youtube_*` / `thumbnail_*` | 元数据/缩略图文案变默认或缺失 |

### C. 静默无害（不看也不影响完整性）

| 字段/写法 | 行为 |
|---|---|
| `speaker` 的值 | 出片播放**忽略** `speaker`；A/B 完全由**行序**决定（偶数行 = a、奇数行 = b）。`pipeline/sleep/audio_sleep.py:349-351` 直接切 `dialogue[0::2]` / `[1::2]` |
| 行数 > 800 或为奇数 | `total = min(num_pairs, len(rows_a), len(rows_b))`，**多出来的尾部（含落单的 A 或 B）静默丢弃**（`pipeline/sleep/audio_sleep.py:349-351`） |
| `char_a_role` / `char_b_role` / `char_*_description` / `cefr` / `lesson_type` / `channel_id` | 不进 prompt、不参与合成；本 skill 里 A/B 是**朗读声部**（男声/女声），不是剧情角色。`cefr` 等只影响元数据与徽章文案 |

---

## 三、成本与时长口径

- **每组 5 个 TTS 文件**（`pipeline/sleep/audio_sleep.py:176-180` 的 `pair_steps`）：
  `a_m`（A 男声常速）、`b_m`（B 男声常速）、`b_f`（B 女声常速）、`a_slow`（A 女声慢速）、
  `b_slow`（B 女声慢速）；另有 `intro_sleep.mp3` 与 `outro_sleep.mp3`。
  800 行 = 400 组 → 2000 段 TTS + 片头片尾。**TTS 全部本地**（Kokoro / Qwen / MOSS），
  **零积分**；时间成本是本地推理时长，不是钱。
- **可选 AI 背景图**（`--sleep-bg-image`）才会调用外部生图通道，是唯一会花钱的一项；
  未配 Key / MCP 未配置时**只跳过后成纯渐变，不中断**（`pipeline/pipeline.py:499-537`，
  注释写明「增强功能，失败不中断运行」）。
- **本文件不做成片时长预估**：成片长度由 `sleep_sequence`（每对播哪几步）、句间/组间 gap、
  卡片留白与片头片尾开关共同决定，这些是**运行参数**而不是脚本字段，用固定公式估算只会误导排期。
  要真实时长就跑一期或读运行目录 `subtitles/meta.json` 的 `timeline`（`pipeline/pipeline.py:622-634`）。
  预检只报规模口径：`{pairs} 组 · TTS {segments} 段（本地零积分）`。

---

## 四、四处门禁口径对比（**词数各不相同，别背错**）

| # | 门禁 | 位置 | 判据 | 是否阻断 |
|---|---|---|---|---|
| 1 | **管理页体检** | `app/ai_scripts_admin.py:522 validate_script` | error：`META_REQUIRED`（`:31`）缺失/空、`structure != sleep`、每行 `text`/`phonetic`/`zh` 空、`phonetic` 未 `/.../` 闭合、`speaker` 未按偶数 `char_a`/奇数 `char_b` 交替；warning：非 Big5 字形、篇内整句重复、跨主题撞句、行数不是 400/800、**词数不在 3–9**（`:560-564`，按 `re.split(r"[^A-Za-z']+")` 计数） | 不阻断（页面展示） |
| 2 | **脚本库本地检查** | `app/script_library.py:602 local_checks` | 结构（import `pipeline._validate_script`，severity high）、硬编码简体命中表（medium）、**每行词数 > `resolve_max_line_words("SLEEP_MAX_LINE_WORDS", 7)`（默认 7）→ `line_too_long`**（`:626-635`，注释写明「运行时 QA 门禁将按此拦截」）、性别/描述不一致（high） | 不阻断 |
| 3 | **生成期门禁** | `pipeline/sleep/llm_client_sleep.py:17-19 _sleep_max_line_words` | env `SLEEP_MAX_LINE_WORDS` → 默认 **7**，clamp **[4,10]**；`:286` 取用，约束 LLM 生成 | 生成路径内部重试 |
| 4 | **运行时结构校验** | `pipeline/pipeline.py:133 _validate_script` | **只查**：`len(dialogue) >= num_lines`、每行 `text`/`zh`/`phonetic`/`speaker` 非空。**不看词数、不看交替、不看 IPA 形状、不看简体** | 在 pipeline 内只被生成重试调用（`:169`）；预生成落盘路径不调 |

补充两条运行时事实（行号会随重构变动，字段名与行为是稳定的）：组数钳位见
`pipeline/pipeline.py:1113-1114`（`max(10, min(400, …))`、`num_lines = pairs * 2`）；
预生成落盘路径自己同步组数 `pairs = max(10, min(400, len(dialogue)//2))`
（`app/pipeline_service.py:837`），所以**超过 800 行的部分播不到**。

**写稿按最严格的一侧对齐**：3–7 词（满足 4 处全部口径）；≥8 词会被脚本库记
`line_too_long`，>9 词会被管理页与 `audit_script.py` 记 warn/error。

---

## 五、门禁分布在四处会出现的现象

| 现象 | 成因 |
|---|---|
| **skill 说干净、网页报红** | 管理页体检读 `app/ai_scripts_admin.META_REQUIRED` / `STANDARD_LINES`（`app/ai_scripts.py:26-27`），skill 侧的 `validate_all.py` 直接 import 同两个常量——只要一边改了口径而另一边没跟，就会出现「skill 报 0 错误、网页体检报 error」 |
| **稿子跑成功但少播** | 行数 > 800 或为奇数：`_seed_from_ai_scripts` 与 `audio_sleep.py:351` 都会截断尾部；`speaker` 写反也不报错，因为播放只看行序 |
| **同一句一边算错一边不算** | 繁体判定：管理页用 `_non_big5_chars` + `_ZH_OK_OUTSIDE_BIG5`，`audit_script.py` 有同名集合（`ZH_OK_OUTSIDE_BIG5 = set("咔哐噼咣擀哒")`）——两边不同步就会出现同一句一边报一边不报 |
| **收尾工具互相打架** | `write_review.py` 是唯一允许改写 `script.json` 的工具，且只写 `review` 键；`normalize_zh.py --apply` 会整档重写——两者**必须串行**，不能对同一个编号同时跑 |

一条命令把六道门一起跑：`python .qoder/skills/hot-sleep-dialogues/scripts/check_gates.py`
（结构/索引 · 内容工艺 · 出片就绪三组；exit `0` 干净 / `2` 仅 warning / `1` 有 blocker / `3` 用法错误）。

---

## 六、输出目录命名规则与同名不消歧风险

- 目录名 = `safe_filename(youtube_title, fallback=topic)`：emoji 剥掉、`\ / : * ? " ' < > |`
  去掉、空白转 `_`、**截断 80 字符**、为空则回落清洗过的 `topic`、再空则 `final_video`
  （`pipeline/media_utils.py:118-128`）；
- 落点 = `<output_dir>/<mode_name>/<safe_title>`，`mkdir(parents=True, exist_ok=True)`
  （`app/pipeline_service.py:846-849`；`output_dir` 默认 `./output`，`mode_name` sleep 即 `sleep`）；
- **同名不消歧**：两篇的 `youtube_title` 清洗后**前 80 字符相同**就写进**同一个运行目录**，
  后跑那次会覆盖/复用同目录产物，`exist_ok=True` 不提醒。当前基线：`ai_scripts_hot/` 20 篇 →
  20 个唯一目录名、0 冲突——**这是结果，不是保证**。写 `youtube_title` 时把主题关键词放前半段。

---

## 七、一次成功出片产出什么（sleep 模式）

```
output/sleep/<safe_filename(youtube_title)>/
├── script.json            # 本次实际使用的脚本（落盘副本，含运行时补写字段）
├── checkpoint.json        # 断点续跑用；跑完删除
├── images/                # 可选 AI 背景图（sleep_bg.png）；clips/ 是 no-op 空目录
├── audio/                 # intro_sleep.mp3 / outro_sleep.mp3 + pair_0001_a_m.mp3 …（5 个/组）
├── subtitles/             # output.srt（闭源字幕 sidecar）+ meta.json（timeline + 整份 script）
├── videos/                # final*.mp4（<safe_filename(yt_title)>.mp4，BGM 版为 _bgm，4K 版为 _4K）
├── thumbnail.jpg          # 缩略图（`--no-thumbnail` 时只有元数据，无图）
└── youtube_metadata.json  # 标题/简介/标签/章节
```

（`app/pipeline_service.py:1122-1123` 跑完删 `checkpoint.json`；`pipeline/sleep/video_compose_sleep.py:393` 决定成片文件名。）

---

## 八、为什么不设「时效性事实门」

- 本系列每行是 **A2 日常短句**（点单、问价、约时间），**时效性主张不是承重结构**：
  没有政策条文、表格编号、费率这类会随年份失效的断言；机器扫数字只会产生**假阳性**
  （`two of these` / `nine hundred a month` / `eight fifty` 都是剧情示例数值，
  扫出来既不能判真假、也不能自动修）；
- 唯一真实的数值风险是**前后不一致**（同一笔押金前面 900 后面 500、`eight fifty` 译文写成
  「八十五」）——靠**评审环节逐处核对金额/时间链**解决，不靠联网核实（见
  `references/review_rubric.md` 的 `pair_logic` / `idiomatic_a2` 判据）；
- 所以本 skill 的门禁只做**结构性 + 工艺性**判定（行数、词数、IPA 形状与 token 对齐、
  繁体字形、重复度、出片就绪），**不引入联网事实核查**。
