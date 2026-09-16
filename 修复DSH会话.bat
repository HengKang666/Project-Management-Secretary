@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 即将修复 DSH 坏会话。请确认 DSH Desktop 已完全退出。
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0修复DSH会话.ps1"
echo.
pause
