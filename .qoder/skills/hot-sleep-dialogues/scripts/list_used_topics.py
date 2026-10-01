"""Topic ledger for the hot-dialogues skill: what is already used, and whether a
candidate batch collides with it. Deterministic bookkeeping only — topic choices
come from the web-research step (references/topic-research.md), never from here.

Usage:
    python list_used_topics.py                     # every used topic (both libraries)
    python list_used_topics.py --categories        # category histogram + gaps
    python list_used_topics.py --check candidates.json
        # candidates.json = [{"en":..., "zh":..., "category":...}, ...]
        # a .md/.txt file with one "| NNN | En | 中文 | Category |" table row per
        # topic works too.
    python list_used_topics.py --check candidates.json --json

Exit（--check）：0 无冲突 / 1 硬撞车（英文去词序后全同、中文全同、批内自撞，不放行）
              / 2 只有近义提示（Jaccard ≥ 0.6，需人工判断，不算失败）。
不带 --check 的清单模式恒 exit 0。

The baseline is the ledger (references/topics.md) ∪ both preset libraries on disk ∪
ai_scripts_hot/_topics_batch.json, so a topic selected in the current (not yet
generated) batch — or one whose scripts were deleted during a renumbering — still
counts as taken. That is what keeps two batches running in parallel, and a fresh
library, from re-picking the same subject. 库被清空也不影响：台账是权威排重基线。
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

HERE = SC.HERE
ROOT = SC.ROOT
LIBS = ("ai_scripts", "ai_scripts_hot")
LEDGER = HERE.parent / "references" / "topics.md"
STOP = {"a", "an", "the", "at", "of", "to", "in", "on", "for", "and", "or",
        "my", "me", "your", "you", "with", "our"}
SIMILARITY = 0.6


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(s or "").lower()).strip()


def zh_key(s: str) -> str:
    """中文主题的比对键：norm() 会把非 ASCII 全删掉（所有 CJK 标题都变成空串），
    所以中文只能按「去标点与空白后的原字」比较。"""
    return re.sub(r"[\s　（）()「」《》、，,。.：:；;！!？?／/－-]", "", str(s or ""))


def stem(s: str) -> str:
    return " ".join(sorted(w for w in norm(s).split() if w and w not in STOP))


def planned_topics():
    """本批已定稿、还没生成文件夹的主题（_topics_batch.json）——也算查重基线，
    否则两批并行时彼此看不见对方的选题。"""
    path = ROOT / "ai_scripts_hot" / "_topics_batch.json"
    rows = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = []
        for t in data if isinstance(data, list) else (data.get("themes") or []):
            if isinstance(t, dict) and t.get("en"):
                rows.append({"lib": "planned", "folder": str(t.get("folder", "")),
                             "en": str(t.get("en", "")), "zh": str(t.get("zh", "")),
                             "category": str(t.get("category", "")),
                             "lines": int(t.get("lines", 0) or 0)})
    return rows


def ledger_topics():
    """台账（references/topics.md）里登记过的主题 —— 权威的排重基线。

    磁盘上的脚本库可以被整体清空（重编批次），但台账里的历史主题永远算「已用过」，
    所以查重读台账、编号起点读磁盘，两者不再互相依赖。
    """
    rows = []
    if not LEDGER.exists():
        return rows
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or not re.fullmatch(r"\d+", cells[0]):
            continue
        rows.append({"lib": "ledger", "folder": cells[4] if len(cells) >= 5 else "",
                     "en": cells[1], "zh": cells[2], "category": cells[3], "lines": 0})
    return rows


def used_topics(include_planned=True, include_ledger=True):
    rows = []
    for lib in LIBS:
        base = ROOT / lib
        manifest = base / "manifest.json"
        if manifest.exists():
            try:
                themes = json.loads(manifest.read_text(encoding="utf-8")).get("themes") or []
            except (json.JSONDecodeError, OSError):
                themes = []
            for t in themes:
                if isinstance(t, dict) and t.get("folder"):
                    rows.append({"lib": lib, "folder": str(t["folder"]),
                                 "en": str(t.get("en", "")), "zh": str(t.get("zh", "")),
                                 "category": str(t.get("category", "")),
                                 "lines": int(t.get("lines", 0) or 0)})
        else:
            print(f"WARN {lib}/manifest.json missing — scanning folders", file=sys.stderr)
        seen = {r["folder"] for r in rows if r["lib"] == lib}
        if base.is_dir():
            for sub in sorted(base.iterdir()):
                if not sub.is_dir() or sub.name.startswith(("_", ".")) or sub.name in seen:
                    continue
                if not re.match(r"^\d+_", sub.name):
                    continue
                rows.append({"lib": lib, "folder": sub.name,
                              "en": re.sub(r"^\d+_", "", sub.name).replace("_", " "),
                              "zh": "", "category": "", "lines": 0})
    if include_planned:
        generated = {r["folder"] for r in rows}
        rows += [p for p in planned_topics()
                 if p["folder"] not in generated and p["en"] not in {r["en"] for r in rows}]
    if include_ledger:
        have_en = {norm(r["en"]) for r in rows}
        have_zh = {zh_key(r["zh"]) for r in rows if zh_key(r["zh"])}
        have_folder = {r["folder"] for r in rows}
        # folder 相同 ⇒ 磁盘/本批与台账描述的是同一篇（可能只是主题名后来改过），跳过避免重复计数。
        # 注意：重编号后同一"编号前缀"可以同时出现在历史归档与新批次里（如
        # ai_scripts/021_Doing_Homework 与 021_Berry_Picking_at_a_Farm），它们 folder 名不同，
        # 因此历史行不会被顶掉 —— 这里只比对完整 folder 名。
        rows += [t for t in ledger_topics()
                 if t["folder"] not in have_folder
                 and norm(t["en"]) not in have_en
                 and zh_key(t["zh"]) not in have_zh]
    return rows


def parse_candidates(path: Path):
    text = path.read_text(encoding="utf-8")
    out = []
    if path.suffix == ".json" or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("topics") or data.get("themes") or []
        for item in data:
            out.append({"en": str(item.get("en") or item.get("topic") or "").strip(),
                        "zh": str(item.get("zh") or item.get("topic_zh") or "").strip(),
                        "category": str(item.get("category") or "").strip(),
                        "folder": str(item.get("folder") or "").strip()})
        return out
    for line in text.splitlines():
        if "|" not in line or re.match(r"^\s*\|?[\s:-]+\|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        en, zh = (cells[1], cells[2]) if re.match(r"^\d+", cells[0]) else (cells[0], cells[1])
        cat = cells[-1] if len(cells) >= 4 and not re.match(r"^\d", cells[-1]) else ""
        if re.match(r"^[A-Za-z]", en):
            out.append({"en": en, "zh": zh, "category": cat})
    return out


def same_topic_row(candidate, row):
    """A planned batch file checked against itself must not flag its own rows."""
    return bool(candidate.get("folder")) and candidate["folder"] == row["folder"]


def next_free_index():
    """下一个可用编号 = 所有「已占号」的最大值 +1。

    已占号不止磁盘上的脚本目录：_parts/ 里写到一半的、_topics_batch.json 里刚定稿的
    都算（控制台的「新增脚本」也按同一套全局唯一编号分配，见
    app/ai_scripts_admin.next_index —— 本批号段被它抢走就会撞车）。
    """
    taken = set()
    for lib in LIBS:
        base = ROOT / lib
        if base.is_dir():
            for sub in base.iterdir():
                m = re.match(r"^(\d+)_", sub.name) if sub.is_dir() else None
                if m:
                    taken.add(int(m.group(1)))
            parts = base / "_parts"
            if parts.is_dir():
                for sub in parts.iterdir():
                    m = re.match(r"^(\d+)_", sub.name) if sub.is_dir() else None
                    if m:
                        taken.add(int(m.group(1)))
    for p in planned_topics():
        m = re.match(r"^(\d+)_", p["folder"])
        if m:
            taken.add(int(m.group(1)))
    return (max(taken) + 1) if taken else 1


def main() -> int:
    args = sys.argv[1:]
    as_json = "--json" in args
    check = args[args.index("--check") + 1] if "--check" in args else None
    categories = "--categories" in args
    used = used_topics()
    findings: list[dict] = []

    if not check:
        for r in sorted(used, key=lambda x: x["folder"]):
            SC.line(f"{r['folder']}\t{r['en']}\t{r['zh']}\t{r['category']}\t{r['lines']}",
                    as_json)
        SC.line(f"\n# {len(used)} used topics (磁盘库 + 台账 + 本批清单；台账只作排重，不参与编号)",
                as_json)
        SC.line(f"# next index: {next_free_index()}  ← 新批次从这里连续分配", as_json)
        if not categories:
            if as_json:
                SC.emit_json(SC.envelope(
                    "list_used_topics", ROOT, [], checked=len(used),
                    summary=f"{len(used)} used topics",
                    extra={"used": used, "next_index": next_free_index()}))
            return SC.EXIT_OK

    hist = Counter(r["category"] for r in used if r["category"])
    for cat, n in hist.most_common():
        SC.line(f"{n:>4}  {cat}", as_json)
    SC.line(f"\n# {sum(hist.values())} categorised / {len(used)} total", as_json)

    if not check:
        if as_json:
            SC.emit_json(SC.envelope(
                "list_used_topics", ROOT, [], checked=len(used),
                summary=f"{len(used)} used topics",
                extra={"used": used, "categories": dict(hist),
                       "next_index": next_free_index()}))
        return SC.EXIT_OK
    try:
        cands = parse_candidates(SC.resolve_path(check))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot parse {check}: {e}", file=sys.stderr)
        return SC.EXIT_USAGE
    if not cands:
        print(f"ERROR: no candidates parsed from {check}", file=sys.stderr)
        return SC.EXIT_USAGE
    hard, review = [], []
    for c in cands:
        prior = [r for r in used if not same_topic_row(c, r)]
        s, zn = stem(c["en"]), zh_key(c["zh"])
        if s and s in {stem(r["en"]) for r in prior if r["en"]}:
            hit = next(r for r in prior if stem(r["en"]) == s)
            hard.append(SC.finding("error", "topic-collision",
                                   f"{c['en']} — 英文主题（去词序）与 "
                                   f"{hit['lib']}/{hit['folder']} 完全相同",
                                   fix="换题或换角度（语义重复也算重复）"))
        elif zn and zn in {zh_key(r["zh"]) for r in prior if r["zh"]}:
            hit = next(r for r in prior if zh_key(r["zh"]) == zn)
            hard.append(SC.finding("error", "topic-collision",
                                   f"{c['en']} — 中文主题与 {hit['lib']}/{hit['folder']} 完全相同",
                                   fix="换题"))
        else:
            cwords = set(s.split())
            for r in prior:
                rw = set(stem(r["en"]).split())
                if not cwords or not rw:
                    continue
                jac = len(cwords & rw) / len(cwords | rw)
                if jac >= SIMILARITY:
                    review.append(SC.finding(
                        "warning", "topic-near-dup",
                        f"{c['en']} ~ {r['lib']}/{r['folder']} (Jaccard {jac:.2f})",
                        fix="人工判断：不同子场景可留，同场景改写"))
                    break
    for t, n in Counter(stem(c["en"]) for c in cands).items():
        if n > 1:
            hard.append(SC.finding("error", "batch-self-dup", f"批内重复: {t}"))
    findings = hard + review
    summary = (f"candidates: {len(cands)} | hard: {len(hard)} | review: {len(review)}")
    SC.line(f"\n# {summary}", as_json)
    for f in findings:
        SC.line(("HARD " if f["level"] == "error" else "REVIEW ") + f["detail"], as_json)
    if as_json:
        SC.emit_json(SC.envelope("list_used_topics", ROOT, findings,
                                 checked=len(cands), summary=summary,
                                 extra={"candidates": len(cands), "used": len(used),
                                        "next_index": next_free_index()}))
    if hard:
        return SC.EXIT_BLOCK
    return SC.EXIT_WARN if review else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
