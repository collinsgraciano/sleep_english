"""全库「词 -> IPA」多数表决：找出标音与全库主流写法不一致的行。

只做检测，不改写任何文件。用法：
    python ipa_vote.py 108            # 单个脚本（编号）
    python ipa_vote.py 108 121        # 多个脚本
    python ipa_vote.py --all          # 语料里所有可疑行
    python ipa_vote.py --dir ai_scripts_hot --all
    python ipa_vote.py 108 --limit 0  # 打全量可疑行（默认只打前 40 条，计数不受影响）
    python ipa_vote.py --min-votes 6 --min-ratio 0.15
    python ipa_vote.py --all --json   # stdout 只输出统一 JSON 信封

原理：同一单词在全库标音里出现最多的写法视为正确主流写法，少数派即疑似标错
（机器审计只比词数，看不到 /blæk/ 写成 /blʌk/、漏尾 /k/ 这类符号级错误）。

Exit 0 无 SUSPECT / 2 有 SUSPECT（review 信号，逐条判定） / 3 用法错误。
"""
import glob
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

ROOT = SC.ROOT
DIRS = ["ai_scripts_hot", "ai_scripts"]
WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")
PUNCT = ".,;:!?¡¿«”\"'()[]{}·–—"
# 弱读/强读都合法的虚词：表决对它们没有意义
WEAK_OK = {
    "a", "an", "the", "and", "or", "but", "at", "to", "of", "for", "from", "with",
    "as", "is", "are", "was", "were", "do", "does", "did", "can", "will", "would",
    "should", "could", "have", "has", "had", "not", "that", "this", "it", "he",
    "she", "they", "them", "their", "there", "here", "your", "you", "we", "us",
    "our", "me", "my", "i", "some", "any", "all", "one", "on", "in", "be", "been",
}
# 两种读音在美式里都成立的词，表决只会制造噪音
VARIANT_OK = {
    "what", "off", "often", "either", "neither", "adult", "data", "aunt",
    "pecan", "caramel", "tomato", "again", "basil", "pasta", "counter", "sir",
}


def clean(token):
    """Drop sentence punctuation glued to the last IPA token of a line."""
    return token.strip(PUNCT)


def norm(token):
    """Collapse pure typography variants so they are not flagged as defects."""
    out = token.replace("ʧ", "tʃ").replace("ʤ", "dʒ")
    out = out.replace("ɡ", "g")
    return out.replace("ː", "").replace("ə", "")


def corpus_roots(scan_root: str | None = None):
    """表决语料 = 两个预生成库；被单独扫描的目录也并进来（保证它自己参与投票）。"""
    roots = []
    for d in DIRS:
        p = ROOT / d
        if p.is_dir():
            roots.append(p)
    if scan_root:
        p = SC.resolve_dir(scan_root, "ai_scripts_hot")
        if p.is_dir() and p not in roots:
            roots.append(p)
        return roots, p
    return roots, None


def scripts(root: Path):
    for folder in sorted(glob.glob(str(root / "*"))):
        path = Path(folder) / "script.json"
        if path.is_file():
            yield Path(folder).name, path


def tokens(data):
    """Return (word, ipa) pairs for lines whose word count matches token count."""
    pairs = []
    for i, line in enumerate(data.get("dialogue", [])):
        words = WORD_RE.findall(line.get("text", ""))
        ipa = line.get("phonetic", "").strip().strip("/").split()
        if not words or len(words) != len(ipa):
            continue
        for w, t in zip(words, ipa):
            pairs.append((i, w.lower(), clean(t)))
    return pairs


def build_corpus(roots):
    votes = defaultdict(Counter)
    for root in roots:
        for _, path in scripts(root):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for _, w, t in tokens(data):
                votes[w][t] += 1
    return votes


def same_glyph(a, b):
    """Pure typography variant (length mark / affricate spelling) -> style, not defect."""
    return norm(a) == norm(b)


def report(folder, path, votes, min_votes, min_ratio, limit=40, as_json=False):
    """扫描一篇 -> findings 列表（人类可读行按 --json 走 stderr）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        SC.line(f"{folder}: unreadable ({exc})", as_json)
        return []
    hits = []
    for i, w, t in tokens(data):
        if w in WEAK_OK or w in VARIANT_OK:
            continue
        counter = votes.get(w)
        if not counter:
            continue
        total = sum(counter.values())
        top, top_n = counter.most_common(1)[0]
        if top == t or top_n < min_votes or total < min_votes:
            continue
        if counter[t] / total > min_ratio:
            continue
        tag = "style" if same_glyph(t, top) else "SUSPECT"
        hits.append((tag, i, w, t, top, counter[t], top_n))
    real = [h for h in hits if h[0] == "SUSPECT"]
    SC.line(f"{folder}: {len(real)} suspect / {len(hits)} incl. style variants", as_json)
    # 打印上限只影响屏幕，不截断计数：验收 agent 常把 stdout 重定向到文件后逐条处理，
    # 截在前 40 条会让它们以为全篇就这 40 处（--limit 0 打全量）。
    for tag, i, w, t, top, n, tn in (hits if limit <= 0 else hits[:limit]):
        SC.line(f"  [{tag}] line {i}: {w} -> /{t}/ ({n}x) vs majority /{top}/ ({tn}x)",
                as_json)
    if 0 < limit < len(hits):
        SC.line(f"  ... {len(hits) - limit} more (use --limit 0 to print all)", as_json)
    return [SC.finding("warning", "ipa-suspect",
                       f"{folder} line {i + 1}: {w} -> /{t}/ ({n}x) "
                       f"vs majority /{top}/ ({tn}x)",
                       stage="音标", lines=[i + 1],
                       fix="按多数表决写法改写 phonetic；确认后同字形统一")
            for tag, i, w, t, top, n, tn in real]


def main() -> int:
    args = [a for a in sys.argv[1:]]
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    # --limit 的取值可能长得像编号（200），必须先从 args 摘掉再挑编号，否则会被当成脚本筛选
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 40
    if "--limit" in args:
        i = args.index("--limit")
        del args[i:i + 2]
    scan_root = args[args.index("--dir") + 1] if "--dir" in args else None
    if "--dir" in args:
        i = args.index("--dir")
        del args[i:i + 2]
    nums = [a for a in args if re.fullmatch(r"\d{3}", a)]
    all_mode = "--all" in args or not nums
    min_votes = int(next((args[args.index(v) + 1] for v in ("--min-votes",) if v in args), 5))
    min_ratio = float(next((args[args.index(v) + 1] for v in ("--min-ratio",) if v in args), 0.2))

    roots, target = corpus_roots(scan_root)
    if target is not None and not target.is_dir():
        print(f"ERROR: {target} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    if not roots:
        print("ERROR: no corpus directory found (both libraries missing)", file=sys.stderr)
        return SC.EXIT_USAGE
    votes = build_corpus(roots)

    findings: list[dict] = []
    total = 0
    scanned = 0
    for root in ([target] if target else roots):
        for folder, path in scripts(root):
            if not all_mode and not any(folder.startswith(n) for n in nums):
                continue
            scanned += 1
            hits = report(folder, path, votes, min_votes, min_ratio, limit, as_json)
            findings.extend(hits)
            total += len(hits)
    summary = f"suspect-lines: {total}"
    SC.line(f"SUMMARY {summary}", as_json)
    if as_json:
        shown = findings[:200]
        if len(findings) > len(shown):
            shown.append(SC.finding("note", "details-truncated",
                                    f"另有 {len(findings) - len(shown)} 条未列入 JSON；"
                                    f"用 --limit 0 或逐篇 编号 复跑查看"))
        SC.emit_json(SC.envelope("ipa_vote", target or roots[0], shown,
                                 checked=scanned, summary=summary,
                                 errors=0, warnings=total,
                                 extra={"suspect_lines": total,
                                        "corpus": [str(r) for r in roots]}))
    return SC.EXIT_WARN if total else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
