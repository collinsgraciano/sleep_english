"""LLM 基础设施 — sleep 脚本生成与 Web 层（脚本库/主题 AI/音色设计）共用。

Provider: SenseNova (OpenAI 兼容) / OpenAI 兼容端点 / Gemini，代理与限速在 _chat 内实现。
No external project imports.
"""
import contextlib
import json
import re
import os
import sys
import threading
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path


# 线程局部 LLM 环境覆盖：Web 批量生成等后台线程自带 provider 配置，
# 与运行中 pipeline 线程读的 os.environ 互不污染（并发运行互不干扰）。
_LLM_ENV_TLS = threading.local()


def set_llm_env_override(cfg: dict | None) -> None:
    """设置当前线程的 LLM 环境覆盖（传 None 清除）。

    键名与 env var 同名（LLM_PROVIDER / OPENAI_API_KEY / ...）；
    未覆盖的键回退 os.environ。仅影响调用线程内的 LLM 调用。
    """
    _LLM_ENV_TLS.cfg = cfg


def _env_get(name: str, default: str = "") -> str:
    """os.environ.get 的线程局部覆盖版：当前线程的 override 优先。"""
    cfg = getattr(_LLM_ENV_TLS, "cfg", None)
    if cfg is not None and name in cfg:
        return cfg[name]
    return os.environ.get(name, default)


def _env_int_clamped(name: str, default: int, lo: int, hi: int) -> int:
    """线程局部 override 优先读取整型 env 配置，clamp 到 [lo, hi]。"""
    raw = (_env_get(name, "") or "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        val = default
    return max(lo, min(hi, val))


def resolve_max_line_words(env_name: str = "LISTENING_MAX_LINE_WORDS",
                           default: int = 10) -> int:
    """对话/旁白每句最大词数（字幕最多两行约束）：env（线程局部优先）→ clamp [4,20]。"""
    return _env_int_clamped(env_name, default, 4, 20)


def _env_flag(name: str) -> bool:
    """布尔 env 解析（"1"/"true"/"yes"/"on"，大小写不敏感，线程局部优先）。"""
    return _env_get(name, "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# 用户停止（「停止运行」按钮即时生效）：线程局部 stop hook + 可中断 sleep。
# LLMStoppedError 继承 BaseException（非 Exception），穿透脚本生成的全部
# except Exception 重试层（Attempt×N / retry×M），由 pipeline_service
# 的 _run_inner 显式捕获并标记 stopped。仅 pipeline 工作线程注册 hook，
# Web 端其他线程的 LLM 调用（topics AI / 脚本库 / ai_test）行为不变。
# ---------------------------------------------------------------------------

class LLMStoppedError(BaseException):
    """用户请求停止：LLM 等待/重试循环探测到 stop hook 后抛出。"""


def set_llm_stop_hook(hook) -> None:
    """设置当前线程的停止探测（传 None 清除）。hook() → True 即中止。

    线程局部（复用 _LLM_ENV_TLS）：仅影响注册它的线程。
    """
    _LLM_ENV_TLS.stop_hook = hook


def _stop_requested() -> bool:
    hook = getattr(_LLM_ENV_TLS, "stop_hook", None)
    return bool(hook and hook())


def _check_stop() -> None:
    """停止探测：命中即抛 LLMStoppedError（穿透所有 except Exception 层）。"""
    if _stop_requested():
        raise LLMStoppedError("stopped by user")


def _sleep_interruptible(sec: float) -> None:
    """可中断 sleep：每 0.5s 探测一次用户停止，命中抛 LLMStoppedError。

    LLM 重试退避最长单轮 315s（15+30+60+90+120）——不可中断的 sleep
    曾导致点「停止运行」后要等十几分钟才真正停。
    """
    import time as _time
    remaining = float(sec)
    while remaining > 0:
        _check_stop()
        _time.sleep(min(0.5, remaining))
        remaining -= 0.5


# Rate limiting: enforce minimum interval between LLM API calls to avoid HTTP 429.
# glm-5.2 is especially aggressive about "request rate increased too quickly".
_LAST_CALL_TIME = 0.0  # 最近一次调用的开始时刻（预约槽位）
_RATE_LOCK = threading.Lock()


def _get_min_call_interval() -> float:
    """Read LLM_MIN_INTERVAL per-call (thread-local override first)."""
    return float(_env_get("LLM_MIN_INTERVAL", "5.0"))


def _enforce_rate_limit(min_interval: float | None = None):
    """预约式限速：相邻两次 LLM 调用的开始时刻间隔 ≥ min_interval。

    min_interval 缺省读 LLM_MIN_INTERVAL（线程局部 override 优先）；
    显式传入时用调用方的值（topics_ai / script_library 网关按各自配置），
    但共享同一 _LAST_CALL_TIME，使 Web 批量任务与 pipeline 并发时限速互认，
    避免两路同时打同一 provider 触发 429。
    """
    import time as _time
    global _LAST_CALL_TIME
    with _RATE_LOCK:
        interval = _get_min_call_interval() if min_interval is None else float(min_interval)
        now = _time.time()
        start_at = max(now, _LAST_CALL_TIME + interval)
        _LAST_CALL_TIME = start_at  # 预约本次调用的开始槽位
    if start_at > now:
        _sleep_interruptible(start_at - now)


def _dump_raw_debug(raw: str, model: str, kind: str) -> str | None:
    """Write a full raw LLM response to LLM_DEBUG_DIR for offline inspection.

    Returns the file path on success, or None when LLM_DEBUG_DIR is unset or
    the write fails — debug dumping must never break the pipeline.
    """
    debug_dir = os.environ.get("LLM_DEBUG_DIR", "").strip()
    if not debug_dir:
        return None
    try:
        from datetime import datetime as _dt
        d = Path(debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe_model = re.sub(r"[^\w.-]", "_", model) or "model"
        path = d / f"{kind}_{safe_model}_{_dt.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        path.write_text(raw, encoding="utf-8")
        return str(path)
    except OSError:
        return None


def _diagnose_response(result: dict) -> str:
    """Build a compact human-readable diagnosis of an LLM response dict.

    Surfaces the fields that explain empty/failed completions (finish_reason,
    message keys, reasoning_content length, usage counts, provider error).
    """
    parts: list[str] = []
    try:
        choices = result.get("choices") or []
        if choices:
            finish = choices[0].get("finish_reason")
            if finish:
                parts.append(f"finish_reason={finish!r}")
            msg = choices[0].get("message") or {}
            keys = list(msg.keys())
            if keys:
                parts.append(f"message keys={keys}")
            rc = msg.get("reasoning_content") or msg.get("reasoning")
            if rc:
                parts.append(f"reasoning_content={len(str(rc))} chars")
    except (AttributeError, IndexError, TypeError):
        pass
    usage = result.get("usage")
    if isinstance(usage, dict):
        u = {k: usage[k] for k in
             ("prompt_tokens", "completion_tokens", "reasoning_tokens", "total_tokens")
             if k in usage}
        if u:
            parts.append(f"usage={u}")
    if result.get("error"):
        parts.append(f"error={result['error']}")
    return "; ".join(parts) if parts else "no diagnostic fields found"


# HTTP 层可重试状态码与退避表（_chat 与 gemini_chat 共用）
_RETRY_CODES = [429, 502, 503, 504, 524]
_RETRY_BACKOFFS = [15, 30, 60, 90, 120]

# ---------------------------------------------------------------------------
# LLM 代理支持（全部 Provider 通用）：llm_proxy_enabled + llm_proxy_url。
# 支持 http(s):// 与 socks5://、socks5h://（socks 系列 DNS 一律经代理解析，
# 防本地 DNS 污染）。仅作用于 LLM API 调用窗口；MCP 生图/视频、SenseNova 生图、
# TTS、模型下载等其他流量不受影响。
# ---------------------------------------------------------------------------

_SOCKS_SCHEMES = ("socks5h", "socks5", "socks4")
_PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _proxy_scheme(proxy_url: str) -> str:
    return urllib.parse.urlparse(proxy_url).scheme.lower()


def _llm_proxy_url() -> str:
    """env 版代理读取（线程局部 override 优先），仅 llm_proxy_enabled 时非空。"""
    if not _env_flag("LLM_PROXY_ENABLED"):
        return ""
    return _env_get("LLM_PROXY_URL", "").strip()


def proxy_url_from_config(config: dict) -> str:
    """config dict 版代理读取，供 Web 直连调用方（topics_ai 等）使用。"""
    if not config.get("llm_proxy_enabled"):
        return ""
    return str(config.get("llm_proxy_url") or "").strip()


@contextlib.contextmanager
def _socks_socket_window(proxy_url: str):
    """urllib + SOCKS 代理窗口：PySocks 临时接管 socket.socket，结束即恢复。

    urllib 没有 per-opener 的 socks 钩子，PySocks 官方用法即临时替换
    socket.socket；rdns=True 让 DNS 经代理解析（防本地 DNS 污染）。
    """
    import socket as _socket
    import socks
    u = urllib.parse.urlparse(proxy_url)
    ptype = {"socks5": socks.SOCKS5, "socks5h": socks.SOCKS5,
             "socks4": socks.SOCKS4}.get(_proxy_scheme(proxy_url), socks.SOCKS5)
    socks.set_default_proxy(ptype, u.hostname, u.port or 1080, rdns=True,
                            username=u.username, password=u.password)
    saved = _socket.socket
    _socket.socket = socks.socksocket
    try:
        yield
    finally:
        _socket.socket = saved


@contextlib.contextmanager
def _requests_proxy_env(proxy_url: str):
    """httpx/requests（google-genai SDK）代理窗口：临时设 HTTP(S)_PROXY。

    SDK 在 genai.Client 构造时一次性创建 httpx.Client 并冻结代理（读 env），
    故窗口必须覆盖 Client 构造与全部请求；socks5:// 归一为 socks5h://，
    与 urllib 侧 rdns=True 的远端 DNS 行为保持一致。
    """
    if not proxy_url:
        yield
        return
    if _proxy_scheme(proxy_url) == "socks5":
        proxy_url = "socks5h://" + proxy_url.split("://", 1)[1]
    saved = {k: os.environ.get(k) for k in _PROXY_ENV_KEYS}
    for k in _PROXY_ENV_KEYS:
        os.environ[k] = proxy_url
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def llm_urlopen(req, timeout: int, proxy_url: str = ""):
    """urllib.urlopen 的 LLM 代理感知版。

    无代理→urlopen；socks→PySocks 窗口内 urlopen；http(s)→显式 ProxyHandler
    opener（urllib 全局 opener 会缓存首次 getproxies() 的结果，env 窗口对
    长驻 Web 进程不可靠，必须逐请求显式建 opener）。
    """
    if not proxy_url:
        return urllib.request.urlopen(req, timeout=timeout)
    if _proxy_scheme(proxy_url) in _SOCKS_SCHEMES:
        with _socks_socket_window(proxy_url):
            return urllib.request.urlopen(req, timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return opener.open(req, timeout=timeout)


# ---------------------------------------------------------------------------
# Gemini（google-genai SDK，Interactions API）：llm_provider="gemini"
# 参考 https://ai.google.dev/gemini-api/docs/interactions-overview
# input 支持 string（单轮）或 array(Step)（无状态多轮，user_input/model_output）；
# system_instruction 独立参数；generation_config 含 max_output_tokens /
# thinking_level(minimal|low|medium|high) / temperature；interaction.output_text
# 为 SDK 拼接的最后一轮模型文本。
# ---------------------------------------------------------------------------

_GEMINI_DEFAULT_MODEL = "models/gemini-3.8-flash"
_GEMINI_THINKING_LEVELS = ("minimal", "low", "medium", "high")

# 限流自动降级链（从新到旧；仅含免费档实际有配额的文本输出模型——Pro 系 0 配额、
# TTS/图像/Agent 类不在列）。配置的模型是起点，受限时向更旧方向逐个降级。
_GEMINI_FALLBACK_CHAIN = [
    "models/gemini-3.8-flash",
    "models/gemini-3.7-flash",
    "models/gemini-3.6-flash",
    "models/gemini-3.5-flash",
    "models/gemini-3.5-flash-lite",
    "models/gemini-3.1-flash-lite",
    "models/gemini-3-flash",
    "models/gemini-2.5-flash",
    "models/gemini-2.5-flash-lite",
]
# 触发降级的状态码：429 限流(RPM/TPM/RPD)、503 过载、400 参数不被该模型支持、
# 403/404 该模型无权限或不存在。401 鉴权不降级（全局问题立即报错）；
# 5xx 网关/网络错误不降级（对同模型退避重试）。
_GEMINI_FALLBACK_CODES = (400, 403, 404, 429, 503)
# 整条链全部 429 时（RPM 窗口恢复需要时间），等待后整链重试的轮数与间隔
_GEMINI_CHAIN_RETRIES = 1
_GEMINI_CHAIN_RETRY_WAIT = 30

# Gemini 专用退避（比共享 _RETRY_BACKOFFS 短）：代理抖动/网关错误不干等
# 15s+；仅 gemini_chat 使用，sensenova/openai 的 urllib 路径退避不变。
_GEMINI_RETRY_BACKOFFS = [5, 10, 20, 40]

# 降级记忆：降级后成功的模型在 24h 内作为后续调用的链起点（不再从头试
# 配置模型），超时自动释放回配置模型。仅进程内存，重启即失效。
_GEMINI_FALLBACK_TTL = 24 * 3600
_GEMINI_LAST_FALLBACK: dict[str, float] = {}  # {bare_model: 记录时刻}
_GEMINI_FALLBACK_LOCK = threading.Lock()

# Client 连接复用：httpx 连接池按 Client 实例隔离，每次新建要重付代理
# 握手 + TLS 建连；按 (api_key, proxy_url) 缓存复用，代理在构造时冻结。
_GEMINI_CLIENTS: dict[str, object] = {}
_GEMINI_CLIENTS_LOCK = threading.Lock()


def _gemini_remember_fallback(model: str) -> None:
    """记录降级成功的模型（单槽，仅保留最近一次）。"""
    import time as _time
    bare = (model or "").split("/")[-1]
    if not bare:
        return
    with _GEMINI_FALLBACK_LOCK:
        _GEMINI_LAST_FALLBACK.clear()
        _GEMINI_LAST_FALLBACK[bare] = _time.time()


def _gemini_recent_fallback() -> str:
    """24h 内降级成功的模型 bare 名；过期清除并返回空串。"""
    import time as _time
    with _GEMINI_FALLBACK_LOCK:
        for bare, ts in list(_GEMINI_LAST_FALLBACK.items()):
            if _time.time() - ts > _GEMINI_FALLBACK_TTL:
                _GEMINI_LAST_FALLBACK.pop(bare, None)
                return ""
        return next(iter(_GEMINI_LAST_FALLBACK), "")


def _get_gemini_client(api_key: str, proxy_url: str):
    """按 (api_key, proxy_url) 缓存复用 genai.Client（构造需覆盖代理 env 窗口）。"""
    from google import genai as _genai
    cache_key = f"{api_key}|{proxy_url}"
    with _GEMINI_CLIENTS_LOCK:
        cached = _GEMINI_CLIENTS.get(cache_key)
    if cached is not None:
        return cached
    proxy_cm = (_requests_proxy_env(proxy_url) if proxy_url
                else contextlib.nullcontext())
    with proxy_cm:
        client = _genai.Client(api_key=api_key)
    with _GEMINI_CLIENTS_LOCK:
        return _GEMINI_CLIENTS.setdefault(cache_key, client)


def _gemini_fallback_chain(model: str) -> list[str]:
    """配置模型起点 + 从新到旧的降级候选链。

    配置模型在链内 → 从它的下一个更旧模型接续；不在链内（如 Pro 或未来新模型）
    → 先试配置模型，再走整条链兜底。比较时忽略 "models/" 前缀。
    """
    bare = model.split("/")[-1] if model else ""
    chain = [model] if model else []
    start = 0
    for i, m in enumerate(_GEMINI_FALLBACK_CHAIN):
        if m.split("/")[-1] == bare:
            start = i + 1
            break
    chain.extend(_GEMINI_FALLBACK_CHAIN[start:])
    return chain


def _gemini_input_and_system(messages: list[dict]) -> tuple:
    """chat messages → (input, system_instruction)。

    单轮：input 用字符串（官方示例形式）；多轮：input 用 array(Step)
    无状态全历史（assistant → model_output step，其余 → user_input step）。
    """
    system_parts = [str(m.get("content") or "") for m in messages
                    if m.get("role") == "system"]
    system_instruction = "\n\n".join(p for p in system_parts if p.strip())
    turns = [m for m in messages if m.get("role") != "system"]
    if len(turns) <= 1:
        text = str(turns[0].get("content") or "") if turns else ""
        return text, system_instruction
    steps = []
    for m in turns:
        step_type = "model_output" if m.get("role") == "assistant" else "user_input"
        steps.append({
            "type": step_type,
            "content": [{"type": "text", "text": str(m.get("content") or "")}],
        })
    return steps, system_instruction


def gemini_chat(api_key: str, model: str, messages: list[dict], *,
                temperature: float = 0.8, timeout: int = 600,
                max_tokens: int = 8192, reasoning_effort: str = "low",
                proxy_url: str = "") -> str:
    """同步调 Gemini Interactions API，返回输出文本（公开函数，Web 直连功能复用）。

    代理窗口覆盖 genai.Client 构造与全部请求（httpx 代理在构造时冻结）。
    模型从新到旧自动降级：配置模型受限（429/503/400/403/404）时切换到下一个
    更旧模型；401 鉴权立即报错；网络/网关错误对同模型退避重试；整链全部 429
    时等待后整链重试一轮（RPM 窗口恢复）。max_tokens 恒拉满 16384（推理模型
    不再烧尽预算出空响应）；空输出直接降级下一个模型。
    Client 按 (api_key, 代理) 缓存复用连接；降级成功的模型 24h 内直接作为
    后续调用的链起点（不再从头试配置模型）。
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    try:
        from google import genai as _genai
    except ImportError as e:
        raise RuntimeError(
            "未安装 google-genai SDK，请先执行：pip install google-genai") from e

    # max_tokens 直接拉满（用户需求：不再从 2048/8192 起步逐级翻倍爬升）
    if max_tokens < 16384:
        max_tokens = 16384

    thinking = reasoning_effort if reasoning_effort in _GEMINI_THINKING_LEVELS else "low"
    input_value, system_instruction = _gemini_input_and_system(messages)
    proxy_cm = (_requests_proxy_env(proxy_url) if proxy_url
                else contextlib.nullcontext())
    chain = _gemini_fallback_chain(model)
    recent = _gemini_recent_fallback()
    if recent:
        for _idx, _m in enumerate(chain):
            if _m.split("/")[-1] == recent:
                if _idx > 0:
                    chain = chain[_idx:]
                    print(f"  [Gemini] 使用 24h 内降级记忆 {_m} 作为起点"
                          "（跳过已限流的更早候选）")
                break
    with proxy_cm:
        client = _get_gemini_client(api_key, proxy_url)
        for chain_round in range(_GEMINI_CHAIN_RETRIES + 1):
            failures: list[str] = []
            all_rate_limited = True
            for model_idx, current_model in enumerate(chain):
                attempt = 0
                while True:
                    _check_stop()  # 用户停止即时生效（穿透 BaseException）
                    _enforce_rate_limit()  # 共享限速槽位（与 sensenova/openai 互认）
                    try:
                        kwargs = {
                            "model": current_model,
                            "input": input_value,
                            "generation_config": {
                                "max_output_tokens": max_tokens,
                                "thinking_level": thinking,
                                "temperature": temperature,
                            },
                            "store": False,
                            "timeout": timeout,  # create() 的 per-call 超时，单位秒
                        }
                        if system_instruction:
                            kwargs["system_instruction"] = system_instruction
                        interaction = client.interactions.create(**kwargs)
                    except Exception as e:  # noqa: BLE001 — SDK/网络异常统一归类
                        code = getattr(e, "code", None)
                        if code is None:
                            # google-genai ≥2.23 interactions 兼容错误类
                            # （RateLimitError 等）只带 status_code 不带 code，
                            # 旧版 ClientError 仍带 code，两种都兼容
                            code = getattr(e, "status_code", None)
                        if code in _GEMINI_FALLBACK_CODES:
                            # 该模型受限/不可用 → 降级到下一个更旧模型
                            failures.append(f"{current_model}: HTTP {code}")
                            if code != 429:
                                all_rate_limited = False
                            nxt = chain[model_idx + 1] \
                                if model_idx + 1 < len(chain) else None
                            print(f"  [Gemini] {current_model} 受限（HTTP {code}）"
                                  + (f"，降级到 {nxt}" if nxt else "，已无更旧候选"))
                            break
                        if code is not None and code not in _RETRY_CODES:
                            raise RuntimeError(f"Gemini API HTTP {code}: {e}") from e
                        if attempt < len(_GEMINI_RETRY_BACKOFFS):
                            wait = _GEMINI_RETRY_BACKOFFS[attempt]
                            attempt += 1
                            reason = (f"HTTP {code}" if code is not None else
                                      f"网络错误 ({type(e).__name__}: {str(e)[:120]})")
                            print(f"  [Gemini] {current_model} {reason}，{wait}s 后重试 "
                                  f"({attempt}/{len(_GEMINI_RETRY_BACKOFFS)})...")
                            _sleep_interruptible(wait)
                            continue
                        raise RuntimeError(
                            f"Gemini API 调用失败（重试后仍失败）: "
                            f"{type(e).__name__}: {e}") from e
                    text = (getattr(interaction, "output_text", None) or "").strip()
                    if text:
                        if model_idx > 0:
                            print(f"  [Gemini] 已降级使用 {current_model} 成功")
                            _gemini_remember_fallback(current_model)
                        return text
                    # 空输出：按模型级失败处理，降级下一个模型
                    failures.append(f"{current_model}: 空输出")
                    all_rate_limited = False
                    print(f"  [Gemini] {current_model} 空输出，降级下一个模型...")
                    break
            if not failures:
                break  # 防御性保护：循环内必然 return / append / raise
            if all_rate_limited and chain_round < _GEMINI_CHAIN_RETRIES:
                print(f"  [Gemini] 整链 {len(chain)} 个模型全部限流（429），"
                      f"{_GEMINI_CHAIN_RETRY_WAIT}s 后整链重试一轮...")
                _sleep_interruptible(_GEMINI_CHAIN_RETRY_WAIT)
                continue
            raise RuntimeError(
                "Gemini 全部候选模型不可用（从新到旧已尝试 "
                + str(len(chain)) + " 个）: " + "; ".join(failures))


def _chat(messages: list[dict], temperature: float = 0.8, timeout: int = 180,
          max_tokens: int = 8192, reasoning_effort: str = "low") -> str:
    """Call LLM chat completion (SenseNova / OpenAI-compatible / Gemini), return content string.

    Dispatches based on LLM_PROVIDER env var:
    - "sensenova" (default): SenseNova DeepSeek V4 Flash / glm-5.2
    - "openai": any OpenAI-compatible endpoint (x666.me, etc.)
    - "gemini": Google Gemini via google-genai SDK (Interactions API)

    max_tokens is always raised to 16384 (reasoning models burn small budgets).
    Retries on HTTP 429 (rate limit) with exponential backoff.
    Enforces a minimum interval between calls to avoid triggering rate limits.
    """
    # max_tokens 直接拉满（用户需求：不再从 2048/8192 起步逐级翻倍爬升）
    if max_tokens < 16384:
        max_tokens = 16384

    provider = _env_get("LLM_PROVIDER", "sensenova")

    if provider == "gemini":
        return gemini_chat(
            _env_get("GEMINI_API_KEY", ""),
            _env_get("GEMINI_MODEL", _GEMINI_DEFAULT_MODEL),
            messages,
            temperature=temperature,
            timeout=max(timeout, 600),  # SDK per-call 超时（秒），不短于默认 600s
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            proxy_url=_llm_proxy_url())

    # 全 Provider 共用的 LLM 代理（llm_proxy_enabled + llm_proxy_url）
    proxy_url = _llm_proxy_url()

    if provider == "openai":
        model = _env_get("OPENAI_MODEL", "grok-4.6")
        api_key = _env_get("OPENAI_API_KEY", "")
        base_url = _env_get("OPENAI_BASE_URL", "https://x666.me/v1")
    else:
        model = _env_get("SENSENOVA_MODEL", "deepseek-v4-flash")
        api_key = _env_get("SENSENOVA_API_KEY", "")
        base_url = _env_get("SENSENOVA_BASE", "https://token.sensenova.cn/v1")

    # Retry on 429 (rate limit), 502/503/504 (gateway), 524 (Cloudflare timeout)
    # _mt_fallback_done: 个别 Provider 拒绝 16384 上限（HTTP 400 报文提到
    # max_tokens）时回退 8192 重试一次（channel_factory 同款先例）
    _mt_fallback_done = False

    for _retry_attempt in range(len(_RETRY_BACKOFFS) + 1):
        _check_stop()  # 用户停止即时生效（穿透 BaseException）
        _enforce_rate_limit()
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        # reasoning_effort is SenseNova-specific; OpenAI-compatible APIs don't support it
        if provider != "openai":
            body["reasoning_effort"] = reasoning_effort
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            method="POST",
        )
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        # Cloudflare-protected endpoints (e.g. x666.me) block default Python User-Agent with 403
        req.add_header("User-Agent", "CodelyLLM/1.0")
        try:
            with llm_urlopen(req, timeout, proxy_url) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    raise RuntimeError("LLM returned empty response body (HTTP 200, 0 bytes)")
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    dbg = _dump_raw_debug(raw, model, "non_json")
                    print(f"  [LLM] Non-JSON response (model={model}). "
                          f"Full raw response ({len(raw)} chars)"
                          f"{' saved to ' + dbg if dbg else ''}:")
                    print(raw)
                    print("  [LLM] End raw response")
                    raise RuntimeError(
                        f"LLM returned non-JSON response (model={model}). "
                        f"Full raw ({len(raw)} chars)"
                        f"{' saved to ' + dbg if dbg else ''} shown above: {raw}"
                    ) from None
                if "choices" not in result or not result["choices"]:
                    diag = _diagnose_response(result)
                    dbg = _dump_raw_debug(raw, model, "no_choices")
                    print(f"  [LLM] Response has no 'choices' (model={model}). {diag}")
                    print(f"  [LLM] Full raw response ({len(raw)} chars)"
                          f"{' saved to ' + dbg if dbg else ''}:")
                    print(raw)
                    print("  [LLM] End raw response")
                    raise RuntimeError(
                        f"LLM response has no 'choices' field (model={model}). {diag}. "
                        f"Full raw ({len(raw)} chars)"
                        f"{' saved to ' + dbg if dbg else ''} shown above: {raw}"
                    )
                content = result["choices"][0]["message"]["content"]
                if not content or not content.strip():
                    # 空响应（HTTP 200）：max_tokens 已恒拉满 16384，无翻倍爬升
                    # 余地 — 直接报错交由外层重试（reasoning 烧尽已诊断在 diag）
                    diag = _diagnose_response(result)
                    dbg = _dump_raw_debug(raw, model, "empty_content")
                    print(f"  [LLM] Empty content (HTTP 200, model={model}). {diag}")
                    print(f"  [LLM] Full raw response ({len(raw)} chars)"
                          f"{' saved to ' + dbg if dbg else ''}:")
                    print(raw)
                    print("  [LLM] End raw response")
                    raise RuntimeError(
                        f"LLM returned empty content (HTTP 200, model={model}). {diag}. "
                        f"Full raw ({len(raw)} chars)"
                        f"{' saved to ' + dbg if dbg else ''} shown above: {raw}"
                    )
                # 限速槽位已在 _enforce_rate_limit 中预约（start-to-start 间隔），
                # 无需在响应后再次记录时间。
                return content
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            if (e.code == 400 and not _mt_fallback_done
                    and "max_tokens" in err.lower()):
                # 个别 Provider 拒绝 16384 上限 → 回退 8192 重试一次
                _mt_fallback_done = True
                max_tokens = 8192
                print(f"  [LLM] Provider rejected max_tokens=16384, "
                      f"retrying with 8192... Model: {model}")
                continue
            if e.code in _RETRY_CODES and _retry_attempt < len(_RETRY_BACKOFFS):
                wait = _RETRY_BACKOFFS[_retry_attempt]
                reason = ("rate limited" if e.code == 429
                          else "gateway error" if e.code in (502, 503, 504)
                          else "gateway timeout")
                print(f"  [LLM] HTTP {e.code} ({reason}), "
                      f"waiting {wait}s before retry "
                      f"({_retry_attempt+1}/{len(_RETRY_BACKOFFS)})... "
                      f"Model: {model}")
                _sleep_interruptible(wait)
                continue
            raise RuntimeError(f"LLM HTTP {e.code}: {err}") from e
        except OSError as e:
            # 网络层瞬断（连接超时/拒绝/DNS/读超时/服务器断连）— HTTPError 的
            # 父类也是 OSError，故放在其后；与 429 同样走 backoff 重试，
            # 避免一次网络抖动报废整个 quest 会话（20+ 次调用）。
            if _retry_attempt < len(_RETRY_BACKOFFS):
                wait = _RETRY_BACKOFFS[_retry_attempt]
                print(f"  [LLM] Network error ({type(e).__name__}: {e}), "
                      f"waiting {wait}s before retry "
                      f"({_retry_attempt+1}/{len(_RETRY_BACKOFFS)})... "
                      f"Model: {model}")
                _sleep_interruptible(wait)
                continue
            raise RuntimeError(
                f"LLM network error after retries: {type(e).__name__}: {e}") from e


def _repair_truncated_json(text: str) -> str:
    """Attempt to repair truncated JSON by closing open strings, arrays, and objects."""
    # Count unmatched braces/brackets
    in_string = False
    escape = False
    stack = []
    i = 0
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == '\\' and in_string:
            escape = True
            i += 1
            continue
        if c == '"' and not escape:
            in_string = not in_string
        elif not in_string:
            if c == '{':
                stack.append('}')
            elif c == '[':
                stack.append(']')
            elif c in ('}', ']'):
                if stack and stack[-1] == c:
                    stack.pop()
        i += 1
    # If we're in an unterminated string, close it (a trailing lone backslash
    # must be doubled first, otherwise the appended quote stays escaped)
    if in_string:
        if escape:
            text += "\\"
        text += '"'
    stripped = text.rstrip()
    if stack and stack[-1] == '}':
        # Cut inside an object: dangling colon → empty value; dangling key
        # (a full quoted string right after { or ,) → append ": \"\"" so the
        # object stays parseable instead of "Expecting ',' delimiter"
        if stripped.endswith(':'):
            text = stripped + ' ""'
        elif re.search(r'[{,]\s*"(?:[^"\\]|\\.)*"\s*$', stripped):
            text = stripped + ': ""'
        else:
            text = re.sub(r',\s*$', '', stripped)
    else:
        text = re.sub(r',\s*$', '', stripped)
    # Close all open structures
    while stack:
        text += stack.pop()
    return text


def _extract_json(text: str) -> dict:
    """Extract JSON from LLM response (handles markdown fences + truncated JSON)."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try repairing truncated JSON
        repaired = _repair_truncated_json(text)
        return json.loads(repaired)



def generate_random_voice_designs(count: int = 10,
                                  avoid_names: list[str] | None = None,
                                  language: str = "english",
                                  gender: str = "any") -> list[dict]:
    """Generate diverse random VoiceDesign voice specs via LLM.

    用于 Web 端「自定义音色 → LLM 随机生成」：LLM 批量产出音色设计候选，
    用户试听后挑选喜欢的保存为设计音色。
    gender: "female"/"male" 只生成该性别，"any" 混合。

    Returns:
        list[dict]: 每项 {name, gender, language("en"/"zh"), description, instruct}
    """
    avoid = ", ".join(sorted(n for n in (avoid_names or []) if n)) or "(none)"
    want_gender = gender if gender in ("female", "male") else ""

    lang_req = {
        "english": 'All voices speak ENGLISH. Set every "language" field to "en".',
        "chinese": 'All voices speak CHINESE (Mandarin). Set every "language" field to "zh".',
        "mixed": 'Mix languages: roughly half English ("en") and half Chinese ("zh"). '
                 'Set each "language" field individually.',
    }.get(language, 'All voices speak ENGLISH. Set every "language" field to "en".')

    gender_req = {
        "female": 'All voices MUST be FEMALE. Set every "gender" field to "female".',
        "male": 'All voices MUST be MALE. Set every "gender" field to "male".',
    }.get(want_gender, 'Mix genders: roughly half female ("female") and half male ("male"). '
                        'Set each "gender" field individually.')

    prompt = f"""You are a creative voice director designing voices for Qwen3-TTS VoiceDesign.
Generate {count} DIVERSE, DISTINCT voice designs for language learning videos.

{lang_req}
{gender_req}

Each voice design MUST include these fields:
- "name": a short English given name (3-10 letters, capitalized, e.g. "Luna", "Jasper"). Unique within the list.
- "gender": "female" or "male"
- "language": "en" or "zh" (as required above)
- "description": a short Chinese summary of the voice character, ≤14 chars (e.g. "温柔知性美式女声")
- "instruct": an English description for the VoiceDesign model (2-4 sentences). Must cover:
  (1) timbre: age feel, pitch (high/mid/low), texture (bright/warm/husky/crisp/deep/soft)
  (2) accent (e.g. General American, British RP)
  (3) style: pace, energy, intonation, personality
  (4) learner-friendly delivery: clear articulation, natural pauses

Example instructs (follow this style):
- "Speak in a warm, friendly young American female voice with a mid-range pitch and a relaxed, moderate pace. Sound conversational and expressive, like a native speaker talking with a student."
- "Speak in a mature, confident American female voice with clear articulation, suitable for narration. Keep a steady, engaging pace with gentle emphasis on key phrases."

DIVERSITY requirements — cover a wide spectrum:
- Mix age feels (youthful / young adult / middle-aged / senior)
- Mix pitches: high, mid, low
- Mix energies: calm, warm, lively, energetic, playful, gentle, husky, crisp, confident
- Vary use cases: conversation partner, narrator, cheerful host, storyteller
- For English voices, mostly General American; optionally 1-2 British RP

CONSTRAINTS:
- Every voice must be CLEAR and easy for ESL learners to understand — no mumbling, no extreme speed, no heavy regional accents
- Names MUST NOT duplicate any of these existing voice names: {avoid}
- Do not duplicate names within the list

Output JSON ONLY (no markdown fences):
{{"voices": [{{"name": "...", "gender": "...", "language": "...", "description": "...", "instruct": "..."}}, ...]}}"""

    content = _chat(
        [{"role": "user", "content": prompt}],
        temperature=1.0,
        max_tokens=8192,
        reasoning_effort="low",
    )
    data = _extract_json(content)
    raw = data.get("voices", []) if isinstance(data, dict) else (
        data if isinstance(data, list) else [])

    # Normalize + validate: drop empty entries and name collisions
    seen = set(avoid_names or [])
    result: list[dict] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name", "")).strip()
        v_gender = str(v.get("gender", "")).strip().lower()
        lang = str(v.get("language", "")).strip().lower()
        instruct = str(v.get("instruct", "")).strip()
        desc = str(v.get("description", "")).strip()
        if not name or not instruct or name in seen:
            continue
        # 指定性别时丢弃不符项（instruct 描述的音色性别与标签一致，改标签会造成错配）
        if want_gender and v_gender != want_gender:
            continue
        if v_gender not in ("female", "male"):
            v_gender = "female"
        if lang not in ("en", "zh"):
            lang = "en"
        seen.add(name)
        result.append({
            "name": name,
            "gender": v_gender,
            "language": lang,
            "description": desc or f"随机设计音色 ({'女声' if v_gender == 'female' else '男声'})",
            "instruct": instruct,
        })
        if len(result) >= count:
            break
    if not result:
        raise RuntimeError("LLM 未返回有效的音色设计")
    return result

