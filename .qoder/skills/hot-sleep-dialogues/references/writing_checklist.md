# 交稿前速查（写完先过这一遍，再跑门禁）

这份清单来自**实际写稿被门禁反复拦下的记录**——每一条都是**不改就会被判 error/warn** 的，
不是建议。格式细节看 `references/script-format.md`，写作方法看 `references/craft.md`，
字段被管线怎么消费看 `references/integration.md`。

## 一、一写就要对

| 约束 | 具体标准 | 常见踩法 |
|---|---|---|
| **行数** | 必须是 **2 的倍数且 ≤800**（标准 800 行 = 400 对） | 写到 801 / 799 行：出片时 `total = min(num_pairs, len(rows_a), len(rows_b))` 会**静默丢弃尾部**，不报错；行数为奇数时最后落单的 A 或 B 直接被丢 |
| **每行词数** | 新稿目标 **3–7 词**；管理页体检在 **3–9** 之外报 warn；`audit_script.py` 在 3–9 之外判 `word_count`（hard） | 写了 8–9 词：管理页不报、但脚本库本地检查按 `SLEEP_MAX_LINE_WORDS`（默认 7）记「行太长」→ 拆成两句 |
| **A/B 交替** | 偶数行 `char_a`、奇数行 `char_b`（`speaker` 由装配脚本写入；出片播放只看行序，不看 `speaker` 值） | **删句时只删一句** → 后半篇交替整体错位。删句必须**删相邻一整对**（a+b 两句） |
| **`title_quote`** | 必须与 `dialogue[0].text` **逐字一致** | 改完第一句忘了同步顶层 `title_quote` |
| **`phonetic`** | 每行都用 `/.../` **闭合**，含 `ˈ` 重音，与 `text` 逐词对应 | 只写了开头 `/`；弱读的 a/the/of 漏 token；把 `so much` 并成一个 token |
| **`zh`** | 每行都有，台湾正体，自然意译；`dialogue[].zh` **不加「」** | 混进简体字 / 异体字；译文里夹小写英文（人名品牌除外）；新写的内容加「」 |
| **不许舞台提示** | 台词里不能出现 `Ten minutes later…` / `(laughing)` / 画面说明 | 时间跳跃、动作描写写进了角色嘴里 |
| **首词单一度** | 同一首词全篇 **≤40 次**（400 对；审计阈值 = `max(10, 对数//10)`） | `I…` / `So…` 连开几十对 → `repeat_opener` |
| **整句唯一** | 篇内不重复、**不与全库（`ai_scripts/` + `ai_scripts_hot/`）任何一句相同** | 改了本脚本却撞上旧库；或与本批别的 agent 写出同一句 |

## 二、繁体陷阱

1. **判定方式**：汉字用「**Big5 编不编得出**」判繁简（`audit_script.py` 的 `zh_simplified`
   是 hard；管理页体检对同一批字只报 warning）。别手工背简繁对照表——同形字（被/女）
   不会被误报，这是刻意的设计。**两个字形要点**：`〇`(U+3007) → `○`、`⋯`(U+22EF) → `…`。
2. **白名单**：`咔哐噼咣擀哒`（拟声/台派口语写法）**不是简体**，不进错误清单。
3. **刻意不动的三个字**：`只/隻`、`游`、`念`。繁化工具屏蔽它们（`只能/只有/只要` 会被错提升成
   `隻能…`，`游泳→遊泳`、`紀念→紀唸` 是改坏），**两种写法都 Big5-clean、都不算缺陷**。
4. **meta 字段不会自动繁化**：`title_zh` / `scene_zh` / `youtube_title` / `youtube_description` /
   `youtube_tags` / `thumb_*` 要手工改成正体，或跑 `normalize_zh.py --meta --apply <编号…>`。

## 三、机器审计阈值速查（`audit_script.py`）

**HARD_KINDS（必须清零）**

| kind | 触发条件 |
|---|---|
| `json` | `script.json` 解析失败 |
| `word_count` | 该行词数不在 **3–9** |
| `ipa_shape` | `phonetic` 未以 `/` 开头并以 `/` 结尾 |
| `ipa_glyph` | 音标里混入非 IPA 字符（假名/汉字/西里尔等 lookalike） |
| `zh_simplified` | `zh` 含 Big5 编不出的汉字，或出现 `〇`/`⋯`（白名单 `咔哐噼咣擀哒` 除外） |
| `capital` | 句首小写 |
| `end_punct` | 句末缺 `.` `!` `?` `…` |
| `spacing` | 连续空格、空格+标点、`..` 等排布异常 |
| `grammar` | 命中约 25 条硬搭配（`I has` / `he don't` / `more better` / `discuss about` / `listen music` / `everyday` 误用…） |
| `article` | `a` + 元音开头、`an` + 辅音开头（`a user` 一类合法写法已排除） |
| `self_dup` | 篇内整句重复（按去标点小写归一后比较） |
| `cross_dup` | 与另一个 folder 的整句撞车（只改本脚本那行） |

**REVIEW_KINDS（信号，逐条判断后修或具名保留）**

| kind | 触发条件 |
|---|---|
| `ipa_tokens` | 音标 token 数与词数不一致（差值 ≥1；句子含数字/连字符/全大写缩写/撇号时放宽到 ≥3） |
| `spelling` | 全库仅出现 ≤1 次、且与某常见词编辑距离 1 的词（多数是笔误，也可能是合法专名） |
| `near_dup` | 与别的主题共用 **≥3 个 4-gram 且占本句 ≥60%** |
| `repeat_opener` | 同一首词出现 **≥ `max(10, 对数//10)`** 次（400 对 = 40 次） |
| `word_cap` | 该行词数 > `SLEEP_MAX_LINE_WORDS`（默认 **7**，与 `app/script_library.py::local_checks` 走同一个 `resolve_max_line_words` 入口，口径不会分家）；存量稿会有数百条命中，报告里**聚合成一条**只点名前几处 |
| `flat_b` | b 句没有实词（全是停用词） |
| `zh_latin` | 译文里夹独立小写英文（白名单：`wifi tv app gps mp3 dvd id atm kg cm mr mrs ms dr`） |

> 审计会额外统计 `generic_b`（奇数行是 `Okay.`/`Sure.`/`Right.` 这类通用回应或 ≤2 词），
> 它**不计入 hard / review 总数**，但连续出现就是节奏问题，按 `craft.md §3.1` 处理。

## 四、出片就绪（`preflight.py`）

`python .qoder/skills/hot-sleep-dialogues/scripts/preflight.py [dir] [--json] [--expect-lines 800]`
把问题分成三级（判据与 `references/integration.md` §二 完全一致）：

- **🔴 will-abort（不打就中止整次运行 / 静默丢内容）**：`dialogue` 缺失或为空；
  某行 `text` 为空、或**不含任何拉丁字母**（纯标点/纯数字会让 TTS 抛
  `Kokoro produced no audio`）；`text`/`zh`/`phonetic` 键存在但值为 `null`；
  行数 **> 800**（组数被钳到 400，超出部分不播）；`script.json` 解析失败。
- **🟡 will-degrade（能出片，但少播/降级）**：行数为**奇数**（最后落单一行不朗读）或
  不足 20 行；`zh`/`phonetic` 为空；`speaker` 与奇偶不符（播放不看它，但管理页报 error）；
  `structure != "sleep"`；单行词数 > `SLEEP_MAX_LINE_WORDS`；`scene` 空（背景提示退化成
  `topic`）；`title`/`title_zh`/`youtube_title`/`category`/`cefr` 空；
  `thumb_badge`/`thumb_main`/`thumb_hook`/`thumbnail_subtitle` 空（回落到内置默认）；
  `char_a_gender`/`char_b_gender` 空（音色按默认兜底）；`youtube_tags` 空；
  `zh` 含非 Big5 字形；行数不是标准 800；**批级**：两篇的
  `safe_filename(youtube_title)` 前 80 字符相同 → 会写进同一个运行目录。
- **· notes（提示）**：每篇一行成本口径 —— `{pairs} 组 · TTS {segments} 段（本地零积分，
  5 段/组 + 片头片尾）· 输出目录 <safe_title>`。

预检还会打印按 `STEP_DEFS` 派生的就绪表与成本口径
（`{pairs} 组 · TTS {segments} 段（本地零积分）`）。**本 skill 不做成片时长预估**
（理由见 `references/integration.md` §三）。

## 五、收尾顺序（别打乱）

1. **繁化**：`normalize_zh.py`（先 dry run，再 `--apply`）——只改中文，不改英文/音标；
   顶层文案加 `--meta`。**必须排在所有改写之后**，否则改写又带回简体。
2. **查重改写**：全库整句复比到 0；改完再跑一次 `audit_script.py`（别的脚本也在动，
   你写完时唯一的句子几分钟后可能变成别人的近重复）。
3. **评审写回**：`write_review.py <NNN>` 是**唯一**允许改写 `script.json` 的工具，
   它只写 `review` 键；**只能串行**，一次一个编号（`normalize_zh.py --apply` 会整档重写，
   两者对同一编号必须排队）。
4. **别再跑 `assemble_script.py`**：`_parts/` 里是改写前的分块，重装配会覆盖掉「繁化 +
   查重改写 + 评审」的全部修正。要改内容就直接改 `script.json`；全部完成后重跑
   `check_gates.py` 与 `build_manifest.py --check`。

## 六、跑门禁（一条命令）

```
python .qoder/skills/hot-sleep-dialogues/scripts/check_gates.py [dir] [--only 1,2] [--json]
```

一条命令跑六道门，分三组：**结构/索引**（`validate_all.py --lines 800`、`build_manifest.py --check`）
· **内容工艺**（`audit_script.py --fast`、`normalize_zh.py` dry run、`ipa_vote.py --limit 40`）
· **出片就绪**（`preflight.py`）。

退出码：**`0` 干净 / `2` 只有 warning / `1` 有 blocker / `3` 用法错误**。
`--json` 走统一信封 `{"tool","root","checked","errors","warnings","state","summary","findings":[…]}`，
`findings[].level ∈ {error, warning, note}`，每条带 `code` / `stage` / `detail` / `fix` / `lines`。
**六道门全绿（或只剩允许的既有 warning）才算交稿。**
