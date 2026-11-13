@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Pending interview notices (read only)
echo  Nothing will be sent by this script.
echo ============================================
echo.
".venv\Scripts\python.exe" scripts\interview_notices.py list
echo.
pause
