@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title BiliData System Launcher

if "%1"==":elev_start" goto :elev_start

echo ======================================================
echo       BiliData 弹幕/评论采集与分析系统
echo ======================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 Python 环境，请安装 Python 3.9+ 并添加到 PATH。
    pause
    exit /b 1
)

REM 1. 检查 MySQL 服务状态
set NEED_START=
sc query MySQL80 2>nul | find "RUNNING" >nul 2>nul
if errorlevel 1 (
    echo [INFO] 缓冲库服务 (MySQL80) 尚未运行
    set "NEED_START=!NEED_START! MySQL80"
)

if exist "E:\" (
    sc query MySQL80-E 2>nul | find "RUNNING" >nul 2>nul
    if errorlevel 1 (
        echo [INFO] 归档库服务 (MySQL80-E) 尚未运行
        set "NEED_START=!NEED_START! MySQL80-E"
    )
)

REM 2. 如有未启动的服务，单次 UAC 提权静默拉起
if not "!NEED_START!"=="" (
    echo [INFO] 正在申请管理员权限启动数据库服务（请在 UAC 弹窗中允许）...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" :elev_start!NEED_START!\"' -Verb RunAs -WindowStyle Hidden"
    echo [INFO] 等待数据库服务就绪...
    for %%S in (!NEED_START!) do (
        for /L %%i in (1,1,20) do (
            sc query %%S 2>nul | find "RUNNING" >nul 2>nul
            if not errorlevel 1 goto :done_%%S
            timeout /t 1 /nobreak >nul
        )
        :done_%%S
    )
)

echo [INFO] 启动图形界面中...
set PYTHONPATH=%CD%
python main.py

endlocal
exit /b 0

:elev_start
shift
:loop_start
if "%1"=="" exit /b 0
net start %1 >nul 2>nul
shift
goto :loop_start
