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


def wsl_path(p: str) -> str:
    """Windows 盘符路径转换为 WSL 挂载路径，如 C:\\Users\\x → /mnt/c/Users/x；其他路径原样返回。"""
    s = str(p).strip()
    if len(s) >= 2 and s[1] == ':' and s[0].isalpha():
        rest = s[2:].replace('\\', '/').strip('/')
        return f'/mnt/{s[0].lower()}' + (f'/{rest}' if rest else '')
    return s


def unc(path) -> str:
    """WSL 路径转换为 Windows 侧可读的 UNC 路径，如 \\\\wsl.localhost\\Ubuntu-24.04\\home\\...；挂载盘路径转为盘符路径。"""
    s = str(path)
    if s.startswith('/mnt/'):
        return win_path(s)
    return f"\\\\wsl.localhost\\{load()['wsl_distro']}" + s.replace('/', '\\')


def win_parent(path: str) -> str:
    """Windows 路径的父目录，带结尾反斜杠，方便在文件选择框里直接定位。"""
    s = str(path or '').rstrip('\\')
    return s.rsplit('\\', 1)[0] + '\\' if '\\' in s else ''


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
    # 沿用 Windows codex 的 config.toml 中的供应商与模型（CC Switch 或第三方 API 时打开）；
    # 只读取 model、model_provider 和对应的 [model_providers.<名称>]，MCP、插件等仍不加载
    'codex_follow_toml': False,
    # config.toml 的 WSL 路径，留空时按 Windows 的 CODEX_HOME 或 %USERPROFILE%\.codex 自动定位
    'codex_config_toml': '',
    # codex 连续报告重连（Reconnecting...）达到该次数即判定连不上供应商并结束运行，0 表示不限制
    'codex_max_reconnects': 10,
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
    # 录屏软件的输出目录（WSL 路径），完成录屏时自动取其中最新的 mp4
    'recording_dir': '/mnt/e/GSB-Recordings',
    # ---------- 自动出题与自动做题 ----------
    # 补充题目池每轮出题数量
    'pool_gen_count': 10,
    # 自动做题时，验收、查验、录屏阶段积压的自动题达到该数量就暂不取新题
    'auto_backlog_max': 2,
    # 等待 Claude Code 启动到可输入状态的上限（秒）
    'do_ready_timeout_sec': 180,
    # 单题做题的超时（分钟），超时后关闭会话并判为失败
    'do_timeout_min': 240,
    # 轨迹记录到本轮结束后，会话文件再静止这么多秒才算做完
    'do_idle_sec': 30,
    # 自动录屏的时长上限（秒）
    'record_max_sec': 90,
    # 自动录屏是否录制声音（需要 WSL 中安装 pulseaudio）
    'record_audio': True,
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
