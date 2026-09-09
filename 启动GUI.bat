@echo off
setlocal
cd /d "%~dp0"
title Bilibili GUI Launcher
echo ============================================
echo   Bilibili Danmaku/Comment Collector GUI
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.9+ and add it to PATH.
    pause
    exit /b 1
)

python -c "import pymysql, tkinter" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Missing dependency "pymysql", installing now...
    python -m pip install pymysql --quiet
)

echo [INFO] Starting GUI...
set PYTHONPATH=%CD%
start "bilibili-gui" python gui_entry.py
echo [DONE] GUI launched. Close this window if it stays open.
endlocal