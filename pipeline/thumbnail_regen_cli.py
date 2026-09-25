"""缩略图重生成 CLI（独立子进程入口，供 Web 并行调用）。

Web 端 app/thumbnail_regen_service.py 通过 subprocess 启动本脚本：
- 参数经 stdin 传一行 UTF-8 JSON（避免 argv 中文/emoji run 目录名乱码）
- 日志直接 print 到 stdout，由 Web 层 PIPE 收集展示
- 退出码 0=成功 / 1=失败

与主 pipeline 完全进程隔离：独立 sys.stdout、独立 mcp_client 会话，
可与正在运行的 pipeline / 模式测试 / 4K 生成并行（不获取 run_mutex）。
"""
import json
import os
import sys
import traceback
from pathlib import Path

# Windows GBK 控制台安全输出
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# stdin 显式 UTF-8：Windows 子进程管道默认按 locale（GBK）解码，而父进程
# 写入的是 UTF-8 字节——中文 run 名会变乱码、emoji 变 \udcXX 代理转义，
# 导致 run_dir 路径 FileNotFoundError（双保险：父进程还用 ensure_ascii=True）
sys.stdin.reconfigure(encoding="utf-8", errors="replace")

_PARENT = str(Path(__file__).parent.resolve())
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)


def main() -> int:
    params = json.loads(sys.stdin.readline() or "{}")
    run_dir = Path(params["run_dir"])
    structure = params.get("structure", "sleep")
    out_name = params["out_name"]
    style_id = params.get("style_id", "pixar3d")
    style_prompt = params.get("style_prompt", "")

    print("=" * 60)
    print(f"ThumbnailRegen: {run_dir.name} -> {out_name}")

    # 先读脚本（路径错误快速失败，不白初始化 MCP）
    script = json.loads((run_dir / "script.json").read_text(encoding="utf-8"))

    # env 注入（与 Web 端 _set_env 的最小子集一致）
    os.environ["VISUAL_STYLE_ID"] = style_id
    if style_prompt:
        os.environ["VISUAL_STYLE_PROMPT"] = style_prompt

    from thumbnail_gen import generate_thumbnail

    # 生图 Provider 分派（与主 pipeline _step45_thumbnail 一致）：
    # aixoras / sensenova（HTTP）→ image_gen_fn；mcp → 初始化 MCP 会话 + 回调。
    image_provider = str(params.get("image_provider", "mcp") or "mcp")
    image_gen_fn = None
    mcp_call_tool = mcp_parse_task_id = mcp_poll_task = mcp_download_file = None
    if image_provider != "mcp":
        from image_http import generate_image_http
        if image_provider == "sensenova":
            api_key = str(params.get("image_sensenova_api_key", "") or "").strip()
            base_url = str(params.get("image_sensenova_base_url", "")
                           or "https://token.sensenova.cn/v1")
            model = str(params.get("image_sensenova_model", "")
                        or "sensenova-u1.5-fast")
            size = str(params.get("image_sensenova_size", "") or "2720x1536")
            quality = ""
        else:  # aixoras
            api_key = str(params.get("image_aixoras_api_key", "") or "").strip()
            base_url = str(params.get("image_aixoras_base_url", "")
                           or "https://api.aixoras.com/v1")
            model = str(params.get("image_aixoras_model", "") or "gpt-image-2")
            size = str(params.get("image_aixoras_size", "") or "1536x1024")
            quality = "standard"
        if api_key:
            def image_gen_fn(prompt: str, dest: str) -> bool:
                output_format = ("jpeg" if str(dest).lower().endswith((".jpg", ".jpeg"))
                                 else "png")
                return generate_image_http(
                    prompt, dest, api_key=api_key, base_url=base_url,
                    model=model, size=size, quality=quality,
                    output_format=output_format)
        else:
            print(f"ThumbnailRegen: HTTP 生图通道（{image_provider}）未配置 Key —— 回退 Pillow 兜底")
    else:
        from mcp_client import initialize as mcp_initialize
        tokens = [t.strip() for t in params.get("mcp_tokens", []) if t.strip()]
        mcp_initialize(tokens=tokens or None)
        from pipeline import call_tool, parse_task_id, poll_task, download_file
        mcp_call_tool = call_tool
        mcp_parse_task_id = parse_task_id
        mcp_poll_task = poll_task
        mcp_download_file = download_file
    char_scene_url = ""

    # sleep 缩略图：AI 分支纯 prompt 生成；Pillow 兜底卡需要 sleep 主题配色
    sleep_theme = None
    sleep_channel = ""
    if structure == "sleep":
        from sleep.sleep_cards import build_theme
        sleep_cfg = params.get("sleep_cfg", {}) or {}
        sleep_theme = build_theme(sleep_cfg)
        sleep_channel = str(sleep_cfg.get("sleep_channel_name", "")
                            or "English with me")

    # 场景图：仅 Pillow 兜底分支需要；sleep AI 分支纯 prompt 生成不受影响
    scene_img = run_dir / "images" / "scene.png"

    out_path = generate_thumbnail(
        script=script,
        scene_img=str(scene_img),
        output_path=str(run_dir / out_name),
        mcp_call_tool=mcp_call_tool,
        mcp_parse_task_id=mcp_parse_task_id,
        mcp_poll_task=mcp_poll_task,
        mcp_download_file=mcp_download_file,
        structure=structure,
        char_scene_url=char_scene_url,
        sleep_theme=sleep_theme,
        sleep_channel=sleep_channel,
        image_gen_fn=image_gen_fn,
    )
    if out_path and Path(out_path).exists():
        print("=" * 60)
        print(f"ThumbnailRegen DONE! {out_name}")
        return 0
    print("ThumbnailRegen FAILED: AI 生成与 Pillow 兜底均未产出文件"
          "（检查生图 Provider 配置 / MCP token / images 场景图）")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        print(f"ThumbnailRegen ERROR: {traceback.format_exc()}")
        sys.exit(1)
