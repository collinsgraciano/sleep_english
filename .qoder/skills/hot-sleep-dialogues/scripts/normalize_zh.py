"""Normalize the Chinese of preset scripts to Traditional Chinese.

Content is never generated or rephrased here: each line's translation (and, with
--meta, the top-level Chinese copy fields) is passed through OpenCC s2t and then a
Taiwan-standard variant fix-up table. `text`/`phonetic`/`speaker` stay byte-identical.

Usage:
    python normalize_zh.py                     # dry run, all scripts, dialogue zh only
    python normalize_zh.py --apply             # rewrite script.json in place
    python normalize_zh.py --meta 1 55         # also fix title_zh/story_hook/youtube_*
    python normalize_zh.py --dir ai_scripts --json

`--apply` 会整档重写 script.json，所以只能对「当前没有 agent 在改」的编号跑
（用尾部编号参数限定范围，别把正在审阅的脚本卷进去）。

Exit（dry run）：0 已闭合 / 2 有待繁化行；Exit（--apply）：0 幂等闭合 / 1 仍有残留
（非幂等，需查 fix 表）；3 用法错误。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skill_common as SC  # noqa: E402

from opencc import OpenCC

ROOT = SC.ROOT
OUT_DIR = ROOT / "ai_scripts_hot"

CC = OpenCC("s2t")

# OpenCC s2t prefers variants that Taiwan standard writing does not use.
VARIANT_FIX = {
    "喫": "吃", "臺": "台", "檯": "台", "裏": "裡", "纔": "才", "牀": "床",
    "羣": "群", "瞭": "了", "峯": "峰", "皁": "皂", "覈": "核", "迴": "回",
    "脣": "唇", "兇": "凶", "齣": "出", "麪": "麵",
    # forms OpenCC leaves alone but Taiwan standard writing spells differently
    "着": "著", "爲": "為", "説": "說", "啓": "啟", "枱": "台", "衆": "眾",
    # s2t over-promotes two words: 風采 keeps 采, and the guy suffix is 傢伙
    # (傢 belongs to 傢具/傢俱 only).
    "風採": "風采", "傢夥": "傢伙",
    # glyphs OpenCC never touches but Big5 cannot encode (see audit ZH_BAD_GLYPHS)
    "〇": "○", "⋯": "…",
}
WATCH_BEFORE = set("手腕帶時鐘錶")
# 隻 is only ever written deliberately here (reviewers type the classifier by
# hand). No "only" adverb can be spelled 隻 in this corpus, so the old
# downgrade-by-context rule was a pure regression: its numeral set never covered
# 這/那/有/兩/五…, so 「這隻鯊魚」 and 「五隻」 both came out broken.

# Taiwan-standard forms that OpenCC would otherwise "promote" to rare variants
# (吃->喫, 台->臺, 裡->裏 ...). Shielding them keeps the pass idempotent.
PROTECT = {
    "吃": "\ue000", "台": "\ue001", "裡": "\ue002", "才": "\ue003",
    "床": "\ue004", "群": "\ue005", "峰": "\ue006", "皂": "\ue007",
    "核": "\ue008", "回": "\ue009", "唇": "\ue00a", "凶": "\ue00b",
    "出": "\ue00c", "麵": "\ue00d", "了": "\ue00e",
    # s2t rewrites 游泳 -> 遊泳-style forms and 紀念 -> 紀唸; both are wrong for
    # Taiwan standard writing, and reviewers had already corrected them by hand.
    "游": "\ue00f", "念": "\ue010",
    # s2t promotes every 只 to 隻, which breaks the adverb ("只能/只剩/只有/只要/
    # 只收"). Context can't tell the two apart either: 「有隻狗」 (classifier) and
    # 「有只給小孩的房間」 (adverb) share the same neighbours. So leave 只/隻 as
    # written — both are Big5-clean and reviewers already read every line.
    "只": "\ue011",
}
RESTORE = {v: k for k, v in PROTECT.items()}


def fix_contextual(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        prev = text[i - 1] if i else ""
        if ch == "錶" and prev not in WATCH_BEFORE:
            ch = "表"
        out.append(ch)
    return "".join(out).replace("濃鬱", "濃郁")


def normalize(text: str) -> str:
    shielded = "".join(PROTECT.get(ch, ch) for ch in text)
    converted = CC.convert(shielded)
    for bad, good in VARIANT_FIX.items():
        converted = converted.replace(bad, good)
    converted = "".join(RESTORE.get(ch, ch) for ch in converted)
    return fix_contextual(converted)


# title_quote must mirror dialogue[0].text, and these carry no Chinese at all.
META_SKIP = {"dialogue", "title_quote", "channel_id", "cefr"}


def iter_meta(script):
    """Yield (setter, old value) for every top-level Chinese-bearing string."""
    def setter(key, index=None):
        def _set(value):
            if index is None:
                script[key] = value
            else:
                script[key][index] = value
        return _set

    for key, value in script.items():
        if key in META_SKIP:
            continue
        if isinstance(value, str):
            yield setter(key), value
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, str):
                    yield setter(key, i), item


def main() -> int:
    argv = sys.argv[1:]
    as_json = "--json" in argv
    apply = "--apply" in argv
    with_meta = "--meta" in argv
    dir_arg = argv[argv.index("--dir") + 1] if "--dir" in argv else None
    if "--dir" in argv:
        i = argv.index("--dir")
        del argv[i:i + 2]
    nums = {int(a) for a in argv if a.isdigit()}
    out_dir = SC.resolve_dir(dir_arg, "ai_scripts_hot") if dir_arg else OUT_DIR
    if not out_dir.is_dir():
        print(f"ERROR: {out_dir} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    paths = [p for p in sorted(out_dir.glob("*/script.json"))
             if not nums or int(re.match(r"^\d+", p.parent.name).group()) in nums]
    if not paths:
        print(f"ERROR: no script.json under {out_dir}"
              + (f" matching {sorted(nums)}" if nums else ""), file=sys.stderr)
        return SC.EXIT_USAGE
    total_lines = total_meta = 0
    for path in paths:
        script = json.loads(path.read_text(encoding="utf-8"))
        lines = meta = 0
        for line in script["dialogue"]:
            new = normalize(line["zh"])
            if new != line["zh"]:
                line["zh"] = new
                lines += 1
        if with_meta:
            for set_value, old in list(iter_meta(script)):
                new = normalize(old)
                if new != old:
                    set_value(new)
                    meta += 1
        total_lines += lines
        total_meta += meta
        if lines or meta:
            SC.line(f"{'applied' if apply else 'pending'} {path.parent.name}: "
                    f"{lines} zh lines, {meta} meta fields", as_json)
        if apply and (lines or meta):
            path.write_text(
                json.dumps(script, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    leftover = 0
    if apply:
        for path in paths:
            script = json.loads(path.read_text(encoding="utf-8"))
            for line in script["dialogue"]:
                if normalize(line["zh"]) != line["zh"]:
                    leftover += 1
            if with_meta:
                for _, old in iter_meta(script):
                    if normalize(old) != old:
                        leftover += 1
    scope = "zh + meta" if with_meta else "zh"
    pending = leftover if apply else (total_lines + total_meta)
    summary = (f"OK {len(paths)} scripts, {total_lines} zh lines + {total_meta} meta fields "
               f"{'rewritten' if apply else 'to rewrite'} ({scope}), "
               f"{'not-idempotent fields: ' + str(leftover) if apply else 'dry-run'}")
    SC.line(summary, as_json)
    findings = []
    if pending:
        findings.append(SC.finding(
            "warning" if not apply else "error", "zh-pending",
            f"{pending} 处{'未闭合（非幂等）' if apply else '待繁化'}"
            + (f"（目录 {out_dir}）" if dir_arg else ""),
            stage="字幕字形",
            fix=("python .qoder/skills/hot-sleep-dialogues/scripts/normalize_zh.py --apply"
                 if not apply else "检查 VARIANT_FIX / PROTECT 表，转换后应幂等")))
    if as_json:
        SC.emit_json(SC.envelope("normalize_zh", out_dir, findings, checked=len(paths),
                                 summary=summary,
                                 errors=leftover if apply else 0,
                                 warnings=0 if apply else total_lines + total_meta,
                                 extra={"applied": apply, "meta": with_meta,
                                        "zh_lines": total_lines, "meta_fields": total_meta,
                                        "leftover": leftover}))
    if apply:
        return SC.EXIT_BLOCK if leftover else SC.EXIT_OK
    return SC.EXIT_WARN if pending else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
