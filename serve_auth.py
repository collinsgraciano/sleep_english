"""受密码保护的 uvicorn 入口（VPS systemd 用这个，而不是 app.main:app）。

    /opt/sleep_english/.venv/bin/python -m uvicorn serve_auth:application \
        --host 0.0.0.0 --port 8766

- 密码：环境变量 SLEEP_AUTH_PASSWORD（默认 inriynisse）；置空 = 关闭闸门。
- 行为与免鉴权路径见 app/auth_gate.py 的模块说明。
- 本机 `run.bat` 与 Colab（colab/serve.sh + secure_gate.py）不受影响，仍走 app.main:app。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth_gate import wrap  # noqa: E402
from app.main import app as _app  # noqa: E402

application = wrap(_app)
