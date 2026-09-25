"""OpenAI 兼容 /v1/images/generations 生图通道（AIxoras 等）。

供 sleep 背景图（Step 2）与缩略图（Step 4.5）在 image_provider != "mcp" 时
使用；与 MCP 通道互斥。接口对齐 AIxoras 图片生成：

    POST {base_url}/images/generations
    {model, prompt, n, size | aspect_ratio, quality, response_format: "url",
     watermark: false}
    → {"data": [{"url": "..."}]}  或 b64_json：data[0].b64_json

注：实测 gpt-image-2 / gpt-image-2.5-flare / gpt-image-2.5-sunburst 走
ChatGPT/Codex 后端，只认 size（1024x1024 / 1536x1024 / 1024x1536）、
无视 response_format 恒返回 b64_json；gpt-image-2-1k 按文档走 aspect_ratio。
本模块两参皆可传：size 非空时发 size，否则发 aspect_ratio（两参都空则不传，
由模型默认）。响应 url 与 b64_json 都兼容。

下载走 llm_client.llm_urlopen（复用 LLM 代理感知：无代理 / socks / http(s)），
成功落盘为文件后返回 True；任何失败返回 False（调用方回退 Pillow 兜底，
不中断与生图无关的 sleep 运行）。
"""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

try:
    from llm_client import llm_urlopen
except Exception:  # noqa: BLE001 — 独立导入兜底（pipeline 已在 sys.path）
    llm_urlopen = None

_DEFAULT_BASE_URL = "https://api.aixoras.com/v1"
_DEFAULT_MODEL = "gpt-image-2"


def _urlopen(req, timeout, proxy_url):
    if llm_urlopen is not None:
        return llm_urlopen(req, timeout, proxy_url)
    return urllib.request.urlopen(req, timeout=timeout)


def generate_image_http(prompt: str, dest: str, api_key: str,
                        base_url: str = "", model: str = "",
                        size: str = "", aspect_ratio: str = "",
                        quality: str = "", output_format: str = "",
                        n: int = 1, proxy_url: str = "",
                        timeout: int = 300) -> bool:
    """调用 OpenAI 兼容生图端点并把图片下载到 dest。成功返回 True。

    size 非空时发 size（gpt-image-2 系列 / SenseNova 均用此参数），
    否则发 aspect_ratio；两参都空则不传。quality / output_format 为空时不发
    （SenseNova 无 quality；output_format 控制 png/jpeg/webp，由其按 dest 后缀传入）。
    response_format 固定 "url"、watermark 固定 False（去水印），响应 url 缺失
    时回退 b64_json（AIxoras 恒返回 b64_json）。
    """
    api_key = str(api_key or "").strip()
    if not api_key:
        print("  [ImageHTTP] 未配置 API Key —— 跳过生图")
        return False
    base_url = (str(base_url or "").strip() or _DEFAULT_BASE_URL).rstrip("/")
    model = str(model or "").strip() or _DEFAULT_MODEL
    endpoint = f"{base_url}/images/generations"
    body = {
        "model": model,
        "prompt": str(prompt or "").strip(),
        "n": max(1, int(n)),
        "response_format": "url",
        "watermark": False,
    }
    if str(size or "").strip():
        body["size"] = str(size).strip()
    elif str(aspect_ratio or "").strip():
        body["aspect_ratio"] = str(aspect_ratio).strip()
    if str(quality or "").strip():
        body["quality"] = str(quality).strip()
    if str(output_format or "").strip():
        body["output_format"] = str(output_format).strip()
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "CodelyImage/1.0")

    try:
        with _urlopen(req, timeout, proxy_url) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        print(f"  [ImageHTTP] 生图 HTTP {e.code}: {detail}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [ImageHTTP] 生图请求失败: {e}")
        return False

    data = result.get("data") or []
    if not isinstance(data, list) or not data:
        print(f"  [ImageHTTP] 生图响应无 data: {str(result)[:200]}")
        return False
    item = data[0] if isinstance(data[0], dict) else {}
    url = str(item.get("url", "") or "").strip()
    b64 = str(item.get("b64_json", "") or "").strip()

    if url:
        try:
            req2 = urllib.request.Request(url)
            req2.add_header("User-Agent", "CodelyImage/1.0")
            with _urlopen(req2, timeout, proxy_url) as resp:
                content = resp.read()
        except Exception as e:  # noqa: BLE001
            print(f"  [ImageHTTP] 下载图片失败: {e}")
            return False
    elif b64:
        try:
            content = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            print(f"  [ImageHTTP] b64_json 解码失败: {e}")
            return False
    else:
        print("  [ImageHTTP] 生图响应既无 url 也无 b64_json")
        return False

    if not content or len(content) < 10_000:
        print(f"  [ImageHTTP] 图片内容过小（{len(content)}B），判失败")
        return False
    try:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(content)
    except OSError as e:
        print(f"  [ImageHTTP] 图片落盘失败: {e}")
        return False
    print(f"  [ImageHTTP] 图片已生成: {dest} ({len(content) // 1024}KB)")
    return True
