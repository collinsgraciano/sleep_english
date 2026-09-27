"""预生成脚本管理台（ai_scripts / ai_scripts_hot 的增删改查 + 校验 + 回收站）。

app/ai_scripts.py 只管「读 + 入库同步」；本模块补上「写」的一侧，供
app/routers/ai_presets.py（页面 /ai_presets）调用：

  新增/编辑  写 NNN_Folder/script.json 并回写 manifest.json 对应条目
  删除       移入 ai_scripts*/_recycle_bin/（可恢复），同时摘掉 manifest 条目
  校验       确定性检查（字段/行数/speaker 交替/IPA 闭合/简体字/重复句）
  导入       粘贴文本或 JSON、脚本库文档反向导出 → 落盘成新的预生成脚本
  导出       单条下载或批量打包 zip

改完 script.json 的 mtime 会变 → 下一次列目录时 ai_scripts.sync_library() 自动把
新内容刷进脚本库（status=used 的那份不覆盖，与 ai_scripts.py 的约定一致）。
"""
import json
import re
import shutil
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

from .ai_scripts import (
    AI_SCRIPTS_DIRS, EXPECTED_LINES, RECYCLE_DIRNAME, SLEEP_MODE, STANDARD_LINES,
    TRASH_META_NAME, _load_state, _save_state, list_ai_scripts, load_ai_script,
    source_dir,
)

# 与 skill 的 validate_all.py 同一套必填字段（缺了会跑不出完整卡片/YouTube 元数据）
META_REQUIRED = ("title", "title_zh", "topic", "cefr", "category", "structure",
                 "lesson_type", "youtube_title", "youtube_description",
                 "youtube_tags", "thumb_badge", "thumb_main", "thumb_hook",
                 "char_a_gender", "char_b_gender")

# 译文里允许出现在 Big5 之外的拟声/方言字（台派口语写法，不算简体）
# —— 与 skill 的 audit_script.py 同一套判定：繁简用 Big5 可编码性判断，
#    比手写字表可靠（「被/女」这类两体同形的字不会被误报）。
_ZH_OK_OUTSIDE_BIG5 = set("咔哐噼咣擀哒")

_INDEX_RE = re.compile(r"^(\d+)_")

# 写侧互斥（保存/新建/删除并发）；可重入 —— 保存路径上会嵌套调用刷新库文档的辅助函数
_WRITE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 目录 / manifest 读写
# ---------------------------------------------------------------------------

def folder_dir(folder: str) -> Path | None:
    """folder → 磁盘上的脚本目录（跨两个预生成目录查找，Path().name 防目录穿越）。"""
    folder = Path(str(folder)).name
    if not folder:
        return None
    for base in AI_SCRIPTS_DIRS:
        p = base / folder / "script.json"
        if p.exists():
            return p.parent
    return None


def _src_of_dir(d: Path) -> str:
    return "hot" if d.parent.name.endswith("_hot") else "main"


def _index_of(folder: str) -> int:
    m = _INDEX_RE.match(folder)
    return int(m.group(1)) if m else 0


def read_manifest(source: str) -> dict:
    path = source_dir(source) / "manifest.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"count": 0, "failed": 0, "lines_per_script": EXPECTED_LINES,
            "note": "", "themes": []}


def _write_json(path: Path, payload) -> None:
    """先写 .tmp 再 replace —— 并发扫描/读取不会看到写了一半的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else \
        json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _manifest_upsert(source: str, entry: dict) -> None:
    data = read_manifest(source)
    themes = [t for t in (data.get("themes") or [])
              if isinstance(t, dict) and t.get("folder") != entry["folder"]]
    themes.append(entry)
    themes.sort(key=lambda t: _index_of(str(t.get("folder", ""))))
    data["themes"] = themes
    data["count"] = len(themes)
    _write_json(source_dir(source) / "manifest.json", data)


def _manifest_drop(source: str, folder: str) -> None:
    data = read_manifest(source)
    themes = [t for t in (data.get("themes") or []) if isinstance(t, dict)]
    kept = [t for t in themes if t.get("folder") != folder]
    if len(kept) == len(themes):
        return
    data["themes"] = kept
    data["count"] = len(kept)
    _write_json(source_dir(source) / "manifest.json", data)


def _entry_of(folder: str, script: dict, zh: str = "") -> dict:
    return {"index": _index_of(folder),
            "en": str(script.get("topic") or folder.split("_", 1)[-1]).replace("_", " "),
            "zh": zh or str(script.get("title_zh", "")),
            "category": str(script.get("category", "")),
            "folder": folder,
            "lines": len(script.get("dialogue") or [])}


def next_index(source: str) -> int:
    """下一个可用编号：跨两个目录取最大值，守住「编号全局唯一」的约定。"""
    lo = 101 if source == "hot" else 1
    nums = [_index_of(p.name) for base in AI_SCRIPTS_DIRS if base.is_dir()
            for p in base.iterdir() if p.is_dir() and _INDEX_RE.match(p.name)]
    return max([lo - 1] + nums) + 1



def slugify(en: str) -> str:
    slug = re.sub(r"[/\\:*?\"<>|\x00-\x1f]", "", str(en or "").strip())
    slug = re.sub(r"\s+", "_", slug).strip("._")
    return slug[:60] or "New_Topic"


# ---------------------------------------------------------------------------
# 增 / 改
# ---------------------------------------------------------------------------

def blank_script(topic_en: str, topic_zh: str = "", category: str = "",
                 cefr: str = "A2") -> dict:
    """新建脚本的骨架：元数据齐全（能直接跑），dialogue 留给用户填。"""
    en = (topic_en or "New Topic").strip()
    zh = (topic_zh or en).strip()
    n = EXPECTED_LINES
    return {
        "title": f"Everyday Phrases — {en}",
        "title_zh": zh,
        "scene_zh": f"{zh} · 情境對話",
        "title_quote": "",
        "cefr": cefr or "A2",
        "youtube_title": (f"【睡前英文聽力】💬 實用{zh}必備常用英文短句 ｜ 不用背！睡覺聽就會"
                          f" ｜ 🌱{cefr} ｜ 💡{n}句循環聽 ｜ 零基礎也能開口說"),
        "youtube_title_en": f"{n} Everyday English Phrases While You Sleep — {en}",
        "youtube_description": (f"睡著學英文！這部影片收錄實用{zh}情境對話（每兩句成一組："
                                "男聲常速 ➡ 女聲慢速 ➡ 男女連貫），不用背，睡前聽就會。"
                                " #英文聽力 #睡前英文 #LearnEnglish"),
        "youtube_description_en": (f"Learn English while you sleep! This video features "
                                   f"practical {en} phrase pairs (each pair: male normal "
                                   "speed → female slow speed → combined dialogue)."),
        "thumb_badge": "不用背！",
        "thumb_main": "睡覺聽",
        "thumb_hook": "聽久自然開口說",
        "lesson_type": "listening",
        "structure": SLEEP_MODE,
        "topic": en,
        "char_a_description": "male narrator voice",
        "char_b_description": "female narrator voice",
        "char_a_gender": "male",
        "char_b_gender": "female",
        "char_a_role": "narrator (male voice)",
        "char_b_role": "narrator (female voice)",
        "welcome_en": "", "welcome_zh": "", "story_hook": "", "intro_zh": "",
        "outro": "", "outro_zh": "",
        "practice_intro_en": "", "practice_intro_zh": "",
        "scene": en,
        "thumbnail_expression": "", "thumbnail_action": "",
        "thumbnail_subtitle": f"{n}句睡前英文",
        "thumbnail_icons": [],
        "channel_id": "",
        "category": category or "Daily Life",
        "youtube_tags": [zh, en, "英文聽力", "睡前英文", "Sleep learning",
                         f"{cefr} English", "ESL", "English conversation"],
        "dialogue": [],
    }


def _normalize_dialogue(lines: list) -> list[dict]:
    """只保留 text/phonetic/zh 三字段，speaker 按 a/b 严格重排。"""
    out: list[dict] = []
    for raw in lines or []:
        if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
            continue
        out.append({"speaker": "char_a" if len(out) % 2 == 0 else "char_b",
                    "text": str(raw.get("text", "")).strip(),
                    "phonetic": str(raw.get("phonetic", "")).strip(),
                    "zh": str(raw.get("zh", "")).strip()})
    return out


def parse_pairs_text(text: str) -> list[dict]:
    """粘贴文本 → dialogue 行。一行一句，列用 | 或 Tab 分隔：
       `English sentence | 中文翻譯 | /ˈfə·nɛt·ɪk/`（后两列可省略）。
       行号、项目符号等前缀自动忽略；speaker 由 _normalize_dialogue 排。
    """
    rows: list[dict] = []
    for ln in str(text or "").splitlines():
        ln = re.sub(r"^[-•*\s]+", "", ln)
        ln = re.sub(r"^\d+\s*[.、:)]\s*", "", ln).strip()
        if not ln:
            continue
        parts = [p.strip() for p in re.split(r"[|\t｜]+", ln)]
        ph = next((p for p in parts if p.startswith("/")), "")
        rest = [p for p in parts if p and p != ph]
        zh = next((p for p in rest if re.search(r"[\u4e00-\u9fff]", p)), "")
        en = next((p for p in rest if p != zh), "")
        if not en:
            continue
        if ph and not ph.endswith("/"):
            ph += "/"  # 粘贴时漏掉闭合斜杠（agent 常见手误）
        rows.append({"text": en, "zh": zh, "phonetic": ph})
    return rows


def save_script(folder: str, script: dict, topic_zh: str = "") -> dict:
    """编辑保存：整份 script.json 落盘 + manifest 条目同步（主题/中文/行数）。"""
    d = folder_dir(folder)
    if not d:
        return {"ok": False, "error": f"预生成脚本不存在: {folder}"}
    with _WRITE_LOCK:
        script["dialogue"] = _normalize_dialogue(script.get("dialogue") or [])
        script["structure"] = SLEEP_MODE
        if topic_zh.strip():
            script["title_zh"] = topic_zh.strip()
        _write_json(d / "script.json", script)
        src = _src_of_dir(d)
        _manifest_upsert(src, _entry_of(d.name, script, topic_zh))
        notice = _sync_library_doc(d.name, script, src)
    return {"ok": True, "folder": d.name,
            "lines": len(script["dialogue"]), "notice": notice}


def _sync_library_doc(folder: str, script: dict, source: str) -> str:
    """落盘后立刻把新内容刷进脚本库（含主题改名），返回给页面看的结果。"""
    from . import ai_scripts
    r = ai_scripts.sync_library(source, folders=[folder])
    if r.get("errors"):
        return "已保存，但入库刷新出错：" + "；".join(r["errors"][:2])
    if r.get("skipped"):
        return "已保存（该主题已产出视频，脚本库里那份按约定不覆盖）"
    state = _load_state()["folders"].get(folder) or {}
    _rename_library_doc(folder, str(state.get("sid", "")),
                        str(script.get("topic", "")))
    return "已保存并刷新脚本库"


def _rename_library_doc(folder: str, sid: str, topic: str) -> None:
    """页面改了主题名 → 草稿库文档与入库指纹跟着改。

    used 文档保持原名：已产出视频的 run 记录是按老主题名登记的，改了会对不上。
    """
    from . import script_library
    if not topic or not sid.startswith("script_"):
        return
    doc = script_library.get_script_doc(sid)
    if not doc or doc.get("status") == "used" or doc.get("topic") == topic:
        return
    script_library.update_script(sid, {"topic": topic})
    with _WRITE_LOCK:
        state = _load_state()
        if state["folders"].get(folder):
            state["folders"][folder]["topic"] = topic
            _save_state(state)


def create_script(source: str, script: dict, index: int = 0) -> dict:
    """新增一条预生成脚本（自动分配编号 + 建目录 + 登记 manifest）。"""
    topic = str(script.get("topic") or "").strip()
    if not topic:
        return {"ok": False, "error": "缺少主题名（topic）"}
    with _WRITE_LOCK:
        idx = int(index) if index and int(index) > 0 else next_index(source)
        stem = slugify(topic)
        folder = f"{idx:03d}_{stem}"
        n = 2
        while _folder_taken(folder):
            folder = f"{idx:03d}_{stem}_{n}"
            n += 1
        script["structure"] = SLEEP_MODE
        script["topic"] = topic
        script["dialogue"] = _normalize_dialogue(script.get("dialogue") or [])
        d = source_dir(source) / folder
        d.mkdir(parents=True)
        _write_json(d / "script.json", script)
        _manifest_upsert(source, _entry_of(folder, script,
                                           str(script.get("title_zh", ""))))
    from . import ai_scripts
    ai_scripts.sync_library(source, folders=[folder])
    return {"ok": True, "folder": folder, "index": idx,
            "lines": len(script["dialogue"])}


def _folder_taken(folder: str) -> bool:
    return any((base / folder).exists() for base in AI_SCRIPTS_DIRS)


def create_from_payload(payload: dict) -> dict:
    """页面「新增」统一入口：粘贴 JSON / 粘贴对话文本 / 空白骨架三种来源。"""
    source = "hot" if payload.get("source") == "hot" else "main"
    try:
        index = int(payload.get("index") or 0)
    except (TypeError, ValueError):
        index = 0
    raw = str(payload.get("json_text", "") or "").strip()
    if raw:
        try:
            script = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"JSON 解析失败：{e}"}
        if not isinstance(script, dict):
            return {"ok": False, "error": "JSON 顶层必须是对象（script.json 格式）"}
        if not (script.get("dialogue") or []):
            return {"ok": False, "error": "这份 JSON 里没有 dialogue 对话内容"}
        if not str(script.get("topic", "")).strip():
            script["topic"] = str(payload.get("topic", "")).strip() or \
                str(script.get("title", "")).split("—")[-1].strip()
        return create_script(source, script, index)

    topic_en = str(payload.get("topic", "")).strip()
    if not topic_en:
        return {"ok": False, "error": "主题（英文）不能为空"}
    script = blank_script(topic_en, str(payload.get("zh", "")),
                          str(payload.get("category", "")),
                          str(payload.get("cefr", "A2")))
    rows = parse_pairs_text(str(payload.get("pairs_text", "")))
    script["dialogue"] = rows
    if rows:
        script["title_quote"] = rows[0]["text"]
    return create_script(source, script, index)


def duplicate_script(folder: str, topic_en: str = "", source: str = "") -> dict:
    """复制一条预生成脚本（换主题名 / 换来源目录），得到一份可自由改的新副本。"""
    script = load_ai_script(folder)
    if not script:
        return {"ok": False, "error": f"预生成脚本不存在: {folder}"}
    d = folder_dir(folder)
    dst = source or _src_of_dir(d)
    if topic_en.strip():
        script["topic"] = topic_en.strip()
        script["scene"] = topic_en.strip()
        script["title"] = f"Everyday Phrases — {topic_en.strip()}"
    return create_script(dst, script)


def import_from_library(sid: str, source: str = "main") -> dict:
    """把脚本库里的一份 sleep 脚本另存为预生成脚本（反向导出，便于二次编辑复用）。"""
    from . import script_library
    doc = script_library.get_script_doc(sid)
    if not doc:
        return {"ok": False, "error": f"脚本库文档不存在: {sid}"}
    script = json.loads(json.dumps(doc.get("script") or {}))
    if not script.get("dialogue"):
        return {"ok": False, "error": "该库脚本没有对话内容"}
    script.setdefault("topic", str(doc.get("topic", "")))
    return create_script(source, script)


# ---------------------------------------------------------------------------
# 删 / 回收站
# ---------------------------------------------------------------------------

def delete_script(folder: str, hard: bool = False,
                  drop_library: bool = False) -> dict:
    """删除预生成脚本：默认移入 _recycle_bin（可恢复），hard=True 才真删文件。

    同步摘掉 manifest 条目与入库指纹，避免 sync_library 再认领这个目录。
    drop_library=True 时连脚本库里对应文档一起删（不可恢复）。
    """
    d = folder_dir(folder)
    if not d:
        return {"ok": False, "error": f"预生成脚本不存在: {folder}"}
    folder = d.name
    src = _src_of_dir(d)
    with _WRITE_LOCK:
        if hard:
            shutil.rmtree(d)
        else:
            root = source_dir(src) / RECYCLE_DIRNAME
            root.mkdir(parents=True, exist_ok=True)
            dest = root / f"{int(time.time() * 1000)}_{folder}"
            shutil.move(str(d), str(dest))
            _write_json(dest / TRASH_META_NAME,
                        {"original_name": folder, "source": src,
                         "deleted_at": time.time()})
        _manifest_drop(src, folder)
        state = _load_state()
        if state["folders"].pop(folder, None) is not None:
            _save_state(state)
    lib = _drop_library_doc(folder) if drop_library else None
    return {"ok": True, "folder": folder, "hard": hard, "library": lib}


def _drop_library_doc(folder: str) -> dict:
    from . import script_library
    sid = str(_load_state()["folders"].get(folder, {}).get("sid", ""))
    doc = script_library.get_script_doc(sid) if sid.startswith("script_") else None
    if not doc:
        return {"ok": False, "reason": "该预生成脚本还没入库，无库文档可删"}
    script_library.remove_topic_used_mode(doc.get("structure") or SLEEP_MODE,
                                          str(doc.get("topic", "")), sid)
    return {"ok": script_library.delete_script(sid), "sid": sid}


def list_recycle() -> list[dict]:
    """回收站条目（按删除时间倒序）。"""
    out: list[dict] = []
    for src in ("main", "hot"):
        root = source_dir(src) / RECYCLE_DIRNAME
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if not d.is_dir():
                continue
            meta = _trash_meta(d)
            lines = 0
            try:
                s = json.loads((d / "script.json").read_text(encoding="utf-8"))
                lines = len(s.get("dialogue") or [])
            except (OSError, json.JSONDecodeError):
                pass
            folder = str(meta.get("original_name") or d.name)
            out.append({"key": f"{src}:{d.name}", "source": src,
                        "folder": folder,
                        "topic": folder.split("_", 1)[-1].replace("_", " "),
                        "lines": lines,
                        "deleted_at": float(meta.get("deleted_at")
                                            or d.stat().st_mtime)})
    out.sort(key=lambda x: x["deleted_at"], reverse=True)
    return out


def _trash_meta(d: Path) -> dict:
    try:
        return json.loads((d / TRASH_META_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _recycle_dir(key: str) -> Path | None:
    src, _, entry = str(key).partition(":")
    if src not in ("main", "hot") or not entry:
        return None
    d = source_dir(src) / RECYCLE_DIRNAME / Path(entry).name
    return d if d.is_dir() else None


def restore_script(key: str) -> dict:
    d = _recycle_dir(key)
    if not d:
        return {"ok": False, "error": "回收站里没有这一条"}
    src, _, _ = str(key).partition(":")
    folder = Path(str(_trash_meta(d).get("original_name") or d.name)).name
    with _WRITE_LOCK:
        target = source_dir(src) / folder
        if target.exists():
            folder = f"{target.stem}_{int(time.time())}"
            target = source_dir(src) / folder
        shutil.move(str(d), str(target))
        (target / TRASH_META_NAME).unlink(missing_ok=True)  # 回收站便签不跟着回原位
        try:
            script = json.loads((target / "script.json").read_text(encoding="utf-8"))
            _manifest_upsert(src, _entry_of(folder, script))
        except (OSError, json.JSONDecodeError):
            pass
    return {"ok": True, "folder": folder}


def purge_recycle(key: str = "", all_entries: bool = False) -> dict:
    """彻底删除：单条（key）或清空（all_entries）。"""
    if all_entries:
        targets = [d for src in ("main", "hot")
                   for d in (source_dir(src) / RECYCLE_DIRNAME).glob("*")
                   if d.is_dir()]
    else:
        hit = _recycle_dir(key)
        targets = [hit] if hit else []
    for d in targets:
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "removed": len(targets)}


# ---------------------------------------------------------------------------
# 校验（确定性检查，零积分）
# ---------------------------------------------------------------------------

def validate_script(folder: str, cross: dict[str, str] | None = None) -> dict:
    """一条脚本的机器体检。cross 给了就额外做跨主题整句查重（整句→folder）。"""
    script = load_ai_script(folder)
    if not script:
        return {"folder": folder, "ok": False, "errors": ["script.json 读取失败"],
                "warnings": [], "stats": {}}
    errors, warnings = _check_meta(script)
    dlg = script.get("dialogue") or []
    stats = {"lines": len(dlg), "pairs": len(dlg) // 2, "no_phonetic": 0,
             "no_zh": 0, "short_words": 0}
    seen: dict[str, int] = {}
    for i, line in enumerate(dlg):
        want = "char_a" if i % 2 == 0 else "char_b"
        if line.get("speaker") != want:
            errors.append(f"第 {i + 1} 行 speaker={line.get('speaker')!r}（应为 {want}）")
        text = str(line.get("text", "")).strip()
        ph = str(line.get("phonetic", "")).strip()
        zh = str(line.get("zh", "")).strip()
        if not text:
            errors.append(f"第 {i + 1} 行 text 为空")
            continue
        if not ph:
            stats["no_phonetic"] += 1
            errors.append(f"第 {i + 1} 行 phonetic 为空")
        elif not (ph.startswith("/") and ph.endswith("/") and len(ph) > 2):
            errors.append(f"第 {i + 1} 行音标未用 /.../ 闭合：{ph[:24]}")
        if not zh:
            stats["no_zh"] += 1
            errors.append(f"第 {i + 1} 行 zh 为空")
        elif (bad_zh := _non_big5_chars(zh)):
            warnings.append(f"第 {i + 1} 行译文含非繁体字形 {bad_zh[:12]}")
        key = text.lower()
        if key in seen:
            warnings.append(f"第 {i + 1} 行与本篇第 {seen[key] + 1} 行整句重复")
        else:
            seen[key] = i
        if cross is not None and (dup := cross.get(key)) and dup != folder:
            warnings.append(f"第 {i + 1} 行与主题「{dup}」整句撞车")
        n = len([w for w in re.split(r"[^A-Za-z']+", text) if w])
        if n < 3 or n > 9:
            stats["short_words"] += 1
    if stats["short_words"]:
        warnings.append(f"{stats['short_words']} 行词数不在 3–9 区间（口语短句建议值）")
    return {"folder": folder, "ok": not errors, "errors": errors[:20],
            "warnings": warnings[:20], "stats": stats}


def _non_big5_chars(text: str) -> str:
    """挑出译文中 Big5 编不出的汉字（＝简体/异体字形），返回去重后的字符串。"""
    bad = set()
    for c in text:
        if '一' <= c <= '鿿' and c not in _ZH_OK_OUTSIDE_BIG5:
            try:
                c.encode("big5")
            except UnicodeEncodeError:
                bad.add(c)
    return "".join(sorted(bad))


def _check_meta(script: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for k in META_REQUIRED:
        if script.get(k) in ("", [], None, {}):
            errors.append(f"元字段「{k}」缺失或为空")
    if script.get("structure") != SLEEP_MODE:
        errors.append(f"structure 必须是 {SLEEP_MODE}")
    n = len(script.get("dialogue") or [])
    if n == 0:
        errors.append("dialogue 为空（还没有内容）")
    elif n not in STANDARD_LINES:
        std = " 或 ".join(f"{x} 行（{x // 2} 对）" for x in STANDARD_LINES)
        warnings.append(f"对话 {n} 行，标准是 {std}")
    return errors, warnings


def _cross_index() -> dict[str, str]:
    """整句 → 首个出现它的 folder，全库查重的反查表（约 8 万句，1~2 秒）。"""
    idx: dict[str, str] = {}
    for p in list_ai_scripts("all"):
        for line in (load_ai_script(p["folder"]) or {}).get("dialogue") or []:
            key = str(line.get("text", "")).strip().lower()
            if key:
                idx.setdefault(key, p["folder"])
    return idx


def validate_many(folders: list[str], cross: bool = False) -> dict:
    table = _cross_index() if cross else None
    results = [validate_script(f, cross=table) for f in folders]
    return {"results": results,
            "bad": [r["folder"] for r in results if not r["ok"]],
            "total": len(results)}


# ---------------------------------------------------------------------------
# manifest 重建 / 导出
# ---------------------------------------------------------------------------

def rebuild_manifest(source: str) -> dict:
    """扫描磁盘目录重建 manifest.json（手工新增/改名目录后补登记用）。"""
    root = source_dir(source)
    themes: list[dict] = []
    failed: list[str] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        if not _INDEX_RE.match(sub.name) or not (sub / "script.json").exists():
            continue
        try:
            s = json.loads((sub / "script.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            failed.append(f"{sub.name}: {e}")
            continue
        themes.append(_entry_of(sub.name, s))
    data = read_manifest(source)
    data["themes"] = themes
    data["count"] = len(themes)
    data["failed"] = len(failed)
    _write_json(root / "manifest.json", data)
    return {"ok": True, "count": len(themes), "failed": failed}


def export_zip(folders: list[str]) -> tuple[bytes, str]:
    """批量导出 script.json 打包（备份 / 交接用）。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folders:
            d = folder_dir(f)
            if d:
                zf.write(d / "script.json", f"{d.name}/script.json")
    return buf.getvalue(), f"ai_scripts_export_{time.strftime('%Y%m%d_%H%M%S')}.zip"
