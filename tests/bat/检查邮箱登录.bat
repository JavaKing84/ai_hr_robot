@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
set PYTHONIOENCODING=utf-8
echo ============================================
echo  Mail login self-check (no fetch, no send)
echo  Checks IMAP and SMTP auth only.
echo ============================================
echo.
".venv\Scripts\python.exe" scripts\check_mail_login.py
set RC=%ERRORLEVEL%
echo.
if "%RC%"=="0" echo [RESULT] Mail channel OK, you can run the full cycle bat now.
if not "%RC%"=="0" echo [RESULT] Mail auth FAILED, fix the 163 auth code first.
echo.
pause
