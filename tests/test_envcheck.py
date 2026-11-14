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


def test_toolchain_check_runs_in_task_dir(tmp_path):
    """系统工具的检查命令在做题目录执行。"""
    item = {'name': 'pwd', 'check_command': 'pwd'}
    assert envcheck.toolchain_check([item], cwd=str(tmp_path))[0]['detail'] == str(tmp_path)


def test_project_dependencies_are_not_prechecked(monkeypatch):
    """项目依赖由做题的模型自己安装：直接算通过，不执行检查命令。"""
    calls = _capture_run(monkeypatch, envcheck)
    tc = [{'name': 'numpy', 'check_command': '.venv/bin/python -c "import numpy; print(numpy.__version__)"',
           'min_version': '1.26'},
          {'name': 'vite', 'check_command': 'node_modules/.bin/vite --version', 'min_version': '5'},
          {'name': 'cli', 'check_command': './bin/cli --version', 'min_version': ''}]
    items = envcheck.toolchain_check(tc)
    assert all(i['ok'] for i in items) and not calls
    for cmd in ('python3 --version', 'ffmpeg -version', 'go version', 'node -v'):
        assert not envcheck.is_project_local(cmd)
