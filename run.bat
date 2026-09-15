@echo off
cd /d "%~dp0"
echo Starting Sleep English Web UI on http://localhost:8766
echo Press Ctrl+C to stop.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8766 --reload
pause
