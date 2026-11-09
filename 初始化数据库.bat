@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "var" mkdir "var"
echo ============================================
echo  Init database
echo ============================================
".venv\Scripts\python.exe" scripts\seed.py --reset > "var\_seed_log.txt" 2>&1
set RC=%ERRORLEVEL%
type "var\_seed_log.txt"
echo.
if "%RC%"=="0" (
  echo [OK] Done. Now start the server.
) else (
  echo [FAILED] Exit code %RC%. See var\_seed_log.txt
)
echo.
pause
