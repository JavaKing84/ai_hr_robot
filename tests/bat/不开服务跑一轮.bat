@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Run one full cycle WITHOUT the web server
echo  poll - ingest - screen - Feishu - email
echo ============================================
echo.
".venv\Scripts\python.exe" scripts\run_cycle_once.py
echo.
echo Saved into the same var\hr.db and same mail cursor.
echo.
echo To reply in Feishu (confirm / cancel), run 启动飞书确认服务.bat first.
echo.
pause
