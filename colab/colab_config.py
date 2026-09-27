"""Colab 配置写入器：把仓库里为 Windows 准备的配置改写成 Colab/Linux 可直接跑的版本。

只做三件必须做的事：
1. 路径落地 —— output_dir 指向 Drive/本地输出目录，topics_file 指回仓库内 topics.json，
   清掉 H:\\ 开头的模型/素材路径（Colab 上不存在）
2. LLM 通道 —— Gemini 或自定义 OpenAI 兼容端点，写进 configs/mode_sleep.json
   （自定义端点同时写 configs/llm_providers.json）
3. 导出 /content/colab_env_llm.sh —— 不开 Web UI、直接命令行跑 pipeline.py 时 source 它

密钥只从环境变量读：不落仓库、不打印明文。
用法（notebook 里已封装）：
    LLM_PROVIDER_TYPE=gemini GEMINI_API_KEY=... COLAB_OUTPUT_DIR=... python3 colab/colab_config.py
"""
import os
import shlex
import sys
from pathlib import Path

REPO_DIR = Path(os.environ.get("REPO_DIR", "/content/sleep_english")).resolve()
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from app.config_manager import (  # noqa: E402
    get_active_mode, load_llm_providers, load_mode_config, save_llm_providers,
    save_mode_config, set_active_mode,
)

ENV_LLM_FILE = Path(os.environ.get("COLAB_ENV_LLM_FILE", "/content/colab_env_llm.sh"))
CUSTOM_PROVIDER_ID = "colab_openai"
MODE = "sleep"

# Colab 上没有的本机素材/模型路径字段：值形如 H:\... 就清空，让代码走内置回退
_WINDOWS_PATH_KEYS = (
    "qwen_model_path", "qwen_base_model_path", "qwen_voicedesign_model_path",
    "moss_model_path", "moss_tokenizer_path", "moss_repo_dir",
    "character_source", "character_library", "bgm_music_dir",
    "sleep_bg_image_path", "sleep_logo_path", "sleep_handwrite_font",
    "sleep_font_en", "sleep_font_ph", "sleep_font_zh",
    "sleep_intro_video", "sleep_outro_video",
)


def _env(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _mask(value: str) -> str:
    v = str(value or "")
    if not v:
        return "(未设置)"
    return f"{v[:4]}…{v[-4:]}" if len(v) > 12 else "(已设置)"


def _is_windows_path(value) -> bool:
    s = str(value or "")
    return len(s) > 2 and s[1] == ":" and ("\\" in s or "/" in s)


def resolve_llm(cfg: dict) -> tuple[str, dict]:
    """环境变量 → 写 cfg 的 LLM 字段，并返回 (provider 标识, 给 CLI 用的 env dict)。"""
    kind = _env("LLM_PROVIDER_TYPE").lower()
    gemini_key = _env("GEMINI_API_KEY")
    gemini_model = _env("GEMINI_MODEL")
    openai_key = _env("OPENAI_API_KEY")
    openai_base = _env("OPENAI_BASE_URL")
    openai_model = _env("OPENAI_MODEL")
    wbk_key = _env("WBK_API_KEY")

    if kind == "gemini" or (not kind and gemini_key):
        if not gemini_key:
            raise SystemExit("LLM_PROVIDER_TYPE=gemini 但 GEMINI_API_KEY 为空")
        cfg["llm_provider"] = "gemini"
        cfg["gemini_api_key"] = gemini_key
        if gemini_model:
            cfg["gemini_model"] = gemini_model
        return "gemini", {
            "LLM_PROVIDER": "gemini",
            "GEMINI_API_KEY": gemini_key,
            "GEMINI_MODEL": str(cfg.get("gemini_model", "")),
        }

    if kind == "openai" or (not kind and openai_key):
        missing = [n for n, v in (("OPENAI_API_KEY", openai_key),
                                  ("OPENAI_BASE_URL", openai_base),
                                  ("OPENAI_MODEL", openai_model)) if not v]
        if missing:
            raise SystemExit(f"自定义 OpenAI 兼容端点缺环境变量: {', '.join(missing)}")
        providers = [p for p in load_llm_providers() if p.get("id") != CUSTOM_PROVIDER_ID]
        providers.append({
            "id": CUSTOM_PROVIDER_ID,
            "name": "Colab OpenAI-compatible",
            "base_url": openai_base,
            "api_key": openai_key,
            "models": [openai_model],
        })
        save_llm_providers(providers)
        cfg["llm_provider"] = f"custom:{CUSTOM_PROVIDER_ID}"
        # resolve_provider 对 custom:* 用 wbk_model 字段存「所选模型」
        cfg["wbk_model"] = openai_model
        return cfg["llm_provider"], {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": openai_key,
            "OPENAI_BASE_URL": openai_base,
            "OPENAI_MODEL": openai_model,
        }

    if kind == "wbk" or wbk_key:
        cfg["llm_provider"] = "wbk"
        cfg["wbk_api_key"] = wbk_key
        if _env("WBK_MODEL"):
            cfg["wbk_model"] = _env("WBK_MODEL")
        return "wbk", {
            "LLM_PROVIDER": "wbk",
            "WBK_API_KEY": wbk_key,
            "WBK_MODEL": str(cfg.get("wbk_model", "cn:auto")),
        }

    return str(cfg.get("llm_provider", "")), {}


def write_cli_env(cli_env: dict) -> None:
    """把 CLI 跑 pipeline.py 需要的 env 落成可 source 的 shell 片段。"""
    ENV_LLM_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# 由 colab/colab_config.py 生成；命令行跑 pipeline.py 前 source 本文件"]
    for key in ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
                "WBK_API_KEY", "WBK_MODEL"):
        if cli_env.get(key):
            lines.append(f"export {key}={shlex.quote(str(cli_env[key]))}")
    lines.append(f"export LLM_RETRIES={shlex.quote(_env('LLM_RETRIES') or '10')}")
    ENV_LLM_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_LLM_FILE, 0o600)
    except OSError:
        pass


def main() -> None:
    # active_mode.json 是仓库里少数被 Git 跟踪的配置：内容已经是 sleep 就不重写，
    # 免得在 Colab 克隆里留下脏工作树、拖累下次 git pull --ff-only
    if get_active_mode() != MODE:
        set_active_mode(MODE)
    cfg = load_mode_config(MODE)

    out_dir = _env("COLAB_OUTPUT_DIR")
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        cfg["output_dir"] = out_dir
    cfg["topics_file"] = str(REPO_DIR / "pipeline" / "topics.json")
    cfg["used_topics_file"] = ""

    cfg["tts_engine"] = _env("COLAB_TTS_ENGINE") or "kokoro"
    cfg["no_4k"] = _flag("COLAB_NO_4K", True)
    cfg["sleep_4k_native"] = False
    cfg["no_thumbnail"] = _flag("COLAB_NO_THUMBNAIL", False)

    if _env("COLAB_TOPIC"):
        cfg["topic"] = _env("COLAB_TOPIC")
    if _env("COLAB_SLEEP_PAIRS").isdigit():
        cfg["sleep_pairs"] = max(10, min(400, int(_env("COLAB_SLEEP_PAIRS"))))
    if _env("COLAB_CHANNEL_NAME"):
        cfg["sleep_channel_name"] = _env("COLAB_CHANNEL_NAME")
    if _env("MCP_TOKENS"):
        cfg["mcp_tokens"] = _env("MCP_TOKENS")

    # Colab 上没有本机 BGM 曲库 / 127.0.0.1 代理，硬开会直接失败
    cfg["bgm_mix"] = False
    cfg["llm_proxy_enabled"] = False
    cfg["llm_proxy_url"] = ""
    for key in _WINDOWS_PATH_KEYS:
        if _is_windows_path(cfg.get(key)):
            cfg[key] = ""

    provider, cli_env = resolve_llm(cfg)
    save_mode_config(MODE, cfg)
    write_cli_env(cli_env)

    print(f"配置已写入 {REPO_DIR / 'configs' / f'mode_{MODE}.json'}")
    print(f"  LLM 通道     : {provider or '(未设置 —— Step 0 生成脚本会失败)'}")
    print(f"  密钥         : {_mask(cli_env.get('GEMINI_API_KEY') or cli_env.get('OPENAI_API_KEY') or cli_env.get('WBK_API_KEY'))}")
    print(f"  输出目录     : {cfg['output_dir']}")
    print(f"  主题库       : {cfg['topics_file']}")
    print(f"  TTS / 4K     : {cfg['tts_engine']} / {'关' if cfg['no_4k'] else '开'}")
    print(f"  组数 / 主题  : {cfg.get('sleep_pairs')} / {cfg.get('topic') or '(随机)'}")
    print(f"  CLI env 片段 : {ENV_LLM_FILE}")


if __name__ == "__main__":
    main()
