@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0native-client\tools\launch_offline.py" --console
set "DB_CONSOLE_RESULT=%ERRORLEVEL%"
if not "%DB_CONSOLE_RESULT%"=="0" pause
exit /b %DB_CONSOLE_RESULT%
