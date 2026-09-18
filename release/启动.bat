@echo off
chcp 65001 >nul
cd /d "%~dp0app"
set PYTHONUTF8=1
set PYTHONNOUSERSITE=1
set PYTHONPATH=
"%~dp0python\python.exe" -X utf8 server.py
pause
