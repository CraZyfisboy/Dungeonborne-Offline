@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "native-client\local-assets\install.json" (
    echo 请先双击「安装单机 MOD.cmd」。
    pause
    exit /b 1
)
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0native-client\tools\launch_offline.py" %*
set "DB_LAUNCH_RESULT=%ERRORLEVEL%"
if not "%DB_LAUNCH_RESULT%"=="0" pause
exit /b %DB_LAUNCH_RESULT%
