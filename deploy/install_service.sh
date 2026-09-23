#!/usr/bin/env bash
# 把本服务装成 systemd 常驻服务（开机自启 + 崩溃自动拉起 + 与 SSH 会话无关）
#
#   用法：  cd /你的路径/deploy_server
#           sudo bash install_service.sh
#
#   卸载：  sudo bash install_service.sh uninstall
#
# 装完就不用管了（也不会再出现"今天打不开、要重新启动"这种事）：
#   systemctl status secretary      看状态
#   systemctl restart secretary     重启
#   systemctl stop secretary        停止
#   journalctl -u secretary -f      看实时日志
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SEC="$DIR/secretary"
SVC=/etc/systemd/system/secretary.service
PORT="${SECRETARY_PORT:-8200}"

# ---------- 卸载 ----------
if [ "$1" = "uninstall" ]; then
  systemctl disable --now secretary 2>/dev/null || true
  rm -f "$SVC"
  systemctl daemon-reload
  echo "✅ 已卸载 secretary 服务（服务已停止、开机不再自启）"
  exit 0
fi

if [ "$(id -u)" != "0" ]; then
  echo "请用 root 执行：sudo bash install_service.sh"
  exit 1
fi

[ -f "$SEC/server.py" ] || { echo "❌ 找不到 $SEC/server.py —— 请在 deploy_server 目录下执行本脚本"; exit 1; }
[ -f "$DIR/.env" ]      || { echo "❌ 找不到 $DIR/.env —— 数据库与模型凭据缺失，服务起不来"; exit 1; }

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "❌ 没找到 python3，先装：yum install -y python3  或  apt install -y python3"; exit 1; }

# ---------- 停掉手动前台跑的实例，否则端口冲突 ----------
if pgrep -f "server\.py" >/dev/null 2>&1; then
  echo "检测到手动启动的实例，先停掉..."
  pkill -f "server\.py" || true
  sleep 2
fi

# ---------- 生成 systemd 单元 ----------
cat > "$SVC" <<EOF
[Unit]
Description=项目管理秘书服务（随州智能问数）
After=network.target

[Service]
Type=simple
WorkingDirectory=$SEC
Environment=SECRETARY_HOST=0.0.0.0
Environment=SECRETARY_PORT=$PORT
Environment=PYTHONIOENCODING=utf-8
# ★ 检索面：问答与补全检索哪些知识库（逗号分隔，可多个）。
#   这些库必须**已经在百炼控制台加进该检索服务的绑定范围**，否则调用会 401。
#   改完要执行： systemctl daemon-reload && systemctl restart secretary
#   安装时想临时换检索服务/库： SECRETARY_KB_IDS=xxx bash install_service.sh
Environment=SECRETARY_KB_AGENT=${SECRETARY_KB_AGENT:-aid-72fa8cae2b124d819617f157e97d0a1d}
Environment=SECRETARY_KB_IDS=${SECRETARY_KB_IDS:-r57xtq9ypm,razubo7dra}
# 用 bash 重定向写日志，而不是 StandardOutput=append:path ——
# 「append:」是 systemd v240+ 才有的语法，CentOS 7（v219）上会直接启动失败。
# 包一层 bash 就与 systemd 版本无关了。
ExecStart=/bin/bash -c 'exec $PY -X utf8 server.py >> $DIR/server.log 2>&1'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable secretary >/dev/null 2>&1 || true
systemctl restart secretary
sleep 3

echo ""
if systemctl is-active --quiet secretary; then
  echo "✅ 安装成功 —— 服务已常驻，关掉 SSH 窗口也不受影响"
else
  echo "⚠️ 服务没起来，看日志：journalctl -u secretary -n 50 --no-pager"
fi

echo ""
echo "--- 本机自检 ---"
curl -s -m 5 "http://127.0.0.1:$PORT/health" && echo "" || echo "（本机无响应，执行 journalctl -u secretary -n 50 看原因）"

echo ""
echo "--- 常用命令 ---"
echo "  systemctl status secretary          看状态"
echo "  systemctl restart secretary         重启"
echo "  journalctl -u secretary -f          看实时日志"
echo "  tail -f $DIR/server.log             看输出日志"
echo "  bash install_service.sh uninstall   卸载"
echo ""
echo "--- 开机自启 ---"
systemctl is-enabled secretary 2>/dev/null && echo "已设为开机自启（服务器重启后会自动拉起）"
