@echo off
setlocal
chcp 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Download-Release.ps1" -DownloadDir "%~dp0."
set "DOWNLOAD_RESULT=%ERRORLEVEL%"
echo.
if not "%DOWNLOAD_RESULT%"=="0" echo Read download-report.json and WORKBUDDY.md before retrying.
pause
exit /b %DOWNLOAD_RESULT%
