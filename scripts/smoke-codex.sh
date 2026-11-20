#!/usr/bin/env bash
# 新环境冒烟：确认 WSL 能调起 Windows 版 codex、codex 认证有效（ChatGPT 登录）、
# agent/ 下的 skill 能被发现、按 schema 输出、codex 能回调 wsl.exe 执行命令、能用 \\wsl.localhost 路径读 WSL 文件。
# 用法（在 WSL 中，先启动过一次 start.sh 以创建虚拟环境）：bash scripts/smoke-codex.sh
#   加 --fallback 时改用配置页 Codex 里三方 API 的地址、Key 与模型（与正式运行共用 codex_runner.fallback_args）
# 只做一次低推理强度调用；临时 skill 与 schema 跑完即删除，不影响正式运行。
set -uo pipefail

SMOKE_FALLBACK=""
[ "${1:-}" = "--fallback" ] && SMOKE_FALLBACK=1
export SMOKE_FALLBACK
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="${PIPELINE_HOME:-$HOME/jianzhi/feedback-pipeline}"
PY="$RUNTIME/.venv/bin/python"
[ -x "$PY" ] || { echo "未找到 $PY，请先运行一次 scripts/start.sh" >&2; exit 1; }

# 读取流水线配置（页面「配置」中保存的值优先）；模型与供应商参数与正式运行共用 codex_runner.channel_args
VARS="$(cd "$APP_DIR" && PIPELINE_DATA="$RUNTIME/data" "$PY" - <<'PY'
import os
import shlex
import sys
from app import codex_runner, config
c = config.load()
for k in ('codex_path', 'wsl_distro', 'agent_workspace_win', 'agent_workspace_wsl'):
    print(f'{k.upper()}={shlex.quote(str(c[k]))}')
if os.environ.get('SMOKE_FALLBACK'):
    if not codex_runner.fallback_ready(c):
        print('配置页 Codex 里三方 API 的地址、Key、模型没有填完整', file=sys.stderr)
        sys.exit(1)
    env = codex_runner.fallback_env(c)
    # key 只经环境变量交给 Windows 的 codex
    for k in (codex_runner.FALLBACK_KEY_ENV, 'WSLENV'):
        print(f'export {k}={shlex.quote(env[k])}')
    print(f"FALLBACK_URL={shlex.quote(c['codex_fallback_base_url'].strip())}")
args = codex_runner.channel_args(c, 'fallback' if os.environ.get('SMOKE_FALLBACK') else 'official')
print('MODEL_ARGS=(' + ' '.join(shlex.quote(a) for a in args) + ')')
print(f"MODEL={shlex.quote(args[1])}")
print(f"SHOW_ARGS={shlex.quote(' '.join(args))}")
PY
)" || exit 1
FALLBACK_URL=""
eval "$VARS"
[ -n "$FALLBACK_URL" ] && echo "使用三方 API $FALLBACK_URL"

[ -f "$CODEX_PATH" ] || { echo "codex 不存在：$CODEX_PATH（默认值在 app/config.py 的 codex_path，可在 data/config.json 中覆盖）" >&2; exit 1; }

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

echo "模型与供应商参数：$SHOW_ARGS"
echo "调用 codex（模型 $MODEL，推理强度 low），通常需要 1 到 3 分钟"
echo "$INPUT" | timeout 600 "$CODEX_PATH" exec --json --ephemeral --ignore-user-config \
  --skip-git-repo-check "${MODEL_ARGS[@]}" -c 'model_reasoning_effort="low"' \
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
