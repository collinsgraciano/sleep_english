"""出片就绪预检：在花时间/额度跑一次 pipeline 之前，先问清「能不能出片、会降级成什么」。

三段流水线，**复用已有实现、不重写任何规则**：

* A 结构段 —— 管理页同一套体检（`app.ai_scripts_admin.validate_script`）；目标目录不在
  `AI_SCRIPTS_DIRS` 或 app 不可导入时，退回 `validate_all.check_script` 的本地副本。
* B 工艺段 —— `audit_script.audit_one()`（同一套机器审计规则，不重复实现）。
* C 就绪段 —— 本工具独有：sleep 出片路径真正会在哪一步炸 / 在哪一步静默降级。

分级：
  🔴 error   会崩/会静默丢内容（整次运行中止，或「跑成功但少播」）
  🟡 warning 会降级（能出片，但卡片缩字/缩略图退化/背景泛化/音色回退）
  ·  note    提示（成本、输出目录名）

Usage:
    python preflight.py [dir] [--json] [--expect-lines 800] [--audit] [--sequence <编排>]
Exit 0 就绪 / 1 有 blocker / 2 仅降级提示 / 3 用法错误。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_script as AU     # noqa: E402
import skill_common as SC     # noqa: E402
import validate_all as VA     # noqa: E402

# sleep 出片步骤的「唯一事实源」是 app/pipeline_service.STEP_DEFS；导入失败时用这份
# 副本（值必须与 STEP_DEFS 一致，改了那边记得同步这里）。
_FALLBACK_STEPS = [
    ("step0_script", "Step 0", "LLM 脚本生成 / 预生成脚本载入"),
    ("step1_mcp", "Step 1", "MCP 初始化"),
    ("step2_images_tts", "Step 2", "本地 TTS + 可选背景图"),
    ("step3_video", "Step 3", "视频片段生成"),
    ("step4_timeline", "Step 4", "时间轴 + SRT"),
    ("step45_thumbnail", "Step 4.5", "YouTube 元数据 + 缩略图"),
    ("step5_compose", "Step 5", "卡片块合成 + concat"),
    ("step55_bgm", "Step 5.5", "BGM 版权音乐混合"),
    ("step6_4k", "Step 6", "4K 超分"),
]

# 会崩：这些字段为 null 时下游不是"取默认值"而是直接抛（缺键安全、null 会崩）
NULL_UNSAFE = ("title", "title_zh", "topic", "youtube_title", "youtube_description",
               "category", "cefr", "scene", "structure")
QUALITY_FIELDS = ("title", "title_zh", "topic", "youtube_title", "category", "cefr", "scene")
THUMB_FIELDS = ("thumb_badge", "thumb_main", "thumb_hook", "thumbnail_subtitle")
PAIR_STEPS = 5          # audio_sleep.pair_steps：a_m/b_m/b_f/a_slow/b_slow
STANDARD_LINES = (400, 800)


def engine_steps_root():
    """项目根（标记法）—— 复用 skill_common，不重复实现。"""
    return SC.ROOT


def steps_table():
    try:
        sys.path.insert(0, str(SC.ROOT))
        from app.pipeline_service import STEP_DEFS  # type: ignore
        return [(sid, num, name) for sid, num, name, _ in STEP_DEFS], "app/pipeline_service.STEP_DEFS"
    except Exception:
        return _FALLBACK_STEPS, "local fallback"


def _is_unspeakable(text) -> bool:
    """TTS 会抛 'produced no audio' 的台词：非字符串 / 空白 / 不含任何英文字母。"""
    if not isinstance(text, str):
        return True
    return not re.search(r"[A-Za-z]", text)


def console_findings(root: Path, folders: list[str]):
    """A 段：管理页同一套体检。返回 (findings, source) 或 (None, why)。"""
    try:
        sys.path.insert(0, str(SC.ROOT))
        from app import ai_scripts_admin as admin  # type: ignore
        from app.ai_scripts import AI_SCRIPTS_DIRS  # type: ignore
    except Exception as exc:
        return None, f"app 不可导入（{type(exc).__name__}）"
    if root.resolve() not in {d.resolve() for d in AI_SCRIPTS_DIRS}:
        return None, "目标目录不在 app 登记的预生成目录里"
    out = []
    for f in folders:
        r = admin.validate_script(f)
        for e in r.get("errors", []):
            out.append(SC.finding("error", "console-meta", f"{f}: {e}", stage="结构"))
        for w in r.get("warnings", []):
            out.append(SC.finding("warning", "console-warn", f"{f}: {w}", stage="结构"))
    return out, "app/ai_scripts_admin.validate_script"


def _core(detail: str) -> str:
    """去掉 'folder: ' 前缀，用于与 A 段去重（同一条别报两遍）。"""
    return re.sub(r"^[^:]{1,60}:\s*", "", detail).strip().lower()


def render_findings(folder: str, script: dict, cap: int, batch_lines: int = 0) -> list[dict]:
    """C 段：sleep 出片路径的就绪检查（本工具独有）。"""
    out: list[dict] = []

    def add(level, code, detail, stage="就绪", fix="", lines=None):
        out.append(SC.finding(level, code, f"{folder}: {detail}", stage=stage,
                              fix=fix, lines=lines))

    if not isinstance(script, dict):
        add("error", "json", "script.json 不是 JSON 对象", stage="结构")
        return out
    for key in NULL_UNSAFE:
        if key in script and script[key] is None:
            add("error", "null-meta", f"顶层字段 '{key}' 是 null（缺键安全、null 会崩）",
                stage="结构", fix=f"删掉该键或补上非空值")
    dialogue = script.get("dialogue")
    if not isinstance(dialogue, list) or not dialogue:
        add("error", "dialogue-missing", "dialogue 缺失或为空 → 预生成脚本会被直接拒跑",
            stage="Step 0")
        return out

    n = len(dialogue)
    if n > 800:
        add("error", "pairs-clamped",
            f"{n} 行 → 组数被钳到上限 400，只播前 800 行，尾部 {n - 800} 行被静默丢弃"
            f"（app/pipeline_service._seed_from_ai_scripts 按 len(dialogue)//2 同步组数）",
            stage="Step 0", fix="拆成两篇或压到 800 行以内")
    if n < 20:
        add("warning", "too-few-lines",
            f"{n} 行不足 10 组（pipeline 下限）：实际只播 {n // 2} 组",
            stage="Step 0", fix="补到至少 20 行（10 组）")
    if n % 2:
        add("warning", "odd-lines",
            f"{n} 行是奇数：A/B 配对按奇偶切片（audio_sleep 用 dialogue[0::2]/[1::2]），"
            f"最后一行不会被朗读",
            stage="Step 2", fix="删掉或补上最后一行，保持成对")
    if n not in STANDARD_LINES:
        add("warning", "nonstandard-lines",
            f"{n} 行不在控制台认可的标准行数 {list(STANDARD_LINES)}（旧批次只记 warn）",
            stage="结构")

    empty_text, unspeakable, null_line, bad_speaker, missing_zh, missing_ph = \
        [], [], [], [], [], []
    for i, line in enumerate(dialogue):
        if not isinstance(line, dict):
            null_line.append(i + 1)
            continue
        null_here = [k for k in ("text", "zh", "phonetic") if k in line and line[k] is None]
        if null_here:
            null_line.append(i + 1)
        text = line.get("text")
        if "text" not in null_here:
            if _is_unspeakable(text):
                (empty_text if not str(text or "").strip() else unspeakable).append(i + 1)
        if "zh" not in null_here and not str(line.get("zh") or "").strip():
            missing_zh.append(i + 1)
        if "phonetic" not in null_here and not str(line.get("phonetic") or "").strip():
            missing_ph.append(i + 1)
        want = "char_a" if i % 2 == 0 else "char_b"
        if line.get("speaker") != want:
            bad_speaker.append(i + 1)

    if null_line:
        add("error", "null-or-nonobject-line",
            f"{len(null_line)} 行不是对象或含 null 字段（TTS/卡片直接炸）",
            stage="Step 2", lines=null_line,
            fix="每行必须是 {speaker,text,phonetic,zh} 四个非 null 字符串")
    if empty_text:
        add("error", "empty-dialogue-text",
            f"{len(empty_text)} 行 text 为空或纯空白 → TTS 抛 'produced no audio'",
            stage="Step 2", lines=empty_text, fix="补写这一行的英文台词")
    if unspeakable:
        add("error", "unspeakable-dialogue-text",
            f"{len(unspeakable)} 行 text 不含任何英文字母（纯标点/数字）→ TTS 抛错",
            stage="Step 2", lines=unspeakable, fix="改写成真正的英文句子")
    if missing_zh:
        add("warning", "missing-zh", f"{len(missing_zh)} 行 zh 为空（字幕/卡片中文缺失）",
            stage="Step 2", lines=missing_zh)
    if missing_ph:
        add("warning", "missing-phonetic",
            f"{len(missing_ph)} 行 phonetic 为空（音标字幕缺失）",
            stage="Step 2", lines=missing_ph)
    if bad_speaker:
        add("warning", "bad-speaker",
            f"{len(bad_speaker)} 行 speaker 与奇偶不符（出片按奇偶切片，字段被忽略；"
            f"但管理页体会报 error）", stage="Step 2", lines=bad_speaker,
            fix="偶数行 char_a、奇数行 char_b")
    if script.get("structure") != "sleep":
        add("warning", "structure-mismatch",
            f"structure={script.get('structure')!r} 不是 'sleep'"
            f"（预生成路径会被覆写成当前模式；管理页体检报 error）", stage="结构")
    if batch_lines and n != batch_lines:
        add("warning", "batch-lines-mismatch",
            f"{n} 行 ≠ 本批目标 {batch_lines} 行（控制台标准行数 "
            f"{list(STANDARD_LINES)} 都收；这条只说明它不是本批规模）", stage="结构")

    for key in QUALITY_FIELDS:
        if not str(script.get(key) or "").strip():
            add("warning", "empty-quality-field", f"顶层字段 '{key}' 为空",
                stage="Step 4.5" if key in ("title", "title_zh", "youtube_title") else "Step 2",
                fix="补上非空值（为空不报错，但会静默降级）")
    for key in THUMB_FIELDS:
        if not str(script.get(key) or "").strip():
            add("warning", "thumbnail-default",
                f"'{key}' 为空 → thumbnail_gen 用内置默认文案顶替", stage="Step 4.5")
    for key in ("char_a_gender", "char_b_gender"):
        if not str(script.get(key) or "").strip():
            add("warning", "voice-gender-default",
                f"'{key}' 为空 → build_voice_map 走性别默认回退", stage="Step 2")
    if not script.get("youtube_tags"):
        add("warning", "empty-tags", "'youtube_tags' 为空（SEO 退化）", stage="Step 4.5")
    try:
        from app.ai_scripts_admin import _non_big5_chars  # type: ignore
        bad = set()
        for line in dialogue:
            bad |= set(_non_big5_chars(str(line.get("zh") or "")) if isinstance(line, dict) else "")
        if bad:
            add("warning", "zh-non-big5",
                f"{len(bad)} 个非正体字形出现在 zh：「{''.join(sorted(bad)[:12])}」",
                stage="字幕字形", fix="跑 normalize_zh.py --apply 繁化")
    except Exception:
        pass
    return out


def cost_note(folder: str, script: dict) -> dict:
    n = len(script.get("dialogue") or [])
    pairs = max(10, min(400, n // 2))
    segments = pairs * PAIR_STEPS + 2       # + intro_sleep.mp3 / outro_sleep.mp3
    yt = str(script.get("youtube_title") or script.get("title")
             or script.get("topic") or folder)
    try:
        sys.path.insert(0, str(SC.ROOT / "pipeline"))
        from media_utils import safe_filename  # type: ignore
        out_dir = safe_filename(yt, str(script.get("topic") or folder))
    except Exception:
        out_dir = re.sub(r"[\\/:*?\"'<>|]", "", yt)[:80]
    return SC.finding("note", "cost",
                      f"{folder}: {pairs} 组 · TTS {segments} 段（本地零积分，"
                      f"按默认序列 {PAIR_STEPS} 段/组 + 片头片尾）· 输出目录 {out_dir}")


def main() -> int:
    argv = [a for a in sys.argv[1:]]
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    expect_lines = 800
    expect_explicit = False
    if "--expect-lines" in argv:
        i = argv.index("--expect-lines")
        if i + 1 >= len(argv):
            print("ERROR: --expect-lines needs a value", file=sys.stderr)
            return SC.EXIT_USAGE
        try:
            expect_lines = int(argv[i + 1])
            expect_explicit = True
        except ValueError:
            print("ERROR: --expect-lines must be an integer", file=sys.stderr)
            return SC.EXIT_USAGE
        del argv[i:i + 2]
    pos = [a for a in argv if not a.startswith("--")]

    root = SC.resolve_dir(pos[0] if pos else None, "ai_scripts_hot")
    if not root.is_dir():
        print(f"ERROR: {root} not found", file=sys.stderr)
        return SC.EXIT_USAGE
    paths = SC.iter_scripts(root)
    if not paths:
        print(f"ERROR: no NNN_*/script.json under {root}", file=sys.stderr)
        return SC.EXIT_USAGE
    folders = [p.parent.name for p in paths]

    steps, steps_src = steps_table()
    cap = AU.word_cap()
    SC.line(f"# 规则：句长上限 SLEEP_MAX_LINE_WORDS={cap}（与 app/script_library.py 同源）"
            f" · 步骤表来自 {steps_src}", as_json)

    # A 段：先一次性跑管理页体检（app 不可用时退回本地副本）
    a_findings, a_src = console_findings(root, folders)
    if a_findings is None:
        a_findings = []
        for name, p in zip(folders, paths):
            script = SC.load_script(p)
            if script is None:
                a_findings.append(SC.finding("error", "json", f"{name}: script.json 不可读",
                                             stage="结构"))
                continue
            errs, warns = VA.check_script(p, expect_lines)
            for f in errs + warns:
                a_findings.append({**f, "detail": f"{name}: {f['detail']}"})
        a_src = f"local fallback（{a_src}）"
    SC.line(f"# A 结构段：{a_src}", as_json)

    # B 段：机器审计（一次构建跨篇索引，逐篇复用）
    corpus = {p.parent.name: (SC.load_script(p) or {}).get("dialogue") for p in paths}
    ctx = AU.build_context(corpus, do_near=True)

    results = []
    all_findings: list[dict] = []
    for name, p in zip(folders, paths):
        script = SC.load_script(p)
        if script is None:
            all_findings.append(SC.finding("error", "json", f"{name}: script.json 不可读",
                                           stage="结构"))
            continue
        b_issues, _counts = AU.audit_one(name, script, ctx, cap=cap)
        b_findings = AU.findings_of(name, b_issues)
        c_findings = render_findings(name, script, cap,
                                      batch_lines=expect_lines if expect_explicit else 0)
        note = cost_note(name, script)

        # 去重：A 段与管理页同源，B/C 段对同一篇已给出的细节不再重复
        #（按去掉 folder 前缀的核心文本比）
        seen = {_core(f["detail"]) for f in b_findings + c_findings}
        a_kept = [f for f in a_findings
                  if str(f["detail"]).startswith(name + ": ")
                  and _core(f["detail"]) not in seen]
        findings = a_kept + b_findings + c_findings + [note]
        errors = [f for f in findings if f["level"] == "error"]
        warns = [f for f in findings if f["level"] == "warning"]
        all_findings.extend(SC.budget(findings, low_max=8))
        results.append({"folder": name, "lines": len(script.get("dialogue") or []),
                        "errors": len(errors), "warnings": len(warns),
                        "path": str(p)})

    # 批级：输出目录同名冲突（safe_filename 截断 80 字符后不再消歧）
    try:
        sys.path.insert(0, str(SC.ROOT / "pipeline"))
        from media_utils import safe_filename  # type: ignore
        names: dict[str, list[str]] = {}
        for name, p in zip(folders, paths):
            s = SC.load_script(p) or {}
            yt = str(s.get("youtube_title") or s.get("title") or s.get("topic") or name)
            names.setdefault(safe_filename(yt, str(s.get("topic") or name)), []).append(name)
        for out_name, group in names.items():
            if len(group) > 1:
                all_findings.append(SC.finding(
                    "warning", "output-dir-collision",
                    f"输出目录同名：{group} 都会写进 output/<mode>/{out_name}"
                    f"（mkdir exist_ok，不报错，后跑的会覆盖前一次的运行目录）",
                    stage="Step 0", fix="改 youtube_title 使其前 80 字符不同"))
    except Exception:
        pass

    n_err = sum(r["errors"] for r in results)
    n_warn = sum(r["warnings"] for r in results)
    clean = sum(1 for r in results if not r["errors"])

    # 人读报告
    SC.line("\n" + "─" * 72, as_json)
    verdict = "🔴 有 blocker" if n_err else ("🟡 就绪（有降级项）" if n_warn else "🟢 完全就绪")
    SC.line(f"出片就绪预检 · {len(results)} 篇 · {verdict}", as_json)
    for r in results:
        tag = "🔴" if r["errors"] else ("🟡" if r["warnings"] else "🟢")
        SC.line(f"  {tag} {r['folder']}  {r['lines']} 行 · "
                f"{r['errors']} blocker / {r['warnings']} 降级提示", as_json)
    for level, title in (("error", "会崩"), ("warning", "会降级")):
        rows = SC.budget([f for f in all_findings if f["level"] == level], low_max=10)
        if rows:
            SC.line(f"── {title}", as_json)
            for f in rows:
                SC.line(f"  {'✗' if level == 'error' else '!'} [{f.get('stage','-')}] "
                        f"{f['detail']}" + (f"  → {f['fix']}" if f.get("fix") else ""), as_json)
    notes = [f for f in all_findings if f["level"] == "note"]
    if notes:
        SC.line("── 渲染就绪", as_json)
        for sid, num, name in steps:
            SC.line(f"  ✓ {num:<8} {name}", as_json)
        for f in notes[:3]:
            SC.line(f"  · {f['detail']}", as_json)
    summary = (f"{clean}/{len(results)} 篇无 blocker · 共 {n_err} 个 blocker · "
               f"{n_warn} 条降级提示")
    SC.line(f"SUMMARY {summary}", as_json)
    SC.line(f"下一步：python .qoder/skills/hot-sleep-dialogues/scripts/check_gates.py {root}",
            as_json)

    payload = SC.envelope("preflight", root, all_findings, checked=len(results),
                          summary=summary, errors=n_err, warnings=n_warn,
                          extra={
                              "results": results, "word_cap": cap,
                              "steps_source": steps_src, "console_source": a_src,
                              "blockers": n_err, "degrade_warnings": n_warn})
    if as_json:
        SC.emit_json(payload)
    if n_err:
        return SC.EXIT_BLOCK
    return SC.EXIT_WARN if n_warn else SC.EXIT_OK


if __name__ == "__main__":
    SC.setup_stdout()
    sys.exit(main())
