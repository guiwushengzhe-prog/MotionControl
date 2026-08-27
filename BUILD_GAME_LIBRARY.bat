@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BUILD_GAME_LIBRARY.ps1" %*
exit /b %ERRORLEVEL%
