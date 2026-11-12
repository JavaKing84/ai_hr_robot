@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Run full cycle: poll - ingest - screen - Feishu
echo  (Start the server first, keep that window open)
echo ============================================
echo.
curl -s -m 120 -X POST http://127.0.0.1:8000/api/v1/recruitment/run
echo.
echo.
echo Check JSON above: screened_passed ^>=1 and ok=true means success.
echo If you see Connection refused, start the server first.
echo.
pause
