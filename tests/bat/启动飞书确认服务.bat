@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  AI-HR confirm service
echo  Opens 2 windows: HTTP server + Feishu bridge
echo  Keep BOTH open, then reply in Feishu.
echo ============================================
echo.
start "AI-HR Server" cmd /k ".venv\Scripts\python.exe -m uvicorn core.main:app --host 127.0.0.1 --port 8000"
timeout /t 5 >nul
start "Feishu Bridge" cmd /k ".venv\Scripts\python.exe scripts\feishu_ws_bridge.py"
echo.
echo Started. Now go to Feishu and reply:
echo    confirm            = send all pending interview notices
echo    confirm 1,3        = send only No.1 and No.3
echo    cancel             = send nothing
echo.
pause
