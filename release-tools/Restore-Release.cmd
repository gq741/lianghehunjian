@echo off
setlocal
chcp 65001 >nul
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Restore-Release.ps1" -DownloadDir "%~dp0." -DestinationParent "%~dp0." -RunCheck
set "RESTORE_RESULT=%ERRORLEVEL%"
echo.
if not "%RESTORE_RESULT%"=="0" echo Please read the restore report and WORKBUDDY.md before retrying.
pause
exit /b %RESTORE_RESULT%
