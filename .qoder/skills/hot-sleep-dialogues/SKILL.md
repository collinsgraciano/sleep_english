---
name: hot-sleep-dialogues
description: >
  为 sleep_english 生成「睡觉听英语」预生成脚本：先联网调研出华人英语学习者会主动搜索、
  且睡前愿意反复听的新主题，再逐主题由 AI 真实撰写 800 行（400 对）A2 睡前英文情境对话，
  输出 ai_scripts_hot/ 并登记进 manifest，打包成 pipeline 可直接零-LLM 出片的 script.json。
  覆盖：主题调研与查重、逐主题生成、多轮交叉审查、IPA 逐词与繁体字形收口、出片就绪预检、
  控制台索引登记。Use when asked to 生成热门主题脚本 / 新增预生成脚本 / 批量生成对话脚本 /
  找热门主题 / 审查或修既有脚本 / 判断某篇能不能直接出片 / 跑门禁与全库校验。
  质量承诺：成对逻辑与情境推进、A2 口语地道度、美式 IPA 逐词对齐、台湾正体字形、
  且**每条硬规则都挂着可执行的门禁**（check_gates.py 一条命令跑六道门）。
  禁止用 Python 模板拼句生成内容：英文句、音标、中文翻译必须由 AI 手写，
  Python 只允许用于合并分块、校验、审计、查重台账、写回审查结论与生成 manifest。
---

# Hot Sleep Dialogues — 联网选题 + 热门主题睡前英文脚本生成

## Overview

两步走：**① 联网调研出「有真实需求证据、且与台账不重复」的新主题；② 每个主题由 AI
（subagent）真实撰写 800 行情境对话**。所有英文句、音标、中文翻译必须是 AI 手写，
**严禁**用脚本/模板拼句生成内容。输出目录：`ai_scripts_hot/`。

四条质量目标驱动下面所有选择，缺一条就不算完成：

1. **成对逻辑与情境推进** —— 每对是 a 发起 + b 直接回应，8 段 × 50 对段内推进、段间过渡，
   不是 400 个孤立句子（`references/craft.md` §二/§三）。
2. **A2 口语地道度** —— 3–7 词、高频具体词、缩写与口语标记齐全，译文是自然繁中意译
   （`references/craft.md` §四/§六）。
3. **美式 IPA 逐词对齐** —— `/.../` 闭合、token 与词一一对应、符号级错标靠全库表决收口
   （`references/craft.md` §五，工具 `ipa_vote.py`）。
4. **可出片、不静默降级** —— 行数是 2 的倍数且 ≤800、每行 `text` 非空且含英文字母、
   必填字段非空；**这些没有任何下游环节替你保证**，由 `preflight.py` 与 `check_gates.py` 拦
   （`references/integration.md` §二）。

> **为什么 skill 要自己扛这条线**：控制台只做两件事——管理页体检（`app/ai_scripts_admin.py:522`
> `validate_script`，查字段/行数/`/.../` 闭合/奇偶交替/3–9 词）与脚本库本地检查
> （`app/script_library.py:602` `local_checks`）。而「预生成脚本零-LLM 出片」这条路径
> **不跑逐行校验**（`app/pipeline_service.py:823-829` `_seed_from_ai_scripts` 只查
> `dialogue` 非空），单句词数上限、渲染就绪、审查结论都不由管线保证。所以写稿侧必须自带门禁。

## Where things live

| path | what it is |
|---|---|
| `ai_scripts_hot/` | **本 skill 的产出库**：`NNN_Topic/script.json` + `manifest.json`（控制台唯一索引） |
| `ai_scripts_hot/_parts/<NNN_Topic>/` | 生成中间产物（`meta.json` + `pairs_01..08.json`），全部装配完成后可删 |
| `ai_scripts_hot/_review/<FOLDER>.round{1,2}.json` | 交叉审查报告（round1=reviewer，round2=独立 verifier） |
| `ai_scripts_hot/_audit/<FOLDER>.json` | 机器审计逐条结论（`audit_script.py` 写；stdout 只打预算内摘要） |
| `ai_scripts_hot/_topics_batch.json` | 本批定稿主题的机器可读清单（供 `--check` 复核与断点续跑；不入 git） |
| `ai_scripts_hot/_research/source_{A..D}.json` | Step 1 四个来源的调研候选（含出处证据） |
| `ai_scripts/` | 另一个预生成目录，**当前实测已空**（`manifest.json` count=0）；查重与编号仍把它算作两库之一 |
| `.qoder/skills/hot-sleep-dialogues/references/` | 契约与手册：`script-format.md`（格式）、`craft.md`（怎么写）、`integration.md`（字段被管线怎么消费）、`review_rubric.md`（怎么审）、`writing_checklist.md`（阈值速查）、`topic-research.md`（选题 playbook）、`topics.md`（选题台账）、`agent-prompt.md` / `reviewer-prompt.md`（派单模板） |
| `.qoder/skills/hot-sleep-dialogues/scripts/` | 全部确定性工具（见 Resources）；项目根按标记法反推，任何 CWD 下都能跑 |
| `app/ai_scripts.py` / `app/ai_scripts_admin.py` | 控制台侧的索引、体检、增删改查（与本 skill 是**同一套规则的两份实现**） |
| `pipeline/sleep/` | 出片侧：`audio_sleep.py`（TTS 编排）、`sleep_cards.py`（卡片）、`timeline_sleep.py`、`llm_client_sleep.py`（另一条产出口） |
| `output/<mode>/<safe_filename(youtube_title)>/` | 出片落点（`mode` = `sleep`） |

命名不变量：脚本目录名必须是 `^\d+_` 且含 `script.json`——`app/ai_scripts.py:52 list_ai_scripts`
只遍历 `manifest.json` 的 `themes`，而 `manifest` 由 `^\d+_` 目录扫描得到；`_`/`.` 前缀目录
（`_parts`/`_review`/`_recycle_bin`…）一律不算脚本。编号全局唯一，新批次起点看
`list_used_topics.py` 末尾的 `# next index`（= 两库磁盘最大号 ∪ `_parts/` ∪ 本批清单 +1）。

## 硬规则（每条都挂门禁；改规则前先看这一节）

1. **AI 亲写，禁模板**。英文句/音标/译文必须由撰写 agent 现场写；唯一允许运行的脚本是
   `scripts/` 下的确定性工具（合并、校验、审计、查重、繁化、写回、建索引）。
   判据：`grep` 产物里不应出现 `THEMES`/模板表之类的批量填充结构（对照
   `.workbuddy/skills/sleep-script-batch/scripts/generate_self.py` 那种内置 100 主题对的写法
   ——**那是另一条产出口，不是本 skill 的做法**）。
2. **格式契约**：完整字段表见 `references/script-format.md`；不可协商项：`dialogue` 长度
   = 800（= 400 对 × 2 行）、行序严格 `char_a`/`char_b` 交替、`phonetic` 用 `/.../` 闭合、
   `zh` 全为台湾正体、`structure: "sleep"`、`lesson_type: "listening"`、
   `title_quote` == `dialogue[0].text`。判据：`validate_all.py`（error 级）。
3. **不重复**：主题在写作前查重（`list_used_topics.py --check <候选清单>`，硬撞 exit 1 不放行），
   句子在写完后查重（`audit_script.py` 的 `self_dup` / `cross_dup` / `near_dup`）。
   撞句时**只改本脚本**那一句（改完必须再跑一次审计——别的脚本也在动）。
4. **行数必须是 2 的倍数且 ≤800**。>800 或奇数行会被**静默丢尾**：组数钳位见
   `pipeline/pipeline.py:1113-1114` 与 `app/pipeline_service.py:837`，播放按
   `dialogue[0::2]`/`[1::2]` 切片（`pipeline/sleep/audio_sleep.py:349-351`）。
   判据：`preflight.py` 的 `pairs-clamped`（🔴）/`odd-lines`（🟡）。
5. **每篇必须走多轮交叉审查，并把结论写回产物**。机器审计 → round1 reviewer → round2 独立
   verifier，直到审计 hard=0 且 verdict=APPROVED；循环策略与 4 维判据见
   `references/review_rubric.md`。最后**必须**跑
   `write_review.py <NNN>` 把结论写进 `script.json` 的 `review` 字段——不跑这一步，
   控制台脚本库永远显示「未审查」，多轮审查等于白做。
6. **六道门全绿才算完成**：
   ```bash
   python .qoder/skills/hot-sleep-dialogues/scripts/check_gates.py ai_scripts_hot
   ```
   一条命令跑「结构/索引 · 内容工艺 · 出片就绪」三组六门；退出码 `0` 干净 / `2` 只有 warning /
   `1` 有 blocker / `3` 用法错误（编排器把 `3` 也算失败）。**warning 不阻断，但新稿要求清零**。
7. **收尾顺序不可打乱**：繁化（`normalize_zh.py`）→ 分片查重改写 → `write_review.py` →
   重跑 `check_gates.py`。**已经收尾过的 script.json 不要再跑 `assemble_script.py`**：
   `_parts/` 里是改写前的分块，重装配会覆盖掉繁化 + 查重 + 审查的全部修正。

## 工作流

### Step 1 — 联网调研选题（不可跳过，不可凭印象编）
按 `references/topic-research.md` 走：

1. 先读台账：`python .qoder/skills/hot-sleep-dialogues/scripts/list_used_topics.py --categories`
   （已用题数 + 分类分布 + 末行 `# next index`＝本批起始编号；配额：每类 ≤ 本批 25%）；
2. 并行 spawn 4 个只读调研 agent（来源 A 华人搜索热词 / B 同类频道爆款 / C 趋势与长尾词 /
   D 教材与 CEFR 清单），各交回 25–40 条**带 2 条出处证据**的候选，写进
   `ai_scripts_hot/_research/source_{A..D}.json`；
3. 主 agent 合并 → 按 rubric（需求热度×2、可展开性×2、睡前友好×1.5、差异化×1.5、
   A2 密度×1、点击潜力×1）打分取前 N（默认 20）；
4. 硬查重：`python …/scripts/list_used_topics.py --check ai_scripts_hot/_topics_batch.json`
   → 硬撞 exit 1 就不放行（`2` = 只有近义提示，需人工判断）；再逐条做**语义**查重
   （换措辞的同场景＝重复）；
5. 定稿写两处：表格 append 到 `references/topics.md` 的新批次段（含「需求来源」与
   「八段大纲关键词」两列）+ `ai_scripts_hot/_topics_batch.json`。

### Step 2 — 逐主题 AI 生成（每主题一个 subagent）
派单提示词用 `python …/scripts/generator_prompt.py <NNN...>` 生成（它从 `_topics_batch.json`
读真实文件夹名/八段大纲/行数，避免手抄过期；模板本体在 `references/agent-prompt.md`）。
把打印出来的文本原样交给一个 general-purpose agent（后台运行，**每批 ≤5 个并行**）。
每个 agent 负责：

1. 撰写 8 个 `pairs_XX.json`（各 50 对，格式 `{"a":{text,phonetic,zh},"b":{...}}` 数组）
   和 1 个 `meta.json`（script.json 除 dialogue 外的全部字段），写入
   `ai_scripts_hot/_parts/<NNN_Folder>/`；八段大纲照台账的 `eight_beats`；
2. 运行 `python …/scripts/assemble_script.py ai_scripts_hot/_parts/<NNN_Folder> 400`
   合并校验（自动修正 speaker 交替、检查字段完整性/行数/`title_quote` 对齐，失败信息会打印，
   agent 须修复分块后重跑直到通过）。**通过后它会顺手把本篇登记进
   `ai_scripts_hot/manifest.json`**——控制台只认 manifest，没登记的脚本在网页和外包 worker
   眼里等于不存在，所以别把这一步留到批次收尾。

主 agent 收集每个 subagent 的结果（通过/失败），失败的重新 spawn 一次。
**重派前先看 `_parts/<NNN_Folder>/` 落到哪一步**：里面空（agent 跑几步就死于运行时错误）
就原样重派，已有若干 chunk 就只派缺的那几段，别让新写者重写已有的段。
800 行最常出现的失败＝后段偷工/少写 chunk：按 `agent-prompt.md`「写不完怎么办」派**续写
agent**（一次 Write 一段，禁止回头重读已写文件），同一篇补两次仍不合格就删 `_parts/<FOLDER>`
重开，别反复打补丁。

### Step 2.5 — 多轮交叉审查（每个脚本都要走，不可跳过）

判据与循环策略在 `references/review_rubric.md`，角色模板在 `references/reviewer-prompt.md`
（模板里 `{LINES}`/`{PAIRS}` 按本篇实际值填；`reviewer_prompt.py` / `verifier_prompt.py`
会从磁盘读真实值再打印派单）。

1. **机器审计**（Python，只检测不生成）：
   `python …/scripts/audit_script.py [NNN...] [--fast] [--json]`
   → `ai_scripts_hot/_audit/<FOLDER>.json`。`!` = 有 hard 缺陷（必须改），`~` = review 信号
   （由 agent 判断）；`--fast` 跳过跨脚本近重复扫描（全库扫描较慢，单个编号用它）。
   exit `0` 全清 / `1` 有 hard / `2` 只有 review 信号。
2. **reviewer agent（每脚本 1 个）**：拿审计报逐条处理 + 通读 400 对修「答非所问 /
   情境不推进 / 不地道 / 不好玩」，改完自己重跑审计到 hard=0，写
   `ai_scripts_hot/_review/<FOLDER>.round1.json`；派单用
   `python …/scripts/reviewer_prompt.py <NNN> [--extra 本篇重点]`（前人只改了一半时加 `--resumed`）；
3. **verifier agent（每脚本另起 1 个，没参与撰写）**：陌生读者视角重读并补修，写
   `round2.json`（**必须带 `verdict` 与 `score`**，见 review_rubric.md），只有可发布才给
   `APPROVED`；否则再来一轮。派单用 `python …/scripts/verifier_prompt.py <NNN...>`
   （它会把 `ipa_vote.py` 与 `write_review.py` 两步写进派单）；
4. **写回结论**：`python …/scripts/write_review.py <NNN>` → 写进 `script.json` 的 `review`
   （控制台据此显示分数/verdict 与「审查过期」）。它是**唯一改写 script.json 的工具**，
   只动 `review` 键，**必须串行**（与 `normalize_zh.py --apply` 对同一编号不能并发）。
5. 编排：一脚本一 agent，**800 行的审阅并发上限 10**，「完成 1 个 → 补发 1 个」；
   一篇没跑完可补派「后半 4 段」，但**必须串行**；批次结束后主流程重跑全库审计
   （不带 `--fast`）+ `ipa_vote.py` 全量（agent 常留下没闭合的 `/...` 与符号级错标），
   不能只信 agent 自报。

审查四个维度：① 成对逻辑与情境推进/故事弧线 ② 口语地道度 + A2 用词 + 繁体译文自然度
③ 美式 IPA 逐词准确 ④ 句长与整句/近重复。

### Step 3 — 汇总校验（含控制台口径）
```
python .qoder/skills/hot-sleep-dialogues/scripts/check_gates.py ai_scripts_hot
python .qoder/skills/hot-sleep-dialogues/scripts/validate_all.py ai_scripts_hot --lines 800 --console
python .qoder/skills/hot-sleep-dialogues/scripts/build_manifest.py ai_scripts_hot --check
```
- `check_gates.py` 是日常入口（六门一次跑完，分组汇报、统一退出码、`--json`）；`--only 1,2`
  只跑结构组。
- `validate_all.py --console` 会对每篇再跑一遍管理页那套体检——「skill 说干净」必须等于
  「网页体检不报红」，否则就是两边规则又分家了；末尾自带 manifest 对账，漂移即 exit 1。
- `build_manifest.py --check` 只报盘↔索引漂移（漏登记/幽灵条目/行数过期）；去掉 `--check`
  就是整体重建（装配时已逐篇登记，这里只是兜底）。
- 报 0 error 才算完成（旧批次 400 行只记 warn；要按批卡数量就加 `--expect <N>`）。

### Step 3.5 — zh 全量繁化（收尾一次性）
生成 agent 的译文常混入简体字，最后一遍确定性繁化（只改中文，不改英文/音标，
不属于内容生成）：
```
python …/scripts/normalize_zh.py                # dry run（exit 2 = 有待繁化行）
python …/scripts/normalize_zh.py --apply        # 写回 dialogue[].zh
python …/scripts/normalize_zh.py --meta --apply 1 2 …   # 连顶层中文文案一起繁化
```
`--meta` 额外覆盖全部顶层中文字段（`title_zh / scene_zh / story_hook / intro_zh / outro_zh /
welcome_zh / practice_intro_zh / youtube_description / youtube_tags / thumbnail_*` 等），
因为 reviewer 只改 `dialogue[].zh`；`title_quote`（必须与 `dialogue[0].text` 一致）与
`channel_id / cefr` 跳过。
**`--apply` 会整档重写 script.json，所以只能对「当前没有 agent 在改」的编号跑**。
它用 OpenCC s2t + 台湾标准字形修正表（喫→吃、臺/檯→台、裏→裡、纔→才、牀→床、羣→群、
瞭→了、麪→麵、錶 按上下文还原、風採→風采、傢夥→傢伙、〇→○、⋯→…），转换后再跑一次
Step 3 校验。**`只/隻`、`游`、`念` 三个字刻意屏蔽不动**（s2t 会把「只能/只剩」提升成
「隻能…」，把「游泳→遊泳」「紀念→紀唸」改坏；反向降级 `隻` 又会毁掉正当量词）。
跨主题整句查重：`ai_scripts_hot/_existing_lines.txt` 是既有全量整句清单（生成 agent 用它
自查，会被并发任务删除或过期），收尾/需要时重新生成：
```
python -c "import json,glob;L={l['text'].strip() for f in glob.glob('ai_scripts/*/script.json')+glob.glob('ai_scripts_hot/*/script.json') for l in json.load(open(f,encoding='utf-8'))['dialogue']};open('ai_scripts_hot/_existing_lines.txt','w',encoding='utf-8').write('\n'.join(sorted(L))+'\n')"
```
撞句处理：按脚本切分片（每条重复句只在编号较大/后到的脚本里改），spawn agent 逐句在原对话
语境改写 text + phonetic + zh，改完全库集合复比至 0。

### Step 4 — 出片就绪预检（花时间跑一期之前）
```
python .qoder/skills/hot-sleep-dialogues/scripts/preflight.py ai_scripts_hot
```
三级分类：🔴 会崩/静默丢内容（`dialogue` 缺失、`text` 为空或不含英文字母、`null` 字段、
行数 > 800）、🟡 会降级（奇数行、`zh`/`phonetic` 空、`speaker` 错位、词数超上限、
`scene`/`thumb_*`/性别/分类为空、非 Big5 字形、输出目录同名冲突）、· 提示（每篇的成本口径与
输出目录名）。判据与 `references/integration.md` §二 完全一致。

### Step 5 — 与控制台的契约（两头都会动到，改前先对齐）

产出能不能在网页里被选中、跑出来是不是完整一期，取决于下面这些约定；skill 侧脚本与控制台
侧代码是**同一套规则的两份实现**，任何一边改了都要去看另一边。

| 约定 | 控制台侧 | skill 侧 | 破坏后的现象 |
|---|---|---|---|
| manifest 是唯一索引 | `app/ai_scripts.py:52 list_ai_scripts` 只遍历 `manifest["themes"]` | `assemble_script.py` 逐篇登记 + `build_manifest.py` 重建 | 脚本在磁盘上，但下拉/脚本库/`colab_worker --ai-script` 全看不到 |
| 自动入库 | `app/ai_scripts.py:169 sync_library()` 每次列目录按 `script.json` mtime 增量建档/刷新 | 无需手工「导入」 | 手点导入只是同一套逻辑 |
| 必填字段 / 标准行数 | `app/ai_scripts_admin.py:31 META_REQUIRED`、`app/ai_scripts.py:26-27 STANDARD_LINES=(400,800)` | `validate_all.py` 直接 import 这两个 | skill 报干净、网页体检报红 |
| 繁体判定 | `app/ai_scripts_admin.py:569 _non_big5_chars` + `_ZH_OK_OUTSIDE_BIG5` | `audit_script.py` 的同名集合、`normalize_zh.py` | 同一句一边算错一边不算 |
| 组数上限 | `pipeline/pipeline.py:1113-1114` 把 `sleep_pairs` 钳在 10–400；`app/pipeline_service.py:837` 按脚本实际行数同步 | 800 行＝400 对＝上限 | 超 800 行的后半段被静默丢掉 |
| 审查结论可见 | `app/script_library._doc_meta` 读 `doc.review`（`score`/`verdict`/`stale`） | `write_review.py` 写 `script.json["review"]` | 多轮审查白做，网页永远显示「未审查」 |
| 编号全局唯一 | `app/ai_scripts_admin.py:145 next_index()` = 两目录磁盘最大号 与 `_topics_batch.json` 已定稿号 一起取 max+1 | `list_used_topics.py` 的 `# next index`（另算 `_parts/`） | 批次进行中在管理页「新增脚本」会抢走本批号段 |
| 过程产物不入库 | `.gitignore` 排除 `ai_scripts*/_*` | 所有脚本跳过 `_`/`.` 前缀目录 | 回收站/半成品被当成脚本登记 |
| 删除＝回收站 | 管理页删除移进 `ai_scripts*/_recycle_bin/`（`app/ai_scripts.py:30`） | 所有脚本跳过 `_` 前缀目录 | 回收站里的旧稿被当成脚本登记 |

管理页能改 `script.json`（改完 mtime 变了会自动刷进脚本库），所以派 reviewer/verifier agent
之前先确认没有人在网页里手改同一篇。

## 质量门槛（什么算好稿）

- **成对逻辑**：每对的 b 都直接接住 a；b 不只有语气词；相邻对之间情境推进；至少 1 处真意外
  且它改变走向；结尾收束（`references/craft.md` §二/§三）。
- **语言**：3–7 词、A2 高频具体词、缩写与口语标记齐全、无课本套句、无舞台提示混进台词
  （`references/craft.md` §四）。
- **IPA**：`/.../` 闭合、token 与词一一对应、弱读虚词各占一个 token、符号级错标经全库表决
  收口（`references/craft.md` §五）。
- **译文**：台湾正体、自然意译、`dialogue[].zh` 不加「」、无 Big5 表外字（白名单
  `咔哐噼咣擀哒` 除外）（`references/craft.md` §六）。
- **变化度**：篇内不重复、不与全库整句撞车、近重复清零、同一首词 ≤40 次
  （`references/craft.md` §七）。
- **每行都能教一句能立刻用的话**：≥3 句观众第二天能原样复述。
- 每条硬约束的阈值与常见踩法在 `references/writing_checklist.md`（写稿时摊在旁边看的那张表）。

## Resources

references：
- `references/script-format.md` — **格式契约**：doc/script/dialogue 全字段、行序、逐字段规范、
  字段三级分类与 Direct-use check。
- `references/craft.md` — **写作方法**：选题三线、8 段骨架表、成对逻辑（含 ❌/✅ 对照）、
  A2 语言质感、IPA 规则、繁体字形、变化度、交稿自检清单。
- `references/integration.md` — **字段 ↔ 管线消费地图**：9 步逐字段、会崩/会降级/静默无害三级、
  成本口径、四处门禁口径对比、输出目录命名与同名风险。
- `references/review_rubric.md` — **评审判据**：独立性、循环策略（≥2 轮 / 连续 2 轮 clean /
  上限 4 轮）、4 维 × 25 分判据、issue 格式、What NOT to flag、`review` 写回形状。
- `references/writing_checklist.md` — **交稿前速查**：一写就要对的三列表、繁体陷阱、
  审计阈值速查、出片就绪三级、收尾顺序、门禁命令。
- `references/topic-research.md` — **联网选题 playbook**：四类来源与检索式、打分 rubric、
  8 段可展开性判定、查重规则、调研 agent 模板。
- `references/topics.md` — 选题台账（已用主题登记册，新批次 append 到末尾；权威排重基线）。
- `references/agent-prompt.md` — 单主题生成 agent 的提示词模板（**`generator_prompt.py` 直接读它**，
  单一来源；含 8 chunk × 50 对、用语约束与「写不完怎么办」续写策略）。
- `references/reviewer-prompt.md` — 三个角色模板（round1 修订 / round2 独立验收 / round3 对账）
  与批量编排经验；判据在 `review_rubric.md`，两份文件分工固定。

scripts（全部支持从任意 CWD 运行；退出码统一 `0 干净 / 1 有 blocker / 2 仅警告 / 3 用法错误`，
除注明外都支持 `--json`）：
- `scripts/skill_common.py` — 共用底座：标记法解析项目根、退出码常量、JSON 信封、报告预算。
  **不含任何检查规则**（规则只在各自脚本里实现一次）。
- `scripts/check_gates.py [dir] [--only 1,2] [--lines 800] [--expect N] [--json]` — **一条命令跑完
  六道门**（结构/索引 · 内容工艺 · 出片就绪），只解析子脚本的 JSON 信封，不抓文本；
  任何一门返回 `1`/`3` 都算失败。
- `scripts/preflight.py [dir] [--expect-lines 800] [--json]` — **出片就绪预检**（read-only）：
  复用管理页体检 + 机器审计，再加 sleep 特有的渲染就绪检查与成本提示，输出
  🔴 会崩 / 🟡 会降级 / · 提示 + 步骤就绪表。
- `scripts/motif_scan.py [NNN...] [--all] [--rank] [--json]` — **段落级「母题重复」扫描**：
  按 4 段 × 50 对统计实词母题 top-8、相邻段实词 Jaccard、b 句 Yes/No 起句占比、char_a 开头词分布。
  `audit_script.py` 只查*整句*重复与 4-gram 近重复，**查不出「同一件事换个说法讲 11 遍」**，这一支专治它。
  阈值经库内基线校准：**主题名词在一个 50 对段里 ≥25 次**才算「极端集中」（`⛔`），12–24 次只记 `·` 提示
  —— 因为主题式循环练习本来就高频复现主题词（实测 001–020 老批次里 007 pot×26 / 008 oil×25 /
  017 gel×29 / 013 warm×30 都是常态）；b 句 Yes/No 起句 ≥50% 才算节拍单一（全库中位数 24%）。
  `--rank` 按「最热母题词频」排序，用来判断某篇相对库内基线是否真的异常。
- `scripts/validate_all.py [dir] [--lines 800] [--expect N] [--console] [--json]` — 全量结构校验
  + 重复句检测 + manifest 对账；`--console` 按管理页体检复核；必填字段与标准行数 import 自
  `app.ai_scripts_admin`。
- `scripts/audit_script.py [NNN...] [--dir <dir>] [--fast] [--json]` — 确定性审计（句长/IPA 对齐/
  简体字/拼写疑似/整句与近重复/句式单一度/硬语法）；**会写** `<dir>/_audit/<FOLDER>.json`；
  `word_count`(3–9) 与 `word_cap`(`SLEEP_MAX_LINE_WORDS`) 两个口径分别报。
- `scripts/ipa_vote.py [NNN...] [--all] [--dir <dir>] [--limit N] [--min-votes N] [--json]` — 全库
  「词→IPA」多数表决，列出机器查不出的符号级错标（read-only；`--limit 0` 打全量）。
- `scripts/normalize_zh.py [--apply] [--meta] [--dir <dir>] [NNN...] [--json]` — 确定性繁化
  `dialogue[].zh`（`--meta` 连顶层中文文案；只转换不改写内容）。`--apply` 会重写 script.json。
- `scripts/write_review.py <NNN...> [--round 2] [--score N] [--dir <dir>] [--dry-run] [--json]` —
  把 `_review/<FOLDER>.round<N>.json` 的结论写进 `script.json` 的 `review` 键（唯一改写
  script.json 的工具；只动 `review`；幂等；输入不合法 exit 1 拒写）。
- `scripts/list_used_topics.py [--categories] [--check <file>] [--json]` — 已用主题台账 /
  分类直方图 / `# next index` / 候选清单硬查重（`--check`，read-only）。
- `scripts/generator_prompt.py <NNN...>` — 打印 Step 2 生成派单提示词（读 `_topics_batch.json`，
  本机没有时回退 `references/topics.md` 最新批次段）。
- `scripts/reviewer_prompt.py <NNN...> [--extra …] [--resumed]` / `scripts/verifier_prompt.py
  <NNN...> [--scene …] [--extra …]` — 按磁盘真实文件夹名/主题/行数打印 round1/round2 派单。
- `scripts/assemble_script.py <part_dir> [pairs] [--json]` — 合并 meta + 分块 → script.json 并校验
  （不生成内容；通过后把本篇登记进 manifest）。
- `scripts/build_manifest.py [dir] [--check] [--json]` — 整体重建索引；`--check` 只报盘↔索引
  漂移并 exit 1。

## 这条链路实际怎么产出脚本（分析结论，改管线后先核）

脚本 JSON 有**两条**产出口，本 skill 只负责**第一条**：

1. **人工/本 skill 写作 → 零 LLM 出片。** 写到 `ai_scripts_hot/NNN_Topic/script.json` 并登记
   manifest 后，网页「预生成脚本」下拉 / 脚本库自动入库即可直接跑；出片侧走
   `app/pipeline_service.py:812 _seed_from_ai_scripts`（**跳过 Step 0 的 LLM，也跳过逐行校验**），
   组数按 `len(dialogue)//2` 同步（`:837`），`structure` 被写成当前模式（`:854`）。
2. **LLM 生成（网页「批量脚本」或 CLI 不给预生成脚本时）。**
   `pipeline/sleep/llm_client_sleep.py:268 generate_sleep_script` 分批（默认 50 对/批）产出
   pairs + 元数据，批级校验词长（`_sleep_max_line_words`，默认 7）+ 字段完整性，
   然后 `pipeline/pipeline.py:133 _validate_script`（只查行数下限与 text/zh/phonetic/speaker
   非空）→ 落盘 `<run>/script.json`。
   另一处同源工具是 `.workbuddy/skills/sleep-script-batch/`（WorkBuddy 生态，内置 LLM 或
   内置主题表两条路）——**本 skill 不改它**，但产出必须过同一套门禁。

**质量闸门分布在四处，不要混淆**：
* `app/ai_scripts_admin.py:522 validate_script` —— 管理页体检，本 skill 的 `validate_all.py`
  直接 import 它的 `META_REQUIRED` / `STANDARD_LINES`。
* `app/script_library.py:602 local_checks` —— 脚本库本地检查：结构 + 简体命中表 +
  行词数（`SLEEP_MAX_LINE_WORDS`，默认 7，medium「行太长」）+ 性别/描述一致性。
* `pipeline/pipeline.py:133 _validate_script` —— 运行时结构校验，**只在 LLM 生成重试里调用**。
* `pipeline/sleep/llm_client_sleep.py:17-19 _sleep_max_line_words` —— 生成期硬上限（默认 7，
  clamp [4,10]）。

### 容易踩的漂移点（都已实测确认，改代码时留意；行号会随重构变动，字段名与行为是稳定的）

1. **单句词数四处口径不一**：`audit_script.py` 用 3–9 判 `word_count`（hard，与管理页一致），
   另按 `pipeline.llm_client.resolve_max_line_words("SLEEP_MAX_LINE_WORDS", 7)` 判 `word_cap`
   （review）；`app/script_library.py:626-635` 用同一个 resolve 入口记 medium「行太长」；
   `pipeline/sleep/llm_client_sleep.py:17-19` 是生成期硬上限（默认 7、clamp [4,10]，而
   `resolve_max_line_words` 的 clamp 是 [4,20]）。**写稿按 3–7 走，三处都不命中**。
2. **词数分词器不同**：`audit_script.py` 的正则把数字当词（`\d[\d.,]*`），
   `app/ai_scripts_admin.py:560` 用 `re.split(r"[^A-Za-z']+")` 把数字丢掉——`Flight 3 is late`
   在 skill 记 3 词、在管理页记 2 词。
3. **预生成脚本 seed 路径不逐行校验**：`app/pipeline_service.py:823-829` 只查 `dialogue` 非空；
   `pipeline/pipeline.py:133` 的逐行校验只在生成重试里被调用（`:169`）。空 `text` 会一路走到
   Step 2 TTS 才炸（`pipeline/tts_engine.py:521` `Kokoro produced no audio for:`）——
   只能在写稿/预检阶段拦住（`preflight.py` 标 🔴）。
4. **尾部行静默丢弃**：`pipeline/sleep/audio_sleep.py:349-351`
   `total = min(num_pairs, len(rows_a), len(rows_b))`，配合 `app/pipeline_service.py:837` 的
   组数同步 → >800 行或奇数行的尾部被丢掉，**跑成功但少播**。
5. **输出目录不消歧**：`pipeline/media_utils.py:118-128 safe_filename` 剥 emoji、截断 80 字符，
   `app/pipeline_service.py:846-849` 直接 `mkdir(exist_ok=True)` → 前 80 字符相同的两篇写进
   同一 run 目录。`preflight.py` 有批级查重；基线：20 篇 0 冲突。
6. **行数默认值分家**：`app/ai_scripts.py:26-27 EXPECTED_LINES=800 / STANDARD_LINES=(400,800)`
   vs `app/script_library.py:42 DEFAULT_LINES={"sleep":400}`——预生成库标准 800 行，
   脚本库批量生成默认 400 行。
7. **`ai_scripts/` 实测已空**（其 `manifest.json` count=0），但仍是 `AI_SCRIPTS_DIRS`
   （`app/ai_scripts.py:23`）成员。所有门禁都不得把「另一个库为空」当失败；查重基线靠
   `references/topics.md` 台账，编号起点看磁盘。
8. **`speaker` 值被忽略**：播放与卡片都按行序取 A/B
   （`pipeline/sleep/audio_sleep.py:349-350`；`pipeline/sleep/sleep_cards.py` 无 `speaker` 引用），
   所以 speaker 写错不会中止，但管理页体检报 error。
9. **`structure` 被 seed 路径覆写**：`app/pipeline_service.py:854`，写错同样不中止
   （管理页体检报 error）。
10. **库文档 `review` 字段**：`app/ai_scripts.py _new_doc` / `_apply_content` 现在会把
    `script.json["review"]` 透传进库文档，`stale` 由 `content_hash` 与当前 `dialogue` 比对
    得出（`app/script_library.py _dialogue_hash`，算法必须与
    `scripts/skill_common.content_hash()` 保持一致）。任一边改了算法，「审查过期」就会永远亮或永远不亮。

### 接线自检（项目改动后先跑这几条）

1. **唯一事实源**：必填字段/标准行数 = `app/ai_scripts_admin.META_REQUIRED` +
   `app/ai_scripts.STANDARD_LINES`；行词数上限 = `SLEEP_MAX_LINE_WORDS`（默认 7，
   解析入口 `pipeline.llm_client.resolve_max_line_words`）。改任一处，跑
   `check_gates.py` 看 `validate_all.py` 的第一行 `# rules: … from app/local fallback`。
2. **目录与命名不变量**：APP 仍只扫 `^\d+_` 且含 `script.json` 的目录（
   `app/ai_scripts.py:52`），`_`/`.` 前缀仍被跳过；`scripts/*.py` 的项目根仍用标记法
   （`app/ai_scripts.py` + `pipeline/pipeline.py`）——把脚本拷出仓库会回退 `parents[4]`。
3. **空库不算失败**：`ai_scripts/` 为空时 `check_gates.py` 仍应 exit 0/2（不得因缺库报 1）。
4. **门禁基线**：见下表；数字变多说明新写/改写的稿没达到标准，不要直接改门槛。
5. **行数上限基线**：`preflight.py` 对 800 行脚本必须 0 blocker；`>800` 或奇数行必须报出来。
6. **review 透传链路**：`write_review.py <NNN>` → `script.json["review"]` →
   `sync_library()` → 库文档 `review.score` → 页面显示分数；改完 `dialogue` 后应显示
   「审查过期」（`content_hash` 变了）。

### 基线（2026-10-03 实测）

库现状：`ai_scripts_hot/` **43 篇** —— 001–020 是 800 行/400 对老批次（无 `review` 块，按祖父条款），
**021–043 是 400 行/200 对小批三批，全部 APPROVED**（`list_used_topics.py` 末行 `# next index` = 44）；
`ai_scripts/` 仍为空（manifest count=0）。混合行数是允许的（`STANDARD_LINES=(400,800)`），
但门禁的 `--lines` 一次只能对准一个批次目标，所以跑门禁时要指明本批规模。

| 项 | 实测值 | 说明 |
|---|---|---|
| `validate_all.py --lines 400 --console` | 43 folders，**0 error**，20 warning，manifest drift=no | warning 全部是 001–020「不是本批目标的 400 行」 |
| `build_manifest.py --check`（hot / main） | 两边都 **CLEAN** | `ai_scripts/` manifest count=0（库已空，属现状） |
| `audit_script.py --fast` | **hard=0**；review 信号 5127 | 021–043 全部 ≤7 词、无长行；`word_cap` 只剩 001–020 存量 |
| `normalize_zh.py`（dry） | 0 行待繁化 | 每批收尾都要 `--apply <本批编号>`（034–043 共 16 行） |
| `ipa_vote.py --all` | 634 行 SUSPECT，**其中 0 行属 021–043** | 语料每加一批都会让多数票漂移；新稿一律 0，旧篇历史偏差不追改 |
| `preflight.py` | 43/43 篇 **0 blocker**，121 条降级提示 | 提示集中在 001–020 存量 |
| `check_gates.py --lines 400` | exit 2，gates `{1:2, 2:0, 3:2, 4:0, 5:2, 6:2}` | 2 号（manifest）与 4 号（繁化）全 0；其余是既有 warning，非 blocker |

**021–043 的审查结果（`review` 已写回 script.json，全部 APPROVED 且 `content_hash` 与最终文本一致）**

| 编号 | 主题 | score | 编号 | 主题 | score |
|---|---|---|---|---|---|
| 021 | Berry Picking at a Farm | 95 | 033 | At the Dog Groomer | 91 |
| 022 | At the Convenience Store | 93 | 034 | Asking the Cabin Crew | 88 |
| 023 | Adopting a Shelter Dog | 90 | 035 | A First Driving Lesson | 88 |
| 024 | Asking the Landlord for Repairs | 94 | 036 | Asking for a Pay Rise | 92 |
| 025 | A Piano Lesson | 92 | 037 | When the Power Goes Out | 92 |
| 026 | Visiting a Sick Friend | 91 | 038 | At the Vending Machine | 84 |
| 027 | Asking About Allergies | 92 | 039 | Rehearsing in a Community Choir | 85 |
| 028 | Using a Fitness Tracker | 92 | 040 | A Physiotherapy Session | 90 |
| 029 | Moving into a Dorm Room | 95 | 041 | Planning Weekly Meals | 92 |
| 030 | Taking the Car to the Mechanic | 94 | 042 | A Parent-and-Baby Group | 91 |
| 031 | Booking a Sleep-Friendly Room | 90 | 043 | Choosing a Streaming Plan | 90 |
| 032 | Collecting a Missed Parcel | 91 | | （021–043 平均 91.0） | |

**段落级「母题重复」这一轮（新增 `motif_scan.py` 之后）**：审计只能抓*整句*重复与 4-gram 近重复，
抓不到「同一件事换个说法讲 11 遍」。新增 `scripts/motif_scan.py` 后实测：

| 项 | 数值 | 说明 |
|---|---|---|
| 库内基线 | 001–020 老批次 **10 段被标记** | 007 pot×26 / 008 oil×25 / 017 gel×29 / 013 warm×30 属常态 → **主题名词高频是格式固有**，阈值定在「一个 50 对段里 ≥25 次」才算极端 |
| 修前（021–043） | 043 plan×45（全库第一）· 031 room×21 + 段2 Yes 84% · 025 段3/4 Yes 68%/60% · 034 seat×18 · 036 quarter×14 · 038 machine×18 | 这些都是「换个说法也还是同一个词」的段 |
| 修后（021–043） | **0 段被标记**（只剩 83 条 `·` 提示） | 做法：同义替换 + 换提问角度；Yes/No 起句改直述（全库中位数 24%，超 50% 才算节拍单一） |

**教训**：`motif_scan` 报「⚠ 与上段重叠/Jaccard 高」时先看是不是**两段确实在讲同一件事**；
`hot` 词频高但主题就是那个词（vending machine / streaming plan）时，**只有到 25+ 才值得改**，
否则改完只是把同一个词换成它的同义词，听感并没有变。

**「已知遗留再优化一轮」的可复用做法（034–043 实测）**：先用工具量出**可验证项**，再动 agent；
只有量不到的（内容单调、设定矛盾）才交窄任务 agent。这一轮的结果：

| 指标 | 优化前 | 优化后 | 做法 |
|---|---|---|---|
| 超 7 词的行（`word_cap`） | 1 行（040 idx 81） | **0 行** | 逐行改短，改完用 `audit_script.py` 复核 |
| 机器可见的 `near_dup` | 17 处 | **0 处** | 逐条改写共用 3/4–4/4 个 4-gram 的套语；中途一度变成 3（改写把撞点搬到别的行），**再跑一轮才清零** |
| 开头词峰值 | 038 can=19、043 which=19 | 全部 **≤19** | 已达标就不用动（阈值 20） |
| 验收员具名的小瑕 | 每篇 3–8 条 | 逐条改掉（约 60 行） | 按 `_review/*.verdict.json` 的 `issues` 行号直接改；改完**重新出视图再验** |
| 分数 | 85–94 | **85–92，平均 89.2** | 每篇重新独立验收并 `write_review` 重写回 |

这一轮固化下来的两条纪律：
1. **改稿与出视图必须成对做**（重跑 `make_vbrief.py`），否则验收员照着旧视图判「修改未生效」。
2. **改写会搬运撞点**：`near_dup` 清零后必再跑一次 `measure.py`；补丁的 `index`/`side` 要逐条核对
   （`apply_patch.py` 的「1 基自动校正」曾把 `index=159 side=a` 移到 158，连带产生答非所问；补一个冠词后行也可能涨到 8 词，要再压回）。

**034–043 这批的实测经验（与上一批的差别）**

| 项 | 数值 | 说明 |
|---|---|---|
| 生成成功率 | 约 50%（2 分块/agent） | 50 对/agent 仍有约一半零产出；**1 个 slot = 2 个分块 = 恰好一整段**（第 i 段 → `pairs_(2i-1)`/`pairs_(2i)`），别把 slot 和「段的一半」搞混 |
| 装配失败原因 | 11 次 | 全是 `cross_dup`（跨块重复句）与分块少 1–2 对；前者改「序号靠后」的那一句，后者让 agent **读出来再整份写回**补足 25 |
| 机器可验证的三项遗留 | 长行 47 → **0**、near_dup 8 → **0**、首词峰值 → **≤19** | 用 `word_cap` / `near_dup` / 首词直方图量出来，再定点改；改完必须重跑 `audit_script.py` |
| 内容层 error（验收员点名的） | 每篇 0–3 处 | 典型：同一篇两个半段设定不同（040 前半天「右肩」后半天「下背」）、宝宝性别/归属混用（042）、同一箱内容前后不一（032 上一批）、`Yes/No` 应答错配（038/043） |

**本轮踩到的新坑（已固化）**

1. **派单前必须重新生成文本视图**：我先改了 `script.json`、却忘了跑 `make_vbrief.py`，
   结果验收员照着旧视图判「修改未生效」，白白来回两轮。**改稿与出视图要成对做**。
2. **`apply_patch.py` 的「1 基自动校正」会误伤**：我写 `index=60, side=b` 而该行其实是 char_a，
   它按 side 反推成 59 并把**上一行**改掉（连带产生答非所问）。**补丁的 index/side 必须逐条核对**。
3. **并发写同一个分块文件会互相覆盖**：补块 agent 与生成 agent 同时写 `pairs_06.json` 时，
   两者都在改同一文件（一个追加、一个整份重写）。**同一篇的分块同一时刻只允许一个写入者**。
4. **`score` 口径要写在派单里**：有验收员自创「扣分制（0 = 满分）」，把 87 分写成 0 分。
   派单必须写明「score 是 0–100、越高越好，四维各 0–25 且合计 = score」。
