#!/usr/bin/env bash
# 等待后台启动的流水线后端就绪：持续输出本次启动新增的日志，直到接口可访问、进程退出或超时
# 用法：bash scripts/wait-ready.sh <启动前日志的行数> [超时秒数，默认 180]
# 返回码：0 就绪，1 进程已退出（启动失败），2 超时
set -uo pipefail

RUNTIME="${PIPELINE_HOME:-$HOME/jianzhi/feedback-pipeline}"
PORT="${PIPELINE_PORT:-8765}"
LOG="$RUNTIME/data/logs/server.log"
FROM="${1:-0}"
TIMEOUT="${2:-180}"

mkdir -p "$(dirname "$LOG")"
touch "$LOG"
# 只跟踪本次启动之后写入的日志
tail -n +"$((FROM + 1))" -F "$LOG" 2>/dev/null &
TAIL_PID=$!
trap 'kill "$TAIL_PID" 2>/dev/null' EXIT

for ((i = 0; i < TIMEOUT; i++)); do
  if curl -sf -o /dev/null "http://127.0.0.1:$PORT/api/meta"; then
    sleep 0.5  # 让最后几行启动日志输出完
    exit 0
  fi
  # 首次启动要建虚拟环境、装依赖，期间只有 start.sh 在跑；两者都不在时说明启动失败
  if ((i >= 5)) && ! pgrep -f '[b]in/uvicorn app.main:app' >/dev/null \
      && ! pgrep -f '[s]cripts/start\.sh' >/dev/null; then
    sleep 0.5
    exit 1
  fi
  sleep 1
done
exit 2
