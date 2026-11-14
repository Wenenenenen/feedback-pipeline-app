# -*- coding: utf-8 -*-
"""环境检查与素材脚本的子进程调用。"""
import subprocess

from app import envcheck, materials


def _capture_run(monkeypatch, module):
    """记录 subprocess.run 的参数，返回一个成功结果。"""
    calls = []

    def fake_run(args, **kw):
        calls.append(kw)
        return subprocess.CompletedProcess(args, 0, 'Python 3.12.3', '')
    monkeypatch.setattr(module.subprocess, 'run', fake_run)
    return calls


def test_interactive_bash_runs_in_new_session(monkeypatch, tmp_path):
    """bash -ic 必须在新会话里执行，否则会抢占后端的控制终端，后续调用向后端发 SIGTTIN 把它停住。"""
    calls = _capture_run(monkeypatch, envcheck)
    envcheck.toolchain_check([{'name': 'python', 'check_command': 'python3 --version'}])
    assert calls and all(c.get('start_new_session') for c in calls)

    calls = _capture_run(monkeypatch, materials)
    (tmp_path / 'a.txt').write_text('ok', encoding='utf-8')
    materials.run_script(tmp_path, {'path': 'a.txt', 'script': 'true'}, lambda m: None)
    assert calls and all(c.get('start_new_session') for c in calls)


def test_toolchain_check_resolves_relative_paths_in_task_dir(tmp_path):
    """检查命令里的相对路径按做题目录解析。"""
    tool = tmp_path / '.venv' / 'bin' / 'tool'
    tool.parent.mkdir(parents=True)
    tool.write_text('#!/bin/sh\necho 1.26.4\n', encoding='utf-8')
    tool.chmod(0o755)
    item = {'name': 'numpy', 'check_command': '.venv/bin/tool', 'min_version': '1.26'}
    assert envcheck.toolchain_check([item], cwd=str(tmp_path))[0]['ok']
    assert not envcheck.toolchain_check([item])[0]['ok']
