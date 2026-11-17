# -*- coding: utf-8 -*-
"""自动做题：在独立的 tmux 服务器里启动交互式 Claude Code，粘贴 prompt 原文并等待这一轮结束。

与人工做题保持一致：做题目录下以 bypass 模式启动全新会话，首条输入就是 prompt 原文，只发送一次；
结束时直接关闭会话，不再输入任何内容（避免轨迹里多出人工输入导致收集校验不过）。
接口报错中断时不输入「继续」：报错会在轨迹里留下 model 为 <synthetic> 的占位消息，甲方质检判不通过，
所以按配置作废这次会话、恢复初始快照后重新做题。
做题过程可以用 attach_command() 给出的命令只读观看。
"""
import os
import subprocess
import threading
import time
from pathlib import Path

from . import config, db, jobs, pipeline as P, trajectory

# 自动做题与自动录屏共用的 tmux 服务器名，与用户自己的 tmux 互不影响
TMUX_SOCKET = 'feedback'
# 做题会话继承的环境变量：只保留一个新开 WSL 终端里本来就有的，不带后端进程的虚拟环境等
ENV_KEEP = ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'WSL_DISTRO_NAME', 'WSL_INTEROP', 'WSLENV',
            'DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'PULSE_SERVER')
# 启动时可能出现的确认框：(识别文字, 要选的选项)，都按小写比较
DIALOGS = [
    ('trust this folder', 'yes, i trust this folder'),
    ('bypass permissions mode', 'yes, i accept'),
]
READY_MARK = 'bypass permissions on'
BUSY_MARK = 'esc to interrupt'
CURSOR = '❯'


class DriverError(RuntimeError):
    pass


class ApiErrorInterrupt(DriverError):
    """做题会话被接口报错中断：这次会话的轨迹不能提交，由 run_do 按配置作废重做。"""


# 已发出 prompt、Claude Code 正在做题的题目 id（调度器据此开始准备下一道题）
_started: set[int] = set()
_started_lock = threading.Lock()


def started(task_id: int) -> bool:
    with _started_lock:
        return task_id in _started


def _mark_started(task_id: int, on: bool):
    with _started_lock:
        (_started.add if on else _started.discard)(task_id)


def session_name(task_id: int) -> str:
    return f'fbdo-{task_id}'


def attach_command(task_id: int) -> str:
    """只读观看做题会话的命令（在 WSL 终端执行，按 Ctrl+b d 退出观看）。"""
    return f'tmux -L {TMUX_SOCKET} attach -r -t {session_name(task_id)}'


def tmux(*args: str, data: bytes | None = None, check: bool = True, timeout: int = 30) -> str:
    try:
        r = subprocess.run(['tmux', '-L', TMUX_SOCKET, *args], input=data, capture_output=True,
                           timeout=timeout)
    except FileNotFoundError as e:
        raise DriverError('WSL 中没有 tmux，先执行 scripts/setup-verify-tools.sh') from e
    if check and r.returncode != 0:
        raise DriverError(f"tmux {args[0]} 失败：{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout.decode('utf-8', 'replace')


def alive(name: str) -> bool:
    return subprocess.run(['tmux', '-L', TMUX_SOCKET, 'has-session', '-t', name],
                          capture_output=True).returncode == 0


def kill(name: str):
    if alive(name):
        tmux('kill-session', '-t', name, check=False)


def capture(name: str) -> str:
    # -J 把自动折行的长行拼回一行，方便按文字识别
    return tmux('capture-pane', '-p', '-J', '-t', name, check=False)


def clean_env_prefix() -> list[str]:
    """用 env -i 启动，只带白名单里的变量，效果等同于新开一个 WSL 终端。"""
    keep = [f'{k}={os.environ[k]}' for k in ENV_KEEP if os.environ.get(k)]
    return ['env', '-i', *keep, 'SHELL=/bin/bash', 'TERM=xterm-256color']


def start_session(name: str, cwd: Path, command: str, width: int = 220, height: int = 50):
    """新建后台会话执行命令（bash -lic 加载登录与交互配置，nvm 等才可用）。"""
    kill(name)
    tmux('new-session', '-d', '-s', name, '-x', str(width), '-y', str(height), '-c', str(cwd),
         *clean_env_prefix(), 'bash', '-lic', command)


def pick_option(screen: str, target: str) -> list[str]:
    """确认框里从当前光标移到目标选项需要的按键（方向键加回车）。找不到时返回空列表。"""
    lines = [s.strip().lower() for s in screen.splitlines()]
    cur = next((i for i, s in enumerate(lines) if s.startswith(CURSOR)), None)
    tgt = next((i for i, s in enumerate(lines) if target in s), None)
    if cur is None or tgt is None:
        return []
    step = 'Down' if tgt >= cur else 'Up'
    return [step] * abs(tgt - cur) + ['Enter']


def wait_ready(name: str, timeout: int, log, stop) -> None:
    """等 Claude Code 进入可输入状态，途中自动处理已知的确认框。"""
    deadline = time.time() + timeout
    handled: dict[str, int] = {}
    screen = ''
    while time.time() < deadline:
        if stop.is_set():
            raise DriverError('人工停止')
        if not alive(name):
            raise DriverError('Claude Code 启动后立即退出，最后的屏幕：\n' + screen[-1500:])
        screen = capture(name)
        low = screen.lower()
        dialog = next(((m, opt) for m, opt in DIALOGS if m in low), None)
        if dialog:
            keys = pick_option(screen, dialog[1])
            if not keys:
                raise DriverError(f'无法在确认框中找到选项「{dialog[1]}」，屏幕：\n{screen[-1500:]}')
            if handled.get(dialog[0], 0) >= 3:
                raise DriverError('确认框反复出现，屏幕：\n' + screen[-1500:])
            log(f'自动确认「{dialog[0]}」')
            for k in keys:
                tmux('send-keys', '-t', name, k)
                time.sleep(0.3)
            handled[dialog[0]] = handled.get(dialog[0], 0) + 1
            time.sleep(2)
            continue
        if READY_MARK in low:
            return
        time.sleep(2)
    raise DriverError(f'{timeout} 秒内 Claude Code 没有进入可输入状态，最后的屏幕：\n{screen[-1500:]}')


def paste_and_submit(name: str, text: str):
    """以括号粘贴模式送入原文（多行不会提前发送），再按回车提交。"""
    tmux('load-buffer', '-b', name, '-', data=text.encode('utf-8'))
    tmux('paste-buffer', '-p', '-d', '-b', name, '-t', name)
    time.sleep(2)
    tmux('send-keys', '-t', name, 'Enter')


def wait_session_file(pdir: Path, before: set[str], prompt: str, name: str, log, stop,
                      timeout: int = 90) -> Path:
    """等出现首条输入与 prompt 原文一致的新会话文件。30 秒没出现时补按一次回车。"""
    want = trajectory._norm(prompt)
    deadline = time.time() + timeout
    nudged = False
    start = time.time()
    while time.time() < deadline:
        if stop.is_set():
            raise DriverError('人工停止')
        for f in sorted(pdir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True) if pdir.is_dir() else []:
            if f.stem in before:
                continue
            fp = trajectory.first_prompt(f)
            if fp is None:
                continue
            if trajectory._norm(fp) != want:
                raise DriverError(f'新会话的首条输入与 prompt 原文不一致（会话 {f.stem}），需要重做')
            return f
        if not nudged and time.time() - start > 30:
            log('30 秒内没有看到新会话，补按一次回车')
            tmux('send-keys', '-t', name, 'Enter')
            nudged = True
        time.sleep(2)
    raise DriverError(f'{timeout} 秒内没有出现首条输入为 prompt 的新会话，屏幕：\n{capture(name)[-1500:]}')


def _last_error_text(path: Path) -> str:
    for r in reversed(trajectory.load_rows(path)):
        if r.get('type') == 'assistant' and r.get('isApiErrorMessage'):
            for x in (r.get('message') or {}).get('content') or []:
                if isinstance(x, dict) and x.get('text'):
                    return x['text'][:500]
    return ''


# 做题中途检查违规调用（子代理、MCP、非内置 skill）的间隔秒数
FORBIDDEN_CHECK_SEC = 60


def _sleep(sec: float, stop) -> None:
    """可被人工停止打断的等待。"""
    if stop.wait(sec):
        raise DriverError('人工停止')


def wait_done(name: str, path: Path, cfg: dict, log, stop) -> None:
    """轮询轨迹与屏幕，直到这一轮正常结束。

    接口报错中断时抛 ApiErrorInterrupt，由 run_do 决定是否作废重做；
    中途出现违规调用（子代理、MCP、非内置 skill）立即判失败，不再空等到做完；
    模型提问、超时都抛错。
    """
    deadline = time.time() + int(cfg['do_timeout_min']) * 60
    idle = int(cfg['do_idle_sec'])
    last_report = time.time()
    last_check = 0.0
    while True:
        if stop.is_set():
            raise DriverError('人工停止')
        if time.time() - last_check >= FORBIDDEN_CHECK_SEC:
            last_check = time.time()
            bad = trajectory.forbidden_calls(trajectory.parse(path), cfg['builtin_skills'])
            if bad:
                raise DriverError('做题中出现不允许的调用，轨迹不能提交：' + '；'.join(bad))
        state = trajectory.turn_state(path)
        if state == 'api_error':
            raise ApiErrorInterrupt(f"Claude Code 接口报错中断：{_last_error_text(path) or '见做题会话'}")
        if state == 'asking':
            raise DriverError('模型在向人提问（等待回答），按做题规则需要重做')
        if state == 'done':
            quiet = time.time() - path.stat().st_mtime
            if quiet >= idle and (not alive(name) or BUSY_MARK not in capture(name).lower()):
                return
        elif not alive(name):
            raise DriverError('Claude Code 会话意外退出，这一轮没有正常结束')
        if time.time() > deadline:
            raise DriverError(f"超过 {cfg['do_timeout_min']} 分钟仍未做完")
        if time.time() - last_report > 300:
            info = trajectory.parse(path)
            log(f"做题中 工具调用 {sum(info['tools'].values())} 次 轨迹 {path.stat().st_size // 1024}KB")
            last_report = time.time()
        time.sleep(5)


def run_do(task_id: int, log, stop):
    """自动做题：做一轮，接口报错中断时按配置等待后作废会话重做，做完进入待收集。"""
    cfg = config.load()
    redo_max = int(cfg.get('do_api_error_redo_max') or 0)
    wait_sec = int(cfg.get('do_api_error_wait_sec') or 0)
    redone = 0
    while True:
        try:
            _do_once(task_id, cfg, log, stop)
            break
        except ApiErrorInterrupt as e:
            if redone >= redo_max:
                raise DriverError(f'{e}（已自动作废重做 {redone} 次）' if redone else str(e)) from e
            redone += 1
            # 会话已在 _do_once 中关闭；下一轮开头发现未作废的会话，会作废并恢复初始快照
            log(f'{e}；{wait_sec} 秒后作废这次会话重新做题（第 {redone}/{redo_max} 次）')
            _sleep(wait_sec, stop)
    db.update_task(task_id, status=P.S_COLLECT, failed_from=None, last_error=None)


def _do_once(task_id: int, cfg: dict, log, stop):
    """做一轮：清理上次残留 → 启动会话 → 粘贴 prompt → 等待结束，结束或出错都关闭会话。"""
    t = db.get_task(task_id)
    d = P.task_dir(t)
    home = Path(cfg['claude_home']).expanduser()
    pdir = home / 'projects' / trajectory.encode_project_dir(str(d))
    name = session_name(task_id)
    # 后端异常退出后 tmux 里可能还留着上次的 Claude Code：先关掉，避免恢复初始快照后它继续往做题目录里写
    if alive(name):
        log(f'关闭上次残留的做题会话 {name}')
        kill(name)
    # 上次自动做题被中断或失败时会留下会话：按重做处理（作废会话、恢复初始快照）
    leftover = set(trajectory.list_sessions(home, str(d))) - set(t['voided_sessions'] or [])
    if leftover:
        log(f'发现 {len(leftover)} 个未作废的会话，按重做处理')
        t = P.void_and_reset(t, log)
    before = set(trajectory.list_sessions(home, str(d)))
    log(f'启动 Claude Code 会话 {name}，观看命令：{attach_command(task_id)}')
    start_session(name, d, 'claude --dangerously-skip-permissions')
    try:
        wait_ready(name, int(cfg['do_ready_timeout_sec']), log, stop)
        log('Claude Code 已就绪，粘贴 prompt 原文')
        paste_and_submit(name, t['user_prompt'])
        path = wait_session_file(pdir, before, t['user_prompt'], name, log, stop)
        log(f'已发送 会话 {path.stem}')
        _mark_started(task_id, True)
        wait_done(name, path, cfg, log, stop)
        log('这一轮已结束')
    except DriverError:
        log('最后的屏幕：\n' + capture(name)[-2000:])
        raise
    finally:
        _mark_started(task_id, False)
        kill(name)


def start_auto_do(task_id: int) -> str:
    P.need(db.get_task(task_id), P.S_DO)
    return jobs.submit('auto-do', task_id, lambda log, run_key, stop: run_do(task_id, log, stop))
