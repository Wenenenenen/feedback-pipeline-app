# -*- coding: utf-8 -*-
"""环境检查：做题客户端体检、工具链版本、外网连通性。只记录键名与布尔值，不输出凭据。"""
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

REQUIRED_ENV = {
    'CLAUDE_CODE_MAX_CONTEXT_TOKENS': lambda v: str(v) == '1000000',
    'DISABLE_COMPACT': lambda v: str(v) == '1',
    'ANTHROPIC_BETAS': lambda v: 'context-1m-2025-08-07' in str(v),
}


def _item(name: str, ok: bool, detail: str, fix: str = '') -> dict:
    return {'name': name, 'ok': ok, 'detail': detail, 'fix': fix}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def client_check(claude_home: Path, task_dir: str) -> dict:
    """检查 WSL 的 ~/.claude 是否干净、1M 上下文配置是否齐全。"""
    items = []
    items.append(_item('全局 CLAUDE.md', not (claude_home / 'CLAUDE.md').exists(),
                       '存在' if (claude_home / 'CLAUDE.md').exists() else '不存在',
                       '删除或移走 ~/.claude/CLAUDE.md'))
    for sub, label in (('rules', '全局 rules'), ('skills', '用户 skills'), ('agents', '用户子代理'),
                       ('commands', '用户命令')):
        d = claude_home / sub
        n = len([x for x in d.iterdir()]) if d.is_dir() else 0
        items.append(_item(label, n == 0, f'{n} 项', f'清空 ~/.claude/{sub}'))
    settings = _read_json(claude_home / 'settings.json')
    env = settings.get('env') or {}
    for key, rule in REQUIRED_ENV.items():
        ok = key in env and rule(env[key])
        items.append(_item(f'env {key}', ok, '已设置' if key in env else '未设置',
                           '按 vendor 配置脚本恢复'))
    plugins = [k for k, v in (settings.get('enabledPlugins') or {}).items() if v]
    items.append(_item('已启用插件', not plugins, ', '.join(plugins) or '无', '在 settings.json 关闭插件'))
    hooks = settings.get('hooks') or {}
    items.append(_item('hooks', not hooks, ', '.join(hooks) or '无', '移除 settings.json 中的 hooks'))
    user_json = _read_json(claude_home.parent / '.claude.json')
    mcp = list((user_json.get('mcpServers') or {}).keys())
    proj = (user_json.get('projects') or {}).get(task_dir) or {}
    mcp += list((proj.get('mcpServers') or {}).keys())
    mcp += list((settings.get('mcpServers') or {}).keys())
    items.append(_item('MCP 服务器', not mcp, ', '.join(mcp) or '无', 'claude mcp remove <名称>'))
    effort = settings.get('effortLevel')
    items.append(_item('推理强度', effort in (None, 'high'),
                       f'settings 中为 {effort}' if effort else 'settings 未设置，收集时按轨迹校验 high',
                       '把 effortLevel 设为 high'))
    model = env.get('ANTHROPIC_MODEL') or settings.get('model') or ''
    return {'ok': all(i['ok'] for i in items), 'items': items, 'expected_model': model}


def _bash(cmd: str, timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    """用交互式 bash 执行，以便加载 nvm 等环境。"""
    try:
        # 新会话执行，不继承后端的控制终端：交互式 bash 会抢占终端前台进程组，
        # 单条命令时 bash 直接 exec 不归还，下一个 bash -ic 就向整个进程组发 SIGTTIN 把后端停住
        p = subprocess.run(['bash', '-ic', cmd], capture_output=True, text=True, timeout=timeout,
                           cwd=cwd, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL,
                           start_new_session=True)
    except subprocess.TimeoutExpired:
        return -1, '超时'
    noise = ('no job control', 'cannot set terminal process group')
    err = '\n'.join(l for l in p.stderr.splitlines() if not any(n in l for n in noise))
    return p.returncode, (p.stdout + ('\n' + err if err else '')).strip()


def _version_tuple(text: str) -> tuple:
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', text or '')
    return tuple(int(x or 0) for x in m.groups()) if m else ()


# 检查命令指向项目内的虚拟环境、node_modules 或相对路径，说明是项目依赖而不是系统工具
_PROJECT_LOCAL = re.compile(r'(^|[\s"\'=(])(\.{1,2}/|\.?venv/|node_modules/)')


def is_project_local(cmd: str) -> bool:
    """项目依赖（pip、npm 包等）由做题的模型在做题目录内自己安装，准备环境时不预检、不预装。"""
    return bool(_PROJECT_LOCAL.search(cmd or ''))


def toolchain_check(toolchain: list[dict], cwd: str | None = None) -> list[dict]:
    """逐项执行检查命令，比较最低版本。cwd 为做题目录，检查命令里的相对路径（如 .venv/bin/python）按它解析。"""
    items = []
    for t in toolchain or []:
        cmd = t.get('check_command') or f"{t.get('name')} --version"
        if is_project_local(cmd):
            items.append(_item(t.get('name', ''), True, '项目内依赖 做题时由模型安装 不预检'))
            continue
        code, out = _bash(cmd, cwd=cwd)
        first = out.splitlines()[0] if out else ''
        ok = code == 0
        need = t.get('min_version') or ''
        if ok and need:
            have, want = _version_tuple(first), _version_tuple(need)
            ok = bool(have) and (not want or have >= want)
        items.append(_item(t.get('name', ''), ok, first[:200] or '命令不可用',
                           t.get('install_hint') or f"安装 {t.get('name')} {need}".strip()))
    return items


def gpu_items(env_req: dict) -> list[dict]:
    """显卡能力需人工确认，这里只生成待确认项。"""
    if not env_req.get('needs_gpu'):
        return []
    code, out = _bash('glxinfo -B 2>/dev/null | grep -i "opengl core profile version"', 20)
    hint = out if code == 0 and out else '未取到 OpenGL 信息（可安装 mesa-utils 后执行 glxinfo -B）'
    return [{'name': '显卡能力', 'ok': False, 'manual': True,
             'detail': f"{env_req.get('gpu_note', '')} 本机 {hint}",
             'fix': 'WebGPU 题在 Windows Chrome 打开 chrome://gpu 确认；OpenGL 题确认版本满足；确认后点「确认显卡能力并继续」'}]


def net_check(urls: list[str], timeout: int = 20) -> list[dict]:
    items = []
    for url in urls or []:
        t0 = time.time()
        status, note = None, ''
        try:
            req = urllib.request.Request(url, method='GET', headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status, note = e.code, str(e.reason)
        except Exception as e:  # noqa: BLE001 网络错误统一记录
            note = type(e).__name__ + ' ' + str(e)[:120]
        cost = round(time.time() - t0, 1)
        ok = status is not None and 200 <= status < 400
        items.append({'name': url, 'ok': ok, 'detail': f'{status or "失败"}（{cost}s）',
                      'fix': note})
    return items
