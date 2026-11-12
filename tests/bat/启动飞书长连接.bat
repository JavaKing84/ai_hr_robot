@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Feishu long-connection bridge (no public IP needed)
echo  Start the server first, keep BOTH windows open.
echo ============================================
echo.
".venv\Scripts\python.exe" scripts\feishu_ws_bridge.py
echo.
echo [Bridge exited] Exit code %ERRORLEVEL%
pause
