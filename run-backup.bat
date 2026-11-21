@echo off
rem Double-click launcher for fastmail_backup.py (Windows)
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0fastmail_backup.py" %*
) else (
    python "%~dp0fastmail_backup.py" %*
)
pause
