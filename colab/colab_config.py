"""Colab 配置写入器：把仓库里为 Windows 准备的配置改写成 Colab/Linux 可直接跑的版本。

与以往 Colab 版最大的差别 —— **配置的事实源在 Drive**（由 colab/drive_config.py 把
configs/ 整个 symlink 到 Drive）。所以本脚本的定位从「每次开机重造配置」变成
「每次开机只做平台归一化 + 刷新密钥」：

  1. 永远归一化（环境事实，不归用户管）
       output_dir → 容器本地盘；topics_file → 仓库内 topics.json；used_topics_file → 置空
       （= <output>/used_topics.json，由 archive_to_drive.py 备份到 Drive）
       tts_engine → kokoro（Colab 没有 Qwen/MOSS 的大模型文件）
       sleep_4k_native / bgm_mix / 本机代理 → 关；H:\\ 开头的本机路径 → 清空
  2. 只在**首次生成配置**（Drive 上还没有 mode_sleep.json）或显式 COLAB_CONFIG_RESET=1 时
     采用参数区的 TOPIC / SLEEP_PAIRS / NO_4K / NO_THUMBNAIL / CHANNEL_NAME / MCP_TOKENS
     —— 之后一律以 Drive 上的（也就是 Web 控制台里改过的）值为准，开机不覆盖
  3. LLM 通道：Secrets 里给了密钥就刷新写入（便于轮换）；没给就沿用 Drive 里的旧值，
     不再报错退出（旧版是每次都从 Secrets 重造，缺密钥就失败）

密钥只从环境变量读：不落仓库、日志只打脱敏值。
用法（notebook 的「写入配置」格已封装）：
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

# 首跑判定必须在 load_mode_config 之前算：它一旦被调用就会落盘模式配置
FIRST_BOOT = not (REPO_DIR / "configs" / f"mode_{MODE}.json").exists()

# Colab 上没有的本机素材/模型路径字段：值形如 H:\... 就清空，让代码走内置回退
_WINDOWS_PATH_KEYS = (
    "qwen_model_path", "qwen_base_model_path", "qwen_voicedesign_model_path",
    "moss_model_path", "moss_tokenizer_path", "moss_repo_dir",
    "character_source", "character_library", "bgm_music_dir",
    "sleep_bg_image_path", "sleep_logo_path", "sleep_handwrite_font",
    "sleep_font_en", "sleep_font_ph", "sleep_font_zh",
    "sleep_intro_video", "sleep_outro_video",
)

# 只在首跑（或 COLAB_CONFIG_RESET=1）时从参数区落盘的业务参数
_PARAM_ENV_KEYS = ("COLAB_TOPIC", "COLAB_SLEEP_PAIRS", "COLAB_NO_4K",
                   "COLAB_NO_THUMBNAIL", "COLAB_CHANNEL_NAME", "MCP_TOKENS")


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
    """环境变量 → 写 cfg 的 LLM 字段，并返回 (provider 标识, 给 CLI 用的 env dict)。

    环境里没有密钥时**保持 cfg 现状**（Drive 上持久化的密钥继续生效），不算失败。
    """
    kind = _env("LLM_PROVIDER_TYPE").lower()
    gemini_key = _env("GEMINI_API_KEY")
    gemini_model = _env("GEMINI_MODEL")
    openai_key = _env("OPENAI_API_KEY")
    openai_base = _env("OPENAI_BASE_URL")
    openai_model = _env("OPENAI_MODEL")
    wbk_key = _env("WBK_API_KEY")

    if kind == "gemini" or (not kind and gemini_key):
        if not gemini_key:
            print("  !! LLM_PROVIDER_TYPE=gemini 但 GEMINI_API_KEY 为空 —— 沿用配置里已有的通道")
            return str(cfg.get("llm_provider", "")), {}
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
            print(f"  !! 自定义 OpenAI 兼容端点缺环境变量: {', '.join(missing)} —— 沿用配置里已有的通道")
            return str(cfg.get("llm_provider", "")), {}
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
        if not wbk_key:
            print("  !! LLM_PROVIDER_TYPE=wbk 但 WBK_API_KEY 为空 —— 沿用配置里已有的通道")
            return str(cfg.get("llm_provider", "")), {}
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
    # active_mode.json 是仓库里少数被 Git 跟踪的配置：内容已经是 sleep 就不重写
    if get_active_mode() != MODE:
        set_active_mode(MODE)
    cfg = load_mode_config(MODE)

    apply_params = FIRST_BOOT or _flag("COLAB_CONFIG_RESET", False)

    # ---- 1) 平台归一化：每次都做（幂等，不碰用户参数） ----------------------
    out_dir = _env("COLAB_OUTPUT_DIR")
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        cfg["output_dir"] = out_dir
    cfg["topics_file"] = str(REPO_DIR / "pipeline" / "topics.json")
    cfg["used_topics_file"] = ""          # = <output>/used_topics.json（Drive 有备份）
    cfg["tts_engine"] = "kokoro"          # Colab 没有 Qwen/MOSS 模型文件
    cfg["sleep_4k_native"] = False
    cfg["bgm_mix"] = False                # bgm_music_60s/ 未进 Git
    cfg["llm_proxy_enabled"] = False      # 本机 127.0.0.1 代理在 Colab 不存在
    cfg["llm_proxy_url"] = ""
    for key in _WINDOWS_PATH_KEYS:
        if _is_windows_path(cfg.get(key)):
            cfg[key] = ""

    # ---- 2) 业务参数：只在首跑 / 显式 RESET 时从参数区落盘 -------------------
    if apply_params:
        if _env("COLAB_TOPIC"):
            cfg["topic"] = _env("COLAB_TOPIC")
        if _env("COLAB_SLEEP_PAIRS").isdigit():
            cfg["sleep_pairs"] = max(10, min(400, int(_env("COLAB_SLEEP_PAIRS"))))
        if _env("COLAB_NO_4K"):
            cfg["no_4k"] = _flag("COLAB_NO_4K", True)
        if _env("COLAB_NO_THUMBNAIL"):
            cfg["no_thumbnail"] = _flag("COLAB_NO_THUMBNAIL", False)
        if _env("COLAB_CHANNEL_NAME"):
            cfg["sleep_channel_name"] = _env("COLAB_CHANNEL_NAME")
        if _env("MCP_TOKENS"):
            cfg["mcp_tokens"] = _env("MCP_TOKENS")

    # ---- 3) LLM 通道：给了密钥就刷新，没给就用 Drive 里的旧值 ----------------
    provider, cli_env = resolve_llm(cfg)
    save_mode_config(MODE, cfg)
    write_cli_env(cli_env)

    persisted = _flag("COLAB_CONFIG_PERSISTED", False)
    key_now = (cli_env.get("GEMINI_API_KEY") or cli_env.get("OPENAI_API_KEY")
               or cli_env.get("WBK_API_KEY") or cfg.get("gemini_api_key")
               or cfg.get("wbk_api_key") or "")
    print(f"配置已写入 {'Drive 上的 ' if persisted else ''}configs/mode_{MODE}.json")
    print(f"  配置来源     : {'首次生成（采用参数区）' if FIRST_BOOT else '沿用 Drive 上的既有配置'}"
          f"{'（COLAB_CONFIG_RESET=1 强制采用参数区）' if not FIRST_BOOT and apply_params else ''}")
    print(f"  LLM 通道     : {provider or '(未设置 —— Step 0 生成脚本会失败)'}")
    print(f"  密钥         : {_mask(key_now)}{'（本次由 Secrets 刷新）' if cli_env else '（沿用配置里的旧值）'}")
    print(f"  输出目录     : {cfg['output_dir']}")
    print(f"  主题库       : {cfg['topics_file']}")
    print(f"  TTS / 4K     : {cfg['tts_engine']} / {'关' if cfg.get('no_4k') else '开'}")
    print(f"  组数 / 主题  : {cfg.get('sleep_pairs')} / {cfg.get('topic') or '(随机)'}")
    print(f"  CLI env 片段 : {ENV_LLM_FILE}")


if __name__ == "__main__":
    main()
