@echo off
chcp 65001 >nul
cd /d D:\秘书智能体\secretary
echo 启动项目管理秘书演示页面...
echo 打开浏览器访问 http://127.0.0.1:8200
py -X utf8 server.py
pause