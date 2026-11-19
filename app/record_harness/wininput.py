# -*- coding: utf-8 -*-
"""本机桌面录屏的 Windows 输入助手（Windows 的 Python 执行，只用标准库 ctypes）。

两种用法：
- 常驻：`python wininput.py serve`，标准输入每行一个 JSON 请求 {"id": n, "op": ...}，标准输出逐行回复
  {"id": n, "ok": true, ...}；开启 watch 后，检测到真人的鼠标键盘操作时另外输出 {"event": "user-input"}。
- 单次：`python wininput.py idle|screen|windows|quiet|quiet-restore`，输出一行 JSON 后退出（后端检查桌面是否空闲时用）。

鼠标键盘一律用 SendInput 注入，并在 dwExtraInfo 打上标记；监听真人操作用低级钩子，
带注入标记的事件不算真人操作。
"""
import contextlib
import ctypes
import json
import os
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
winmm = ctypes.WinDLL('winmm')

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
# 注入事件的标记，钩子据此区分自己注入的事件
MARK = 0x46424331

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
LLMHF_INJECTED, LLKHF_INJECTED = 0x01, 0x10


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('u', _INPUTUNION)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, wintypes.UINT]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.GetForegroundWindow.restype = wintypes.HWND

# 按键名（兼容 xdotool 写法）到虚拟键码；带 * 的是扩展键
VK = {
    'ctrl': 0x11, 'control': 0x11, 'shift': 0x10, 'alt': 0x12, 'win': 0x5B, 'super': 0x5B,
    'return': 0x0D, 'enter': 0x0D, 'tab': 0x09, 'escape': 0x1B, 'esc': 0x1B, 'backspace': 0x08,
    'space': 0x20, 'delete': 0x2E, 'insert': 0x2D, 'home': 0x24, 'end': 0x23, 'pageup': 0x21,
    'page_up': 0x21, 'prior': 0x21, 'pagedown': 0x22, 'page_down': 0x22, 'next': 0x22,
    'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
    **{f'f{i}': 0x6F + i for i in range(1, 13)},
}
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}

_out_lock = threading.Lock()


def emit(obj: dict):
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def _send(*inputs: INPUT):
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise OSError(f'SendInput 失败：{ctypes.get_last_error()}')


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    i = INPUT(type=INPUT_MOUSE)
    i.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, MARK)
    return i


def _key(vk: int = 0, up: bool = False, scan: int = 0, unicode: bool = False) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_UNICODE if unicode else 0)
    if vk in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    # 带上扫描码，与真键盘一样：浏览器的 KeyboardEvent.code（如 KeyA、ArrowRight）按扫描码判断，网页游戏多用它
    if vk and not unicode and not scan:
        scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, MARK)
    return i


def screen() -> dict:
    """主屏尺寸与工作区（去掉任务栏的部分）。"""
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)
    # 主屏的显示器句柄（gfxcapture 按它录主屏）
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    hmon = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)
    try:
        dpi = user32.GetDpiForSystem()
    except AttributeError:
        dpi = 96
    return {'width': user32.GetSystemMetrics(0), 'height': user32.GetSystemMetrics(1),
            'work': [rect.left, rect.top, rect.right, rect.bottom], 'hmonitor': int(hmon or 0), 'dpi': dpi}


def _wait_until(t: float):
    """等到 perf_counter 的时刻 t：先粗睡再自旋，保证 10 毫秒级的节奏。"""
    while True:
        left = t - time.perf_counter()
        if left <= 0:
            return
        if left > 0.002:
            time.sleep(left - 0.0015)


def move_path(pts: list, dt: float):
    """沿给定的屏幕坐标逐点移动指针（绝对坐标，按主屏归一化）。点是 [x, y] 或 [x, y, 停顿毫秒]，
    没给停顿时每点间隔 dt 秒。"""
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    t = time.perf_counter()
    for p in pts:
        x, y = p[0], p[1]
        nx = int(round(x * 65535 / max(1, w - 1)))
        ny = int(round(y * 65535 / max(1, h - 1)))
        _send(_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, nx, ny))
        t += (p[2] / 1000.0) if len(p) > 2 else dt
        _wait_until(t)


# 还按着的键（虚拟键码，按下顺序）与鼠标键，助手退出时统一松开
_held_keys: list[int] = []
_held_buttons: set[str] = set()


def button(btn: str, down: bool):
    (_held_buttons.add if down else _held_buttons.discard)(btn)
    flags = {('left', True): MOUSEEVENTF_LEFTDOWN, ('left', False): MOUSEEVENTF_LEFTUP,
             ('right', True): MOUSEEVENTF_RIGHTDOWN, ('right', False): MOUSEEVENTF_RIGHTUP,
             ('middle', True): MOUSEEVENTF_MIDDLEDOWN, ('middle', False): MOUSEEVENTF_MIDDLEUP}[(btn, down)]
    _send(_mouse(flags))


def wheel(notches: int):
    """滚轮：正数向下（与 xdotool 的按钮 5 一致）。"""
    _send(_mouse(MOUSEEVENTF_WHEEL, data=-120 * notches))


def _vk_of(name: str) -> int:
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1 and (n.isalnum()):
        return ord(n.upper())
    raise ValueError(f'不认识的按键：{name}')


def key(combo: str, hold: float = 0.06):
    """按组合键，如 ctrl+a、Return、F5：修饰键依次按下，主键按下松开，再倒序松开修饰键。"""
    vks = [_vk_of(p) for p in combo.split('+') if p]
    # 按住某个键时再按别的键，原来那个键不再自动重复（与真键盘一样）
    _stop_repeat()
    for vk in vks:
        _send(_key(vk))
        time.sleep(0.02)
    time.sleep(hold)
    for vk in reversed(vks):
        _send(_key(vk, up=True))
        time.sleep(0.02)


# 自动重复：与真键盘一样，只有最后按下的键在按住超过重复延迟后按重复速度连续触发按下
_repeat_lock = threading.Lock()
_repeat = {'vk': None, 'gen': 0}


def repeat_timing() -> tuple[float, float]:
    """系统设置里的重复延迟（秒）与重复间隔（秒）：延迟档 0 到 3 对应约 250 到 1000 毫秒，速度档 0 到 31 对应每秒约 2.5 到 30 次。"""
    delay, speed = wintypes.UINT(), wintypes.DWORD()
    user32.SystemParametersInfoW(0x0016, 0, ctypes.byref(delay), 0)
    user32.SystemParametersInfoW(0x000A, 0, ctypes.byref(speed), 0)
    return 0.25 * (min(delay.value, 3) + 1), 1.0 / (2.5 + min(speed.value, 31) * 27.5 / 31)


def _start_repeat(vk: int):
    with _repeat_lock:
        _repeat['gen'] += 1
        gen = _repeat['gen']
        _repeat['vk'] = vk

    def run():
        first, every = repeat_timing()
        time.sleep(first)
        while True:
            with _repeat_lock:
                if _repeat['gen'] != gen or vk not in _held_keys:
                    return
                _send(_key(vk))
            time.sleep(every)
    threading.Thread(target=run, daemon=True, name='key-repeat').start()


def _stop_repeat(vk: int | None = None):
    with _repeat_lock:
        if vk is None or _repeat['vk'] == vk:
            _repeat['gen'] += 1
            _repeat['vk'] = None


def key_down(combo: str):
    """按下不放：组合键按顺序依次按下（如 shift+right），之后用 key_up 松开；按住时最后一个键自动重复。"""
    vks = [_vk_of(p) for p in combo.split('+') if p]
    for vk in vks:
        with _repeat_lock:
            _send(_key(vk))
            _held_keys.append(vk)
        time.sleep(0.02)
    if vks:
        _start_repeat(vks[-1])


def key_up(combo: str):
    """松开 key_down 按下的键，倒序松开。"""
    for vk in reversed([_vk_of(p) for p in combo.split('+') if p]):
        _stop_repeat(vk)
        with _repeat_lock:
            _send(_key(vk, up=True))
            if vk in _held_keys:
                _held_keys.remove(vk)
        time.sleep(0.02)


def release_all():
    """松开所有还按着的键与鼠标键（执行器出错或退出时兜底，不让按键卡住）。"""
    _stop_repeat()
    with _repeat_lock:
        for vk in reversed(list(_held_keys)):
            with contextlib.suppress(Exception):
                _send(_key(vk, up=True))
        _held_keys.clear()
    for btn in list(_held_buttons):
        with contextlib.suppress(Exception):
            button(btn, False)
    _held_buttons.clear()


def text(s: str, delays: list | None = None):
    """逐字输入（Unicode 方式，中英文都可以），delays 为每个字之后的停顿秒数。"""
    for i, ch in enumerate(s):
        if ch == '\n':
            key('return')
        else:
            for unit in _utf16_units(ch):
                _send(_key(0, scan=unit, unicode=True), _key(0, up=True, scan=unit, unicode=True))
        if delays and i < len(delays):
            time.sleep(max(0.0, float(delays[i])))


CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]


def set_clipboard(s: str):
    """把文本放进系统剪贴板（Unicode 文本）；剪贴板被别的程序占着时稍等重试。"""
    data = (s + '\0').encode('utf-16-le')
    for _ in range(40):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError('打不开系统剪贴板')
    try:
        user32.EmptyClipboard()
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            raise OSError('分配剪贴板内存失败')
        ptr = kernel32.GlobalLock(h)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise OSError('写入剪贴板失败')
    finally:
        user32.CloseClipboard()


def paste(s: str, combo: str = 'ctrl+v'):
    """与人一样粘贴：文本放进剪贴板后按 Ctrl+V，文字一次出现在当前焦点处。"""
    set_clipboard(s)
    time.sleep(0.08)
    key(combo, 0.08)


def _utf16_units(ch: str) -> list[int]:
    b = ch.encode('utf-16-le')
    return [int.from_bytes(b[i:i + 2], 'little') for i in range(0, len(b), 2)]


def _window_info(hwnd) -> dict:
    title = ctypes.create_unicode_buffer(512)
    cls = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 512)
    user32.GetClassNameW(hwnd, cls, 256)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {'hwnd': int(hwnd), 'title': title.value, 'class': cls.value, 'pid': pid.value, 'exe': _exe_name(pid.value),
            'rect': [rect.left, rect.top, rect.right, rect.bottom], 'iconic': bool(user32.IsIconic(hwnd))}


_exe_cache: dict = {}


def _exe_name(pid: int) -> str:
    """进程的可执行文件名（小写，如 chrome.exe），查不到时为空。"""
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ''
    h = kernel32.OpenProcess(0x1000, False, pid)
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = buf.value.rsplit('\\', 1)[-1].lower()
        kernel32.CloseHandle(h)
    _exe_cache[pid] = name
    return name


def windows(untitled: bool = False) -> list[dict]:
    """可见的顶层窗口（有标题），按 Z 序从上到下；untitled 为真时也包括没有标题的（如下拉框弹出的列表）。"""
    out = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            info = _window_info(hwnd)
            if info['title'] or untitled:
                out.append(info)
        return True
    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def child_rect(hwnd: int, cls: str) -> list | None:
    """窗口里第一个指定类名的可见子窗口的屏幕矩形（如 Chrome 的网页内容区）。"""
    found = []

    def cb(h, _):
        info = _window_info(h)
        if info['class'] == cls and user32.IsWindowVisible(h):
            r = info['rect']
            if r[2] - r[0] > 50 and r[3] - r[1] > 50:
                found.append(r)
                return False
        return True
    user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(cb), 0)
    return found[0] if found else None


def foreground(hwnd: int, maximize: bool = False):
    """把窗口切到前台：先按一下 Alt 解除前台锁定，必要时挂接输入线程。"""
    h = wintypes.HWND(hwnd)
    if user32.IsIconic(h) or maximize:
        user32.ShowWindow(h, 3 if maximize else 9)
    # 已经在前台就不再按 Alt：单按一下 Alt 会让 Sublime、记事本等程序进入菜单栏选择状态，
    # 接下来的 Ctrl+W、Ctrl+Shift+P 等组合键都被菜单吃掉
    if int(user32.GetForegroundWindow() or 0) == hwnd:
        return True
    _send(_key(0x12), _key(0x12, up=True))
    if not user32.SetForegroundWindow(h):
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(fg, None)
        user32.AttachThreadInput(cur, other, True)
        user32.BringWindowToTop(h)
        user32.SetForegroundWindow(h)
        user32.AttachThreadInput(cur, other, False)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def place(hwnd: int, x: int, y: int, w: int, hgt: int):
    user32.ShowWindow(wintypes.HWND(hwnd), 9)
    user32.SetWindowPos(wintypes.HWND(hwnd), None, x, y, w, hgt, 0x0004 | 0x0010)


def close(hwnd: int):
    user32.PostMessageW(wintypes.HWND(hwnd), 0x0010, 0, 0)


def idle() -> dict:
    """距上次鼠标键盘输入的秒数、是否锁屏（输入桌面不是 Default 即视为锁屏）、屏保是否在运行。"""
    lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(lii))
    secs = (kernel32.GetTickCount() - lii.dwTime) / 1000.0
    desk_name = input_desktop()
    saver = screensaver_running()
    # 屏保运行在单独的 Screen-saver 桌面上，不算锁屏；但屏保设了「恢复时显示登录屏幕」时，退出屏保就是锁屏
    locked = desk_name not in ('default', 'screen-saver') or (desk_name == 'screen-saver' and saver_secure())
    return {'idle_sec': round(secs, 1), 'locked': locked, 'screensaver': saver, 'desktop': desk_name}


def input_desktop() -> str:
    """当前接收输入的桌面名（小写）：default 为正常桌面，screen-saver 为屏保，winlogon 等为锁屏；打不开时为空。"""
    desk = user32.OpenInputDesktop(0, False, 0x0100)
    if not desk:
        return ''
    name = ctypes.create_unicode_buffer(256)
    need = wintypes.DWORD()
    ok = user32.GetUserObjectInformationW(desk, 2, name, ctypes.sizeof(name), ctypes.byref(need))
    user32.CloseDesktop(desk)
    return name.value.lower() if ok else ''


def saver_secure() -> bool:
    """屏保是否设了「在恢复时显示登录屏幕」。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Control Panel\Desktop') as k:
            return str(winreg.QueryValueEx(k, 'ScreenSaverIsSecure')[0]) == '1'
    except OSError:
        return False


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ULONG_PTR), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]


def switch_default() -> bool:
    """把输入桌面切回正常桌面（Default）。屏保结束后系统有时停在 Screen-saver 桌面不切回来。"""
    user32.OpenDesktopW.restype = wintypes.HANDLE
    h = user32.OpenDesktopW('Default', 0, False, 0x0100)
    if not h:
        return False
    ok = bool(user32.SwitchDesktop(wintypes.HANDLE(h)))
    user32.CloseDesktop(wintypes.HANDLE(h))
    return ok


def _processes() -> list[tuple[int, int, str]]:
    """当前进程列表：(pid, 父 pid, 程序名小写)。"""
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    out = []
    entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile.lower()))
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return out


def kill_screensavers() -> int:
    """结束正在运行的屏保（*.scr）连同它拉起的子进程（如 Wallpaper Engine 屏保的 wallpaper64.exe -screensaver），
    返回结束的进程数。只结束 .scr 子进程留在屏保桌面上时，系统不会切回正常桌面。"""
    procs = _processes()
    targets = {pid for pid, _, name in procs if name.endswith('.scr')}
    grew = True
    while grew:
        more = {pid for pid, ppid, _ in procs if ppid in targets and pid not in targets}
        grew = bool(more)
        targets |= more
    killed = 0
    for pid in targets:
        h = kernel32.OpenProcess(0x0001, False, pid)
        if h:
            killed += bool(kernel32.TerminateProcess(h, 0))
            kernel32.CloseHandle(h)
    return killed


def screensaver_running() -> bool:
    running = wintypes.BOOL()
    user32.SystemParametersInfoW(0x0072, 0, ctypes.byref(running), 0)
    return bool(running.value)


def wake(timeout: float = 8.0) -> dict:
    """屏保在运行就退出它：屏保在单独的桌面上，注入的输入送不过去，直接结束屏保进程；
    结束不了再注入几下鼠标移动试试。屏保设了「恢复时显示登录屏幕」时退出后会进入锁屏，由调用方按 locked 处理。"""
    was = screensaver_running()
    killed = kill_screensavers() if was else 0
    end = time.time() + timeout
    while (screensaver_running() or input_desktop() == 'screen-saver') and time.time() < end:
        time.sleep(0.3)
        if time.time() > end - timeout / 2:
            for dx in (40, -40):
                with contextlib.suppress(OSError):
                    _send(_mouse(MOUSEEVENTF_MOVE, dx, 0))
                time.sleep(0.05)
    time.sleep(0.5 if was else 0)
    st = idle()
    return {'was_running': was, 'killed': killed, 'running': st['screensaver'], 'locked': st['locked'],
            'desktop': st['desktop']}


def awake(on: bool = True):
    """录屏期间保持屏幕常亮、不睡眠。"""
    kernel32.SetThreadExecutionState(0x80000000 | (0x00000003 if on else 0))


# 勿扰：系统的 QuietHoursSettings（设置里「勿扰」与专注助手用的同一个接口），切到只允许闹钟的配置
QUIET_CLSID = 'f53321fa-34f8-4b7f-b9a3-361877cb94cf'
QUIET_IID = '6bff4732-81ec-4ffb-ae67-b6c1bc29631f'
QUIET_ON = 'Microsoft.QuietHoursProfile.AlarmsOnly'
# 开勿扰前的配置记在文件里：助手意外退出没来得及恢复时，下次启动先恢复
QUIET_SAVE = Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'feedback-record' / 'quiet-restore.json'


class _GUID(ctypes.Structure):
    _fields_ = [('b', ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, s: str):
        g = cls()
        ctypes.memmove(g.b, uuid.UUID(s).bytes_le, 16)
        return g


def _quiet_profile(new: str | None = None) -> str:
    """读出当前的勿扰配置名；给了 new 就先改成 new 再读。"""
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx(None, 2)
    obj = ctypes.c_void_p()
    # CLSCTX_LOCAL_SERVER：由系统的通知服务实现
    hr = ole32.CoCreateInstance(ctypes.byref(_GUID.of(QUIET_CLSID)), None, 4,
                                ctypes.byref(_GUID.of(QUIET_IID)), ctypes.byref(obj))
    if hr:
        raise OSError(f'CoCreateInstance 0x{hr & 0xFFFFFFFF:08x}')
    vtbl = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    get = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))(vtbl[3])
    put = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p)(vtbl[4])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    try:
        if new:
            hr = put(obj, new)
            if hr:
                raise OSError(f'设置勿扰失败 0x{hr & 0xFFFFFFFF:08x}')
        s = ctypes.c_wchar_p()
        hr = get(obj, ctypes.byref(s))
        if hr:
            raise OSError(f'读取勿扰失败 0x{hr & 0xFFFFFFFF:08x}')
        return s.value or ''
    finally:
        release(obj)


def _set_quiet(profile: str) -> str:
    """改勿扰配置并确认生效：通知服务异步应用，两次修改挨得太近时后一次可能被吞掉，读回来不对就重设。"""
    now = ''
    for _ in range(5):
        _quiet_profile(profile)
        time.sleep(0.3)
        now = _quiet_profile()
        if now == profile:
            break
    return now


def quiet(on: bool) -> dict:
    """录屏期间开勿扰（其他程序的通知不弹出，进通知中心），录完恢复成原来的配置。
    原来已经是只允许闹钟就不动，也不恢复；原来是「仅优先通知」的也切过来，优先名单里的程序同样会弹。"""
    if on:
        cur = _quiet_profile()
        if cur == QUIET_ON:
            return {'was': cur, 'now': cur}
        QUIET_SAVE.parent.mkdir(parents=True, exist_ok=True)
        QUIET_SAVE.write_text(json.dumps({'profile': cur}), encoding='utf-8')
        return {'was': cur, 'now': _set_quiet(QUIET_ON)}
    return quiet_restore()


def quiet_restore() -> dict:
    """恢复开勿扰前的配置（没有记录就什么都不做）。"""
    try:
        prev = json.loads(QUIET_SAVE.read_text(encoding='utf-8')).get('profile')
    except (OSError, ValueError):
        return {}
    now = _set_quiet(prev) if prev else ''
    QUIET_SAVE.unlink(missing_ok=True)
    return {'now': now}


class Watcher(threading.Thread):
    """低级鼠标键盘钩子：出现不带注入标记的事件（真人操作）时输出 user-input 事件。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.reported = 0.0

    def _report(self, kind: str):
        now = time.time()
        if now - self.reported > 1:
            self.reported = now
            emit({'event': 'user-input', 'kind': kind})

    def run(self):
        def mouse_cb(code, wparam, lparam):
            if code >= 0:
                s = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not (s.flags & LLMHF_INJECTED) and s.dwExtraInfo != MARK:
                    self._report('mouse')
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def kbd_cb(code, wparam, lparam):
            if code >= 0:
                s = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (s.flags & LLKHF_INJECTED) and s.dwExtraInfo != MARK:
                    self._report('keyboard')
            return user32.CallNextHookEx(None, code, wparam, lparam)

        # 回调对象要一直持有，否则被回收后钩子会崩溃
        self._procs = (HOOKPROC(mouse_cb), HOOKPROC(kbd_cb))
        user32.SetWindowsHookExW(14, self._procs[0], None, 0)
        user32.SetWindowsHookExW(13, self._procs[1], None, 0)
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def handle(req: dict):
    op = req.get('op')
    if op == 'path':
        move_path(req['pts'], float(req.get('dt', 0.01)))
    elif op == 'button':
        button(req.get('btn', 'left'), bool(req['down']))
    elif op == 'wheel':
        wheel(int(req['notches']))
    elif op == 'key':
        key(req['combo'], float(req.get('hold', 0.06)))
    elif op == 'text':
        text(req['text'], req.get('delays'))
    elif op == 'keydown':
        key_down(req['combo'])
    elif op == 'keyup':
        key_up(req['combo'])
    elif op == 'release':
        release_all()
    elif op == 'paste':
        paste(req['text'], req.get('combo', 'ctrl+v'))
    elif op == 'windows':
        return {'windows': windows(bool(req.get('untitled')))}
    elif op == 'quiet':
        return quiet(bool(req.get('on', True)))
    elif op == 'child_rect':
        return {'rect': child_rect(int(req['hwnd']), req['class'])}
    elif op == 'foreground':
        return {'front': foreground(int(req['hwnd']), bool(req.get('maximize')))}
    elif op == 'place':
        place(int(req['hwnd']), *[int(v) for v in req['rect']])
    elif op == 'close':
        close(int(req['hwnd']))
    elif op == 'screen':
        return screen()
    elif op == 'idle':
        return idle()
    elif op == 'wake':
        return wake(float(req.get('timeout', 8)))
    elif op == 'awake':
        awake(bool(req.get('on', True)))
    elif op == 'front':
        return {'hwnd': int(user32.GetForegroundWindow() or 0)}
    elif op == 'cursor':
        p = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(p))
        return {'x': p.x, 'y': p.y}
    elif op == 'watch':
        Watcher().start()
    else:
        raise ValueError(f'未知操作：{op}')
    return {}


def serve():
    # 上次录屏的助手意外退出、勿扰没恢复的，先恢复
    with contextlib.suppress(Exception):
        quiet_restore()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            req = json.loads(line)
            try:
                emit({'id': req.get('id'), 'ok': True, **(handle(req) or {})})
            except Exception as e:  # noqa: BLE001 - 出错原样回给执行器
                emit({'id': req.get('id'), 'ok': False, 'error': f'{type(e).__name__}: {e}'})
    finally:
        # 执行器退出（标准输入关闭）时兜底：松开还按着的键，恢复勿扰与屏幕常亮
        with contextlib.suppress(Exception):
            release_all()
        with contextlib.suppress(Exception):
            quiet_restore()
        awake(False)


def main():
    # 按物理像素处理坐标（缩放不是 100% 时也对得上）
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    winmm.timeBeginPeriod(1)
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    if cmd == 'serve':
        serve()
    elif cmd == 'idle':
        emit(idle())
    elif cmd == 'screen':
        emit(screen())
    elif cmd == 'windows':
        emit({'windows': windows()})
    elif cmd == 'quiet':
        # 查看当前勿扰配置；quiet-restore 恢复录屏前的配置
        emit({'profile': _quiet_profile()})
    elif cmd == 'quiet-restore':
        emit(quiet_restore())
    else:
        raise SystemExit(f'未知命令：{cmd}')


if __name__ == '__main__':
    main()
