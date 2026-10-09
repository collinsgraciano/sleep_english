"""背景图 prompt 组装回归测试（零成本，不调用生图）。

跑法：
    python pipeline/sleep/test_bg_image.py
无 pytest 依赖（纯 stdlib），便于在没有测试框架的仓库里直接跑。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # pipeline/

from sleep.bg_image import (  # noqa: E402
    build_bg_prompt, is_generic_subject, pick_bg_subject,
)

_FAILS: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        _FAILS.append(label)


# 重构前 build_bg_prompt 的输出（逐字符硬编码，防止 mood_first 回退被改动）
LEGACY_EXPECTED = (
    "Calm dreamy pastel scene related to: 取快遞., Soft muted colors, gentle diffused "
    "night lighting, peaceful relaxing sleep atmosphere, clean simple composition, "
    "wide shot., no text, no words, no letters, no watermark, no people, STYLE_X"
)


def main() -> int:
    print("=== 1) 泛化标签判定 ===")
    for v, want in [("情境對話", True), ("客服 · 情境對話", True), ("日常對話", True),
                    ("", True), ("   ", True), ("大學宿舍", False),
                    ("Berry Picking at a Farm", False), ("A Piano Lesson", False),
                    ("取快遞", False), ("情境 · 取快遞", False)]:
        got = is_generic_subject(v)
        check(got == want, f"is_generic_subject({v!r}) == {want}", f"got={got}")

    print("=== 2) 主体优先级（主题优先）===")
    s, src = pick_bg_subject("A Piano Lesson", "情境對話", "Everyday Phrases — A Piano Lesson")
    check(s == "A Piano Lesson" and src == "topic", "topic 非泛化 → 用 topic", f"{s!r}/{src}")
    s, src = pick_bg_subject("", "Berry Picking at a Farm", "x")
    check(s == "Berry Picking at a Farm" and src == "scene", "topic 空 → 用 scene", f"{s!r}/{src}")
    s, src = pick_bg_subject("情境對話", "客服 · 情境對話", "Everyday Phrases — Moving into a Dorm")
    check(s == "Moving into a Dorm" and src == "title", "都泛化 → 用去掉前缀的 title", f"{s!r}/{src}")
    s, src = pick_bg_subject("", "", "")
    check(s == "everyday life" and src == "fallback", "全空 → fallback", f"{s!r}/{src}")
    s, _ = pick_bg_subject("Choosing a Streaming Plan", "串流方案", "")
    check(s == "Choosing a Streaming Plan", "topic 与 scene 都具体 → topic 胜（不混中英）", f"{s!r}")

    print("=== 3) topic_first prompt 内容 ===")
    p = build_bg_prompt("情境對話", "STYLE_X", topic_en="A Piano Lesson",
                        scene="情境對話", title="Everyday Phrases — A Piano Lesson",
                        style_mode="topic_first", allow_people=True)
    check("A Piano Lesson" in p, "含主题")
    check("Wide establishing shot" in p, "含主体段")
    check("clearly recognizable" in p, "要求主体可辨")
    for bad in ("night lighting", "sleep atmosphere", "relaxing sleep", "Soft muted colors"):
        check(bad not in p, f"不再出现旧氛围词 {bad!r}")
    check("no text" in p and "no watermark" in p, "保留无文字水印约束")
    check("no people" not in p, "默认允许人物")
    check(p.rstrip().endswith("STYLE_X") and "Follow the art style below strictly" in p,
          "风格片段收尾且声明优先")
    p2 = build_bg_prompt("情境對話", "STYLE_X", topic_en="A Piano Lesson",
                         allow_people=False)
    check("no people" in p2, "allow_people=False → 禁止人物")

    print("=== 4) mood_first 与重构前逐字符一致 ===")
    got = build_bg_prompt("取快遞", "STYLE_X", topic_en="Collecting a Package",
                          scene="取快遞", title="Everyday Phrases — Collecting a Package",
                          style_mode="mood_first", allow_people=True)
    check(got == LEGACY_EXPECTED, "mood_first == 旧 prompt", f"got={got[:80]}…")
    got2 = build_bg_prompt("取快遞", "", style_mode="mood_first")
    check(got2 == LEGACY_EXPECTED.replace(", STYLE_X", ""),
          "mood_first 无风格片段时一致")
    got3 = build_bg_prompt("", "", style_mode="mood_first")
    check(got3.startswith("Calm dreamy pastel scene related to: everyday life."),
          "mood_first 空主体 → everyday life")

    print()
    if _FAILS:
        print(f"===== 失败 {len(_FAILS)} 项：{_FAILS} =====")
        return 1
    print("===== 背景图 prompt 测试全部通过 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
