# -*- coding: utf-8 -*-
"""本机桌面录屏用到的 Windows 侧工具：定位 node、Python、ffmpeg、Chrome 等，查询屏幕尺寸与桌面是否空闲。

后端在 WSL 里运行，通过 WSL 互操作直接执行 Windows 程序；Windows 程序的工作目录放在 /mnt/c 下，
避免 cmd.exe 在 UNC 当前目录下报警告。
"""
import json
import subprocess
import time
from pathlib import Path

from . import config

HARNESS_DIR = Path(__file__).resolve().parent / 'record_harness'
WIN_CWD = '/mnt/c'
CHROME_CANDIDATES = (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                     r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe')
# 后端的 PATH 里不一定有 Windows 目录，cmd.exe 用绝对路径
CMD = '/mnt/c/Windows/System32/cmd.exe'
_cache: dict = {}


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=WIN_CWD, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    out = p.stdout.decode('utf-8', 'replace')
    if '\ufffd' in out:
        out = p.stdout.decode('gbk', 'replace')
    return p.returncode, out.strip()


def _where(name: str) -> str:
    """Windows PATH 里的程序，跳过应用商店的占位程序（WindowsApps 下的 python.exe 等）；
    wt 本身就是 WindowsApps 下的应用别名，不跳过。"""
    rc, out = _run([CMD, '/c', 'where', name])
    if rc != 0:
        return ''
    for line in out.splitlines():
        line = line.strip()
        if line and (name == 'wt' or 'WindowsApps' not in line):
            return line
    return ''


def detect(force: bool = False) -> dict:
    """Windows 侧工具的路径（Windows 路径），缓存 10 分钟。"""
    if not force and _cache.get('t', 0) > time.time() - 600:
        return dict(_cache['tools'])
    rc, local = _run([CMD, '/c', 'echo', '%LOCALAPPDATA%'])
    local = local.strip() if rc == 0 and '%' not in local else ''
    root = local + '\\feedback-record' if local else ''
    chrome = ''
    for c in CHROME_CANDIDATES + ((local + r'\Google\Chrome\Application\chrome.exe',) if local else ()):
        if Path(config.wsl_path(c)).is_file():
            chrome = c
            break
    tools = {
        'node': _where('node'), 'python': _where('python'), 'ffmpeg': _where('ffmpeg'), 'ffprobe': _where('ffprobe'),
        'wt': _where('wt'), 'chrome': chrome, 'root': root,
        'pw': root + r'\node\node_modules\playwright-core' if root else '',
    }
    _cache.update(t=time.time(), tools=tools)
    return dict(tools)


def exe(win: str) -> str:
    """Windows 程序路径转成 WSL 里可直接执行的路径。"""
    return config.wsl_path(win)


def helper(cmd: str, timeout: int = 30) -> dict:
    """单次调用输入助手（idle、screen、windows），返回它输出的 JSON。"""
    t = detect()
    if not t['python']:
        raise RuntimeError('没有找到 Windows 的 Python')
    rc, out = _run([exe(t['python']), config.win_path(HARNESS_DIR / 'wininput.py'), cmd], timeout)
    if rc != 0:
        raise RuntimeError(f'输入助手 {cmd} 失败：{out[-300:]}')
    return json.loads(out.splitlines()[-1])


def screen() -> dict:
    """主屏尺寸与工作区，查不到时按 1920x1080、任务栏 48 像素。"""
    if 'screen' in _cache and _cache.get('screen_t', 0) > time.time() - 600:
        return dict(_cache['screen'])
    try:
        s = helper('screen')
        res = {'width': s['width'], 'height': s['height'], 'work_w': s['work'][2] - s['work'][0],
               'work_h': s['work'][3] - s['work'][1]}
    except (RuntimeError, ValueError, KeyError, IndexError):
        res = {'width': 1920, 'height': 1080, 'work_w': 1920, 'work_h': 1032}
    _cache.update(screen=res, screen_t=time.time())
    return dict(res)


def idle() -> dict:
    """距上次鼠标键盘输入的秒数与是否锁屏。"""
    return helper('idle')


def check(audio: bool) -> list[dict]:
    """本机桌面录屏需要的 Windows 侧工具是否齐全，逐项给出处理方式。"""
    t = detect(force=True)
    setup = 'pwsh -File scripts\\setup-desktop-record.ps1'
    items = [
        ('Windows node', bool(t['node']), '安装 node 并加入 Windows 的 PATH'),
        ('Windows Python', bool(t['python']), '安装 Python 3 并加入 Windows 的 PATH'),
        ('Windows ffmpeg', bool(t['ffmpeg'] and t['ffprobe']), 'winget install Gyan.FFmpeg'),
        ('Google Chrome', bool(t['chrome']), '安装 Google Chrome'),
        ('Windows Terminal', bool(t['wt']), '从应用商店安装 Windows Terminal'),
        ('playwright-core（Windows）', bool(t['pw']) and Path(config.wsl_path(t['pw'] + r'\package.json')).is_file(),
         setup),
    ]
    if audio and t['python']:
        rc, _ = _run([exe(t['python']), '-c', 'import pyaudiowpatch'])
        items.append(('pyaudiowpatch（系统声音）', rc == 0, setup))
    if t['ffmpeg']:
        rc, out = _run([exe(t['ffmpeg']), '-hide_banner', '-filters'])
        items.append(('ffmpeg ddagrab', rc == 0 and 'ddagrab' in out, 'winget upgrade Gyan.FFmpeg（需要 6.1 以上）'))
    return [{'name': n, 'ok': ok, 'detail': '可用' if ok else '缺失', 'fix': fix} for n, ok, fix in items]
