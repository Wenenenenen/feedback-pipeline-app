#!/usr/bin/env bash
# 停止 WSL 中的流水线后端；有运行中的任务时默认不停并列出任务，加 --force 强制停止
# 用法：bash scripts/stop.sh [--force]
# 返回码：0 已停止或本来就没在运行，1 停止失败，3 有运行中的任务未停止
set -uo pipefail

PORT="${PIPELINE_PORT:-8765}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

PIDS="$(pgrep -f '[b]in/uvicorn app.main:app' || true)"
if [ -z "$PIDS" ]; then
  echo "后端没有在运行"
  exit 0
fi

if [ "$FORCE" -eq 0 ]; then
  # 停止后端会中断正在运行的出题、验收、返修，先列出来让人确认
  RUNNING="$(curl -sf "http://127.0.0.1:$PORT/api/runs" | python3 -c '
import json, sys
try:
    runs = json.load(sys.stdin)
except Exception:
    runs = []
for r in runs:
    if r.get("status") == "running":
        print("  %s  题目 %s  开始于 %s" % (r.get("run_key"), r.get("task_id") or "-", r.get("started_at")))
' 2>/dev/null)"
  # 自动出题、自动做题开关开着时，停止后端会让它们停下（重启后需要重新点开始）
  AUTO="$(curl -sf "http://127.0.0.1:$PORT/api/auto" | python3 -c '
import json, sys
try:
    s = json.load(sys.stdin)
except Exception:
    s = {}
names = {"gen": "补充题目池", "do": "自动做题"}
for k, n in names.items():
    st = (s.get(k) or {}).get("state")
    if st in ("running", "stopping", "finishing"):
        print("  %s  %s" % (n, {"running": "运行中", "stopping": "停止中", "finishing": "做完当前题后停止"}[st]))
' 2>/dev/null)"
  if [ -n "$AUTO" ]; then
    RUNNING="$RUNNING${RUNNING:+
}$AUTO"
  fi
  if [ -n "$RUNNING" ]; then
    echo "以下任务正在运行，停止后端会中断它们（重启后记为失败，需要重新发起）："
    echo "$RUNNING"
    exit 3
  fi
fi

echo "正在停止后端（进程 $(echo $PIDS)）"
kill $PIDS 2>/dev/null
for ((i = 0; i < 15; i++)); do
  pgrep -f '[b]in/uvicorn app.main:app' >/dev/null || { echo "后端已停止"; exit 0; }
  sleep 1
done
# 15 秒内没有正常退出时强制结束
pkill -9 -f '[b]in/uvicorn app.main:app'
sleep 1
if pgrep -f '[b]in/uvicorn app.main:app' >/dev/null; then
  echo "后端没能停止，请在 WSL 中手动结束 uvicorn 进程" >&2
  exit 1
fi
echo "后端已强制停止"
