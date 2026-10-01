"""Colab 公网入口的令牌闸门（ASGI 中间件，不改 app/ 任何代码）。

为什么必须有它：cloudflared 快速隧道给的是**公开** URL，而 GET /api/config 会原样
返回整份配置（含 gemini_api_key / wbk_api_key / mcp_tokens），任何拿到 URL 的人都能
读走密钥、并用你的额度跑视频。

用法：uvicorn secure_gate:app（colab/serve.sh 已封装）。令牌取自环境变量
COLAB_ACCESS_TOKEN（由 colab/drive_config.py 生成并持久化在 Drive 的 state/ 下，
所以域名每次换、令牌不变）。令牌为空则闸门放行（等于本地直连，不挂隧道时用）。
浏览器首次访问 serve.sh 打印的 `<url>/?ct=<token>`，闸门种下 cookie，之后页面里的
/api 请求自动带 cookie，前端无需任何改动。
"""
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from app.main import app as inner_app  # noqa: E402

TOKEN = (os.environ.get("COLAB_ACCESS_TOKEN", "") or "").strip()
COOKIE_NAME = "colab_token"
TOKEN_HEADER = b"x-colab-token"
COOKIE_HEADER = b"cookie"


async def _send_plain(send, status: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(payload)).encode()),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


def _token_from_request(scope: dict) -> tuple[str, bool]:
    """返回 (请求携带的令牌, cookie 是否已命中)。"""
    headers = dict(scope.get("headers") or [])
    cookie = headers.get(COOKIE_HEADER, b"").decode("latin-1")
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and secrets.compare_digest(value, TOKEN):
            return TOKEN, True

    query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
    for key in ("ct", "token"):
        values = query.get(key)
        if values and values[0]:
            return values[0], False
    return headers.get(TOKEN_HEADER, b"").decode("latin-1"), False


class TokenGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not TOKEN:
            await self.app(scope, receive, send)
            return

        supplied, via_cookie = _token_from_request(scope)
        if not (supplied and secrets.compare_digest(supplied, TOKEN)):
            await _send_plain(
                send, 401,
                "401 缺少访问令牌\n\n请用启动单元格打印的完整链接打开（形如 "
                "https://xxxx.trycloudflare.com/?ct=令牌）。\n"
                "令牌也可以放在请求头 X-Colab-Token 里。")
            return

        if via_cookie:
            await self.app(scope, receive, send)
            return

        async def send_with_cookie(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers") or []) + [(
                    b"set-cookie",
                    f"{COOKIE_NAME}={TOKEN}; Path=/; SameSite=Lax; Max-Age=86400".encode(),
                )]
            await send(message)

        await self.app(scope, receive, send_with_cookie)


app = TokenGate(inner_app)
