"""录屏执行器（record_harness）的静态检查：语法正确，新增的多选上传与控制台操作在执行器和 qa-record 说明里一致。"""
import glob
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'app' / 'record_harness'
SKILL = ROOT / 'agent' / '.agents' / 'skills' / 'qa-record' / 'SKILL.md'


def _node() -> str | None:
    # 非交互 shell 里 nvm 的 node 不在 PATH 上，退回到 nvm 目录里找
    found = shutil.which('node')
    if found:
        return found
    cands = sorted(glob.glob(os.path.expanduser('~/.nvm/versions/node/*/bin/node')))
    return cands[-1] if cands else None


@pytest.mark.parametrize('name', ['harness.mjs', 'backend-win.mjs', 'backend-x11.mjs', 'human.mjs'])
def test_harness_syntax(name):
    node = _node()
    if not node:
        pytest.skip('没有 node')
    r = subprocess.run([node, '--check', str(HARNESS / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_multi_upload_supported_by_both_backends():
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    # 不再限制一次只能选一个文件，多选时文件名不能重复
    assert '一次只能选一个文件' not in h
    assert '多选的文件名不能重复' in h
    # 虚拟屏后端声明用 filechooser 接管多选；本机桌面后端在文件列表里按住 Ctrl 逐个点或按住 Shift 连选，
    # 列表里找不到时才在文件名框里输入带引号的文件名列表
    assert "b.multiUploadVia = 'filechooser'" in (HARNESS / 'backend-x11.mjs').read_text(encoding='utf-8')
    win = (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert 'uiaFindMany(dlg.hwnd, names)' in win and "contiguous ? 'shift' : 'ctrl'" in win
    assert "helper.req('keyup', { combo: mod })" in win and "combo: 'alt+n'" in win
    # 对话框弹出后指针不再先移到对话框正中间
    choose = win[win.index('b.chooseFile ='):win.index('b.download =')]
    assert 'dlg.rect' not in choose
    ps = (HARNESS / 'uia_find.ps1').read_text(encoding='utf-8-sig')
    assert '[string]$Names' in ps and "$Kind -eq 'menu'" in ps


def test_download_and_open_file():
    """下载由 Chrome 按原文件名存进暂存目录（下载气泡显示格式图标）；下载的文件用系统看图或记事本打开，地址栏长网址粘贴。"""
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    assert 'async download(t' in h and 'async openFile(file' in h and 'closeFile: () => b.closeFile()' in h
    assert 'if (b.viewerOpen?.()) await b.closeFile();' in h
    goto = h[h.index('async goto(url'):h.index('async select(t')]
    assert 'b.pasteText(String(url))' in goto
    win = (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert "behavior: 'allow', downloadPath: staging" in win and 'await ownDownloads();' in win
    assert "['打开方式', 'Open with']" in win and "['记事本', 'Notepad']" in win and '/select,' in win
    # 文本与图片都双击用默认程序打开；原来就开着的编辑器窗口（新开标签页）不按 Alt+F4 整个关掉
    assert "kind === 'default'" in win and "w.reused ? 'ctrl+w' : 'alt+f4'" in win
    assert "if (w.reused && !String(now.title || '').includes(w.name)) continue;" in win
    # 暂存目录里已有相同内容的文件不重写，免得打乱对话框里按日期排的顺序
    assert 'fs.readFileSync(dst).equals(data)' in win
    ps = (HARNESS / 'uia_find.ps1').read_text(encoding='utf-8-sig')
    # 经典右键菜单在 UI 自动化里读不到菜单项，改用 Win32 菜单接口；去掉助记符后完全相等才算匹配
    assert '0x01E1' in ps and 'GetMenuItemRect' in ps and '$n -eq $a' in ps and 'StartsWith' not in ps
    x11 = (HARNESS / 'backend-x11.mjs').read_text(encoding='utf-8')
    assert 'b.download = async' in x11 and 'b.openFile = async' in x11 and 'b.closeFile = async' in x11
    assert "behavior: 'allow', downloadPath: dlDir" in x11 and "page.waitForEvent('download'" not in x11
    s = SKILL.read_text(encoding='utf-8')
    assert 'ctx.download(' in s and 'ctx.openFile(' in s and "waitForEvent('download')" in s


def test_console_action_documented():
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    assert 'async console(expr' in h and "b.key('ctrl+shift+j')" in h
    s = SKILL.read_text(encoding='utf-8')
    assert 'ctx.console(' in s
    assert '不要用 `ctx.page.evaluate`' in s
    assert '多选不是人工录屏的理由' in s


def test_number_input_click_avoids_spinner():
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    # 数字输入框的落点偏左，不落到右侧增减箭头上
    assert "el.type === 'number'" in h and 'jx = -box.width * 0.15' in h


def test_tail_pad():
    node = _node()
    if not node:
        pytest.skip('没有 node')
    url = (HARNESS / 'muxutil.mjs').as_uri()
    js = (f"import {{ tailPad }} from '{url}';"
          "console.log(JSON.stringify([tailPad(1000.0, 960.0, 36.3), tailPad(1000.0, 960.0, 39.95),"
          " tailPad(null, 960.0, 36.3), tailPad(1000.0, 900.0, 36.3), tailPad(1000.0, 970.0, 36.3)]));")
    r = subprocess.run([node, '--input-type=module', '-e', js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 正常补 3.7 秒；不到 0.1 秒、没有停止时刻、超过 30 秒、停止早于画面结束都不补
    assert r.stdout.strip() == '[3.7,0,0,0,0]'


def test_desktop_capture_marks_wallclock_and_mux_pads():
    win = (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert 'wallclock: true' in win
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    assert 'tpad=stop_mode=clone:stop_duration=' in h and 'stopWall = Date.now() / 1000' in h


def test_typing_slowed_down():
    node = _node()
    if not node:
        pytest.skip('没有 node')
    url = (HARNESS / 'human.mjs').as_uri()
    # 不含空格的 2000 个字：放慢前平均停顿约为基准的 0.98 倍（快慢字混合加偶尔的停顿），放慢后约 147 毫秒
    js = (f"import {{ typingDelays, TYPING_SLOWDOWN }} from '{url}';"
          "const d = typingDelays('a'.repeat(2000), 100);"
          "console.log(JSON.stringify([TYPING_SLOWDOWN, d.reduce((s, x) => s + x, 0) / d.length]));")
    r = subprocess.run([node, '--input-type=module', '-e', js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    slow, mean = json.loads(r.stdout)
    assert slow == 1.5
    assert 130 < mean < 165, mean


def test_long_text_pastes():
    node = _node()
    if not node:
        pytest.skip('没有 node')
    url = (HARNESS / 'human.mjs').as_uri()
    js = (f"import {{ isLongText, PASTE_OVER }} from '{url}';"
          "console.log(JSON.stringify([PASTE_OVER, isLongText('字'.repeat(200)), isLongText('字'.repeat(201))]));")
    r = subprocess.run([node, '--input-type=module', '-e', js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # 超过 200 字才粘贴，按字数（不是字节数）算
    assert json.loads(r.stdout) == [200, False, True]
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    assert 'b.pasteInPage(String(text))' in h and 'b.pasteText(String(text))' in h
    assert "helper.req('paste'" in (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert 'SetClipboardData(CF_UNICODETEXT' in (HARNESS / 'wininput.py').read_text(encoding='utf-8')


def test_drag_and_hold():
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    # 拖动：按下、移动、在 finally 里松开；按住：keyDown 与 keyUp 成对，等待函数超时报错
    assert 'async drag(from, to' in h and 'await b.button(btn, true)' in h and 'await b.button(btn, false)' in h
    assert 'async hold(combo, msOrUntil' in h and 'async keyDown(combo)' in h and 'async keyUp(combo)' in h
    # 收尾统一松开还按着的键与鼠标键，在关闭窗口之前
    i, j = h.index('await releaseHeld()'), h.index('await b.close();\n  }')
    assert i < j
    win = (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert "helper.req('keydown'" in win and "helper.req('keyup'" in win
    x11 = (HARNESS / 'backend-x11.mjs').read_text(encoding='utf-8')
    assert "xdo('keydown'" in x11 and "xdo('keyup'" in x11
    helper = (HARNESS / 'wininput.py').read_text(encoding='utf-8')
    # Windows 助手：按住与松开、退出时兜底松开，按键带扫描码（浏览器的 KeyboardEvent.code 靠它）
    assert "op == 'keydown'" in helper and "op == 'keyup'" in helper and 'release_all()' in helper
    assert 'MapVirtualKeyW(vk, 0)' in helper
    # 按住时与真键盘一样自动重复（系统的重复延迟与速度），松开或按别的键时停止
    assert 'def repeat_timing' in helper and '_start_repeat(vks[-1])' in helper and '_stop_repeat(vk)' in helper
    s = SKILL.read_text(encoding='utf-8')
    assert 'ctx.drag(' in s and 'ctx.hold(' in s and 'ctx.keyDown(' in s
    assert '不要在脚本里直接调 `xdotool`' in s and '不是人工录屏的理由' in s and '自动重复' in s


def test_x11_file_dialog_path_follows_last_dir():
    """虚拟屏的 GTK 文件框会打开在上次选文件的目录：同目录只打文件名，录屏目录下打相对路径，其余打完整路径。"""
    x11 = (HARNESS / 'backend-x11.mjs').read_text(encoding='utf-8')
    assert 'let lastDir = args.cwd;' in x11
    assert 'dir === lastDir ? path.posix.basename(abs)' in x11
    assert 'abs.startsWith(lastDir + \'/\') ? abs.slice(lastDir.length + 1) : abs' in x11
    i, j = x11.index('const rel = dir === lastDir'), x11.index('lastDir = dir;')
    assert i < j
