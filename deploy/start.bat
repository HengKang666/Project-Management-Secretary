@echo off
chcp 65001 >nul
cd /d "%~dp0secretary"

rem 必须绑 0.0.0.0，否则只有服务器本机能访问，同事连不上
set SECRETARY_HOST=0.0.0.0
set SECRETARY_PORT=8200
set PYTHONIOENCODING=utf-8

echo ============================================
echo  项目管理秘书服务
echo  端口：8200
echo ============================================
echo.
echo 前台启动中（关掉这个窗口服务就停了）...
echo.

py -X utf8 server.py

pause
