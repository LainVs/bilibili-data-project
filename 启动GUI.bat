@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title BiliData GUI Launcher

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

echo [INFO] Starting BiliData GUI...
set PYTHONPATH=%CD%
python gui_entry.py
endlocal
exit /b 0
