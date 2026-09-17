"""片尾库索引 IO — 路由与 pipeline_service 共用；按频道上下文分库互不影响。

与片头库（intro_library）同机制，绑定键为 sleep_outro_video：
索引条目：{id, name, source(local/ai/upload), duration, created, subtitle,
bgm, announce}；视频文件固定为 {outro_id}/outro.mp4（统一规格
1280x720 / 25fps / aac 44100 立体声 / 生成路线时长 4-15s 可设（默认 10s）、
上传路线保留原时长，构建函数复用 pipeline/sleep/intro_video.py）。

存储布局（channel_id 为空 = 全局，与片头库一致）：
- 全局：configs/outro_library.json + configs/outro_videos/{id}/outro.mp4
- 频道：configs/channels/{cid}/outro_library.json
  + configs/channels/{cid}/outro_videos/{id}/outro.mp4（与频道片头库同级惯例）

解析回退：频道上下文的绑定值先查频道库，未命中回退全局库 ——
存量频道可能继承指向全局片尾的历史绑定。
"""
import json
import re
import time
from pathlib import Path

from .paths import CHANNELS_DIR, OUTRO_LIBRARY_PATH, OUTRO_VIDEOS_DIR

OUTRO_ID_RE = re.compile(r"^outro_[A-Za-z0-9_]+$")


def _library_path(channel_id: str = "") -> Path:
    """上下文 → 索引文件路径（空 = 全局库，非空 = 频道库）。"""
    cid = str(channel_id or "").strip()
    return (CHANNELS_DIR / cid / "outro_library.json") if cid else OUTRO_LIBRARY_PATH


def videos_dir(channel_id: str = "") -> Path:
    """上下文 → 片尾视频目录（空 = 全局库，非空 = 频道库）。"""
    cid = str(channel_id or "").strip()
    return (CHANNELS_DIR / cid / "outro_videos") if cid else OUTRO_VIDEOS_DIR


def load_library(channel_id: str = "") -> list[dict]:
    path = _library_path(channel_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("outros", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_library(outros: list[dict], channel_id: str = "") -> None:
    path = _library_path(channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"updated": time.time(), "outros": outros},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def outro_file(outro_id: str, channel_id: str = "") -> Path:
    return videos_dir(channel_id) / outro_id / "outro.mp4"


def resolve_video_path(outro_sel: str, channel_id: str = "") -> str:
    """sleep_outro_video 配置值 → mp4 绝对路径。支持 outro_id 或绝对路径。

    频道上下文（channel_id 非空）：outro_id 先查频道库，未命中回退全局库。
    无效（空 / 两库均不存在 / 文件缺失）返回空串，调用方回退默认片尾。
    """
    sel = str(outro_sel or "").strip()
    if not sel:
        return ""
    direct = Path(sel)
    if direct.is_absolute() and direct.suffix.lower() == ".mp4" and direct.exists():
        return str(direct)
    if OUTRO_ID_RE.match(sel):
        cid = str(channel_id or "").strip()
        f = outro_file(sel, cid)
        if f.exists():
            return str(f)
        if cid:
            f_global = outro_file(sel, "")
            if f_global.exists():
                return str(f_global)
    return ""
