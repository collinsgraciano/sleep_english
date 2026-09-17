"""字体扫描/字形覆盖检测（配置页字体下拉 + 卡片渲染智能回退共用）。

- font_covers/covered_count：fontTools cmap 查询 —— 判断字体是否含目标文字
  的字形。Pillow 的 truetype 不会自动逐字回退，频道名含中文而字体（如
  Ink Free）无中文字形时渲染成豆腐块，因此渲染链按文字内容选字体。
  fontTools 缺失时 font_covers 恒 True（优雅降级=旧行为，只是不再智能回退）；
  文件缺失/不可解析时恒 False（链上跳过，防止 Pillow 加载即崩）。
- scan_fonts：winreg 枚举系统字体（显示名友好 + CJK 字形标记）+ 内置项目
  字体，供配置页「频道名字体」下拉选择；结果落盘 configs/font_scan_cache.json
 （键=字体目录 mtime/数量，装删字体自动失效），首扫约数秒后续秒回。
"""
import json
import os
import sys
from pathlib import Path

try:
    from fontTools.ttLib import TTFont
except ImportError:  # 未安装时渲染侧退回旧行为，扫描侧按文件名兜底
    TTFont = None

_PIPE_DIR = Path(__file__).resolve().parent
_FONTS_DIR = _PIPE_DIR / "fonts"
_WIN_FONTS_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_SCAN_DISK_CACHE = _PIPE_DIR.parent / "configs" / "font_scan_cache.json"

# CJK 支持判定样本（覆盖常用简繁字形区）
_CJK_SAMPLE = "中眠晚英語"

# 推荐手写/趣味体（按字体文件名前缀匹配，存在才展示）
_RECOMMENDED_FILES = (
    "Inkfree", "segoepr", "segoeprb", "segoesc", "segoescb",
    "Gabriola", "comicbd", "comic", "seguiemj",
)
# 常见中文手写/楷体系（命中文件名即入推荐组）
_RECOMMENDED_CJK_FILES = ("simkai", "stkaiti", "kaiti", "fzhtjw", "fzstkw")

# path → cmap codepoint 集（进程级；mtime+size 失效防字体替换）
_CMAP_CACHE: dict[tuple, frozenset[int] | None] = {}
# scan_fonts 结果（进程级；磁盘缓存见 _SCAN_DISK_CACHE）
_SCAN_CACHE: dict | None = None


def _cmap_codes(path: str) -> frozenset[int] | None:
    """字体文件 → 可渲染 codepoint 集（.ttc 取第一个 face）。

    None = 无法判定（fontTools 缺失）；frozenset() = 文件缺失/不可解析。
    """
    if TTFont is None:
        return None
    p = str(path)
    try:
        stat = os.stat(p)
    except OSError:
        return frozenset()
    key = (p, stat.st_mtime_ns, stat.st_size)
    cached = _CMAP_CACHE.get(key)
    if cached is not None:
        return cached
    codes: frozenset[int] = frozenset()
    try:
        tt = TTFont(p, fontNumber=0, lazy=True)
        try:
            codes = frozenset(tt.getBestCmap().keys())
        finally:
            tt.close()
    except Exception:  # noqa: BLE001 — 任何字体问题都按「无字形」处理
        codes = frozenset()
    if len(_CMAP_CACHE) > 256:  # 下拉全量 + 渲染链，防极端累积
        _CMAP_CACHE.clear()
    _CMAP_CACHE[key] = codes
    return codes


def _visible_chars(text: str) -> list[str]:
    """参与覆盖判定的字符（空白恒可渲染，跳过）。"""
    return [c for c in (text or "") if not c.isspace()]


def font_covers(path: str, text: str) -> bool:
    """字体是否含 text 全部非空白字符的字形（空文本=覆盖）。"""
    chars = _visible_chars(text)
    if not chars:
        return True
    codes = _cmap_codes(path)
    if codes is None:  # fontTools 缺失 → 不拦链（降级=旧行为）
        return True
    if not codes:      # 文件缺失/损坏 → 不可用（链上跳过）
        return False
    return all(ord(c) in codes for c in chars)


def covered_count(path: str, text: str) -> int:
    """字体覆盖 text 非空白字符的个数（回退链评分用）。"""
    chars = _visible_chars(text)
    if not chars:
        return 0
    codes = _cmap_codes(path)
    if codes is None:
        return len(chars)
    if not codes:
        return 0
    return sum(1 for c in chars if ord(c) in codes)


def supports_cjk(path: str) -> bool:
    """字体是否含中文字形（下拉列表「含中文」标记）。"""
    codes = _cmap_codes(path)
    if not codes:
        return False
    return all(ord(c) in codes for c in _CJK_SAMPLE)


# ---------------------------------------------------------------------------
# 字体列表枚举（配置页下拉）
# ---------------------------------------------------------------------------

def _clean_display_name(name: str) -> str:
    """winreg 值名 → 干净显示名（去 (TrueType)/(OpenType) 尾巴）。"""
    for suffix in ("(TrueType)", "(OpenType)"):
        name = name.replace(" " + suffix, "").replace(suffix, "")
    return name.strip()


def _font_entry(path: str, name: str) -> dict | None:
    """(路径, 显示名) → 字典；不可解析的字体（Pillow 大概率也加载不了）丢弃。"""
    codes = _cmap_codes(path)
    if codes is not None and not codes:
        return None
    return {"name": name, "path": path, "cjk": supports_cjk(path)}


def _dedup_by_path(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for it in items:
        if it["path"] in seen:
            continue
        seen.add(it["path"])
        out.append(it)
    return out


def list_system_fonts() -> list[dict]:
    """系统字体：winreg HKLM 登记项（友好名）+ Fonts 目录未登记的矢量字体。

    仅 Windows 有登记可读；值可能含多个文件（逗号分隔）取第一个。
    .fon 位图字体 Pillow 无法加载，直接排除。
    """
    if sys.platform != "win32" or not _WIN_FONTS_DIR.is_dir():
        return []
    items: list[dict] = []
    referenced: set[str] = set()
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        with key:
            i = 0
            while True:
                try:
                    disp, val, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                fname = str(val or "").split(",")[0].strip()
                if not fname or not fname.lower().endswith((".ttf", ".ttc", ".otf")):
                    continue
                fpath = fname if os.path.isabs(fname) else str(_WIN_FONTS_DIR / fname)
                if not os.path.exists(fpath):
                    continue
                referenced.add(os.path.normcase(fpath))
                entry = _font_entry(fpath, _clean_display_name(str(disp)))
                if entry:
                    items.append(entry)
    except OSError:
        pass
    # 手动拷进目录但未登记的字体（隐藏手工调试场景）
    try:
        for f in _WIN_FONTS_DIR.iterdir():
            if not f.is_file() or f.suffix.lower() not in (".ttf", ".ttc", ".otf"):
                continue
            if os.path.normcase(str(f)) in referenced:
                continue
            entry = _font_entry(str(f), f.stem)
            if entry:
                items.append(entry)
    except OSError:
        pass
    return _dedup_by_path(items)


def list_project_fonts() -> list[dict]:
    """内置项目字体（pipeline/fonts/ 下的矢量字体文件）。"""
    if not _FONTS_DIR.is_dir():
        return []
    items = []
    for f in sorted(_FONTS_DIR.iterdir()):
        if f.is_file() and f.suffix.lower() in (".ttf", ".ttc", ".otf"):
            entry = _font_entry(str(f), f.stem)
            if entry:
                items.append(entry)
    return items


def _is_recommended(item: dict) -> bool:
    stem = Path(item["path"]).stem.lower()
    return (stem in _RECOMMENDED_FILES or stem in _RECOMMENDED_CJK_FILES
            or any(stem.startswith(p) for p in _RECOMMENDED_FILES + _RECOMMENDED_CJK_FILES))


def _scan_fingerprint() -> dict:
    """磁盘缓存指纹：字体目录 mtime + 文件数（装删/换字体自动失效）。"""
    fp: dict[str, object] = {"platform": sys.platform}
    for tag, d in (("win", _WIN_FONTS_DIR), ("proj", _FONTS_DIR)):
        try:
            files = [f for f in d.iterdir()
                     if f.is_file() and f.suffix.lower() in (".ttf", ".ttc", ".otf")]
            fp[f"{tag}_mtime"] = d.stat().st_mtime_ns
            fp[f"{tag}_n"] = len(files)
        except OSError:
            fp[f"{tag}_mtime"] = 0
            fp[f"{tag}_n"] = -1
    return fp


def scan_fonts() -> dict:
    """全部可用字体 → 推荐手写/项目/含中文/其它四组（磁盘+进程两级缓存）。"""
    global _SCAN_CACHE
    if _SCAN_CACHE is not None:
        return _SCAN_CACHE
    fp = _scan_fingerprint()
    data = None
    try:
        if _SCAN_DISK_CACHE.exists():
            blob = json.loads(_SCAN_DISK_CACHE.read_text(encoding="utf-8"))
            if isinstance(blob, dict) and blob.get("fp") == fp \
                    and isinstance(blob.get("fonts"), dict):
                data = blob["fonts"]
    except (json.JSONDecodeError, OSError, ValueError):
        data = None
    if data is None:
        system = list_system_fonts()
        recommended = [it for it in system if _is_recommended(it)]
        recommended_paths = {it["path"] for it in recommended}
        cjk = [it for it in system
               if it["path"] not in recommended_paths and it["cjk"]]
        other = [it for it in system
                 if it["path"] not in recommended_paths and not it["cjk"]]
        for group in (recommended, cjk, other):
            group.sort(key=lambda it: it["name"].lower())
        data = {"recommended": recommended, "project": list_project_fonts(),
                "cjk": cjk, "other": other}
        try:
            _SCAN_DISK_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _SCAN_DISK_CACHE.write_text(
                json.dumps({"fp": fp, "fonts": data}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass  # 缓存写盘失败不影响结果
    _SCAN_CACHE = data
    return data


def all_font_paths() -> set[str]:
    """下拉全量字体路径集（当前值是否「列表外自定义」的判定用）。"""
    data = scan_fonts()
    return {it["path"] for group in data.values() for it in group}
