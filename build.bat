@echo off
rem ===== 一键打包脚本：python -m PyInstaller build.spec =====
cd /d "%~dp0"
echo [1/2] 清理旧产物...
if exist dist\BiliDataAnalyzer rmdir /s /q dist\BiliDataAnalyzer
if exist build\BiliDataAnalyzer rmdir /s /q build\BiliDataAnalyzer

echo [2/2] PyInstaller 打包（约 1-3 分钟）...
python -m PyInstaller build.spec --noconfirm

if errorlevel 1 (
    echo 打包失败，请查看上方错误信息
    pause
    exit /b 1
)
echo.
echo ============ 打包完成 ============
echo 产物目录: dist\BiliDataAnalyzer\
echo 运行环境: Windows x64, 目标机器需可连接 MySQL 8.0+ 数据库
pause
