# Review Rubric — 评审判据（4 维 × 25 分）

**角色提示词在 `references/reviewer-prompt.md`**（审计 / reviewer / verifier 三个模板 +
批量编排经验）。**判据在本文件**。两份文件分工固定：模板管「谁、什么顺序、写哪个文件」，
本文件管「什么算 error、什么算 warning、扣在哪里」——修改任何一边都要同步看另一边，
否则会出现「提示词说这样改、评分标准说这不算问题」的分家。

## 评审独立性

- **评审者不得是撰写者**：round1（reviewer）与 round2（verifier）必须是不同 agent，
  verifier 之前没参与撰写；批量时**先把整批写完，再派独立评审**，不要写完一篇立刻自审。
- 评审者可以就地改内容（改 `text` 必须同步重写该行 `phonetic` 与 `zh`），但**禁止改别的脚本、
  禁止放宽门禁、禁止从模板填字**。派单用 `reviewer_prompt.py <NNN> [--extra …] [--resumed]`
  与 `verifier_prompt.py <NNN> [--scene …]` 现生成——编号→文件夹映射以 `manifest.json` 为准。

## Loop policy（每篇脚本）

1. **最少 2 轮**：round1 修订 + round2 独立验收；round1 全干净也要有 round2 确认。
2. **停机条件**：连续 **2 轮 fully clean**（0 error、0 warning，且机器审计 hard=0）→ 停止。
3. **硬上限 4 轮**：到第 4 轮仍有 surviving warning 的，**必须逐条具名写出保留理由**
   （哪一行、为什么这不是缺陷），不能只写「已保留」。
4. 修复＝**内容重写**（改那几行的 `text`/`phonetic`/`zh`）；禁止放宽门禁阈值、禁止从模板/
   别的脚本填字、禁止为了过审计而删内容。每轮改完**自己重跑**
   `python .qoder/skills/hot-sleep-dialogues/scripts/audit_script.py <NNN>` 到 hard=0——
   `near_dup`/`cross_dup` 是拿全库比出来的，别的脚本一改旧报告就过期，「改完再跑一次审计」
   是硬性收尾。

## 四维判据（每维 25 分，共 100）

| 维度 | error 条件 | warning 条件 | 25 分 vs 20 分 |
|---|---|---|---|
| **`pair_logic`** 成对逻辑与情境推进 | b 答非所问 / b 只回语气词没有实词 / 一对内两句可以互换位置 / 连续多对原地打转 / 段与段断裂或顺序跳跃 / 金额·时间·数量前后矛盾 / 结尾没收束 | 需要补一句过桥才接得上 / 中段（4–6 段）偏平但成立 / 意外只是提了一句、没改变走向 | 25：每对严丝合缝、推进清晰、中段有实事、有真意外且结尾落地；20：逻辑都对但推进平庸，靠一两处过桥句补救，中段有一段靠寒暄撑 |
| **`idiomatic_a2`** 口语地道度 + A2 用词 + 繁体译文自然度 | 语法/搭配硬错 / 超纲抽象词且无上下文支撑 / 书面腔·翻译腔 / 简体或异体字形 / 译文夹英文（人名品牌除外）/ 词数超出 3–9 | 词数 8–9，或 > `SLEEP_MAX_LINE_WORDS`（默认 7，审计 `word_cap`、脚本库 `line_too_long`）——新稿目标 3–7 / 搭配略生硬但不算错 / 译文语气与英文不一致 / 缺缩写与口语标记 | 25：母语者真会这么说，全篇 A2 具体词，译文像中文人话；20：无错但不地道，若干句偏课本腔，译文只做到「意思对」 |
| **`ipa_accuracy`** 美式 IPA 逐词准确 | 与 `text` 缺词/多词（token 不对齐）/ 未用 `/.../` 闭合 / 混入非 IPA 字符 / 明显符号级错标（漏 `ˈ`、用英式 `/ɒ/`、漏尾音） | 写法变体未统一（长度符、增删 `ə`）/ 弱读虚词标法不一致 / 重音位置可两读 | 25：逐词对齐、符号级错标 0、全篇记法统一；20：对齐无误但存在零星符号级错标或记法不统一 |
| **`variety_no_dup`** 句长 / 整句与近重复 / 首词单一度 | 篇内整句重复 / 与全库整句撞车 / 近重复（与别篇共用 ≥3 个 4-gram 且占比 ≥0.6）/ 行数为奇数或 >800（尾部会被静默丢弃） | 同一首词接近 40 次上限 / 连续多对同一句型 / 零星 b 句是空心反应（`flat_b`） | 25：零重复、首词分散、陈述/疑问/祈使/感叹混用；20：无重复但能感觉到模板感，首词集中在少数几个 |

> 分数用于横向排序与留痕，**不单独决定 verdict**：只有四维都不存在 error 级问题、且遗留
> warning 已逐条具名合理化，才可以写 `APPROVED`；否则写 `NEEDS_ANOTHER_ROUND`。

## issue 单行格式

```
[error|warn] <dimension> @ <line|field>: <what's wrong> → <suggested fix>
[error] pair_logic @ 15: b 答的是营业时间，没接住 "How much is the rent?" → 直接给租金并说明是否含水电
[error] ipa_accuracy @ 301: phonetic 少了 month 的 token → 补 /mʌnθ/
```

## What NOT to flag

- **刻意的短回答**：`Sure.` / `Got it.` / `Right.` 出现在**正确的语境**里是自然口语，
  单处、上下文接得上就不算缺陷（错的是**连续连用**、或整段 b 句都没有实词）。
- **已有的零星「」写法**：`dialogue[].zh` 不加「」是**新增内容的规范**；`「」` 本身
  Big5-clean、不算缺陷，**已有脚本里零星几处不必为此返工**。
- **既有稿件的 IPA 偏差不扩大范围**：只处理本次评审这一篇；顺手「全库统一」会覆盖同期
  在跑的别的脚本，实测出现过。
- **`咔哐噼咣擀哒` 不是简体**：拟声/台派口语写法在白名单里，和繁体判定同一套集合。
- **`只/隻`、`游`、`念` 的两种写法都不算缺陷**：繁化工具刻意屏蔽这三个字
  （`只能/只有` 被错提升成 `隻能`、`游泳→遊泳`、`紀念→紀唸` 都是改坏），保留手写形态即可。
- **风格选择**：句长在 3–7 之内的长短、口语标记的具体用词、段落小场景的取舍，都不是缺陷。
- **不要把 `char_a`/`char_b` 当固定剧情角色**：本格式两把声音是**朗读声部（男声/女声）**，不是人物
  （见 `script-format.md`）。「两位朋友一起逛、偶尔与柜台店员对话」是正常读法；
  **同一把声音在相邻若干对之间短暂换成店员/顾客口吻，不算缺陷**。
  只有一种情况要报：**同一把声音在前一对里扮演店员，紧接着又把店员当第三人称谈论**
  （例如 a 说 "Your total is three hundred."，隔两行 b 说 "Just ask the clerk"）——
  那是听感上真卡住的自相矛盾，点名行号并建议中性化表述。
- 已经在本轮报告里具名合理化的审计 `review` 类信号，不重复记 issue。

## 报告形状与写回

**round1**（`ai_scripts_hot/_review/<FOLDER>.round1.json`，修订者写）：

```json
{"folder": "<FOLDER>", "round": 1, "pairs_edited": 0, "lines_edited": 0,
 "story_arc": "一句话概括这个故事从头到尾发生了什么",
 "fixed": [{"index": 12, "why": "答非所问", "before": "...", "after": "..."}],
 "kept": [["near_dup", "line 260 是主题必要说法，保留"]],
 "audit_hard_after": 0}
```

**round2**（独立验收者写；`score` / `dimensions` / `summary_zh` 是 `write_review.py`
的**必填输入**，缺了会被拒）：

```json
{"folder": "<FOLDER>", "round": 2, "verdict": "APPROVED",
 "score": 92,
 "dimensions": {"pair_logic": 24, "idiomatic_a2": 23, "ipa_accuracy": 22, "variety_no_dup": 23},
 "issues": [{"dim": "pair_logic", "line": 15, "note": "..."}],
 "summary_zh": "一到两句结论（繁体）",
 "lines_edited": 0, "audit_hard_after": 0,
 "worst_3": [["index", "还剩什么问题"]],
 "story_check": "结尾是否收束、整体是否可听"}
```

`verdict` 只能是 `APPROVED` 或 `NEEDS_ANOTHER_ROUND`。

**写回（唯一允许改 `script.json` 的入口）**：

```
python .qoder/skills/hot-sleep-dialogues/scripts/write_review.py <NNN> [--round 2] [--score N] [--dir …] [--dry-run] [--json]
```

- 它**只**新增/覆盖 `script.json` 的 `review` 键，`content_hash` 由它自己算，不要手写
  （算法 = `sha1(json.dumps(dialogue, ensure_ascii=False, sort_keys=True))`，与
  `app/script_library._dialogue_hash` 同构；app 侧据此决定「审查过期」）；
- 读 `_review/<FOLDER>.round2.json`，要求 `verdict ∈ {APPROVED, NEEDS_ANOTHER_ROUND}`
  且 `score` 是 `[0,100]` 的整数；输入不合法 **exit 1 拒绝写入**。
  **存量 round1/round2 报告只有 `verdict`、没有 `score`**（200 行规模的旧流程产物），
  所以它们写不进去、控制台仍显示「未审查」——这是**已知现状**：要么补一轮带分数的评审，
  要么显式给分 `write_review.py <NNN> --score 88`（会写 `score_source: "cli"` 留痕）；
- 幂等：内容一致时返回 `unchanged`；
- 写入 `script.json` 的 `review` 块形状**逐字如下**：

```json
{"round": 2, "score": 92, "verdict": "APPROVED", "score_source": "report",
 "dimensions": {"pair_logic": 24, "idiomatic_a2": 23, "ipa_accuracy": 22, "variety_no_dup": 23},
 "issues": [{"dim": "pair_logic", "line": 15, "note": "..."}],
 "summary_zh": "...", "model": "skill:hot-sleep-dialogues",
 "reviewed_at": 1780000000, "content_hash": "sha1:<dialogue 的 sha1>"}
```

写完必须跑一次 `write_review.py <NNN>`，**它只能串行**（一次一个编号，别并发）——
`normalize_zh.py --apply` 会整档重写 `script.json`，两者对同一编号必须排队。

## 批量报告格式（每篇一行）

```
<NNN_Folder>: rounds=R score=S verdict=V 改动 N 处 | 最大一处：<一句话> | audit hard=0
```
