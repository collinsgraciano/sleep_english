"""Deterministic audit of preset scripts, feeding the AI review rounds.

Machine-provable checks only: structure, sentence word count, IPA shape and
IPA/text token alignment, non-IPA glyphs, simplified or variant glyphs and stray
Latin inside the Chinese translation, hard language errors (casing, terminal
punctuation, spacing, a/an agreement, ~25 common grammar collocations, and words
that look like a one-off misspelling of a common word), exact duplicate sentences
inside one script and across both preset libraries, near-duplicate sentences across
scripts (shared 4-word shingles), and two flattening signals: b lines with no
content word, and openers reused 20+ times.

Word limits come from the runtime, not from a constant: `word_count` flags a line
outside 3–9 words (the console 管理页体检 rule), and `word_cap` flags a line over
`SLEEP_MAX_LINE_WORDS` (default 7, resolved the same way as
`app/script_library.py::local_checks` — that check reports 「行太长」 with severity
medium). Keeping both means the skill never claims "clean" about a line the web
console will flag.

Judgement calls (does the reply really answer, does the scene progress, is it
idiomatic, is it interesting) belong to the reviewer agents —
see references/review_rubric.md and references/reviewer-prompt.md.

Usage:
    python audit_script.py                    # audit every script in ai_scripts_hot
    python audit_script.py 1 2                # only these folder numbers
    python audit_script.py --dir ai_scripts   # audit another library root
    python audit_script.py --fast             # skip the cross-script near-duplicate scan
    python audit_script.py --json             # stdout = one JSON envelope (see skill_common)

Writes <dir>/_audit/<FOLDER>.json and prints one line per script.
Exit 0 clean / 1 any hard issue / 2 review signals only / 3 usage error.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

WORD_RE = re.compile(r"[A-Za-z'’]+|\d[\d.,]*")
PUNCT = ".,?!;:—-…'’\"“”"
MIN_WORDS, MAX_WORDS = 3, 9
# 运行时单句上限（SLEEP_MAX_LINE_WORDS，默认 7）：与 app/script_library.py::local_checks
# 同一个解析入口，避免「skill 说干净、网页报行太长」。
WORD_CAP_DEFAULT = 7
SHINGLE_N = 4
NEAR_MIN_SHARED = 3
NEAR_MIN_RATIO = 0.6
ALLOWED_LATIN_IN_ZH = {"wifi", "tv", "app", "gps", "mp3", "dvd", "id", "atm", "kg", "cm", "mr", "mrs", "ms", "dr"}
STOPWORDS = set(
    "a an the and or but so if of to in on at it is are was were you your yours i my mine we our "
    "ours that this these those do does did not no yes me him her them his its there here for "
    "with from about just really very so can could will would should may might must have has had "
    "am be being been oh well okay ok alright now then what when how who why which some any "
    "nothing everything something again too also please thanks thank".split())
GENERIC_B = {
    "okay", "ok", "sure", "yes", "no", "right", "good", "great", "nice", "cool", "fine",
    "wow", "thanks", "thank you", "alright", "oh okay", "of course", "sounds good",
}

# 助动词/使役动词后接动词原形，主语三单由它承载 → 这些词紧邻主语时不算主谓一致错误。
AUX_GUARD = "".join(r"(?<!\b%s )" % w for w in (
    "do", "does", "did", "can", "could", "will", "would", "shall", "should",
    "may", "might", "must", "to", "let", "make", "have", "has", "had"))

# 常见语法/搭配错误（母语者不会说出口的），只报硬错，风格问题交给审阅 agent
GRAMMAR_PATTERNS = [
    (r"\bI has\b", "I has → I have"),
    (r"\b(?:he|she|it) don't\b", "he/she/it don't → doesn't"),
    (r"\b(?:he|she|it) don\b", "he/she/it don → doesn't"),
    (r"\bdon't has\b", "don't has → don't have"),
    (r"\bdoesn't has\b", "doesn't has → doesn't have"),
    (r"\b(?:I|we|they) is\b", "复数主语 + is"),
    (r"\bare is\b|\bis are\b", "are is / is are"),
    (r"\b(?:everyone|everybody|someone|somebody|anyone|anybody|noone|nobody)\s+(?:are|were)\b",
     "单数主语接复数动词"),
    # Does everyone have a tag now? 是对的——三单由 does 承载，have 是原形。
    # 所以只有主语「直接」接 have 才算硬错，实测全库唯一命中就是这种问句。
    # lookbehind 必须定长，故按助动词逐个排除（含 they each have 这类同位语）。
    (AUX_GUARD + r"\b(?:everyone|everybody|someone|noone|nobody)\s+have\b", "单数主语接复数动词"),
    # each 特意排除在主语表外：They each have a folder 里的 each 是同位语，接原形才对。
    (r"\b(?:he|she) (?:have|had) (?:a|an)?\s*(?:good)?\s*(?:idea|time)\b", "he have → he has"),
    (r"\bmore (?:better|worse|bigger|easier|nicer|higher|lower|faster|slower)\b", "more + 比较级"),
    (r"\bmost (?:best|worst|bigger|nice)\b", "most + 最高级误用"),
    (r"\b(?:peoples|informations|advices|equipments|staffs|breads|moneys|tooths|childs|peoples)\b", "不可数/复数形式错"),
    (r"\bexplain (?:me|to I)\b", "explain me → explain to me"),
    (r"\bdiscuss about\b", "discuss about → discuss"),
    (r"\bmarry with\b", "marry with → marry"),
    (r"\blisten music\b", "listen music → listen to music"),
    (r"\b(?:let me to|help me to do|make you to)\b", "使役动词后不接 to"),
    (r"\bsince ... but\b", "since/but 混搭"),
    (r"\balthough .{0,40}\bbut\b", "although 与 but 重复"),
    (r"\bbecause .{0,40}\bso\b", "because 与 so 重复"),
    (r"\b(?:did went|have went|has went)\b", "did/have + went → gone"),
    (r"\b(?:goed|eated|sleeped|buyed|takeds|bringed|thinked|feels goodly)\b", "不规则动词错误"),
    (r"\b(?:yesterday I go|tomorrow I went)\b", "时态与时间状语矛盾"),
    (r"\bhow many much\b|\bhow much many\b", "how much / how many 混用"),
    (r"\beveryday\b(?! life| use)", "everyday(形容词) 常应为 every day"),
]
ARTICLE_VOWELS = "aeiou"
ARTICLE_OK = re.compile(r"\ba\s+(?:uni|use|used|user|euro|ewe|one|once|york)", re.I)
A_AS_LETTER = {"is", "are", "was", "and", "or", "b", "c", "row", "seat"}  # 字母名 A 后接的词
TYPO_FREQ_MIN = 25        # 参照词至少出现这么多次
TYPO_CANDIDATE_MAX = 1    # 可疑拼写：全库只出现过这么少
# 拟声/方言字虽不在 Big5 里，但台派口语写法接受，不算简体字
ZH_OK_OUTSIDE_BIG5 = set("咔哐噼咣擀哒")
# 〇(U+3007) 与 ⋯(U+22EF) 落在下面那个 CJK 区间之外，Big5 测试看不到它们；
# 台湾写法是 ○ 与 …，两者才 Big5-clean。
ZH_BAD_GLYPHS = {"〇": "○", "⋯": "…"}

# hard = 机器可判定、必须改掉的缺陷；review = 信号，由审阅 agent 判断怎么改
HARD_KINDS = ("json", "self_dup", "cross_dup", "ipa_shape", "ipa_glyph", "zh_simplified",
              "capital", "end_punct", "spacing", "word_count", "grammar", "article")
REVIEW_KINDS = ("spelling", "ipa_tokens", "near_dup", "repeat_opener", "flat_b", "zh_latin",
                "word_cap")
# 人类可读说明（报告与 --json 的 findings[].code 用同一套 code）
KIND_LEVEL = {k: "error" for k in HARD_KINDS}
KIND_LEVEL.update({k: "warning" for k in REVIEW_KINDS})
KIND_LEVEL["generic_b"] = "note"
KIND_LEVEL["json"] = "error"

_big5_cache = {}
_word_cap_cache = None


def word_cap() -> int:
    """运行时单句词数上限：`SLEEP_MAX_LINE_WORDS` → 默认 7。

    与 `app/script_library.py::local_checks` 走同一个 `resolve_max_line_words`
    入口（pipeline 不可导入时退回默认 7），保证两边口径不会分家。
    """
    global _word_cap_cache
    if _word_cap_cache is None:
        try:
            sys.path.insert(0, str(SC.ROOT / "pipeline"))
            from llm_client import resolve_max_line_words  # type: ignore
            _word_cap_cache = int(resolve_max_line_words("SLEEP_MAX_LINE_WORDS",
                                                         WORD_CAP_DEFAULT))
        except Exception:
            _word_cap_cache = WORD_CAP_DEFAULT
    return _word_cap_cache


def big5_ok(ch: str) -> bool:
    if ch not in _big5_cache:
        try:
            ch.encode("big5")
            _big5_cache[ch] = True
        except UnicodeEncodeError:
            _big5_cache[ch] = False
    return _big5_cache[ch]


def is_lookalike(ch: str) -> bool:
    code = ord(ch)
    return 0x400 <= code <= 0x4FF or 0x3040 <= code <= 0x30FF


def norm(text: str) -> str:
    return text.strip().lower().strip(PUNCT).rstrip(PUNCT)


def words(text: str):
    return [w for w in WORD_RE.findall(text)]


def content_words(text: str) -> set:
    return {w.lower().strip(PUNCT) for w in words(text)} - STOPWORDS - {""}


def shingles(text: str):
    toks = [w.lower().strip(PUNCT) for w in words(text)]
    return {tuple(toks[i:i + SHINGLE_N]) for i in range(len(toks) - SHINGLE_N + 1)}


def load(dirs):
    corpus = {}
    for d in dirs:
        for sp in sorted(Path(d).glob("*/script.json")):
            try:
                corpus[sp.parent.name] = json.loads(
                    sp.read_text(encoding="utf-8")).get("dialogue", [])
            except (json.JSONDecodeError, OSError) as exc:
                corpus[sp.parent.name] = None
                print(f"!{sp.parent.name}: unreadable ({exc})", file=sys.stderr)
    return corpus


def ipa_tokens(ipa: str):
    return [t for t in re.split(r"\s+", ipa.strip("/")) if t.strip("ˈˌ.· ")]


def build_vocab(corpus):
    """Whole-library word frequency + deletion index of common words (typo detector)."""
    freq = Counter()
    for dialogue in corpus.values():
        for line in dialogue or []:
            for w in words(str(line.get("text", ""))):
                lw = w.lower()
                if lw.isalpha() and len(lw) >= 4 and "'" not in lw:
                    freq[lw] += 1
    common = {w for w, c in freq.items() if c >= TYPO_FREQ_MIN}
    deleps = defaultdict(set)
    for w in common:
        for j in range(len(w)):
            deleps[w[:j] + w[j + 1:]].add(w)
    return freq, deleps


def typo_candidates(word, freq, deleps):
    """Words within edit distance 1 of a common word (precise, not the ≤2 that a
    plain deletion-index join gives)."""
    found = set()
    for j in range(len(word)):
        probe = word[:j] + word[j + 1:]
        found |= {w for w in deleps.get(probe, set()) if len(w) == len(word) - 1}
        found |= {w for w in deleps.get(probe, set()) if len(w) == len(word)}
        found |= {w for w in deleps.get(word, set())}
    near = set()
    for w in found:
        if w == word or abs(len(w) - len(word)) > 1:
            continue
        if len(w) == len(word):
            if sum(1 for a, b in zip(w, word) if a != b) == 1:
                near.add(w)
        elif len(w) == len(word) + 1:      # word misses one char
            if any(w[:i] + w[i + 1:] == word for i in range(len(w))):
                near.add(w)
        else:                               # word has one extra char
            if any(word[:i] + word[i + 1:] == w for i in range(len(word))):
                near.add(w)
    # 相邻字符互换（recieve / receive 这类）
    for i in range(len(word) - 1):
        swap = word[:i] + word[i + 1] + word[i] + word[i + 2:]
        if swap != word and freq.get(swap, 0) >= TYPO_FREQ_MIN:
            near.add(swap)
    return sorted(near, key=lambda w: -freq[w])


def lang_issues(text: str, freq=None, deleps=None):
    """Hard language correctness flags: casing/punctuation, grammar patterns,
    a/an agreement, and words that look like a one-off misspelling."""
    out = []
    stripped = text.strip()
    if stripped and stripped[0].islower():
        out.append(("capital", f"句首小写: {text!r}"))
    tail = stripped.rstrip("”\"')")
    if tail and tail[-1] not in ".!?" and tail[-1] != "…":
        out.append(("end_punct", f"句末缺标点: {text!r}"))
    squashed = stripped.replace("...", "…").replace("…", "")
    if "  " in squashed or " ," in squashed or " ." in squashed or ".." in squashed or " ?" in squashed:
        out.append(("spacing", f"空格/标点排布异常: {text!r}"))
    low = stripped.lower()
    for pat, why in GRAMMAR_PATTERNS:
        if re.search(pat, low):
            out.append(("grammar", f"{why}: {text!r}"))
    for m in re.finditer(r"\ba\s+([a-z]\w+)", low):
        nxt = m.group(1)
        if nxt in A_AS_LETTER or nxt.startswith("ei"):
            continue        # "seat twelve A is free" 里的 A 是字母名，不是冠词
        if nxt[0] in ARTICLE_VOWELS and not ARTICLE_OK.search(m.group(0)):
            out.append(("article", f"a + 元音开头 {nxt!r}: {text!r}"))
    for m in re.finditer(r"\ban\s+([a-z]\w+)", low):
        nxt = m.group(1)
        if nxt[0] not in ARTICLE_VOWELS and not nxt.startswith(("hour", "honest")):
            out.append(("article", f"an + 辅音开头 {nxt!r}: {text!r}"))
    if freq is not None and deleps is not None:
        for w in sorted(set(words(stripped))):
            lw = w.lower()
            if not (len(lw) >= 5 and lw.isalpha() and "'" not in lw):
                continue
            if freq.get(lw, 0) > TYPO_CANDIDATE_MAX:
                continue
            forms = {lw, lw[:-1], lw.rstrip("s"), lw[:-2] if lw.endswith("es") else lw, lw + "s"}
            if any(freq.get(f, 0) >= TYPO_FREQ_MIN for f in forms if len(f) >= 4):
                continue     # 常见词的复数/动名词等规则变形，不算拼写问题
            near = typo_candidates(lw, freq, deleps)
            if near:
                out.append(("spelling", f"{w!r} 全库仅出现 {freq.get(lw, 0)} 次，形近常见词 {near[0]!r}，请判断是否为笔误"))
    return out


def audit_lines(folder, dialogue, owner_of, near_by_shingle, do_near, vocab=None,
                cap: int | None = None):
    issues = []
    counts = Counter()
    cap = WORD_CAP_DEFAULT if cap is None else cap
    if dialogue is None:
        return [{"index": -1, "kind": "json", "detail": "script.json does not parse"}], {"json": 1}

    seen = set()
    for i, line in enumerate(dialogue):
        text = str(line.get("text", ""))
        ipa = str(line.get("phonetic", ""))
        zh = str(line.get("zh", ""))
        nw = len(words(text))

        if not MIN_WORDS <= nw <= MAX_WORDS:
            issues.append({"index": i, "kind": "word_count",
                           "detail": f"{nw} 词（要求 {MIN_WORDS}-{MAX_WORDS}）: {text!r}"})
            counts["word_count"] += 1
        if nw > cap:
            # 运行时/脚本库口径：> cap 会被 local_checks 报「行太长（medium）」，卡片也会自动缩字
            issues.append({"index": i, "kind": "word_cap",
                           "detail": f"{nw} 词 > SLEEP_MAX_LINE_WORDS={cap}: {text!r}"})
            counts["word_cap"] += 1

        if not (ipa.startswith("/") and ipa.endswith("/")):
            issues.append({"index": i, "kind": "ipa_shape", "detail": f"未用 /.../ 包裹: {ipa!r}"})
            counts["ipa_shape"] += 1
        look = sorted({c for c in ipa if is_lookalike(c)})
        if look:
            issues.append({"index": i, "kind": "ipa_glyph",
                           "detail": f"音标混入非 IPA 字符 {look}: {ipa!r}"})
            counts["ipa_glyph"] += 1

        nipa = len(ipa_tokens(ipa))
        if nipa and nw:
            diff = abs(nipa - nw)
            forgiving = (re.search(r"\d", text) or re.search(r"[A-Za-z]-[A-Za-z]", text)
                         or re.search(r"\b[A-Z]{2,}[a-z]?\b", text) or "'" in text or "’" in text)
            floor = 3 if forgiving else 1
            if diff >= floor:
                issues.append({"index": i, "kind": "ipa_tokens",
                               "detail": f"{nw} 词 vs {nipa} 音标 token: {text!r} | {ipa!r}"})
                counts["ipa_tokens"] += 1

        bad_zh = sorted({c for c in zh if (c in ZH_BAD_GLYPHS
                                           or ('一' <= c <= '鿿' and not big5_ok(c))
                                           or is_lookalike(c))
                         and c not in ZH_OK_OUTSIDE_BIG5})
        if bad_zh:
            fixes = " ".join(f"{c}→{ZH_BAD_GLYPHS[c]}" for c in bad_zh if c in ZH_BAD_GLYPHS)
            issues.append({"index": i, "kind": "zh_simplified",
                           "detail": f"译文非正体字形 {''.join(bad_zh)}"
                                     + (f"（应写作 {fixes}）" if fixes else "") + f": {zh!r}"})
            counts["zh_simplified"] += 1
        # 译文里夹的英文只报独立小写词（人名/品牌按惯例可保留原文）
        stray = [w for w in re.findall(r"(?<![A-Za-z])[a-z]{2,}(?![A-Za-z])", zh)
                 if w.lower() not in ALLOWED_LATIN_IN_ZH]
        if stray:
            issues.append({"index": i, "kind": "zh_latin",
                           "detail": f"译文夹英文 {stray}: {zh!r}"})
            counts["zh_latin"] += 1

        key = norm(text)
        if key in seen:
            issues.append({"index": i, "kind": "self_dup", "detail": f"篇内整句重复: {text!r}"})
            counts["self_dup"] += 1
        seen.add(key)
        # owner_of is built from the whole library, so it always contains this folder;
        # the collision is with *other* folders only (keep = lexicographically first,
        # which by policy is the script that must not be edited).
        owners = {f for f in owner_of.get(key, ()) if f != folder}
        if owners:
            keep = sorted(owners)[0]
            issues.append({"index": i, "kind": "cross_dup",
                           "detail": f"与 {keep} 整句撞车: {text!r}"})
            counts["cross_dup"] += 1

        if do_near:
            shs = shingles(text)
            if len(shs) >= NEAR_MIN_SHARED:
                shared = sum(1 for sh in shs if any(f != folder for f in near_by_shingle.get(sh, ())))
                if shared >= NEAR_MIN_SHARED and shared / len(shs) >= NEAR_MIN_RATIO:
                    issues.append({"index": i, "kind": "near_dup",
                                   "detail": f"与外题共用 {shared}/{len(shs)} 个 4-gram: {text!r}"})
                    counts["near_dup"] += 1

        if i % 2 == 1 and (norm(text) in GENERIC_B or nw <= 2):
            counts["generic_b"] += 1

        freq, deleps = vocab or (None, None)
        for kind, detail in lang_issues(text, freq, deleps):
            issues.append({"index": i, "kind": kind, "detail": detail})
            counts[kind] += 1

    openers = Counter()
    pairs = max(1, (len(dialogue) + 1) // 2)
    for i in range(0, len(dialogue) - 1, 2):
        a_words = words(str(dialogue[i].get("text", "")))
        if a_words:
            openers[a_words[0].lower().strip(PUNCT)] += 1
        b = content_words(str(dialogue[i + 1].get("text", "")))
        if not b:
            counts["flat_b"] += 1
    top_opener, top_n = openers.most_common(1)[0] if openers else ("", 0)
    if top_n >= max(10, pairs // 10):      # 一成以上台词同一开头 = 句式模板化
        counts["repeat_opener"] = top_n
        issues.append({"index": -2, "kind": "repeat_opener",
                       "detail": f"{top_n}/{pairs} 对以 {top_opener!r} 开头，句式过于单一"})
    return issues, dict(counts)


def build_context(corpus: dict, do_near: bool = True) -> dict:
    """跨篇查重所需的整库索引（一次构建，多篇复用）。"""
    owner_of = defaultdict(set)
    near_by_shingle = defaultdict(set)
    for folder, dialogue in corpus.items():
        if not dialogue:
            continue
        for line in dialogue:
            t = str(line.get("text", ""))
            owner_of[norm(t)].add(folder)
            if do_near:
                for sh in shingles(t):
                    near_by_shingle[sh].add(folder)
    return {"owner_of": owner_of, "near_by_shingle": near_by_shingle,
            "vocab": build_vocab(corpus), "do_near": do_near}


def audit_one(folder: str, script: dict, ctx: dict, cap: int | None = None):
    """单篇机器审计（供 preflight.py 复用，不重复实现规则）。

    Returns (issues, counts)；issues 元素为 {"index","kind","detail"}。
    """
    dialogue = (script or {}).get("dialogue")
    return audit_lines(folder, dialogue, ctx["owner_of"], ctx["near_by_shingle"],
                       ctx["do_near"], ctx.get("vocab"), cap=cap)


def findings_of(folder: str, issues: list[dict]) -> list[dict]:
    """issues -> skill_common 的 findings（带 level/code/stage）。

    `word_cap` 逐行命中会达数百条（存量稿整体按 3–9 词写），所以聚合成一条
    带行号列表的 finding —— 明细仍在 `_audit/<FOLDER>.json` 里逐行可查。
    """
    out = []
    over_cap = [it for it in issues if it.get("kind") == "word_cap"]
    for it in issues:
        kind = it.get("kind", "?")
        if kind == "word_cap":
            continue
        idx = it.get("index", -1)
        stage = "结构" if kind == "json" else "台词"
        where = "" if idx < 0 else f"line {idx + 1}: "
        if kind in ("cross_dup", "near_dup"):
            stage = "查重"
        out.append(SC.finding(KIND_LEVEL.get(kind, "warning"), kind,
                              f"{folder} {where}{it.get('detail', '')}",
                              stage=stage,
                              lines=[it["index"] + 1] if idx >= 0 else None))
    if over_cap:
        lines = sorted(it["index"] + 1 for it in over_cap if it.get("index", -1) >= 0)
        out.append(SC.finding(
            "warning", "word_cap",
            f"{folder} {len(over_cap)} 行超过运行时上限 "
            f"（SLEEP_MAX_LINE_WORDS={word_cap()}）：前几处 line "
            + ", ".join(str(x) for x in lines[:5])
            + "（脚本库体检会报「行太长」，卡片自动缩字；明细见 _audit/<FOLDER>.json）",
            stage="台词", lines=lines,
            fix="新稿按 3–7 词写；存量稿只提示，不追改"))
    return out


def main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    do_near = "--fast" not in argv
    dir_arg = None
    if "--dir" in argv:
        i = argv.index("--dir")
        if i + 1 >= len(argv):
            print("ERROR: --dir needs a value", file=sys.stderr)
            return SC.EXIT_USAGE
        dir_arg = argv[i + 1]
        del argv[i:i + 2]
    wanted = {a for a in argv if not a.startswith("--")}

    root = SC.resolve_dir(dir_arg, "ai_scripts_hot")
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    audit_dir = root / "_audit"

    corpus_dirs = []
    for lib in ("ai_scripts_hot", "ai_scripts"):   # 后加载的覆盖同名 folder
        p = SC.ROOT / lib
        if p.is_dir():
            corpus_dirs.append(p)
    if root not in corpus_dirs:
        corpus_dirs.append(root)                   # 被审的库必须是语料的一部分
    corpus = load(corpus_dirs)

    audited = sorted(p.parent.name for p in SC.iter_scripts(root))
    if wanted:
        audited = [f for f in audited if f.split("_")[0] in wanted]
        if not audited:
            print(f"ERROR: no folder matches {sorted(wanted)} under {root}", file=sys.stderr)
            return SC.EXIT_USAGE

    ctx = build_context(corpus, do_near)
    cap = word_cap()
    audit_dir.mkdir(exist_ok=True)

    totals = Counter()
    findings: list[dict] = []
    n_findings_all = 0
    dirty = flagged = 0
    for folder in audited:
        issues, counts = audit_one(folder, {"dialogue": corpus.get(folder)}, ctx, cap=cap)
        SC.write_json(audit_dir / f"{folder}.json",
                      {"folder": folder, "lines": len(corpus.get(folder) or []),
                       "counts": counts, "issues": issues, "word_cap": cap})
        for k, v in counts.items():
            totals[k] += v
        hard = sum(counts.get(k, 0) for k in HARD_KINDS)
        review = sum(counts.get(k, 0) for k in REVIEW_KINDS)
        dirty += 1 if hard else 0
        flagged += 1 if review else 0
        mark = "!" if hard else ("~" if review else " ")
        SC.line(mark + folder + ": "
                + " ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v), as_json)
        # 报告预算：hard 全留，warning 每篇最多 6 条（完整清单在 _audit/<FOLDER>.json）
        f_all = findings_of(folder, issues)
        n_findings_all += len(f_all)
        findings.extend(SC.budget(f_all, low_max=6))

    summary = (f"{len(audited)} scripts | hard-issue: {dirty} | review-needed: {flagged} | "
               + " ".join(f"{k}={v}" for k, v in sorted(totals.items()) if v)
               + f" | cap={cap}")
    SC.line(f"\nSUMMARY {summary}", as_json)
    SC.line(f"reports -> {(audit_dir.relative_to(SC.ROOT).as_posix() if audit_dir.is_relative_to(SC.ROOT) else audit_dir)}/<FOLDER>.json", as_json)

    hard_total = sum(totals.get(k, 0) for k in HARD_KINDS)
    review_total = sum(totals.get(k, 0) for k in REVIEW_KINDS)
    if n_findings_all > len(findings):
        findings.append(SC.finding(
            "note", "details-truncated",
            f"报表明细按预算截断（{n_findings_all - len(findings)} 条未打印）；"
            f"完整逐条清单见 {audit_dir}/<FOLDER>.json"))
    payload = SC.envelope("audit_script", root, findings, checked=len(audited),
                          summary=summary,
                          errors=hard_total, warnings=review_total,
                          extra={"hard": hard_total, "review": review_total,
                                 "dirty_scripts": dirty, "flagged_scripts": flagged,
                                 "word_cap": cap, "audit_dir": str(audit_dir)})
    if as_json:
        SC.emit_json(payload)
    return SC.exit_of(hard_total, review_total)


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
