@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0..\.."

echo ============================================================
echo  Semantic score A/B  (read only: no mail, no Feishu, no interview notice)
echo ============================================================
echo.
echo  It parses resumes, scores them twice (rules only vs rules+semantic),
echo  prints a table and writes a CSV under var\ab_semantic\.
echo  Nothing is sent to candidates or supervisors.
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run the app once to create it first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" scripts\ab_semantic.py %*

echo.
echo Done. CSV files are in var\ab_semantic\
pause
