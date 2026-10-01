# script.json 完整 schema（pipeline Step 0 产物格式）

真实样例：`ai_scripts_hot/` 里任意一篇已装配好的 `script.json`（库被清空重编的第一批没有样例，以本文为准）。

## 顶层字段（全部必填，除注明外）

```jsonc
{
  "title": "Everyday Phrases — <Topic>",
  "title_zh": "<主题中文名>",
  "scene_zh": "<主题中文名> · 情境對話",          // 繁体
  "title_quote": "<第 1 对 a 句原文>",
  "cefr": "A2",                                   // 本批统一 A2（旧批次按主题 A1/A2/B1）
  "youtube_title": "【睡前英文聽力】<emoji> 實用<主题中文>必備常用英文短句 ｜ 不用背！睡覺聽就會 ｜ 🌱<cefr> ｜ 💡800句循環聽 ｜ 零基礎也能開口說",
  "youtube_title_en": "800 Everyday English Phrases While You Sleep — <Topic>",
  "youtube_description": "睡著學英文！這部影片收錄實用<主题中文>情境對話（每兩句成一組：男聲常速 ➡ 女聲慢速 ➡ 男女連貫）… #英文聽力 #睡前英文 #LearnEnglish",
  "youtube_description_en": "Learn English while you sleep! This video features practical <Topic> phrase pairs (each pair: male normal speed → female slow speed → combined dialogue). …",
  "thumb_badge": "不用背！",
  "thumb_main": "睡覺聽",
  "thumb_hook": "聽久自然開口說",
  "lesson_type": "listening",
  "structure": "sleep",
  "topic": "<Topic 英文原名>",
  "char_a_description": "male narrator voice",
  "char_b_description": "female narrator voice",
  "char_a_gender": "male",
  "char_b_gender": "female",
  "char_a_role": "narrator (male voice)",
  "char_b_role": "narrator (female voice)",
  "welcome_en": "", "welcome_zh": "", "story_hook": "", "intro_zh": "",
  "outro": "", "outro_zh": "", "practice_intro_en": "", "practice_intro_zh": "",
  "scene": "<Topic>",
  "thumbnail_expression": "", "thumbnail_action": "",
  "thumbnail_subtitle": "800句睡前英文",
  "thumbnail_icons": [],
  "channel_id": "",
  "category": "<分类，见 topics.md>",
  "youtube_tags": [ /* 12-16 个，中英混合：主题中文+英文、英文聽力、睡前英文、Sleep learning、<cefr> English、ESL、English conversation 等 */ ],
  "dialogue": [ /* 800 行，见下 */ ]
}
```

## dialogue（800 行，= 400 对 × 2 行）

```json
{ "speaker": "char_a", "text": "Can I get a large latte?",
  "phonetic": "/kæn aɪ ˈɡɛt ə ˈlɑrɡə ˈlɑteɪ?/", "zh": "來一杯大杯拿鐵好嗎？" }
```

- 行序严格交替：奇数行 `char_a`、偶数行 `char_b`（装配脚本会自动写入 speaker，
  agent 只需在 pairs 分块里提供 a/b 两句）；
- `text`：3–9 词口语短句，**A2 词汇**（高频具体词，不上抽象词），美式拼写；
- `phonetic`：宽式 IPA，`/.../` 包裹，标主重音 `ˈ`，美音（`ɔːr`、flap t 可不标）；
  token 数与 `text` 词数严格一一对应——弱读的 a/the/of 各占一个 token，
  也不许把两个词并成一个 token（`so much` 写成 `/smʌtʃ/` 就是错位）；不可留空或用假音标；
- `zh`：繁体中文自然意译（不是逐词硬译），语气与英文一致；**dialogue 的 `zh` 不加「」『』**
  （全库多数是纯意译，混用会让字幕风格不齐；「」本身是 Big5-clean，不是缺陷，所以
  已有脚本里零星几处不必为此返工，只是别再新增）；
- 每对：b 必须直接回应 a（问答/请求-应答/发起-接话），信息连贯；
- 全篇 400 对按小场景分 8 段（每段 50 对），场景内部有时间/事件顺序推进，
  段与段自然过渡（如咖啡店：进门点单 → 排队看菜单 → 定制口味 → 加甜点 → 等待取餐
  → 外带打包 → 找座位/闲聊 → 结账离店）。8 段是 800 行不写散的关键，段落主题
  在选题阶段就定好（见 topic-research.md 的 eight_beats）。

## 可选顶层字段：`review`（审查结论写回）

由 `scripts/write_review.py <NNN>` 唯一写入（只动这一个键），控制台的脚本库列表读它显示
分数/verdict/「审查过期」。判据与评分口径见 `review_rubric.md`。

```json
"review": {
  "round": 2, "score": 92, "verdict": "APPROVED", "score_source": "report",
  "dimensions": {"pair_logic": 24, "idiomatic_a2": 23, "ipa_accuracy": 22, "variety_no_dup": 23},
  "issues": [{"dim": "pair_logic", "line": 15, "note": "..."}],
  "summary_zh": "...", "model": "skill:hot-sleep-dialogues",
  "reviewed_at": 1780000000, "content_hash": "sha1:…"
}
```

- `content_hash` = `sha1(json.dumps(dialogue, ensure_ascii=False, sort_keys=True))`，由工具计算，
  **不要手写**；app 侧（`app/script_library.py::_dialogue_hash`）用同一算法判断 `stale`
  —— 内容改过就显示「审查过期」。
- 没有 `score` 的存量报告写不进去（控制台仍显示「未审查」），见 `review_rubric.md`。

## 字段分级（空了会怎样 — 详细版见 `integration.md`）

| 级别 | 字段 | 空/错的后果 |
|---|---|---|
| **硬性** | `dialogue`（非空）、每行 `text`（非空且含英文字母）、`structure`/`lesson_type` | 整次运行中止（或预生成脚本被直接拒跑） |
| **质量必需** | `youtube_title`、`title_zh`、`scene`、`thumb_badge`/`thumb_main`/`thumb_hook`、`char_a_gender`/`char_b_gender`、`category`、`cefr` | 不报错，但**静默降级**：输出目录名退化、缩略图文案回落到内置默认、背景提示退化成 topic、音色按性别默认兜底 |
| **格式必需** | 每行 `zh`/`phonetic`（非空、`/.../` 闭合）、`title_quote` == `dialogue[0].text`、`youtube_tags` 非空 | 管理页体检报 error/warning；字幕缺中文或缺音标 |
| **可选** | `review`、`channel_id`、`scene_zh`、`thumbnail_*`（除 `thumbnail_subtitle`）、`welcome_*`/`intro_*`/`outro*`/`story_hook`/`practice_intro_*` | sleep 模式是冷开场，这些保持空串（与 `llm_client_sleep` 生成的一致） |

## Direct-use check（运行时到底读什么）

预生成脚本零-LLM 出片时，唯一决定成败的是：`dialogue` 非空 → 逐行 `text` 能被 TTS 合成
（非空且含英文字母）→ 行数按奇偶配对且 ≤800。其余字段只影响画面/元数据质量。
完整对照（含 file:line）见 `integration.md` §一/§二；一条命令列出来：
`python .qoder/skills/hot-sleep-dialogues/scripts/preflight.py ai_scripts_hot`。
