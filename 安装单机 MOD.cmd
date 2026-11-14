@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "DB_PY=%~dp0runtime\python\python.exe"
if not exist "%DB_PY%" (
    echo 请使用包含运行环境的 Windows 安装 ZIP，或先按 README 构建源码。
    pause
    exit /b 1
)
"%DB_PY%" -B -X utf8 "%~dp0native-client\tools\release\install.py" %*
set "DB_INSTALL_RESULT=%ERRORLEVEL%"
pause
exit /b %DB_INSTALL_RESULT%
