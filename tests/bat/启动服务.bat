@echo off
rem ============================================================
rem  AI-HR robot - one click local service (port 8000)
rem  Keep this window open while you use the service.
rem  Close the window = stop the service.
rem
rem  NOTE: keep this file ASCII-only. cmd.exe parses .bat files with
rem  the system ANSI code page; Chinese text here gets mangled and
rem  cmd tries to run a fragment of it as a command (seen 2026-11-14).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
title AI-HR robot (port 8000)

echo ============================================================
echo   AI-HR recruiting robot  -  local service
echo ------------------------------------------------------------
echo   health    http://127.0.0.1:8000/health
echo   api docs  http://127.0.0.1:8000/docs
echo   funnel    http://127.0.0.1:8000/api/v1/recruitment/stats
echo ------------------------------------------------------------
echo   Wait for "Application startup complete", then open a link.
echo   Close this window to stop the service.
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found.
  echo         Put this file in the project root - next to README.md.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m core.main

echo.
echo ============================================================
echo   Service stopped. Exit code: %ERRORLEVEL%
echo   If it exited with an error, screenshot the red text above.
echo ============================================================
pause
