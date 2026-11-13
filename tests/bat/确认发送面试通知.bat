@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Send pending interview notices
echo  Step 1: preview  Step 2: type SEND to confirm
echo ============================================
echo.
".venv\Scripts\python.exe" scripts\interview_notices.py list
echo.
echo --------------------------------------------
echo  Type SEND (capital letters) to really send
echo  to the candidates above. Anything else=abort.
echo --------------------------------------------
set "ANSWER="
set /p ANSWER=Your input: 
if /I not "%ANSWER%"=="SEND" goto abort
echo.
".venv\Scripts\python.exe" scripts\interview_notices.py send --all --yes
goto end
:abort
echo.
echo [ABORTED] Nothing was sent.
:end
echo.
pause
