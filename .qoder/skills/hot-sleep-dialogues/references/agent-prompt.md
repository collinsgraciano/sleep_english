# 单主题生成 Agent 提示词模板

**本文件是 Step 2 派单的单一来源**：`scripts/generator_prompt.py <NNN...>` 直接读下面
````text 代码块、按 `_topics_batch.json`（本机没有时回退台账）填占位符后打印 ——
要改派单内容就改这里，别去改脚本里的字符串。

占位符：`{WORKDIR}`=项目根绝对路径、`{FOLDER}`=真实文件夹名、`{NUM}`=编号、
`{EN}`/`{ZH}`/`{CATEGORY}`/`{CEFR}`=主题三元组与评级、`{PAIRS}`=组数（默认 400）、
`{LINES}`=行数=组数×2（默认 800）、`{N_CHUNKS}`=`PAIRS/50`（默认 8）、
`{EIGHT_BEATS}`=台账「八段大纲关键词」列、`{SAMPLE}`=磁盘上任一既有 script.json 路径
（提示只读开头约 30 行；库里还没有脚本时是「本批第一篇」的说明）。

````text
你要为「睡前英文听力」视频生成一个完整的 {LINES} 行情境对话脚本（{PAIRS} 对，全部内容由你真实撰写，禁止模板拼句、禁止从其他文件复制句子、禁止缩写/省略/占位）。

主题：{EN}（{ZH}）｜分类：{CATEGORY}｜编号 {NUM}｜CEFR {CEFR}
输出目录：{WORKDIR}/ai_scripts_hot/{FOLDER}/
先读格式规范：{WORKDIR}/.qoder/skills/hot-sleep-dialogues/references/script-format.md
写作方法（分段骨架 / 成对逻辑 / A2 语言 / IPA / 繁体）：{WORKDIR}/.qoder/skills/hot-sleep-dialogues/references/craft.md
可对照真实样例：{SAMPLE}

分段大纲（选题调研时定的推进节点，共 {N_CHUNKS} 段 × 50 对；第 k 段 = pairs_0k.json；照它写，别自己换主线）：
{EIGHT_BEATS}

步骤：
1. 创建 {WORKDIR}/ai_scripts_hot/_parts/{FOLDER}/，写入 {N_CHUNKS} 个 pairs 分块 + 1 个 meta.json：
   - pairs_01.json ~ pairs_{N_CHUNKS:02d}.json：每个是 JSON 数组，含 50 个对象 {{"a":{{"text","phonetic","zh"}},"b":{{...}}}}；
   - **一个 chunk 一次 Write，写完立刻写下一个，中途不要回读已写文件**（{LINES} 行的量最容易死在回头重读上）；
   - meta.json：script.json 除 dialogue 外的全部顶层字段（含 youtube_tags 数组），行数相关文案按 {LINES} 写（如「{LINES}句循環聽」）；中文一律台湾正体。
2. 内容要求：
   - 第 k 个 chunk = 大纲第 k 段，段内按时间/事件顺序推进，段与段自然衔接；**中段最容易塌成寒暄，必须有事发生**（8 段时是第 4–6 段，4 段时是第 2–3 段）；
   - 每对 a/b 逻辑紧耦合（问-答、请求-回应、发起-接话），b 不得答非所问，也不许只回语气词没有实词；
   - 句子 **3–7 词**（硬上限 9 词；>7 词会被脚本库体检记「行太长」、审计记 word_cap）、**{CEFR} 口语用词**（高频具体词，抽象词/生僻词不用）；
   - phonetic 为美式 IPA、`/.../` 包裹、token 数与 text 词数严格一一对应（弱读的 a/the/of 各给一个 token，也不许把两个词并成一个 token，如 "so much" 不能写成 /smʌtʃ/）；zh 为自然繁体中文翻译（不加「」）；
   - {PAIRS} 对两两不重复，也不得与全库已有句子撞车；**同一个首词**全篇不超过 {PAIRS}/10 次（审计按单个首词统计，The/Can/Is 这类最容易写超，多用场景名词或副词起句）；
   - 台词里不许出现舞台提示（`Ten minutes later…`、`(laughing)`、画面说明）；金额/数量/时间在 text 与 zh 里必须一致且全篇自洽。
3. python {WORKDIR}/.qoder/skills/hot-sleep-dialogues/scripts/assemble_script.py {WORKDIR}/ai_scripts_hot/_parts/{FOLDER} {PAIRS}   # 报错就改分块重跑，直到打印 OK
4. PYTHONIOENCODING=utf-8 python {WORKDIR}/.qoder/skills/hot-sleep-dialogues/scripts/audit_script.py {NUM} --fast   # 只修 cross_dup / word_count / ipa_shape / zh_simplified；改分块后重跑第 3 步。其余缺陷留给后续 reviewer，别扩大范围，也别改别的脚本
5. 回复报：一句话结果 + script.json 路径 + 每个 chunk 的对数 + 剩余 hard 数。临时文件只放 H:/tmp，项目根与 ai_scripts_hot/ 里不得留下 _tmp*。
````

## 说明

- `{CEFR}` 默认 `A2`（本批评级统一 A2，用户另行指定才改）；
- `{N_CHUNKS}` = `PAIRS / 50`（400 对 → 8 个 chunk）；每 chunk 恰好 50 对；
- `{EIGHT_BEATS}` 用选题台账「八段大纲关键词」列的内容（`→` 分隔的 8 个小场景名）；
  台账没有该列时，让 agent 自己先列 8 段大纲再动笔，并在回复里报出这 8 段；
- `{SAMPLE}` 由 `generator_prompt.py` 从磁盘挑：整份 800 行 script.json ≈ 4 万 token，
  所以派单里注明**只读开头约 30 行**看结构，别整档 Read（那等于开跑前先烧掉一半上下文）；
- 一次 spawn 一个主题；**800 行的工作量约是 400 行的两倍，每批并行数压到 ≤5**，
  一批完成再开下一批。

## 写不完怎么办（800 行实测最常见的失败）

单个 agent 写到 5–6 个 chunk 时常开始偷工：句子变短变空、后面几段脱离大纲、或干脆
少写一个 chunk。处理办法：

1. 主 agent 用 `assemble_script.py ... {PAIRS}` 的行数校验判断是否真的写满；
2. 缺 chunk / 后段质量崩 → spawn **续写 agent**：prompt 里给它 meta.json 路径、
   已写 chunk 的清单、大纲中它负责的段号，要求「先读最后一个 chunk 的最后 10 对
   （用 `python -c` 打印，别整档 Read）再接着写剩下的 chunk」；
3. 续写 agent 只写自己那几段，写完自己跑 assemble 校验；
4. 若同一篇补了两次仍不合格，删掉 `_parts/<FOLDER>` 重开一个新 agent，别反复打补丁。
