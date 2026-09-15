"""MCP Token 检测."""
from fastapi import APIRouter

from ..config_manager import detect_local_mcp_token

router = APIRouter()


@router.get("/api/mcp/detect_token")
async def api_detect_token():
    token = detect_local_mcp_token()
    if token:
        # Mask for display but return full for use
        return {"ok": True, "token": token}
    return {"ok": False, "error": "未检测到本地 MCP Token"}
