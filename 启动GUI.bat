@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Bilibili GUI Launcher

REM ===== 提权子流程入口（由 powershell -Verb RunAs 回调，仅执行服务命令）=====
if "%1"==":elev_start" goto :elev_start
if "%1"==":elev_stop" goto :elev_stop

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

REM ===== 1. 检查两个库：已在跑的不碰，只记下没跑的 =====
set TODO=
for %%S in (MySQL80 MySQL80-E) do (
    sc query %%S 2>nul | find "RUNNING" >nul 2>nul
    if errorlevel 1 (
        echo [INFO] 服务 %%S 未运行，稍后启动它
        set "TODO=!TODO! %%S"
    ) else (
        echo [OK] 服务 %%S 已在运行（退出时不替你关它）
    )
)

REM ===== 2. 提权启动（只弹一次 UAC；GUI 本体仍是普通权限）=====
set MINE=
if not "!TODO!"=="" (
    echo [INFO] 正在请求管理员权限启动数据库服务（弹出的 UAC 点“是”即可）...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" :elev_start!TODO!\"' -Verb RunAs -WindowStyle Hidden"
    echo [INFO] 等待服务就绪（最多约90秒）...
    for %%S in (!TODO!) do (
        set OKFLAG=
        for /L %%i in (1,1,45) do (
            if not defined OKFLAG (
                sc query %%S 2>nul | find "RUNNING" >nul 2>nul
                if not errorlevel 1 (
                    set OKFLAG=1
                ) else (
                    timeout /t 2 /nobreak >nul
                )
            )
        )
        if defined OKFLAG (
            echo [OK] %%S 已启动
            set "MINE=!MINE! %%S"
        ) else (
            echo [WARN] %%S 启动超时，GUI 照常打开（E 离线时部分功能受限，可稍后点“管理员重连”）
        )
    )
)

echo [INFO] Starting GUI...
set PYTHONPATH=%CD%
echo [INFO] 关闭 GUI 窗口后，会自动停止本次替你启动的数据库服务。
echo [INFO] （若直接关掉本黑窗口，则跳过自动停止，库会继续跑着）
start /wait "bilibili-gui" python gui_entry.py

REM ===== 3. GUI 退出后，只停“本次确认替你启动”的服务 =====
if not "!MINE!"=="" (
    echo [INFO] 正在请求管理员权限停止本次启动的服务:!MINE!（UAC 点“是”即停，点“否”就继续跑着，不影响数据）
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" :elev_stop!MINE!\"' -Verb RunAs -WindowStyle Hidden"
    echo [DONE] Bye.
) else (
    echo [DONE] Bye.（数据库服务保持原样运行）
)
endlocal
exit /b 0

REM ===== 提权段：启动服务（管理员）=====
:elev_start
shift
:loop_start
if "%1"=="" exit /b 0
echo Starting %1 ...
net start %1
shift
goto :loop_start

REM ===== 提权段：停止服务（管理员）=====
:elev_stop
shift
:loop_stop
if "%1"=="" exit /b 0
echo Stopping %1 ...
net stop %1
shift
goto :loop_stop
