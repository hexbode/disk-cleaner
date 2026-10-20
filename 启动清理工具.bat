@echo off
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "disk_cleaner.py"
) else (
    python "disk_cleaner.py"
    pause
)
