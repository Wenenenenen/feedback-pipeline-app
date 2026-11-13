#!/usr/bin/env bash
# 在 WSL 中启动流水线后端（只监听本机），Windows 浏览器访问 http://localhost:8765
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${PIPELINE_HOME:-$HOME/jianzhi/feedback-pipeline}"
PORT="${PIPELINE_PORT:-8765}"
export PIPELINE_DATA="$RUNTIME/data"

# 首次运行：创建虚拟环境并安装依赖
if [ ! -x "$RUNTIME/.venv/bin/python" ]; then
  python3 -m venv "$RUNTIME/.venv"
fi
"$RUNTIME/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$PIPELINE_DATA"
cd "$APP_DIR"
exec "$RUNTIME/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT"
