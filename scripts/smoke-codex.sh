#!/usr/bin/env bash
# 新环境冒烟：确认 WSL 能调起 Windows 版 codex、ChatGPT 登录有效、agent/ 下的 skill 能被发现、
# 按 schema 输出、codex 能回调 wsl.exe 执行命令、能用 \\wsl.localhost 路径读 WSL 文件。
# 用法（在 WSL 中，先启动过一次 start.sh 以创建虚拟环境）：bash scripts/smoke-codex.sh
# 只做一次低推理强度调用；临时 skill 与 schema 跑完即删除，不影响正式运行。
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${PIPELINE_HOME:-$HOME/jianzhi/feedback-pipeline}"
PY="$RUNTIME/.venv/bin/python"
[ -x "$PY" ] || { echo "未找到 $PY，请先运行一次 scripts/start.sh" >&2; exit 1; }

# 读取流水线配置（页面「配置」中保存的值优先）
eval "$(cd "$APP_DIR" && PIPELINE_DATA="$RUNTIME/data" "$PY" - <<'PY'
import shlex
from app import config
c = config.load()
for k in ('codex_path', 'model', 'wsl_distro', 'agent_workspace_win', 'agent_workspace_wsl'):
    print(f'{k.upper()}={shlex.quote(str(c[k]))}')
PY
)"

[ -f "$CODEX_PATH" ] || { echo "codex 不存在：$CODEX_PATH（在页面「配置」里改 codex 可执行文件路径）" >&2; exit 1; }

A="$AGENT_WORKSPACE_WSL"
W="${AGENT_WORKSPACE_WIN%\\}"
RUN="$A/runs/smoke-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$A/.agents/skills/smoke-echo" "$RUN"
cp "$APP_DIR/scripts/smoke/SKILL.md" "$A/.agents/skills/smoke-echo/SKILL.md"
cp "$APP_DIR/scripts/smoke/schema.json" "$A/schemas/smoke-echo.json"
cleanup() { rm -rf "$A/.agents/skills/smoke-echo" "$A/schemas/smoke-echo.json"; }
trap cleanup EXIT

# 探测文件用运行时虚拟环境里的普通文件（与验收目录同在用户目录下；/etc 下多为符号链接，UNC 跟不过去）
PROBE="$RUNTIME/.venv/pyvenv.cfg"
PROBE_UNC="\\\\wsl.localhost\\$WSL_DISTRO$(echo "$PROBE" | sed 's#/#\\#g')"
INPUT="问候 冒烟输入 你好 pipeline
探测命令 wsl.exe -d $WSL_DISTRO --cd ~ -- bash -ic 'node -v'
探测文件 $PROBE_UNC"

echo "调用 codex（模型 $MODEL，推理强度 low），通常需要 1 到 3 分钟"
echo "$INPUT" | timeout 600 "$CODEX_PATH" exec --json --ephemeral --ignore-user-config \
  --skip-git-repo-check -m "$MODEL" -c 'model_reasoning_effort="low"' \
  -s danger-full-access -C "$W" \
  --output-schema "$W\\schemas\\smoke-echo.json" -o "$W\\runs\\$(basename "$RUN")\\result.json" \
  '$smoke-echo' > "$RUN/events.jsonl" 2> "$RUN/stderr.txt"
code=$?

echo "codex 退出码 $code"
if [ -s "$RUN/result.json" ]; then
  cat "$RUN/result.json"; echo
  grep -q '"skill":"smoke-echo"' "$RUN/result.json" && grep -q '"node_version":"v' "$RUN/result.json" \
    && grep -q '"unc_first_line":"home' "$RUN/result.json" && { echo "冒烟通过"; exit 0; }
fi
echo "冒烟未通过，详情见 $RUN/events.jsonl 与 $RUN/stderr.txt" >&2
grep -v 'models cache' "$RUN/stderr.txt" | tail -5 >&2
exit 1
