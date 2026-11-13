# -*- coding: utf-8 -*-
"""配置读写：默认值 + data/config.json 覆盖。"""
import json
import os
import threading
from pathlib import Path

# 运行数据根目录（WSL 原生文件系统）
DATA_DIR = Path(os.environ.get('PIPELINE_DATA', '~/jianzhi/feedback-pipeline/data')).expanduser()
APP_DIR = Path(__file__).resolve().parent
CONFIG_FILE = DATA_DIR / 'config.json'
# Codex 工作区随仓库一起放在 agent/ 子目录
AGENT_DIR = APP_DIR.parent / 'agent'


def win_path(p) -> str:
    """WSL 挂载路径转换为 Windows 路径，如 /mnt/e/a/b.mp4 → E:\\a\\b.mp4；其他路径原样返回。"""
    parts = Path(p).parts
    if len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1:
        return parts[2].upper() + ':\\' + '\\'.join(parts[3:])
    return str(p)


# Claude Code 2.1.322 自带的 skill（来自干净做题轨迹的 skill_listing）
BUILTIN_SKILLS = [
    'dataviz', 'update-config', 'keybindings-help', 'code-review', 'simplify',
    'fewer-permission-prompts', 'loop', 'claude-api', 'workflow-authoring', 'run',
    'plugin-authoring', 'init', 'security-review',
    # 做题环境无用户 skill、无插件时，2.1.322 的 skill_listing 里同样出现这两项，属于自带 skill
    'artifact-design', 'artifact-diagramming',
]

DEFAULTS = {
    # GitHub 仓库（推送使用 WSL 中已配置的 ssh 凭据）
    'github_repo': 'git@github.com:Wenenenenen/feedback-common.git',
    # Windows 版 codex 可执行文件（WSL 路径）
    'codex_path': '/mnt/e/nodejs/npm-global/node_modules/@openai/codex/node_modules/'
                  '@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe',
    # Codex 工作区（Windows 路径与 WSL 路径），默认为仓库内的 agent/
    'agent_workspace_win': win_path(AGENT_DIR),
    'agent_workspace_wsl': str(AGENT_DIR),
    'model': 'gpt-6.1-sol',
    'reasoning_effort': 'xhigh',
    'wsl_distro': 'Ubuntu-24.04',
    'concurrency': 2,
    'run_timeout_min': 60,
    'port_base': 20000,
    'port_span': 100,
    'tasks_root': '~/jianzhi/feedback-tasks',
    'verify_root': '~/jianzhi/feedback-verify',
    'record_root': '~/jianzhi/feedback-record',
    'claude_home': '~/.claude',
    'builtin_skills': BUILTIN_SKILLS,
    # 提交表里已有的最大序号，新完成的题目从它后面接续编号
    'seq_base': 10,
    # 录屏软件的输出目录（WSL 路径），完成录屏时自动取其中最新的 mp4
    'recording_dir': '/mnt/e/GSB-Recordings',
}

_lock = threading.Lock()


def load() -> dict:
    """读取配置，缺省项用默认值补齐。"""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            pass
    return cfg


def save(updates: dict) -> dict:
    """只保存已知配置项，返回合并后的配置。"""
    with _lock:
        # 只落盘用户改过的项，未改的项继续跟随代码默认值（如内置 skill 清单随版本更新）
        saved = {}
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                saved = {}
        for k, v in updates.items():
            if k in DEFAULTS:
                saved[k] = v
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        return load()


def path_of(key: str) -> Path:
    """把配置中的目录项展开为绝对路径。"""
    return Path(load()[key]).expanduser()


def github_web(repo: str) -> str:
    """把 git 远端地址转换为网页地址，用于拼 commit permalink。"""
    r = repo.strip()
    if r.startswith('git@github.com:'):
        r = 'https://github.com/' + r[len('git@github.com:'):]
    elif r.startswith('ssh://git@github.com/'):
        r = 'https://github.com/' + r[len('ssh://git@github.com/'):]
    if r.endswith('.git'):
        r = r[:-4]
    return r.rstrip('/')


def permalink(sha: str | None) -> str:
    """生成 40 位 SHA 的 commit permalink。"""
    if not sha:
        return ''
    return f"{github_web(load()['github_repo'])}/commit/{sha}"
