"""录屏执行器（record_harness）的静态检查：语法正确，新增的多选上传与控制台操作在执行器和 qa-record 说明里一致。"""
import glob
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
    # 虚拟屏后端声明用 filechooser 接管多选，本机桌面后端在对话框里输入带引号的文件名列表
    assert "b.multiUploadVia = 'filechooser'" in (HARNESS / 'backend-x11.mjs').read_text(encoding='utf-8')
    win = (HARNESS / 'backend-win.mjs').read_text(encoding='utf-8')
    assert 'Array.isArray(absOrList)' in win and "combo: 'alt+n'" in win


def test_console_action_documented():
    h = (HARNESS / 'harness.mjs').read_text(encoding='utf-8')
    assert 'async console(expr' in h and "b.key('ctrl+shift+j')" in h
    s = SKILL.read_text(encoding='utf-8')
    assert 'ctx.console(' in s
    assert '不要用 `ctx.page.evaluate`' in s
    assert '多选不是人工录屏的理由' in s
