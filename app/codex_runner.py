# -*- coding: utf-8 -*-
"""通过 WSL 互操作调用 Windows 版 codex exec，流式记录日志，校验 schema 输出。"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
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

# 三方 API 通道：codex 里的供应商名与传 API key 的环境变量（经 WSLENV 传给 Windows 的 codex，不出现在命令行里）
FALLBACK_PROVIDER = 'feedback_fallback'
FALLBACK_KEY_ENV = 'FEEDBACK_CODEX_FALLBACK_KEY'
# 两个通道在日志与失败原因里的叫法
CHANNEL_NAMES = {'official': '官方', 'fallback': '三方 API'}
# 官方额度恢复时间：解析出的时间再多等这么久，避免刚到点时额度还没刷新
RESET_MARGIN = timedelta(minutes=1)
MONTHS = {m: i for i, m in enumerate(
    ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'), 1)}
# 「try again at 7:21 AM」「try again at Nov 24th, 2026 2:41 PM」
RESET_AT_RE = re.compile(
    r'try again at\s+(?:([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4}),?\s+)?'
    r'(\d{1,2}):(\d{2})\s*([ap])\.?m\.?', re.I)
# 「try again in 2 hours 5 minutes」
RESET_IN_RE = re.compile(r'try again in\s+((?:\d+\s*[a-z]+[\s,]*(?:and\s+)?)+)', re.I)
RESET_UNIT_RE = re.compile(r'(\d+)\s*(day|hour|hr|minute|min|second|sec)', re.I)
_channel_lock = threading.Lock()


class CodexError(RuntimeError):
    pass


class RateLimitError(CodexError):
    """额度限流或用量上限；text 是原始报错（stderr 与错误事件），用来解析恢复时间。"""

    def __init__(self, message: str, text: str = ''):
        super().__init__(message)
        self.text = text


# ---------- 官方与三方 API 通道 ----------

def parse_reset_time(text: str, now: datetime | None = None) -> datetime | None:
    """从限流报错里解析官方额度的恢复时间（本机时间，与 Windows 时区一致）；解析不出返回 None。"""
    now = now or datetime.now()
    m = RESET_AT_RE.search(text or '')
    if m:
        mon, day, year, hh, mm, ap = m.groups()
        hour = int(hh) % 12 + (12 if ap.lower() == 'p' else 0)
        try:
            if mon:
                if mon.lower() not in MONTHS:
                    return None
                return datetime(int(year), MONTHS[mon.lower()], int(day), hour, int(mm))
            at = now.replace(hour=hour, minute=int(mm), second=0, microsecond=0)
        except ValueError:
            return None
        # 只有钟点时按最近的将来算：已经过去十分钟以上的就是明天的这个时间
        return at + timedelta(days=1) if at < now - timedelta(minutes=10) else at
    m = RESET_IN_RE.search(text or '')
    if m:
        secs = {'day': 86400, 'hour': 3600, 'hr': 3600, 'minute': 60, 'min': 60, 'second': 1, 'sec': 1}
        total = sum(int(n) * secs[u.lower()] for n, u in RESET_UNIT_RE.findall(m.group(1)))
        if total:
            return now + timedelta(seconds=total)
    return None


def _channel_file() -> Path:
    return config.DATA_DIR / 'codex_channel.json'


def _read_channel() -> dict:
    try:
        return json.loads(_channel_file().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def fallback_ready(cfg: dict) -> bool:
    """三方 API 的地址、Key、模型都已填写。"""
    return all(str(cfg.get(k) or '').strip()
               for k in ('codex_fallback_base_url', 'codex_fallback_key', 'codex_fallback_model'))


def channel_mode(cfg: dict) -> str:
    mode = cfg.get('codex_channel_mode')
    return mode if mode in config.CHANNEL_MODES else 'auto'


def channel_status(cfg: dict | None = None, now: datetime | None = None) -> dict:
    """当前通道。只用三方时为 fallback；官方优先时，官方额度用完且未到恢复时间、三方已填完整为 fallback；其余为 official。"""
    cfg = cfg or config.load()
    now = now or datetime.now()
    mode = channel_mode(cfg)
    st = _read_channel()
    until = None
    try:
        until = datetime.fromisoformat(st['official_blocked_until']) if st.get('official_blocked_until') else None
    except ValueError:
        until = None
    blocked = until is not None and now < until
    ready = fallback_ready(cfg)
    if mode == 'fallback':
        channel = 'fallback'
    elif mode == 'auto' and blocked and ready:
        channel = 'fallback'
    else:
        channel = 'official'
    return {
        'channel': channel,
        'mode': mode,
        'fallback_ready': ready,
        'official_blocked': blocked,
        'official_blocked_until': until.strftime('%Y-%m-%d %H:%M') if blocked else '',
        'reason': st.get('reason', '') if blocked else '',
        'switched_at': st.get('switched_at', '') if blocked else '',
    }


def mark_official_blocked(cfg: dict, text: str, now: datetime | None = None) -> datetime:
    """记下官方额度用完：恢复时间优先取报错里的时间，解析不出时按配置的默认时长；返回恢复时间。"""
    now = now or datetime.now()
    at = parse_reset_time(text, now)
    until = at + RESET_MARGIN if at else now + timedelta(minutes=int(cfg.get('codex_fallback_retry_min') or 60))
    reason = next((s.strip() for s in (text or '').splitlines() if 'limit' in s.lower()), '')[:500]
    with _channel_lock:
        st = _read_channel()
        old = st.get('official_blocked_until')
        # 并发运行先后报限流时取更晚的恢复时间
        try:
            if old and datetime.fromisoformat(old) > until and datetime.fromisoformat(old) > now:
                return datetime.fromisoformat(old)
        except ValueError:
            pass
        st.update(official_blocked_until=until.isoformat(timespec='seconds'), reason=reason,
                  switched_at=now.strftime('%Y-%m-%d %H:%M'))
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _channel_file().write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
    return until


def reset_channel():
    """人工切回官方：清除官方额度用完的记录。"""
    with _channel_lock:
        _channel_file().unlink(missing_ok=True)


def fallback_args(cfg: dict) -> list[str]:
    """三方 API 的模型与供应商参数：responses 协议，key 从环境变量读取。"""
    p = f'model_providers.{FALLBACK_PROVIDER}'
    return ['-m', cfg['codex_fallback_model'].strip(),
            '-c', f'model_provider="{FALLBACK_PROVIDER}"',
            '-c', f'{p}.name="feedback fallback"',
            '-c', f'{p}.base_url={_toml_value(cfg["codex_fallback_base_url"].strip().rstrip("/"))}',
            '-c', f'{p}.wire_api="responses"',
            '-c', f'{p}.env_key="{FALLBACK_KEY_ENV}"']


def fallback_env(cfg: dict) -> dict:
    """三方 API 运行的环境变量：key 放进环境变量，并加入 WSLENV 让 Windows 进程能读到。"""
    env = dict(os.environ)
    env[FALLBACK_KEY_ENV] = cfg['codex_fallback_key'].strip()
    names = [s for s in env.get('WSLENV', '').split(':') if s and s.split('/')[0] != FALLBACK_KEY_ENV]
    env['WSLENV'] = ':'.join(names + [FALLBACK_KEY_ENV])
    return env


def _powershell() -> str:
    # WSL 关闭 appendWindowsPath 时 PATH 里没有 powershell.exe，回退到系统盘的固定位置
    return shutil.which('powershell.exe') or POWERSHELL_FALLBACK


def _toml_value(v) -> str:
    """把值写成 -c 能解析的 TOML 字面量。"""
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    # 字符串与数组用 JSON 写法，与 TOML 的基本字符串、数组语法兼容
    return json.dumps(v, ensure_ascii=False)


def model_args(cfg: dict) -> list[str]:
    """官方通道的模型参数：ChatGPT 登录，只传配置页的模型名。"""
    return ['-m', cfg['model']]


def channel_args(cfg: dict, channel: str) -> list[str]:
    """指定通道的模型与供应商参数。"""
    return fallback_args(cfg) if channel == 'fallback' else model_args(cfg)


def reasoning_effort(cfg: dict, skill: str) -> str:
    """本次运行的推理强度：生成录屏脚本可单独设置（留空沿用通用推理强度），其他环节用通用推理强度。"""
    if skill == 'qa-record' and (cfg.get('record_reasoning_effort') or '').strip():
        return cfg['record_reasoning_effort'].strip()
    return cfg['reasoning_effort']


def build_cmd(cfg: dict, skill: str, run_key: str, channel: str = 'official') -> list[str]:
    ws = cfg['agent_workspace_win'].rstrip('\\')
    return [
        cfg['codex_path'], 'exec', '--json', '--ephemeral', '--ignore-user-config',
        '--skip-git-repo-check',
        *channel_args(cfg, channel), '-c', f'model_reasoning_effort="{reasoning_effort(cfg, skill)}"',
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


def failure_kind(stderr_lines: list[str], error_events: list[str]) -> str:
    """失败类别：auth、rate_limit 或空串；只看 stderr 和 codex 自身的错误事件，不看命令输出。"""
    text = '\n'.join(stderr_lines + error_events).lower()
    if _has_mark(text, AUTH_MARKS):
        return 'auth'
    if _has_mark(text, RATE_LIMIT_MARKS):
        return 'rate_limit'
    return ''


def failure_reason(returncode: int, stderr_lines: list[str], error_events: list[str],
                   channel: str = 'official') -> str:
    """codex 非 0 退出时的失败原因。"""
    kind = failure_kind(stderr_lines, error_events)
    if kind == 'auth':
        if channel == 'fallback':
            return 'codex 认证失败：检查配置页三方 API 的 Key 与地址'
        return 'codex 认证失败：检查 Windows 上 codex 的 ChatGPT 登录'
    if kind == 'rate_limit':
        return '额度限流或用量上限，请稍后重试'
    return f'codex 退出码 {returncode}：' + ' | '.join(stderr_lines[-3:])[:500]


def run_skill(skill: str, input_text: str, run_key: str, log, stop_flag: threading.Event | None = None) -> dict:
    """执行 skill，返回符合 schema 的 JSON 结果；失败抛 CodexError。

    通道为官方优先时，官方额度用完且三方 API 已填完整，记下官方的恢复时间，本次当场改用三方重跑；
    恢复时间之前的运行直接走三方，到点后新运行回到官方。只用官方、只用三方时不切换。
    """
    cfg = config.load()
    ws_wsl = Path(cfg['agent_workspace_wsl'])
    run_dir = ws_wsl / 'runs' / run_key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / 'input.txt').write_text(input_text, encoding='utf-8')
    schema = json.loads((ws_wsl / 'schemas' / f'{skill}.json').read_text(encoding='utf-8'))
    st = channel_status(cfg)
    if st['channel'] == 'fallback' and not st['fallback_ready']:
        raise CodexError('通道设为只用三方 API，但配置页的三方地址、Key、模型没有填完整')
    if st['mode'] == 'auto' and st['channel'] == 'fallback':
        log(f"官方额度用完，预计 {st['official_blocked_until']} 恢复，此前使用三方 API")
    elif st['mode'] == 'auto' and st['official_blocked']:
        log('官方额度用完但三方 API 未填完整，仍使用官方')
    try:
        return _run_once(cfg, skill, run_key, input_text, schema, run_dir, log, stop_flag, st['channel'])
    except RateLimitError as e:
        if (st['mode'] != 'auto' or st['channel'] != 'official' or not st['fallback_ready']
                or (stop_flag is not None and stop_flag.is_set())):
            raise
        until = mark_official_blocked(cfg, e.text)
        log(f"官方额度限流，{until.strftime('%Y-%m-%d %H:%M')} 前改用三方 API，本次立即用三方重跑")
    return _run_once(cfg, skill, run_key, input_text, schema, run_dir, log, stop_flag, 'fallback')


def _run_once(cfg: dict, skill: str, run_key: str, input_text: str, schema: dict, run_dir: Path, log,
              stop_flag: threading.Event | None, channel: str) -> dict:
    """在指定通道上执行一次 codex；三方 API 通道的失败原因前加「三方 API：」。"""
    try:
        return _exec(cfg, skill, run_key, input_text, schema, run_dir, log, stop_flag, channel)
    except RateLimitError as e:
        if channel == 'fallback':
            raise RateLimitError(f'{CHANNEL_NAMES[channel]}：{e}', e.text) from e
        raise
    except CodexError as e:
        if channel == 'fallback':
            raise CodexError(f'{CHANNEL_NAMES[channel]}：{e}') from e
        raise


# 收到 turn.completed 后等 codex 自己退出的秒数，超过就结束进程（结果文件此时已写出）
TURN_EXIT_GRACE = 90


def _is_turn_completed(line: str) -> bool:
    try:
        return json.loads(line).get('type') == 'turn.completed'
    except (ValueError, AttributeError):
        return False


def _exec(cfg: dict, skill: str, run_key: str, input_text: str, schema: dict, run_dir: Path, log,
          stop_flag: threading.Event | None, channel: str) -> dict:
    ws_wsl = Path(cfg['agent_workspace_wsl'])
    # 同一运行在三方上重跑时，先清掉上一次可能留下的结果文件
    (run_dir / 'result.json').unlink(missing_ok=True)
    cmd = build_cmd(cfg, skill, run_key, channel)
    if channel == 'fallback':
        log(f"使用三方 API {cfg['codex_fallback_base_url'].strip()}")
    log('启动 ' + ' '.join(c if ' ' not in c else f"'{c}'" for c in cmd))
    timeout = int(cfg['run_timeout_min']) * 60
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=str(ws_wsl),
                         env=fallback_env(cfg) if channel == 'fallback' else None)
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
    # 收到 turn.completed 的时间；之后进程迟迟不退出（验收时起的后台服务、浏览器脚本占着输出管道）就结束它
    turn_done_at = None
    lingered = False

    def _watch():
        nonlocal killed, lingered
        while p.poll() is None:
            if time.time() > deadline:
                killed = f'超过 {cfg["run_timeout_min"]} 分钟未完成'
            elif stop_flag is not None and stop_flag.is_set():
                killed = '人工停止'
            elif turn_done_at is not None and time.time() > turn_done_at + TURN_EXIT_GRACE:
                lingered = True
                log(f'本轮已结束 {TURN_EXIT_GRACE} 秒 codex 仍未退出（多半是验收时起的后台进程没关），结束进程并读取结果文件')
                p.kill()
                _kill_windows(run_key)
                return
            if killed:
                p.kill()
                _kill_windows(run_key)
                return
            time.sleep(min(2, TURN_EXIT_GRACE))

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
            if turn_done_at is None and _is_turn_completed(line):
                turn_done_at = time.time()
            # 只数连续的重连事件，中间出现其他事件说明已恢复，重新计数
            reconnects = reconnects + 1 if _is_reconnect_event(line) else 0
            if max_reconnects and reconnects >= max_reconnects and not killed:
                killed = (f'codex 连续重连 {reconnects} 次仍连不上供应商，'
                          '检查网络或代理（走三方 API 时检查三方地址）')
                p.kill()
                _kill_windows(run_key)
    p.wait()
    t_err.join(timeout=5)
    if killed:
        raise CodexError(killed)
    # 本轮已正常结束、只是进程没退出时，不看退出码，直接按结果文件判断
    if p.returncode != 0 and not lingered:
        reason = failure_reason(p.returncode, stderr_lines, error_events, channel)
        if failure_kind(stderr_lines, error_events) == 'rate_limit':
            raise RateLimitError(reason, '\n'.join(stderr_lines + error_events))
        raise CodexError(reason)
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
