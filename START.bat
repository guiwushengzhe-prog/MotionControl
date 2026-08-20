@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
python -X utf8 server.py
if errorlevel 1 pause
