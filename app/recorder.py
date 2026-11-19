# -*- coding: utf-8 -*-
"""自动录屏。

两种正式录制方式（配置 record_desktop）：
- 本机桌面（默认）：在 Windows 主屏上用本机 Chrome、Windows Terminal、真实的文件对话框操作并录制，画面与真人录屏一致；
  只在桌面空闲（没人用电脑、没锁屏）够久时开始，录制中有人碰鼠标键盘立即中止，等再次空闲后重录。
- 虚拟屏：在 WSL 的虚拟屏（Xvfb）与虚拟声卡（PulseAudio 空设备）上后台录制，不占用本机桌面。
qa-record 生成脚本时的试跑一律在虚拟屏上进行（尺寸与本机主屏一致），不打扰桌面。

流程：全新克隆录屏目录 → 起虚拟屏与声卡 → 取缓存或执行录屏前准备并调用 qa-record 生成录屏脚本（试跑）→
重新克隆并重新准备（本机桌面方式先等桌面空闲）→ 正式录制 → 执行录完收尾 → 校验 mp4 → 移到待上传目录并标记完成。
"""
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import codex_runner, config, db, envcheck, jobs, pipeline as P, submission, wincursor, wintools

HARNESS = Path(__file__).resolve().parent / 'record_harness' / 'harness.mjs'
# 执行器接口版本：ctx 的用法有变化时加一，缓存的旧录屏脚本随之作废、重新生成
HARNESS_API = 5
# 录屏文件的高度（作业要求 720p）：屏幕更高时合成时按比例缩小
OUT_HEIGHT = 720
SINK = 'fbrec'
# 执行器检测到真人操作电脑而中止时的退出码
EXIT_USER_INPUT = 3
# 本机桌面同一时间只能录一道题
_desktop_lock = threading.Lock()
# 上一次本机桌面录制：开录前那段空闲的起点 since 与录制结束时刻 end
_desk_idle: dict = {}
# 录屏前准备（安装依赖、启动服务）与收尾的超时（秒）
SETUP_TIMEOUT = 1800
CLEANUP_TIMEOUT = 300
# 登录环境里不需要带给子进程的变量
ENV_DROP = {'PS1', 'PS2', 'SHLVL', '_', 'OLDPWD', 'PWD', 'BASH_EXECUTION_STRING', 'COLUMNS', 'LINES'}

# 虚拟屏里用 Windows 自带字体，画面接近 Windows 本机（终端 Cascadia Mono 或 Consolas，界面 Segoe UI 与微软雅黑）
WINDOWS_FONTS = Path('/mnt/c/Windows/Fonts')
FONT_PATTERNS = ('CascadiaMono*.ttf', 'CascadiaCode*.ttf', 'consola*.ttf', 'msyh*.ttc', 'segoeui*.ttf',
                 'seguisym.ttf', 'seguiemj.ttf', 'arial*.ttf', 'times*.ttf', 'simsun.ttc', 'simhei.ttf')
FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<!-- 自动录屏用：系统字体配置加 Windows 字体，默认字体族与 Windows 一致 -->
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>{fdir}</dir>
  <alias binding="strong"><family>sans-serif</family><prefer><family>Segoe UI</family><family>Microsoft YaHei</family></prefer></alias>
  <alias binding="strong"><family>serif</family><prefer><family>Times New Roman</family><family>SimSun</family></prefer></alias>
  <alias binding="strong"><family>monospace</family><prefer><family>{mono}</family><family>Microsoft YaHei</family></prefer></alias>
</fontconfig>
"""

_login_env_cache: dict = {}


def terminal_font() -> str:
    """终端字体：Windows Terminal 默认的 Cascadia Mono，没有时用 Consolas。"""
    if any(WINDOWS_FONTS.glob('CascadiaMono*.ttf')):
        return 'Cascadia Mono'
    if any(WINDOWS_FONTS.glob('consola*.ttf')):
        return 'Consolas'
    return 'DejaVu Sans Mono'


def windows_fonts(root: Path) -> Path | None:
    """把 Windows 字体链接到 root/fonts，写一份 fonts.conf（系统配置加这些字体并设为默认族）。没有 Windows 字体时返回 None。"""
    if not WINDOWS_FONTS.is_dir():
        return None
    fdir = root / 'fonts'
    fdir.mkdir(parents=True, exist_ok=True)
    found = 0
    for pat in FONT_PATTERNS:
        for f in WINDOWS_FONTS.glob(pat):
            link = fdir / f.name
            if not link.exists():
                link.symlink_to(f)
            found += 1
    if not found:
        return None
    conf = root / 'fonts.conf'
    conf.write_text(FONTS_CONF.format(fdir=fdir, mono=terminal_font()), encoding='utf-8')
    return conf


class RecordError(RuntimeError):
    pass


class ManualNeeded(RecordError):
    """这道题无法自动录屏，需要人工录制。"""


class UserInterrupted(RecordError):
    """本机桌面录制中有人操作了电脑。"""


def screen_size() -> dict:
    """录屏画面尺寸：本机主屏的尺寸与工作区（虚拟屏也按这个尺寸，试跑与正式录制的页面布局一致）。"""
    return wintools.screen()


def display_num(task_id: int) -> int:
    return 100 + task_id % 50


def x_ready(n: int) -> bool:
    """虚拟屏是否已在抽象套接字 @/tmp/.X11-unix/X<n> 上监听。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(f'\0/tmp/.X11-unix/X{n}')
        return True
    except OSError:
        return False
    finally:
        s.close()


def script_key(t: dict) -> str:
    """录屏脚本缓存的键：产物快照、录屏步骤、录屏前准备、执行器接口版本任一变化都要重新生成。"""
    raw = json.dumps([t.get('artifact_sha'), t.get('record_steps'), t.get('record_prep'), HARNESS_API],
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


_LOCAL_PORT = re.compile(r'(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})\b')


def _sub_port(v, old: int, new: int):
    """把内容里独立出现的端口号 old 换成 new（不碰更长数字里的同样几位），递归处理列表与字典。"""
    pat = re.compile(rf'(?<!\d){old}(?!\d)')
    if isinstance(v, str):
        return pat.sub(str(new), v)
    if isinstance(v, list):
        return [_sub_port(x, old, new) for x in v]
    if isinstance(v, dict):
        return {k: _sub_port(x, old, new) for k, x in v.items()}
    return v


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(('127.0.0.1', port)) == 0


def remap_excluded_ports(task_id: int, log, rngs: list[tuple[int, int]] | None = None) -> dict:
    """本机桌面录制前：录屏用的 localhost 端口落在 Windows 保留端口段里时，换成本题端口段里可用的端口。
    录屏前准备、录屏步骤、录屏脚本缓存一起换，脚本缓存原本有效就按新内容重算键，不用重新生成。返回 {旧端口: 新端口}。"""
    t = db.get_task(task_id)
    rngs = wintools.excluded_ports() if rngs is None else rngs
    if not rngs:
        return {}
    prep = t.get('record_prep') or {}
    cache = t.get('record_script') or {}
    text = json.dumps([prep, t.get('record_steps'), cache.get('script')], ensure_ascii=False)
    used = {int(p) for p in _LOCAL_PORT.findall(text)}
    bad = sorted(p for p in used if wintools.port_excluded(p, rngs))
    if not bad:
        return {}
    p0, p1 = P.port_range(t)
    free = (p for p in range(p0, p1 + 1)
            if p not in used and not wintools.port_excluded(p, rngs) and not _port_busy(p))
    mapping = {}
    for old in bad:
        new = next(free, None)
        if new is None:
            raise RecordError(f'录屏用的端口 {old} 在 Windows 保留端口段里，本题端口段 {p0}-{p1} 也没有可用端口，'
                              '调整配置里的 port_base 后重试')
        mapping[old] = new
    key_ok = cache.get('key') == script_key(t)
    fields = {'record_prep': prep, 'record_steps': t.get('record_steps')}
    script = cache.get('script')
    for old, new in mapping.items():
        fields['record_prep'] = _sub_port(fields['record_prep'], old, new)
        fields['record_steps'] = _sub_port(fields['record_steps'], old, new)
        if script:
            script = _sub_port(script, old, new)
    if script and key_ok:
        fields['record_script'] = dict(cache, script=script, key=script_key({**t, **fields}))
    db.update_task(task_id, **fields)
    log('录屏端口在 Windows 保留端口段里（本机桌面的浏览器访问会被拒），已换成：'
        + '，'.join(f'{a}→{b}' for a, b in mapping.items()))
    return mapping


def login_env() -> dict:
    """一个新开 WSL 终端里的环境变量（登录加交互配置，含 nvm），缓存 10 分钟。"""
    if _login_env_cache.get('t', 0) > time.time() - 600:
        return dict(_login_env_cache['env'])
    from .claude_driver import clean_env_prefix
    # 交互式 bash 会抢占控制终端，放到新会话里执行（同 envcheck._bash）
    r = subprocess.run([*clean_env_prefix(), 'bash', '-lic', 'env -0'], capture_output=True, timeout=60,
                       stdin=subprocess.DEVNULL, start_new_session=True)
    env = {}
    for item in r.stdout.decode('utf-8', 'replace').split('\0'):
        k, sep, v = item.partition('=')
        if sep and k and k not in ENV_DROP and not k.startswith('BASH_FUNC_'):
            env[k] = v
    if 'PATH' not in env:
        raise RecordError('读取 WSL 登录环境失败：' + r.stderr.decode('utf-8', 'replace')[-300:])
    _login_env_cache.update(t=time.time(), env=env)
    return dict(env)


def playwright_module(env: dict) -> str:
    """全局安装的 playwright 模块路径。"""
    r = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True, env=env, timeout=60)
    root = r.stdout.strip()
    if r.returncode != 0 or not root or not (Path(root) / 'playwright').is_dir():
        raise RecordError('没有找到全局安装的 playwright，先执行 scripts/setup-verify-tools.sh')
    return str(Path(root) / 'playwright')


class VirtualScreen:
    """一题一套虚拟屏与虚拟声卡，退出时连同录屏前准备启动的服务一起清理。"""

    def __init__(self, task_id: int, work: Path, audio: bool, log, size: dict | None = None):
        self.size = size or {'width': 1920, 'height': 1080, 'work_w': 1920, 'work_h': 1032}
        self.n = display_num(task_id)
        self.work = work
        self.audio = audio
        self.log = log
        self.procs: list[subprocess.Popen] = []
        self.groups: list[int] = []
        self.pulse_dir = work / 'pulse'
        self.cursor_root = config.DATA_DIR / 'record-cursors'
        self.fonts_conf: Path | None = None

    @property
    def display(self) -> str:
        return f':{self.n}'

    @property
    def pulse_server(self) -> str:
        return f'unix:{self.pulse_dir}/native'

    def env(self, base: dict) -> dict:
        e = dict(base)
        e['DISPLAY'] = self.display
        e.pop('WAYLAND_DISPLAY', None)
        # Windows 样式的系统指针（录屏时由 ffmpeg 录下）与 Windows 字体
        e['XCURSOR_PATH'] = str(self.cursor_root)
        e['XCURSOR_THEME'] = wincursor.THEME
        e['XCURSOR_SIZE'] = str(wincursor.SIZE)
        if self.fonts_conf:
            e['FONTCONFIG_FILE'] = str(self.fonts_conf)
        e['FB_TERM_FONT'] = terminal_font()
        if self.audio:
            e['PULSE_SERVER'] = self.pulse_server
            e['PULSE_SINK'] = SINK
        else:
            e.pop('PULSE_SERVER', None)
        return e

    def _spawn(self, cmd: list[str], logname: str, env: dict | None = None) -> subprocess.Popen:
        f = open(self.work / logname, 'ab')
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def __enter__(self):
        self.work.mkdir(parents=True, exist_ok=True)
        # 同一编号的虚拟屏可能是上次中断留下的，先结束掉
        subprocess.run(['pkill', '-f', f'Xvfb {self.display} '], capture_output=True)
        # /tmp/.X11-unix 在 WSLg 下只读，Xvfb 改用抽象套接字监听，客户端照常连接；
        # -noreset：最后一个客户端断开时不重置，否则 xsetroot 设的底色与根窗口指针会被清掉（弹窗上显示 X 形指针）
        x = self._spawn(['Xvfb', self.display, '-screen', '0', f"{self.size['width']}x{self.size['height']}x24", '-dpi', '96',
                         '-nolisten', 'tcp', '-ac', '-noreset'], 'xvfb.log')
        for _ in range(50):
            if x.poll() is not None:
                raise RecordError('虚拟屏 Xvfb 启动失败：' + _tail(self.work / 'xvfb.log', 500))
            if x_ready(self.n):
                break
            time.sleep(0.2)
        else:
            raise RecordError('虚拟屏 Xvfb 10 秒内没有就绪：' + _tail(self.work / 'xvfb.log', 500))
        self.log(f"虚拟屏 {self.display} 已启动 {self.size['width']}x{self.size['height']}")
        wincursor.ensure_theme(self.cursor_root)
        self.fonts_conf = windows_fonts(config.DATA_DIR / 'record-fonts')
        self.log('使用 Windows 字体' if self.fonts_conf else '没有找到 Windows 字体，使用系统字体')
        # 桌面底色与窗口没盖住处的指针也换成 Windows 箭头
        if shutil.which('xsetroot'):
            subprocess.run(['xsetroot', '-cursor_name', 'left_ptr', '-solid', '#0C0C0C'],
                           env=self.env(dict(os.environ)), capture_output=True, timeout=10)
        if self.audio:
            self.pulse_dir.mkdir(parents=True, exist_ok=True)
            # 专用实例：不连会话总线（否则和已有的 pulseaudio 抢 D-Bus 名称），运行文件放在本次工作目录
            env = {**os.environ, 'PULSE_RUNTIME_PATH': str(self.pulse_dir), 'XDG_RUNTIME_DIR': str(self.pulse_dir)}
            for k in ('PULSE_SERVER', 'DBUS_SESSION_BUS_ADDRESS'):
                env.pop(k, None)
            p = self._spawn(['pulseaudio', '-n', '--daemonize=no', '--exit-idle-time=-1', '--disallow-exit',
                             '--use-pid-file=no', '--log-target=stderr',
                             '-L', f'module-native-protocol-unix socket={self.pulse_dir}/native auth-anonymous=1',
                             '-L', f'module-null-sink sink_name={SINK} sink_properties=device.description=fbrec',
                             '-L', 'module-always-sink'], 'pulse.log', env=env)
            for _ in range(50):
                if p.poll() is not None:
                    raise RecordError('虚拟声卡 pulseaudio 启动失败：' +
                                      (self.work / 'pulse.log').read_text(errors='replace')[-500:])
                if (self.pulse_dir / 'native').exists():
                    break
                time.sleep(0.2)
            self.log('虚拟声卡已启动')
        return self

    def track_group(self, pgid: int):
        """记下录屏前准备的进程组，退出时一起结束（收尾命令漏掉的服务也能清掉）。"""
        self.groups.append(pgid)

    def kill_services(self):
        for g in self.groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(g, signal.SIGTERM)
        time.sleep(1)
        for g in self.groups:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(g, signal.SIGKILL)
        self.groups = []

    def __exit__(self, *exc):
        self.kill_services()
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        return False


def run_shell(script: str, cwd: Path, env: dict, logfile: Path, timeout: int, vs: VirtualScreen | None = None) -> int:
    """在录屏目录执行一段命令，输出写入文件（后台启动的服务不会卡住读取）；返回退出码。"""
    with open(logfile, 'ab') as f:
        p = subprocess.Popen(['bash', '-c', script], cwd=str(cwd), env=env, stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        if vs:
            vs.track_group(p.pid)
        try:
            return p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(p.pid, signal.SIGKILL)
            return 124


def _tail(path: Path, n: int = 2000) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')[-n:]
    except OSError:
        return ''


def setup(t: dict, rdir: Path, env: dict, vs: VirtualScreen, log, name: str = 'setup') -> str:
    """执行录屏前准备（不录）。失败抛错，返回输出末尾供生成脚本参考。"""
    cmd = ((t.get('record_prep') or {}).get('setup') or '').strip()
    if not cmd:
        log('没有录屏前准备')
        return ''
    logfile = vs.work / f'{name}.log'
    log('执行录屏前准备')
    rc = run_shell(cmd, rdir, env, logfile, SETUP_TIMEOUT, vs)
    out = _tail(logfile)
    log(f'录屏前准备退出码 {rc}，输出末尾：\n{out[-1500:]}')
    if rc != 0:
        raise RecordError(f'录屏前准备失败（退出码 {rc}），按日志修正录屏前准备的命令后重试')
    # 后台启动的服务需要一点时间才能接受连接
    time.sleep(3)
    return out


def cleanup(t: dict, rdir: Path, env: dict, vs: VirtualScreen, log):
    cmd = ((t.get('record_prep') or {}).get('cleanup') or '').strip()
    if cmd:
        rc = run_shell(cmd, rdir, env, vs.work / 'cleanup.log', CLEANUP_TIMEOUT)
        log(f'录完收尾退出码 {rc}')
    vs.kill_services()


def harness_size_args(size: dict) -> list[str]:
    return ['--size', f"{size['width']}x{size['height']}", '--work-size', f"{size['work_w']}x{size['work_h']}"]


def write_dryrun(vs: VirtualScreen, env: dict, rdir: Path, max_sec: int) -> Path:
    """写一个试跑脚本给 qa-record 用：带好虚拟屏环境，参数只需要录屏脚本路径。"""
    lines = ['#!/usr/bin/env bash', '# 自动录屏试跑：同一块虚拟屏，不录制。用法：bash dryrun.sh <录屏脚本.mjs>', 'set -u']
    for k, v in sorted(env.items()):
        lines.append(f'export {k}={shlex.quote(v)}')
    size = ' '.join(shlex.quote(a) for a in harness_size_args(vs.size))
    lines.append(f'exec node {shlex.quote(str(HARNESS))} --dry-run --script "$1" --cwd {shlex.quote(str(rdir))} '
                 f'--work {shlex.quote(str(vs.work / "dryrun"))} --max-sec {max_sec} {size}')
    p = vs.work / 'dryrun.sh'
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return p


def script_input(t: dict, rdir: Path, dryrun: Path, setup_out: str, run_key: str, size: dict | None = None) -> str:
    """传给 qa-record 的输入。"""
    cfg = config.load()
    size = size or {'width': 1920, 'height': 1080, 'work_w': 1920, 'work_h': 1032}
    ws_wsl = Path(cfg['agent_workspace_wsl']) / 'runs' / run_key
    ws_win = cfg['agent_workspace_win'].rstrip('\\') + f'\\runs\\{run_key}'
    p0, p1 = P.port_range(t)
    prep = t.get('record_prep') or {}
    ev = [e for e in t.get('evidence') or [] if e.get('recordable')]
    final = ('Windows 本机桌面（本机 Chrome、Windows Terminal 里的 WSL、Windows 文件对话框）'
             if cfg.get('record_desktop') else 'WSL 虚拟屏（与试跑相同）')
    parts = [
        *P.run_dir_lines(run_key),
        '## 题目',
        f"分支 {t['branch']}",
        f"语言/框架 {t['lang_framework']}",
        'User Prompt 原文：', t['user_prompt'],
        '', '## 验收结论',
        f"交付完整性 {t['delivery_score']}",
        'Feedback：', t['feedback'] or '无',
        '', '## 可录的证据',
        *([f"- {e['phenomenon']} | {e['command']} | {e['output_excerpt']}" for e in ev] or ['无']),
        '', '## 录屏前准备（已执行，服务已在后台运行，不录）', prep.get('setup') or '无',
        '执行输出末尾：', setup_out[-1500:] or '无',
        '', '## 录屏步骤（照这些写录屏脚本）',
        *[f"{i}. {s['action']}" + (f"\n   命令 {s['command']}" if s.get('command') else '') + f"\n   预期 {s['expect']}"
          for i, s in enumerate(t.get('record_steps') or [], 1)],
        '', '## 录完收尾（不录，脚本里不要执行）', prep.get('cleanup') or '无',
        '', '## 录屏环境',
        f'录屏目录 {rdir}（WSL 路径，终端命令都在这里执行）',
        f'录屏目录的 Windows 读取路径 {config.unc(rdir)}',
        f'可用端口 {p0}-{p1}',
        f"屏幕 {size['width']}x{size['height']}，浏览器与终端窗口 {size['work_w']}x{size['work_h']}（去掉任务栏，浏览器含标签栏与地址栏），"
        '产物服务在 WSL 内，用 http://localhost:<端口> 访问',
        f'试跑在 WSL 虚拟屏上进行，正式录制在 {final}',
        f"时长上限 {cfg['record_max_sec']} 秒（脚本 run 部分要在 {int(cfg['record_max_sec']) - 5} 秒内结束）",
        '', '## 文件位置',
        f'录屏脚本写到 {ws_win}\\record.mjs（WSL 路径 {ws_wsl}/record.mjs）',
        f"试跑命令 wsl.exe -d {cfg['wsl_distro']} -- bash {dryrun} {ws_wsl}/record.mjs",
        f"试跑截图目录 {config.unc(dryrun.parent / 'dryrun' / 'shots')}（每次 ctx.shot 与出错时的截图）",
    ]
    return '\n'.join(parts)


def _follow(p: subprocess.Popen, max_sec: int, log, stop) -> int:
    """读执行器的事件流写进日志，返回退出码；出错时抛出执行器报告的原因。"""
    # 录完还要把画面与声音合成并压缩成 mp4，多留一些时间
    deadline = time.time() + max_sec + 300
    error = ''
    for raw in p.stdout:
        line = raw.decode('utf-8', 'replace').rstrip()
        if not line:
            continue
        log('[录屏] ' + line[:800])
        try:
            ev = json.loads(line)
            if ev.get('event') == 'error':
                error = ev.get('message', '')
        except ValueError:
            pass
        if stop.is_set() or time.time() > deadline:
            p.kill()
            raise RecordError('人工停止' if stop.is_set() else '录屏执行超时')
    p.wait()
    if p.returncode == EXIT_USER_INPUT:
        raise UserInterrupted('录屏中检测到有人操作电脑，已中止')
    if p.returncode != 0:
        raise RecordError('录屏脚本执行失败：' + (error or f'退出码 {p.returncode}'))
    return p.returncode


def run_harness(script: Path, rdir: Path, env: dict, vs: VirtualScreen, out: Path | None, max_sec: int,
                log, stop) -> None:
    """在虚拟屏上执行录屏脚本：out 为空时只试跑。"""
    cmd = ['node', str(HARNESS), '--script', str(script), '--cwd', str(rdir), '--work', str(vs.work / 'final'),
           '--max-sec', str(max_sec), '--out-height', str(OUT_HEIGHT), *harness_size_args(vs.size)]
    if out:
        cmd += ['--out', str(out)]
        if vs.audio:
            cmd += ['--audio', f'{SINK}.monitor']
    else:
        cmd.append('--dry-run')
    p = subprocess.Popen(cmd, cwd=str(rdir), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
    _follow(p, max_sec, log, stop)


def desktop_work(run_key: str) -> tuple[str, Path]:
    """本机桌面录制的工作目录：Windows 路径与对应的 WSL 路径。"""
    root = wintools.detect()['root']
    if not root:
        raise RecordError('没有找到 Windows 的 %LOCALAPPDATA%')
    win = f'{root}\\runs\\{run_key}'
    wsl = Path(config.wsl_path(win))
    wsl.mkdir(parents=True, exist_ok=True)
    return win, wsl


def run_desktop_harness(script_text: str, rdir: Path, run_key: str, attempt: int, audio: bool, max_sec: int,
                        log, stop) -> Path:
    """在本机桌面上正式录制，返回 mp4 的 WSL 路径。执行器在 Windows 的 node 里运行。"""
    tools = wintools.detect()
    win_work, wsl_work = desktop_work(f'{run_key}-{attempt}')
    (wsl_work / 'record.mjs').write_text(script_text, encoding='utf-8')
    out = wsl_work / 'record.mp4'
    cmd = [wintools.exe(tools['node']), config.win_path(HARNESS), '--platform', 'win',
           '--script', f'{win_work}\\record.mjs', '--cwd', str(rdir), '--work', win_work,
           '--out', f'{win_work}\\record.mp4', '--max-sec', str(max_sec), '--out-height', str(OUT_HEIGHT),
           '--audio', '1' if audio else '0',
           '--pw', tools['pw'], '--python', tools['python'], '--ffmpeg', tools['ffmpeg'],
           '--ffprobe', tools['ffprobe'], '--chrome', tools['chrome'], '--root', tools['root'],
           '--distro', config.load()['wsl_distro']]
    p = subprocess.Popen(cmd, cwd=wintools.WIN_CWD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL)
    _follow(p, max_sec, log, stop)
    # 合成前的画面与声音文件很大，合成好就删掉（出错时保留现场）
    for name in ('video.mkv', 'audio.wav'):
        (wsl_work / name).unlink(missing_ok=True)
    return out


def desktop_idle_sec(st: dict) -> float:
    """桌面实际空闲了多久：系统的「上次输入」也算上了录屏自己注入的输入，
    最近一次输入不晚于上次录制结束时，说明之后没人碰过电脑，空闲从上次录制前的空闲起点算。"""
    now = time.time()
    if _desk_idle and now - st['idle_sec'] <= _desk_idle['end'] + 5:
        return max(st['idle_sec'], now - _desk_idle['since'])
    return st['idle_sec']


def wait_desktop_idle(idle_min: float, log, stop) -> float:
    """等到本机桌面没锁屏、且实际空闲超过 idle_min 分钟；返回这段空闲的起点（时间戳）。
    屏保（恢复时不需要登录）在运行不影响，开录时执行器会先退出屏保。"""
    need = idle_min * 60
    last_note = 0.0
    while True:
        if stop.is_set():
            raise RecordError('人工停止')
        try:
            st = wintools.idle()
        except (RuntimeError, ValueError) as e:
            raise RecordError(f'查询桌面状态失败：{e}') from e
        idle = desktop_idle_sec(st)
        if not st['locked'] and idle >= need:
            log(f"桌面已空闲 {int(idle // 60)} 分钟" + ('（屏保运行中，开录时退出）' if st.get('screensaver') else '')
                + '，开始录制')
            return time.time() - idle
        if time.time() - last_note > 600:
            last_note = time.time()
            log('等待桌面空闲：' + ('已锁屏，解锁后才能录（锁屏时画面录不到）' if st['locked']
                                   else f"有人在用电脑（{int(idle)} 秒前有操作），需空闲 {idle_min:g} 分钟"))
        stop.wait(min(30.0, max(5.0, need - idle)))


def out_size(width: int, height: int) -> tuple[int, int]:
    """录屏文件的尺寸：按屏幕原尺寸录，合成时高度缩到 OUT_HEIGHT（作业要求 720p），宽度按比例取偶数。"""
    if height <= OUT_HEIGHT:
        return width, height
    return int(round(width * OUT_HEIGHT / height / 2)) * 2, OUT_HEIGHT


def check_video(path: Path, max_sec: int, size: tuple[int, int]) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        raise RecordError('没有生成录屏文件')
    info = submission._probe(path)
    problems = []
    if info['duration'] is None or info['duration'] > max_sec:
        problems.append(f"时长 {info['duration']} 秒超过 {max_sec} 秒")
    if (info['width'], info['height']) != tuple(size):
        problems.append(f"分辨率 {info['width']}x{info['height']} 不是 {size[0]}x{size[1]}")
    if problems:
        raise RecordError('录屏文件不合格：' + '；'.join(problems))
    return info


def generate_script(t: dict, rdir: Path, env: dict, vs: VirtualScreen, setup_out: str, run_key: str, log, stop) -> dict:
    cfg = config.load()
    dryrun = write_dryrun(vs, env, rdir, int(cfg['record_max_sec']))
    text = script_input(t, rdir, dryrun, setup_out, run_key, vs.size)
    with jobs.ai_slot(log):
        r = codex_runner.run_skill('qa-record', text, run_key, log, stop)
    cache = {'key': script_key(t), 'script': r['script'], 'est_seconds': r['est_seconds'],
             'manual_needed': bool(r['manual_needed']), 'reason': r['reason'], 'run_key': run_key,
             'time': db.now()}
    db.update_task(t['id'], record_script=cache)
    return cache


def record_on_desktop(task_id: int, script_text: str, env: dict, vs: VirtualScreen, run_key: str, log, stop) -> Path:
    """本机桌面正式录制：等桌面空闲 → 全新克隆并准备 → 录制；有人操作电脑就中止，等再次空闲后重来。"""
    cfg = config.load()
    idle_min = float(cfg.get('record_idle_min') or 0)
    tries = max(1, int(cfg.get('record_user_retries') or 1))
    with _desktop_lock:
        for attempt in range(1, tries + 1):
            since = wait_desktop_idle(idle_min, log, stop) or time.time()
            t = db.get_task(task_id)
            rdir = P.prepare_record_dir(t, log)
            setup(t, rdir, env, vs, log, name=f'setup-desktop{attempt}')
            human = False
            try:
                st = wintools.idle()
                # 准备期间有人回来用电脑了：不开录，重新等
                if st['locked'] or desktop_idle_sec(st) < idle_min * 60:
                    raise UserInterrupted('准备录屏期间有人用了电脑')
                log(f'开始在本机桌面正式录制（第 {attempt} 次）')
                return run_desktop_harness(script_text, rdir, run_key, attempt, bool(cfg.get('record_audio')),
                                           int(cfg['record_max_sec']), log, stop)
            except UserInterrupted as e:
                human = True
                log(f'{e}，等桌面再次空闲后重录')
            finally:
                # 记下这次录制注入输入的时段：没人打断时，下一道题不用因为录屏自己的输入重新等空闲
                if human:
                    _desk_idle.clear()
                else:
                    _desk_idle.update(since=since, end=time.time())
                cleanup(t, rdir, env, vs, log)
    raise RecordError(f'连续 {tries} 次录制被人操作电脑打断，稍后重试')


def run_record(task_id: int, run_key: str, log, stop):
    cfg = config.load()
    t = P.need(db.get_task(task_id), P.S_RECORD)
    if not t.get('record_steps'):
        raise RecordError('没有录屏步骤')
    audio = bool(cfg.get('record_audio'))
    desktop = bool(cfg.get('record_desktop'))
    missing = envcheck.record_tools_missing(audio)
    if missing:
        raise RecordError('录屏工具缺失：' + '、'.join(missing) + '，执行 scripts/setup-verify-tools.sh 安装')
    if desktop:
        miss = [i['name'] for i in wintools.check(audio) if not i['ok']]
        if miss:
            raise RecordError('本机桌面录屏工具缺失：' + '、'.join(miss) + '，执行 scripts/setup-desktop-record.ps1 安装')
    max_sec = int(cfg['record_max_sec'])
    if desktop:
        # 端口换过就重新读取题目，后面的准备、脚本缓存都按新端口
        if remap_excluded_ports(task_id, log):
            t = db.get_task(task_id)
    cache = t.get('record_script') or {}
    if cache.get('key') == script_key(t) and cache.get('manual_needed'):
        raise ManualNeeded(f"录屏脚本生成时判断需要人工录屏：{cache.get('reason')}")
    size = screen_size()
    work = config.DATA_DIR / 'record-runs' / run_key
    work.mkdir(parents=True, exist_ok=True)
    rdir = P.prepare_record_dir(t, log)
    base = login_env()
    with VirtualScreen(task_id, work, audio, log, size) as vs:
        env = vs.env(base)
        env['FB_PLAYWRIGHT'] = playwright_module(env)
        fresh = True
        if cache.get('key') == script_key(t) and cache.get('script'):
            log(f"复用 {cache.get('time')} 生成的录屏脚本")
        else:
            setup_out = setup(t, rdir, env, vs, log)
            log('调用 qa-record 生成录屏脚本')
            cache = generate_script(t, rdir, env, vs, setup_out, run_key, log, stop)
            cleanup(t, rdir, env, vs, log)
            if cache['manual_needed']:
                raise ManualNeeded(f"qa-record 判断需要人工录屏：{cache['reason']}")
            # 试跑会改动录屏目录与服务状态：重新克隆、重新准备，从干净状态开录
            fresh = False
        if desktop:
            out = record_on_desktop(task_id, cache['script'], env, vs, run_key, log, stop)
        else:
            if not fresh:
                log('试跑过录屏脚本，重新克隆录屏目录')
                rdir = P.prepare_record_dir(db.get_task(task_id), log)
            setup(t, rdir, env, vs, log, name='setup2' if not fresh else 'setup')
            script = work / 'record.mjs'
            script.write_text(cache['script'], encoding='utf-8')
            out = work / 'record.mp4'
            log('开始在虚拟屏正式录制')
            try:
                run_harness(script, rdir, env, vs, out, max_sec, log, stop)
            finally:
                cleanup(t, rdir, env, vs, log)
    info = check_video(out, max_sec, out_size(size['width'], size['height']))
    # 放到待上传目录，与轨迹同名（甲方序号-分支名）；重新录屏时覆盖旧文件
    dest = P.upload_dir() / f"{P.upload_stem(db.get_task(task_id))}.mp4"
    shutil.move(str(out), dest)
    log(f"录屏完成 {config.win_path(dest)} 时长 {info['duration']} 秒 {info['width']}x{info['height']}")
    P.complete(task_id, config.win_path(dest))


def start_auto_record(task_id: int) -> str:
    P.need(db.get_task(task_id), P.S_RECORD)
    return jobs.submit('auto-record', task_id, lambda log, run_key, stop: run_record(task_id, run_key, log, stop))
