#!/usr/bin/env bash
# 项目管理秘书 · 服务端启动脚本（Linux / macOS）
#
#   bash start.sh         前台跑，Ctrl+C 停   —— 第一次部署先用这个，能直接看到报错
#   bash start.sh -d      后台跑，日志写 server.log
#
# ★ 长期对外提供服务，别用上面这两种方式：
#     前台跑 —— SSH 窗口一关 / 会话超时，进程被 SIGHUP 杀掉，同事立刻调不通；
#     后台跑 —— 服务器重启后不会自己起来，还得手动再执行一次。
#   要常驻请用：  bash install_service.sh   （装成 systemd 服务，开机自启 + 崩了自动拉起）
#
# 端口默认 8200，可用环境变量覆盖：  SECRETARY_PORT=8300 bash start.sh
set -e
cd "$(dirname "$0")/secretary"

# 必须绑 0.0.0.0，否则只有服务器本机能访问，同事连不上
export SECRETARY_HOST="${SECRETARY_HOST:-0.0.0.0}"
export SECRETARY_PORT="${SECRETARY_PORT:-8200}"
export PYTHONIOENCODING=utf-8

# 检索面：问答与补全检索哪些知识库（逗号分隔，可多个）。
# 不设的话会退回 config.py 里的单库默认值（r57xtq9ypm）。库必须已加进检索服务的绑定范围，否则 401。
export SECRETARY_KB_IDS="${SECRETARY_KB_IDS:-r57xtq9ypm,razubo7dra}"

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 $PY，请先装 Python 3.9+（yum install -y python3 / apt install -y python3）"
  exit 1
fi

if [ ! -f ../.env ]; then
  echo "缺少 ../.env（数据库与模型凭据），请先把 .env 一起上传"
  exit 1
fi

if [ "$1" = "-d" ]; then
  nohup "$PY" -X utf8 server.py >> ../server.log 2>&1 &
  echo "已后台启动，PID $!   日志：tail -f ../server.log"
  sleep 2
  curl -s -m 5 "http://127.0.0.1:${SECRETARY_PORT}/health" || true
  echo ""
else
  echo "前台启动（Ctrl+C 停）。同事访问地址看下面打印的「同事访问」那行。"
  echo ""
  echo "⚠️  这是前台进程：关掉这个 SSH 窗口、或会话超时，服务马上停，同事就调不通了。"
  echo "    要长期对外服务，请改用：  bash install_service.sh"
  echo ""
  exec "$PY" -X utf8 server.py
fi
