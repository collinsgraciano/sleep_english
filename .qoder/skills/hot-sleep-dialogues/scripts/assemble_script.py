"""Assemble meta.json + pairs_XX.json chunks -> script.json (validation only, no content generation).

Usage: python assemble_script.py <part_dir> [pairs] [--json]   # e.g. ai_scripts_hot/_parts/001_Folder 400
       pairs = 对话组数（默认 400 组 = 800 行；旧批次可传 200）
Output: <library>/<same-folder-name>/script.json，并把这一篇 upsert 进同目录
        manifest.json（控制台只认 manifest，装配完就得登记，别等批次收尾）
Prints 'OK ...' on success, error lines + exit 1 on failure.
Exit 0 OK / 1 校验失败 / 3 用法错误。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_manifest as BM  # noqa: E402  同一套 manifest schema，别各写一份
import skill_common as SC    # noqa: E402

REQUIRED_PAIR_KEYS = ("text", "phonetic", "zh")
DEFAULT_PAIRS = 400


def _err_pair(idx, side, msg):
    return f"pair #{idx} ({side}): {msg}"


def load_pairs(part_dir: Path):
    chunk_files = sorted(part_dir.glob("pairs_*.json"))
    if not chunk_files:
        raise SystemExit(f"ERROR: no pairs_*.json in {part_dir}")
    pairs = []
    for cf in chunk_files:
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: {cf.name} invalid JSON: {e}")
        if not isinstance(data, list):
            raise SystemExit(f"ERROR: {cf.name} must be a JSON array")
        pairs.extend(data)
    return pairs, chunk_files


def validate_pairs(pairs, expected_pairs):
    errors = []
    if len(pairs) != expected_pairs:
        errors.append(f"expected {expected_pairs} pairs, got {len(pairs)}")
    seen = set()
    for i, p in enumerate(pairs, 1):
        if not isinstance(p, dict) or "a" not in p or "b" not in p:
            errors.append(_err_pair(i, "ab", "must have keys a and b"))
            continue
        for side in ("a", "b"):
            line = p[side]
            if not isinstance(line, dict):
                errors.append(_err_pair(i, side, "not an object"))
                continue
            for k in REQUIRED_PAIR_KEYS:
                v = line.get(k)
                if not isinstance(v, str) or not v.strip():
                    errors.append(_err_pair(i, side, f"field '{k}' missing/empty"))
            ph = (line.get("phonetic") or "").strip()
            if ph and not (ph.startswith("/") and ph.endswith("/") and len(ph) > 2):
                errors.append(_err_pair(i, side, "phonetic must be /.../ wrapped"))
            txt = (line.get("text") or "").strip().lower()
            if txt:
                if txt in seen:
                    errors.append(_err_pair(i, side, f"duplicate sentence: {txt!r}"))
                seen.add(txt)
    return errors


def main(part_dir: str, expected_pairs: int = DEFAULT_PAIRS, as_json: bool = False):
    part_dir = BM.resolve_dir(part_dir)
    if not part_dir.is_dir():
        print(f"ERROR: not a directory: {part_dir}", file=sys.stderr)
        return SC.EXIT_USAGE
    meta_path = part_dir / "meta.json"
    if not meta_path.exists():
        print(f"ERROR: missing {meta_path}", file=sys.stderr)
        return SC.EXIT_USAGE
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: meta.json invalid JSON: {e}", file=sys.stderr)
        return SC.EXIT_BLOCK

    try:
        pairs, chunk_files = load_pairs(part_dir)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return SC.EXIT_BLOCK
    errors = validate_pairs(pairs, expected_pairs)
    if errors:
        for e in errors[:40]:
            print(f"ERROR: {e}", file=sys.stderr)
        print(f"ERROR: {len(errors)} problem(s) in {len(pairs)} pairs from "
              f"{len(chunk_files)} chunk(s)", file=sys.stderr)
        return SC.EXIT_BLOCK

    dialogue = []
    for p in pairs:
        dialogue.append({"speaker": "char_a", **{k: p["a"][k].strip()
                         for k in REQUIRED_PAIR_KEYS}})
        dialogue.append({"speaker": "char_b", **{k: p["b"][k].strip()
                         for k in REQUIRED_PAIR_KEYS}})

    script = dict(meta)
    folder = part_dir.name
    if re.match(r"^\d+_", folder):
        script.setdefault("topic", re.sub(r"^\d+_", "", folder).replace("_", " "))
    topic = script.get("topic", folder)
    script.setdefault("title", f"Everyday Phrases — {topic}")
    # title_quote 按 schema 必须就是第一句（卡片/字幕直接拿它当引用句）。
    # 用 setdefault 会保留 agent 手写的那句，实测 011/014 都填成了本篇后段的台词，
    # 而控制台体检不查这一项，所以这里硬性对齐并说明，别留给 reviewer 返工。
    quote = dialogue[0]["text"]
    if "title_quote" in script and str(script["title_quote"]).strip() != quote:
        SC.line(f"NOTE: meta title_quote {script['title_quote']!r} != dialogue[0], "
                f"synced to first line", as_json)
    script["title_quote"] = quote
    script["structure"] = "sleep"
    script["lesson_type"] = "listening"
    script["dialogue"] = dialogue

    for key in ("title", "topic", "youtube_title", "youtube_description",
                "category", "cefr", "thumb_badge", "thumb_main", "thumb_hook"):
        if not str(script.get(key, "")).strip():
            print(f"ERROR: meta.json field '{key}' is empty", file=sys.stderr)
            return SC.EXIT_BLOCK
    if not isinstance(script.get("youtube_tags"), list) or not script["youtube_tags"]:
        print("ERROR: meta.json 'youtube_tags' must be a non-empty array", file=sys.stderr)
        return SC.EXIT_BLOCK

    out_dir = part_dir.parent.parent / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "script.json"
    out_path.write_text(json.dumps(script, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    registered = BM.upsert(out_dir.parent, BM.entry_of(folder, script))
    summary = (f"OK {out_path} ({len(dialogue)} lines, {len(pairs)} pairs, "
               f"chunks={[f.name for f in chunk_files]})")
    SC.line(summary, as_json)
    SC.line(f"   registered -> {registered}", as_json)
    if as_json:
        SC.emit_json(SC.envelope(
            "assemble_script", out_dir.parent, [], checked=len(pairs),
            summary=summary,
            extra={"script": str(out_path), "folder": folder,
                   "lines": len(dialogue), "pairs": len(pairs),
                   "chunks": [f.name for f in chunk_files],
                   "manifest": str(registered)}))
    return SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    argv = sys.argv[1:]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    if not argv or len(argv) > 2:
        print(__doc__, file=sys.stderr)
        sys.exit(SC.EXIT_USAGE)
    n = int(argv[1]) if len(argv) == 2 else DEFAULT_PAIRS
    if not 10 <= n <= 400:
        print(f"ERROR: pairs must be 10-400 (pipeline clamp), got {n}", file=sys.stderr)
        sys.exit(SC.EXIT_USAGE)
    sys.exit(main(argv[0], n, as_json=as_json))
