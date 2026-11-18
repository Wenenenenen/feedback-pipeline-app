# -*- coding: utf-8 -*-
"""通过 WSL 互操作调用 Windows 版 codex exec，流式记录日志，校验 schema 输出。"""
import json
import re
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path

import jsonschema

from . import config

# 不视为失败的 stderr 提示
STDERR_NOISE = ('failed to write models cache', 'Reading additional input from stdin')
RATE_LIMIT_MARKS = ('usage limit', 'rate limit', 'rate_limit', '429', 'quota')
# 认证失败的标记（ChatGPT 登录失效、第三方 key 无效或填错供应商地址）
AUTH_MARKS = ('401', 'unauthorized', 'invalid api key', 'invalid_api_key', 'incorrect api key',
              'not logged in')
POWERSHELL_FALLBACK = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
# codex 内置的供应商，config.toml 里不需要 [model_providers.<名称>] 一节
BUILTIN_PROVIDERS = ('openai', 'ollama', 'lmstudio', 'oss')
# 日志里需要打码的配置键（按最后一段键名匹配）
SECRET_KEY_RE = re.compile(r'(token|key|secret|password|authorization)', re.I)
BARE_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')

_toml_path_cache: dict[str, str] = {}


class CodexError(RuntimeError):
    pass


def _powershell() -> str:
    # WSL 关闭 appendWindowsPath 时 PATH 里没有 powershell.exe，回退到系统盘的固定位置
    return shutil.which('powershell.exe') or POWERSHELL_FALLBACK


def toml_path(cfg: dict) -> Path:
    """Windows codex 的 config.toml（WSL 路径）：配置项优先，否则按 CODEX_HOME 或 %USERPROFILE%\\.codex 定位。"""
    if str(cfg.get('codex_config_toml') or '').strip():
        return Path(cfg['codex_config_toml'].strip()).expanduser()
    if 'auto' not in _toml_path_cache:
        ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }")
        try:
            r = subprocess.run([_powershell(), '-NoProfile', '-Command', ps],
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise CodexError(f'无法定位 Windows 的 .codex 目录：{e}，请在配置页填写 config.toml 路径') from e
        home = r.stdout.decode('utf-8', 'replace').strip().splitlines()
        if r.returncode != 0 or not home or not home[-1].strip():
            raise CodexError('无法定位 Windows 的 .codex 目录，请在配置页填写 config.toml 路径')
        _toml_path_cache['auto'] = config.wsl_path(home[-1].strip()) + '/config.toml'
    return Path(_toml_path_cache['auto'])


def _toml_value(v) -> str:
    """把 toml 读出的值写回 -c 能解析的 TOML 字面量。"""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    # 字符串与数组用 JSON 写法，与 TOML 的基本字符串、数组语法兼容
    return json.dumps(v, ensure_ascii=False)


def _flatten(prefix: str, table: dict) -> list[tuple[str, str]]:
    """把 provider 表展开成「带点的键 = 字面量」，嵌套表继续展开。"""
    out = []
    for k, v in table.items():
        if not BARE_KEY_RE.match(k):
            raise CodexError(f'config.toml 中 {prefix} 下的键名 {k!r} 含特殊字符，流水线无法透传')
        if isinstance(v, dict):
            out += _flatten(f'{prefix}.{k}', v)
        else:
            out.append((f'{prefix}.{k}', _toml_value(v)))
    return out


def model_args(cfg: dict) -> list[str]:
    """模型与供应商参数。关闭跟随时只传配置页的模型名；打开时读取 config.toml 的模型与供应商，转成 -m 与 -c。"""
    if not cfg.get('codex_follow_toml'):
        return ['-m', cfg['model']]
    path = toml_path(cfg)
    try:
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except OSError as e:
        raise CodexError(f'读取 {path} 失败：{e}') from e
    except tomllib.TOMLDecodeError as e:
        raise CodexError(f'{path} 不是合法的 TOML：{e}') from e
    args = ['-m', str(data.get('model') or cfg['model'])]
    name = data.get('model_provider')
    if not name:
        # 未指定供应商：codex 默认走 OpenAI 官方（ChatGPT 登录或官方 key）
        return args
    if not BARE_KEY_RE.match(str(name)):
        raise CodexError(f'config.toml 中的供应商名 {name!r} 含特殊字符，流水线无法透传')
    args += ['-c', f'model_provider={_toml_value(str(name))}']
    table = (data.get('model_providers') or {}).get(name)
    if table is None:
        if name in BUILTIN_PROVIDERS:
            return args
        raise CodexError(f'{path} 中指定了供应商 {name}，但找不到 [model_providers.{name}] 一节')
    for k, v in _flatten(f'model_providers.{name}', table):
        args += ['-c', f'{k}={v}']
    return args


def mask_args(cmd: list[str]) -> list[str]:
    """日志用：-c 参数中键名像密钥的，值打码。"""
    out = []
    for c in cmd:
        k, sep, _ = c.partition('=')
        if sep and '.' in k and SECRET_KEY_RE.search(k.rsplit('.', 1)[-1]):
            c = f'{k}=***'
        out.append(c)
    return out


def reasoning_effort(cfg: dict, skill: str) -> str:
    """本次运行的推理强度：生成录屏脚本可单独设置（留空沿用通用推理强度），其他环节用通用推理强度。"""
    if skill == 'qa-record' and (cfg.get('record_reasoning_effort') or '').strip():
        return cfg['record_reasoning_effort'].strip()
    return cfg['reasoning_effort']


def build_cmd(cfg: dict, skill: str, run_key: str) -> list[str]:
    ws = cfg['agent_workspace_win'].rstrip('\\')
    return [
        cfg['codex_path'], 'exec', '--json', '--ephemeral', '--ignore-user-config',
        '--skip-git-repo-check',
        *model_args(cfg), '-c', f'model_reasoning_effort="{reasoning_effort(cfg, skill)}"',
        '-s', 'danger-full-access', '-C', ws,
        '--output-schema', f'{ws}\\schemas\\{skill}.json',
        '-o', f'{ws}\\runs\\{run_key}\\result.json',
        f'${skill}',
    ]


def _kill_windows(run_key: str):
    """结束命令行中带本次运行 ID 的 Windows codex 进程（互操作进程被杀后 Windows 侧可能残留）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='codex.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{run_key}*' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    try:
        subprocess.run([_powershell(), '-NoProfile', '-Command', ps], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def _event_line(line: str) -> str:
    """把 --json 事件转成简短日志行。"""
    try:
        ev = json.loads(line)
    except ValueError:
        return line
    t = ev.get('type', '')
    item = ev.get('item') or {}
    it = item.get('type')
    if t.startswith('item.') and it == 'command_execution':
        if t == 'item.started':
            return f"[命令] {item.get('command', '')[:500]}"
        out = (item.get('aggregated_output') or '').replace('\x00', '')
        return f"[命令结束 exit={item.get('exit_code')}] {out[-800:]}"
    if t == 'item.completed' and it == 'agent_message':
        return f"[回复] {item.get('text', '')[:2000]}"
    if t == 'item.completed' and it in ('reasoning', 'file_change', 'todo_list', 'web_search'):
        return f"[{it}] {json.dumps(item, ensure_ascii=False)[:500]}"
    if t == 'turn.completed':
        return f"[结束] usage {json.dumps(ev.get('usage'), ensure_ascii=False)}"
    if t in ('turn.failed', 'error'):
        return f"[错误] {json.dumps(ev, ensure_ascii=False)[:1000]}"
    return f'[{t}]'


def _is_error_event(line: str) -> bool:
    """codex 自身报告的错误事件（不含模型执行命令的输出）。"""
    try:
        ev = json.loads(line)
    except ValueError:
        return False
    if not isinstance(ev, dict):
        return False
    item = ev.get('item') or {}
    return ev.get('type') in ('turn.failed', 'error') or (
        ev.get('type') == 'item.completed' and isinstance(item, dict) and item.get('type') == 'error')


def _is_reconnect_event(line: str) -> bool:
    """codex 连不上供应商时反复发出的 error 事件：Reconnecting... waiting for network (...)。"""
    if not _is_error_event(line):
        return False
    return 'reconnecting' in line.lower()


def _has_mark(text: str, marks) -> bool:
    # 数字状态码按整词匹配，避免把耗时、编号里的 401/429 当成状态码
    for m in marks:
        if m.isdigit():
            if re.search(rf'(?<!\d){m}(?!\d)', text):
                return True
        elif m in text:
            return True
    return False


def failure_reason(returncode: int, stderr_lines: list[str], error_events: list[str]) -> str:
    """codex 非 0 退出时的失败原因：只看 stderr 和 codex 自身的错误事件，不看命令输出。"""
    text = '\n'.join(stderr_lines + error_events).lower()
    if _has_mark(text, AUTH_MARKS):
        return 'codex 认证失败：检查 codex 登录，或 CC Switch 当前供应商的 API key 与地址'
    if _has_mark(text, RATE_LIMIT_MARKS):
        return '额度限流或用量上限，请稍后重试'
    return f'codex 退出码 {returncode}：' + ' | '.join(stderr_lines[-3:])[:500]


def run_skill(skill: str, input_text: str, run_key: str, log, stop_flag: threading.Event | None = None) -> dict:
    """执行 skill，返回符合 schema 的 JSON 结果；失败抛 CodexError。"""
    cfg = config.load()
    ws_wsl = Path(cfg['agent_workspace_wsl'])
    run_dir = ws_wsl / 'runs' / run_key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'input.txt').write_text(input_text, encoding='utf-8')
    schema = json.loads((ws_wsl / 'schemas' / f'{skill}.json').read_text(encoding='utf-8'))
    cmd = build_cmd(cfg, skill, run_key)
    if cfg.get('codex_follow_toml'):
        log(f'沿用 {toml_path(cfg)} 的供应商与模型')
    log('启动 ' + ' '.join(c if ' ' not in c else f'"{c}"' for c in mask_args(cmd)))
    timeout = int(cfg['run_timeout_min']) * 60
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=str(ws_wsl))
    p.stdin.write(input_text.encode('utf-8'))
    p.stdin.close()

    stderr_lines: list[str] = []
    error_events: list[str] = []

    def _read_err():
        for raw in p.stderr:
            s = raw.decode('utf-8', 'replace').replace('\x00', '').rstrip()
            if s and not any(n in s for n in STDERR_NOISE):
                stderr_lines.append(s)
                log('[stderr] ' + s[:1000])

    t_err = threading.Thread(target=_read_err, daemon=True)
    t_err.start()
    deadline = time.time() + timeout
    killed = None

    def _watch():
        nonlocal killed
        while p.poll() is None:
            if time.time() > deadline:
                killed = f'超过 {cfg["run_timeout_min"]} 分钟未完成'
            elif stop_flag is not None and stop_flag.is_set():
                killed = '人工停止'
            if killed:
                p.kill()
                _kill_windows(run_key)
                return
            time.sleep(2)

    threading.Thread(target=_watch, daemon=True).start()
    max_reconnects = int(cfg.get('codex_max_reconnects') or 0)
    reconnects = 0
    for raw in p.stdout:
        line = raw.decode('utf-8', 'replace').rstrip()
        if line:
            if _is_error_event(line):
                error_events.append(line[:2000])
                del error_events[:-20]
            log(_event_line(line))
            # 只数连续的重连事件，中间出现其他事件说明已恢复，重新计数
            reconnects = reconnects + 1 if _is_reconnect_event(line) else 0
            if max_reconnects and reconnects >= max_reconnects and not killed:
                killed = (f'codex 连续重连 {reconnects} 次仍连不上供应商，'
                          '检查网络或 CC Switch 当前供应商的地址')
                p.kill()
                _kill_windows(run_key)
    p.wait()
    t_err.join(timeout=5)
    if killed:
        raise CodexError(killed)
    if p.returncode != 0:
        raise CodexError(failure_reason(p.returncode, stderr_lines, error_events))
    result_file = run_dir / 'result.json'
    if not result_file.exists():
        raise CodexError('codex 未写出最终结果文件')
    try:
        result = json.loads(result_file.read_text(encoding='utf-8'))
    except ValueError as e:
        raise CodexError(f'最终结果不是合法 JSON：{e}') from e
    try:
        jsonschema.validate(result, schema)
    except jsonschema.ValidationError as e:
        raise CodexError(f'最终结果不符合 schema：{e.message}') from e
    return result
