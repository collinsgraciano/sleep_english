# 联网选题调研 Playbook（Step 1）

目标：用**真实搜索证据**选出华人英语学习者会主动去搜、且睡前愿意反复听的场景主题，
而不是凭印象编清单。产出：一批 N 个（默认 20）新主题 + 每个主题的八段大纲可行性判定，
编号从台账最大编号 +1 起。

铁律：
- 调研只产出**主题名**（英文主题 + 中文名 + 分类 + 证据 + 打分），不产出对话内容；
- 每个候选必须能说出「在哪看到有人要」（来源 + 链接/标题），说不出来源的候选丢掉；
- 与 `references/topics.md` 台账（现有 200 题）**语义**不重复——换措辞的同场景算重复。

## 编排

主 agent 并行 spawn 4 个只读调研 agent（每类来源一个），每个交回 25–40 条候选；
主 agent 合并、按下面的 rubric 打分，选出本批 N 题。调研 agent 的 prompt 用本文末模板，
把 `{SOURCE}` 换成对应来源段即可。子 agent 只做 WebSearch/WebFetch + 汇总，不写文件到
`ai_scripts_hot/`。

## 四类来源与检索式

### A. 华人学习者主动搜索的场景词（需求侧，优先级最高）
看他们**在搜索引擎/社区里问什么**：
- `睡前英文听力 主题`、`睡前 英语 听力 循环 场景`、`睡觉听英语 有用吗`
- `{场景} 用英语怎么说`（拿知乎/小红书/B站/抖音搜索页里出现的场景词去搜，逐个验证）
- `出国 英语 尴尬 不会说`、`移民 生活英语 场景`、`雅思口语 part1 话题 汇总`
- `site:zhihu.com 英语口语 场景 常用句子`、`site:xiaohongshu.com 英语 场景 口语`
- B 站/YouTube 中文区「睡前英语」播放列表里的高播放标题（标题里的场景＝被验证过的需求）

### B. 同类英文学习频道（供给侧爆款对标）
看同类频道把什么做成了系列、哪些标题带播放量优势：
- `learn english while you sleep youtube`、`sleep english listening phrases`
- `english podcast for beginners before bed`、`everyday english conversations slow`
- 进结果里 2–3 个频道的视频页，记下**高播放视频的主题词**与标题公式（句数/CEFR/emoji 用法）
- 顺手记录他们**没做**的细分场景＝差异化机会

### C. 趋势与长尾词（热度的第三方证据）
- `google trends "learn english" 2026 rising queries`、`most searched english phrases`
- `ESL topics trending reddit`、`r/EnglishLearning how to say {场景}`（高频提问＝高频需求）
- italki/British Council/VOA Learning English 的「everyday english」栏目目录
- 季节性/时事热点（如新政策、大型赛事、流行应用）带来的新场景需求

### D. 教材与功能清单（可展开性与 CEFR 校准）
- `CEFR A2 can do statements list`（British Council / Cambridge 官方 descriptor）
- `{教材名} table of contents functional english`（Survival English、English File A2 units）
- `IELTS speaking part 1 topic list`、`travel english phrasebook topics`
- 用途：确认主题在 **A2 能用词范围内**、并且能拆出足够多的子场景（撑得起 400 对）

## 打分（每项 0–5，加权总分取前 N）

| 维度 | 权重 | 判据 |
|---|---|---|
| 需求热度 | ×2 | A/B/C 里出现的次数与近期性；同一场景被多来源命中分高 |
| 睡前友好 | ×1.5 | 日常、低压、可预测；吵架/急诊/纠纷类压低分（听着清醒＝不合格） |
| 可展开性 | ×2 | 能否拆出 **8 段 × 50 对**（见下）；拆不满 8 段直接淘汰 |
| 差异化 | ×1.5 | 与 200 题台账的语义距离；撞车即淘汰 |
| A2 密度 | ×1 | 具体名物＋高频动作多、抽象词少 |
| 点击潜力 | ×1 | 能写出「實用{中文}必備常用英文短句」这种带钩子的标题 |

**可展开性判定（800 行专属、必须做）**：对每个候选写下 8 个小场景名（每段 50 对的推进
节点，例：`Renewing a Passport` → ①翻资料确认要带什么 ②整理文件袋 ③路上堵车问路
④排队取号 ⑤窗口核对身份 ⑥拍照与指纹 ⑦缴费与选邮寄 ⑧拿到回执约时间聊旅行计划）。
写不满 8 个、或后三段只能靠灌水寒暄凑＝淘汰，换更宽的场景。

## 查重

1. 机械查重（硬）：候选写成 JSON 数组（`[{"en","zh","category","folder"}]`）后跑
   `python .qoder/skills/hot-sleep-dialogues/scripts/list_used_topics.py --check <file>`
   ＝硬撞（英文去词序后全同／中文全同／批内自撞）必须换题；exit `1` 不放行，
   exit `2` = 只有近义提示（Jaccard ≥ 0.6，需人工判断，不算失败），exit `0` 干净。
   基线除两个脚本库外**还包含 `ai_scripts_hot/_topics_batch.json` 里已定稿、还没生成的主题**
   （记为 `planned/`）与 `references/topics.md` 台账——同时跑两批时靠这条互相看不见对方选题；
   带上 `folder` 字段可让批次文件自查时不把自己的行撞回去。
2. 语义查重（软，逐条人工判）：`Ordering Coffee` vs `Buying Coffee to Go`、
   `At the Bank` vs `Opening a Bank Account` 都算重复。判据＝**听众会觉得「这不是听过了吗」**。
3. 分类配额：先看 `list_used_topics.py --categories` 直方图，本批每类不超过总题数的 25%，
   分类名沿用 `topics.md` 里那套粗分类（Food & Drink / Tech / Shopping / Daily Life /
   Driving / City Life / Travel / Outdoors / Leisure / Sightseeing / Health / Work / Study /
   Social / Family），不要新造细分类。

> **文件数 ≠ 覆盖数**：`ai_scripts/` 与 `ai_scripts_hot/` 的目录会被整体清空重编
> （2026-09-28 就重编过一次，当前 `ai_scripts/` 实测为空、manifest count=0），
> 但**台账 `references/topics.md` 里的历史主题永远算「已用」**。所以查重基线是
> 「台账 ∪ 两库 ∪ 本批清单」，编号起点才看磁盘（`# next index`）。空库不是「库没了就能随便写」。

## 产出格式

写进 `references/topics.md` 的新批次段（照抄表格列，编号从 `list_used_topics.py` 末尾的
`# next index` 连续分配 —— 起点看磁盘脚本目录，不看台账，台账只作排重基线）：

```
## 批次 {YYYY-MM}（{N} 题，800 行 / 400 对 / A2）

| 编号 | 英文主题 | 中文 | 分类 | 文件夹名 | 需求来源（2 条） | 八段大纲关键词 |
```

「需求来源」列填检索到的具体出处（站点 + 标题，或链接），「八段大纲关键词」填 8 个小场景名
用 `→` 连接——生成 agent 会照着它排 8 个 chunk，避免 400 对写散。
同时在 `ai_scripts_hot/_topics_batch.json` 落一份机器可读副本（供 `--check` 复核与断点续跑）。

## 调研 agent prompt 模板

````text
你是选题调研员。只用联网搜索（WebSearch / WebFetch）为本项目的「睡前英文情境对话」系列
找新主题，不写任何对话内容，不改任何文件（除 {OUTFILE}）。

项目背景：给华人英语学习者做的睡前听力视频，一个主题 = 800 行（400 对）日常短句对话，
CEFR A2，男声常速→女声慢速→连读的循环结构。已有的 200 个主题见
{WORKDIR}/.qoder/skills/hot-sleep-dialogues/references/topics.md（先通读，别再提相同场景）。

你负责来源：{SOURCE}
按 {WORKDIR}/.qoder/skills/hot-sleep-dialogues/references/topic-research.md 里该来源的
检索式实际去搜（至少 12 次不同关键词的搜索，中英文都试），把结果整理成 25–40 条候选。

每条候选必须给：
1. en（英文主题名，3–5 词，动名词/介词短语优先，不与台账重名）
2. zh（繁体中文名，6 字内）
3. category（沿用 topics.md 的 15 个粗分类之一）
4. evidence（2 条具体出处：站点 + 标题/链接 + 你看到的热度信号，一句话）
5. eight_beats（8 个小场景名，用 → 连接；写不满 8 个就别提这条）
6. sleep_fit（0–5，睡前听着会不会清醒）＋ heat（0–5，需求强度），各附一句理由

不要为了凑数放宽标准：与台账语义重复、拆不出 8 段、需要大词（C1 词汇）的主题一律丢掉。
最后用 Write 把 JSON 数组写到 {OUTFILE}（字段名用上面 6 个，eight_beats 用字符串），
回复里只报：条数、来源里最意外的 3 个发现、以及你认为最该做的 5 题及原因。
````

主 agent 合并 4 份 JSON：先 `--check` 硬查重 → 再去语义重复 → 按加权总分取前 N →
不足 N 则补搜一轮来源 A。定稿后把表格 append 到 `topics.md`，并落 `_topics_batch.json`。
