@echo off
cd /d "%~dp0"
set PYTHONPATH=%CD%
python main.py gui --engine web
if errorlevel 1 (
  echo.
  echo WebView engine failed to start. Falling back to classic Tk UI...
  python main.py gui --engine tk
  if errorlevel 1 pause
)
