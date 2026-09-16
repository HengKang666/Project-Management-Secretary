@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 即将清除 DSH 本地缓存。请确认 DSH Desktop 已完全退出（含托盘）。
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0清除DSH本地缓存.ps1"
echo.
pause
