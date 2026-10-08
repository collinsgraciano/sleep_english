"""Web 控制台密码闸门（纯 ASGI 中间件；给 VPS 入口 serve_auth.py 用）。

为什么需要：控制台没有鉴权时，任何扫到 8766 端口的人都能读走配置里的 API Key
并用你的额度出片。本中间件给整站加一层密码。

设计要点
- **密码**：环境变量 ``SLEEP_AUTH_PASSWORD``（默认 ``inriynisse``）。
  置空字符串 = 关闭闸门（直通，等价改动前行为）。
- **cookie**：通过后种 ``sleep_auth``，默认 **365 天**。
  值 = ``HMAC-SHA256(salt, 密码)``，**不含明文密码**；salt 首次运行随机生成并落盘
  ``configs/.auth_salt``（0600）。随机 salt 的意义：即使别人知道"默认密码"，
  也无法离线预计算 cookie，必须真的走一次登录接口（那里有失败限流）。
  改密码 → 旧 cookie 立即全部失效。
- **三种认证方式**：cookie / ``?ct=<密码>``（校验通过后种 cookie 并 302 去掉查询串，
  避免密码留在地址栏与浏览器历史）/ 请求头 ``X-Sleep-Auth``（脚本与自动化用，
  兼容 ``X-Colab-Token``）。
- **免鉴权**：``/login``（本中间件自己实现）、``/api/health``（systemd 与脚本健康检查）、
  ``/favicon.ico``。
- **失败限流**：同一 IP 5 分钟内失败 10 次 → 429 + Retry-After；每次失败固定延迟 300ms。
- 只处理 ``http`` scope；``lifespan``/``websocket`` 原样透传。不依赖任何第三方包。
- 日志只记 IP 与成功/失败，**不记密码**。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

COOKIE_NAME = "sleep_auth"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 365 天
TOKEN_MESSAGE = b"sleep-english-gate-v1"
DEFAULT_PASSWORD = "inriynisse"
EXEMPT_PATHS = ("/api/health", "/favicon.ico")
FAIL_WINDOW_SEC = 300.0
FAIL_MAX = 10
FAIL_DELAY_SEC = 0.3

_salt_cache: bytes | None = None


def password() -> str:
    """当前密码；空串 = 关闭闸门。"""
    return (os.environ.get("SLEEP_AUTH_PASSWORD", DEFAULT_PASSWORD) or "")


def _salt() -> bytes:
    """随机盐值（configs/.auth_salt，0600）；不可写时退化为进程内临时盐值。"""
    global _salt_cache
    if _salt_cache is not None:
        return _salt_cache
    path = Path(__file__).resolve().parent.parent / "configs" / ".auth_salt"
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if len(data) >= 16:
                _salt_cache = data
                return data
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_hex(32).encode("ascii")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        print("[Auth] 已生成 cookie 盐值: configs/.auth_salt", flush=True)
        _salt_cache = data
    except FileExistsError:
        _salt_cache = path.read_bytes().strip()
    except OSError as e:  # 只读挂载等 → 临时盐值（重启后 cookie 失效）
        _salt_cache = secrets.token_hex(32).encode("ascii")
        print(f"[Auth] 盐值文件不可写（{e}）—— 本次用临时盐值，重启后需重新登录", flush=True)
    return _salt_cache


def token() -> str:
    """当前密码对应的 cookie 令牌（改密码即失效）。"""
    return hmac.new(_salt(), password().encode("utf-8") + TOKEN_MESSAGE,
                    hashlib.sha256).hexdigest()


def _login_page(error: str = "") -> str:
    err_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Sleep English · 需要密码</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#12161c; color:#e6edf3;
         font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"PingFang SC","Microsoft YaHei",sans-serif; }}
  .card {{ width:min(92vw,380px); background:#1a1f27; border:1px solid #2b3440;
           border-radius:14px; padding:28px 26px 24px; box-shadow:0 10px 40px rgba(0,0,0,.45); }}
  h1 {{ font-size:19px; margin:0 0 4px; font-weight:600; }}
  .sub {{ color:#8b98a5; font-size:13px; margin:0 0 20px; }}
  label {{ display:block; font-size:13px; color:#8b98a5; margin:0 0 7px; }}
  input[type=password] {{ width:100%; padding:11px 13px; font-size:15px; border-radius:9px;
        border:1px solid #33404e; background:#11161d; color:#e6edf3; outline:none; }}
  input[type=password]:focus {{ border-color:#3b82f6; }}
  button {{ margin-top:16px; width:100%; padding:11px 13px; font-size:15px; font-weight:600;
        border:0; border-radius:9px; background:#2563eb; color:#fff; cursor:pointer; }}
  button:hover {{ background:#1d4ed8; }}
  .err {{ margin:14px 0 0; padding:9px 11px; border-radius:8px; font-size:13px;
          color:#fecaca; background:#3b1717; border:1px solid #6b2020; }}
  .foot {{ margin-top:18px; font-size:12px; color:#66727f; }}
</style></head>
<body><form class="card" method="post" action="/login">
  <h1>Sleep English 控制台</h1>
  <p class="sub">请输入访问密码（通过后 365 天内免输）</p>
  <label for="password">密码</label>
  <input id="password" name="password" type="password" autocomplete="current-password"
         autofocus required>
  <button type="submit">进入控制台</button>
  {err_html}
  <p class="foot">连续输错会临时限流；忘记密码见服务器 .vps_env 的 SLEEP_AUTH_PASSWORD</p>
</form></body></html>"""


def _json_401() -> bytes:
    return json.dumps({"detail": "unauthorized",
                       "hint": "需要先登录：浏览器打开 /login，或用 X-Sleep-Auth 头 / ?ct=<密码>"},
                      ensure_ascii=False).encode("utf-8")


async def _send(send, status: int, body: bytes, content_type: str,
                extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    headers = [(b"content-type", content_type.encode()),
               (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _cookie_header(value: str) -> tuple[bytes, bytes]:
    # 明文 HTTP 下不能加 Secure（加了浏览器会直接丢），部署到 HTTPS 后再补。
    cookie = (f"{COOKIE_NAME}={value}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
              f"HttpOnly; SameSite=Lax")
    return (b"set-cookie", cookie.encode())


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b"") or b"")
        if not message.get("more_body"):
            break
    return b"".join(chunks)


class PasswordGate:
    """ASGI 中间件：未通过密码校验的请求一律拦下。"""

    def __init__(self, app):
        self.app = app
        self._fails: dict[str, list[float]] = {}

    # --- 失败限流 -------------------------------------------------------
    def _blocked(self, ip: str) -> bool:
        now = time.time()
        hits = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW_SEC]
        self._fails[ip] = hits
        return len(hits) >= FAIL_MAX

    def _record_fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())
        if len(self._fails) > 512:  # 防内存膨胀
            now = time.time()
            self._fails = {k: [t for t in v if now - t < FAIL_WINDOW_SEC]
                           for k, v in self._fails.items()}

    # --- 主入口 ---------------------------------------------------------
    async def __call__(self, scope, receive, send) -> None:
        pw = password()
        if scope["type"] != "http" or not pw:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "/")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in (scope.get("headers") or [])}
        ip = (scope.get("client") or ("?", 0))[0]
        method = scope.get("method", "GET")

        # /login：本中间件自己处理（应用里没有这个路由）
        if path == "/login":
            await self._handle_login(scope, receive, send, headers, ip)
            return

        if path in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return

        supplied, kind = self._supplied_credential(scope, headers)
        if supplied and self._credential_ok(supplied, kind):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            if kind == "password" and (query.get("ct") or query.get("token")):
                # ?ct=<密码>：种 cookie 后跳到去掉查询串的同一 URL
                clean = {k: v for k, v in query.items() if k not in ("ct", "token")}
                qs = urlencode({k: v[0] for k, v in clean.items()})
                target = path + (f"?{qs}" if qs else "")
                await _send(send, 303, b"", "text/plain",
                            [(b"location", target.encode()), _cookie_header(token())])
                return
            await self.app(scope, receive, send)
            return

        # 未通过：API/非 HTML 给 JSON，页面给登录表单（状态码都是 401）
        accept = headers.get("accept", "")
        if path.startswith("/api/") or "text/html" not in accept:
            await _send(send, 401, _json_401(), "application/json; charset=utf-8")
            return
        await _send(send, 401, _login_page().encode("utf-8"),
                    "text/html; charset=utf-8")

    # --- 登录 -----------------------------------------------------------
    async def _handle_login(self, scope, receive, send, headers, ip) -> None:
        valid_cookie = False
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value and secrets.compare_digest(value, token()):
                valid_cookie = True
        if scope.get("method") != "POST":
            if valid_cookie:
                await _send(send, 303, b"", "text/plain", [(b"location", b"/")])
                return
            await _send(send, 200, _login_page().encode("utf-8"),
                        "text/html; charset=utf-8")
            return

        if self._blocked(ip):
            print(f"[Auth] 限流命中 ip={ip}", flush=True)
            await _send(send, 429, _login_page("尝试过于频繁，请几分钟后再试").encode("utf-8"),
                        "text/html; charset=utf-8", [(b"retry-after", b"120")])
            return

        body = await _read_body(receive)
        form = parse_qs(body.decode("utf-8", "replace"))
        given = (form.get("password") or [""])[0]
        if given and secrets.compare_digest(given, password()):
            print(f"[Auth] 登录成功 ip={ip}", flush=True)
            await _send(send, 303, b"", "text/plain",
                        [(b"location", b"/"), _cookie_header(token())])
            return

        self._record_fail(ip)
        await asyncio.sleep(FAIL_DELAY_SEC)
        print(f"[Auth] 登录失败 ip={ip}", flush=True)
        await _send(send, 401, _login_page("密码不正确").encode("utf-8"),
                    "text/html; charset=utf-8")

    # --- 凭据提取与校验 -------------------------------------------------
    @staticmethod
    def _supplied_credential(scope, headers) -> tuple[str, str]:
        """返回 (凭据值, 类型)。类型 "cookie"=已种 cookie 的令牌；"password"=明文密码
        （来自 ?ct= / ?token= / X-Sleep-Auth / X-Colab-Token）。两者校验对象不同：
        cookie 比 HMAC 令牌，密码比明文密码。"""
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value, "cookie"
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        for key in ("ct", "token"):
            values = query.get(key)
            if values and values[0]:
                return values[0], "password"
        header = headers.get("x-sleep-auth") or headers.get("x-colab-token") or ""
        return header, "password"

    @staticmethod
    def _credential_ok(value: str, kind: str) -> bool:
        expected = token() if kind == "cookie" else password()
        return bool(expected) and secrets.compare_digest(value, expected)


def wrap(app):
    """给 ASGI 应用套上密码闸门（serve_auth:application 用）。"""
    if not password():
        print("[Auth] SLEEP_AUTH_PASSWORD 为空 —— 密码闸门已关闭（直通）", flush=True)
    else:
        print(f"[Auth] 密码闸门已启用（cookie 有效期 {COOKIE_MAX_AGE // 86400} 天）",
              flush=True)
    return PasswordGate(app)
