@echo off
setlocal EnableExtensions
chcp 65001 >nul

rem Asks for administrator rights, then runs fix_usb_tether_metric.ps1.
rem
rem Changing an interface metric is a system network setting, so Windows wants
rem elevation.  The app itself does not run elevated -- and should not, to
rem drive a gamepad -- so this is the one place that asks.

net session >nul 2>&1
if not errorlevel 1 goto :run

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_usb_tether_metric.ps1"
echo.
pause
