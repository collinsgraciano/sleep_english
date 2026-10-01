#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验 ai_scripts 下所有 script.json 的 dialogue 行数与四字段非空。"""
import json
from pathlib import Path

ROOT = Path("ai_scripts")


def main():
    ok = 0
    bad = []
    minl = 10 ** 9
    maxl = 0
    for f in sorted(ROOT.iterdir()):
        sj = f / "script.json"
        if not sj.exists():
            bad.append((f.name, "no script.json"))
            continue
        try:
            d = json.loads(sj.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append((f.name, "parse:" + str(e)))
            continue
        dl = d.get("dialogue", [])
        minl = min(minl, len(dl))
        maxl = max(maxl, len(dl))
        if len(dl) < 400:
            bad.append((f.name, "lines=" + str(len(dl))))
            continue
        row_bad = None
        for i, r in enumerate(dl):
            if not (r.get("text", "").strip() and r.get("zh", "").strip()
                    and r.get("phonetic", "").strip() and r.get("speaker", "")):
                row_bad = i
                break
        if row_bad is not None:
            bad.append((f.name, "row" + str(row_bad)))
            continue
        ok += 1
    print("folders checked:", len(list(ROOT.iterdir())))
    print("valid (400 lines, full schema):", ok)
    print("min/max dialogue lines:", minl, maxl)
    print("bad:", bad)


if __name__ == "__main__":
    main()
