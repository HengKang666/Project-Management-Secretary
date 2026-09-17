@echo off
chcp 65001 >nul
cd /d "%~dp0secretary"
set SECRETARY_HOST=0.0.0.0
echo 启动项目管理秘书（局域网可访问）...
echo 下面会打印“同事访问”的地址，把那行连同端口发给同事即可。
echo 若同事打不开，多半是 Windows 防火墙没放行 8200 端口（见 局域网访问说明.md）。
py -X utf8 server.py
pause
