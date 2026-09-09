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
echo 程序目录: dist\BiliDataAnalyzer\
echo 发送给朋友时，把整个 BiliDataAnalyzer 文件夹拷过去即可
echo 对方机器需安装 MySQL 8（或连接远程 MySQL）
pause
