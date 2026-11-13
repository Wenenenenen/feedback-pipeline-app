# -*- coding: utf-8 -*-
"""通过 WSL 互操作调用 Windows 版 codex exec，流式记录日志，校验 schema 输出。"""
import json
import subprocess
import threading
import time
from pathlib import Path

import jsonschema

from . import config

# 不视为失败的 stderr 提示
STDERR_NOISE = ('failed to write models cache', 'Reading additional input from stdin')
RATE_LIMIT_MARKS = ('usage limit', 'rate limit', 'rate_limit', '429', 'quota')


class CodexError(RuntimeError):
    pass


def build_cmd(cfg: dict, skill: str, run_key: str) -> list[str]:
    ws = cfg['agent_workspace_win'].rstrip('\\')
    return [
        cfg['codex_path'], 'exec', '--json', '--ephemeral', '--ignore-user-config',
        '--skip-git-repo-check',
        '-m', cfg['model'], '-c', f'model_reasoning_effort="{cfg["reasoning_effort"]}"',
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
    subprocess.run(['powershell.exe', '-NoProfile', '-Command', ps], capture_output=True, timeout=60)


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


def run_skill(skill: str, input_text: str, run_key: str, log, stop_flag: threading.Event | None = None) -> dict:
    """执行 skill，返回符合 schema 的 JSON 结果；失败抛 CodexError。"""
    cfg = config.load()
    ws_wsl = Path(cfg['agent_workspace_wsl'])
    run_dir = ws_wsl / 'runs' / run_key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'input.txt').write_text(input_text, encoding='utf-8')
    schema = json.loads((ws_wsl / 'schemas' / f'{skill}.json').read_text(encoding='utf-8'))
    cmd = build_cmd(cfg, skill, run_key)
    log('启动 ' + ' '.join(c if ' ' not in c else f'"{c}"' for c in cmd))
    timeout = int(cfg['run_timeout_min']) * 60
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=str(ws_wsl))
    p.stdin.write(input_text.encode('utf-8'))
    p.stdin.close()

    stderr_lines: list[str] = []
    text_tail: list[str] = []

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
    for raw in p.stdout:
        line = raw.decode('utf-8', 'replace').rstrip()
        if line:
            s = _event_line(line)
            text_tail.append(s)
            del text_tail[:-50]
            log(s)
    p.wait()
    t_err.join(timeout=5)
    if killed:
        raise CodexError(killed)
    joined = '\n'.join(stderr_lines + text_tail).lower()
    if p.returncode != 0:
        if any(m in joined for m in RATE_LIMIT_MARKS):
            raise CodexError('ChatGPT 额度限流或用量上限，请稍后重试')
        raise CodexError(f'codex 退出码 {p.returncode}：' + ' | '.join(stderr_lines[-3:])[:500])
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
