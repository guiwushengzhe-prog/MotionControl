@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0BUILD_GAME_LIBRARY.ps1" -SeedFile "game_profiles\seeds_priority.json" -RefreshExisting %*
exit /b %ERRORLEVEL%
