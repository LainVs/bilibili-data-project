@echo off
cd /d "%~dp0"
set PYTHONPATH=%CD%
python main.py
if errorlevel 1 pause
