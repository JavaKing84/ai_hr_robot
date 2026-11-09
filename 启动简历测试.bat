@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "var" mkdir "var"
echo ============================================
echo  AI-HR server starting...
echo  Keep this window open. Close = stop server.
echo ============================================
echo.
".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port 8000
echo.
echo [Server exited] Exit code %ERRORLEVEL%
pause
