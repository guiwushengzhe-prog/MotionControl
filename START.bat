@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0START.ps1"
if errorlevel 1 pause
