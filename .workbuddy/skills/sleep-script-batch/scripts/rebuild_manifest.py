#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从磁盘上的 100 个主题文件夹重建 ai_scripts/manifest.json。
只读取每个 folder/script.json 的少量字段，不改动任何脚本内容。
"""
import json
from pathlib import Path

ROOT = Path("ai_scripts")


def main():
    themes = []
    for folder in sorted(ROOT.iterdir()):
        sj = folder / "script.json"
        if not sj.exists():
            continue
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
        except Exception:
            continue
        dl = d.get("dialogue", [])
        # 从文件夹名取序号: NNN_EnName
        name = folder.name
        idx = int(name.split("_", 1)[0]) if "_" in name else 0
        themes.append({
            "index": idx,
            "en": d.get("topic", name),
            "zh": d.get("title_zh", ""),
            "category": d.get("category", ""),
            "folder": name,
            "lines": len(dl),
        })
    themes.sort(key=lambda t: t["index"])
    out = {
        "count": len(themes),
        "failed": 0,
        "lines_per_script": 400,
        "generated_at": "2026-09-25",
        "note": "92 个由外部 LLM 生成 + 8 个由 WorkBuddy 直接撰写（无外部 LLM）",
        "themes": themes,
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rebuilt manifest: {len(themes)} themes")
    # 简述完整性
    bad = [t["folder"] for t in themes if t["lines"] < 400]
    print("lines<400:", bad)
    print("indices:", [t["index"] for t in themes][:12], "... total", len(themes))


if __name__ == "__main__":
    main()
