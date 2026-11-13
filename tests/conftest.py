# -*- coding: utf-8 -*-
"""测试公共夹具：每个用例使用独立的数据目录与数据库。"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config, db  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / 'data'
    d.mkdir()
    monkeypatch.setattr(config, 'DATA_DIR', d)
    monkeypatch.setattr(config, 'CONFIG_FILE', d / 'config.json')
    db.reset_for_test()
    yield d
    db.reset_for_test()


@pytest.fixture()
def git_identity(monkeypatch, tmp_path):
    """隔离 git 全局配置，避免依赖本机身份。"""
    home = tmp_path / 'gitcfg'
    home.mkdir()
    cfg = home / '.gitconfig'
    cfg.write_text('[user]\n\tname = tester\n\temail = tester@example.invalid\n'
                   '[init]\n\tdefaultBranch = main\n', encoding='utf-8')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(cfg))
    return cfg


def write_jsonl(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n', encoding='utf-8')


def run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
