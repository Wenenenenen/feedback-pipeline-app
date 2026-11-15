# -*- coding: utf-8 -*-
"""Claude Code 轨迹 jsonl：定位、解析、合规校验、摘要生成。"""
import collections
import json
import re
from pathlib import Path

CONTINUE_WORDS = {'继续', '继续吧', '请继续', '接着做', 'continue', 'go on'}
SUBAGENT_TOOLS = {'Agent', 'Task'}
FILE_TOOLS = {'Edit', 'Write', 'NotebookEdit', 'MultiEdit'}


def encode_project_dir(cwd: str) -> str:
    """Claude Code 的项目目录编码：非字母数字一律替换为 '-'。"""
    return re.sub(r'[^A-Za-z0-9]', '-', cwd)


def load_rows(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').replace('\r\n', '\n')).strip()


def _content_text(content) -> str | None:
    """用户消息内容为纯文本时返回文本，否则返回 None（工具结果、图片等）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
            isinstance(x, dict) and x.get('type') == 'text' for x in content):
        return '\n'.join(x.get('text', '') for x in content)
    return None


def human_prompts(rows: list[dict]) -> list[dict]:
    """人工输入的消息：排除元消息、子代理、工具结果、斜杠命令输出。"""
    out = []
    for r in rows:
        if r.get('type') != 'user' or r.get('isMeta') or r.get('isSidechain'):
            continue
        if r.get('toolUseResult') is not None or r.get('sourceToolAssistantUUID'):
            continue
        if r.get('turnOrigin') not in (None, 'human'):
            continue
        text = _content_text((r.get('message') or {}).get('content'))
        if text is None:
            continue
        if text.lstrip().startswith(('<command-name>', '<local-command', '<command-message>')):
            continue
        out.append({'prompt_id': r.get('promptId'), 'uuid': r.get('uuid'),
                    'timestamp': r.get('timestamp'), 'text': text})
    return out


def parse(path: Path) -> dict:
    rows = load_rows(path)
    info = {
        'path': str(path), 'session_id': None, 'version': None, 'cwd': None,
        'models': collections.Counter(), 'requested_models': collections.Counter(),
        'efforts': collections.Counter(), 'tools': collections.Counter(), 'skills': [],
        # Skill 工具调用的 skill 名，按调用顺序记录，缺参数时记为空串
        'skill_calls': [],
        'prompts': human_prompts(rows), 'final_text': '', 'last_timestamp': None,
    }
    for r in rows:
        if not info['session_id'] and r.get('sessionId'):
            info['session_id'] = r['sessionId']
        if not info['version'] and r.get('version'):
            info['version'] = r['version']
            info['cwd'] = r.get('cwd')
        if r.get('timestamp'):
            info['last_timestamp'] = r['timestamp']
        if r.get('type') == 'attachment':
            a = r.get('attachment') or {}
            if a.get('type') == 'skill_listing' and a.get('names'):
                info['skills'] = list(a['names'])
        if r.get('type') == 'assistant' and not r.get('isSidechain'):
            m = r.get('message') or {}
            if m.get('model'):
                info['models'][m['model']] += 1
            if r.get('requestedModel'):
                info['requested_models'][r['requestedModel']] += 1
            if r.get('effort'):
                info['efforts'][r['effort']] += 1
            for x in m.get('content') or []:
                if not isinstance(x, dict):
                    continue
                if x.get('type') == 'tool_use':
                    info['tools'][x.get('name')] += 1
                    if x.get('name') == 'Skill':
                        inp = x.get('input') if isinstance(x.get('input'), dict) else {}
                        info['skill_calls'].append(str(inp.get('skill') or ''))
                elif x.get('type') == 'text' and x.get('text', '').strip():
                    info['final_text'] = x['text']
    return info


def first_prompt(path: Path) -> str | None:
    """只读到第一条人工输入为止，用于快速定位。"""
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
            p = human_prompts(rows[-1:])
            if p:
                return p[0]['text']
    return None


def locate(claude_home: Path, cwd: str, prompt: str, voided: list[str]) -> Path | None:
    """在做题目录对应的项目目录下，找最新一个首条输入与 prompt 一致且未作废的会话。"""
    pdir = claude_home / 'projects' / encode_project_dir(cwd)
    if not pdir.is_dir():
        return None
    want = _norm(prompt)
    for f in sorted(pdir.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.stem in voided:
            continue
        fp = first_prompt(f)
        if fp is not None and _norm(fp) == want:
            return f
    return None


def list_sessions(claude_home: Path, cwd: str) -> list[str]:
    pdir = claude_home / 'projects' / encode_project_dir(cwd)
    return [f.stem for f in pdir.glob('*.jsonl')] if pdir.is_dir() else []


def validate(info: dict, prompt: str, expected_model: str | None,
             builtin_skills: list[str]) -> dict:
    """返回 {'errors': [...], 'continue_count': n}。"""
    errors = []
    prompts = info['prompts']
    if not prompts:
        errors.append('轨迹中没有人工输入')
    else:
        if _norm(prompts[0]['text']) != _norm(prompt):
            errors.append('首条输入与库内 prompt 原文不一致')
        extra = [p for p in prompts[1:]]
        bad = [p for p in extra if _norm(p['text']).lower() not in CONTINUE_WORDS]
        if bad:
            errors.append(f'存在追问或纠错输入 {len(bad)} 条：' + _norm(bad[0]['text'])[:60])
    tools = info['tools']
    # 模型主动调用 Claude Code 内置 skill 属于正常行为，只拦截调用非内置 skill
    bad_calls = [s for s in info.get('skill_calls', []) if s not in builtin_skills]
    if bad_calls:
        names = ', '.join(sorted({s or '（未知）' for s in bad_calls}))
        errors.append(f'轨迹中调用非内置 skill {len(bad_calls)} 次：{names}')
    mcp = sum(v for k, v in tools.items() if k and k.startswith('mcp__'))
    if mcp:
        errors.append(f'轨迹中有 MCP 调用 {mcp} 次')
    sub = sum(tools.get(k, 0) for k in SUBAGENT_TOOLS)
    if sub:
        errors.append(f'轨迹中有子代理调用 {sub} 次')
    extra_skills = sorted(set(info['skills']) - set(builtin_skills))
    if extra_skills:
        errors.append('skill 列表含非内置 skill：' + ', '.join(extra_skills))
    if expected_model:
        used = set(info['requested_models']) or set(info['models'])
        if used and used != {expected_model}:
            errors.append(f"轨迹模型 {', '.join(sorted(used))} 与配置 {expected_model} 不一致")
    efforts = set(info['efforts'])
    if efforts and efforts != {'high'}:
        errors.append('推理强度不是 high：' + ', '.join(sorted(efforts)))
    if not efforts:
        errors.append('轨迹中没有推理强度记录')
    return {'errors': errors, 'continue_count': max(0, len(prompts) - 1)}


def _brief_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:300]
    for key in ('command', 'file_path', 'path', 'pattern', 'url', 'query', 'description'):
        if key in inp:
            return f'{key}={str(inp[key])[:300]}'
    return json.dumps(inp, ensure_ascii=False)[:300]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(x.get('text', '') for x in content if isinstance(x, dict))
    return ''


def summarize(path: Path, max_steps: int = 600) -> dict:
    """按时间顺序生成步骤列表：工具调用、是否报错、模型文字；附改动文件与最终回复。"""
    rows = load_rows(path)
    results = {}
    for r in rows:
        if r.get('type') != 'user':
            continue
        for x in (r.get('message') or {}).get('content') or []:
            if isinstance(x, dict) and x.get('type') == 'tool_result':
                results[x.get('tool_use_id')] = (bool(x.get('is_error')), _result_text(x.get('content')))
    steps, files, final = [], [], ''
    for r in rows:
        if r.get('type') != 'assistant' or r.get('isSidechain'):
            continue
        for x in (r.get('message') or {}).get('content') or []:
            if not isinstance(x, dict):
                continue
            if x.get('type') == 'text' and x.get('text', '').strip():
                final = x['text']
                steps.append({'n': len(steps) + 1, 'kind': 'text', 'text': x['text'][:300]})
            elif x.get('type') == 'tool_use':
                name, inp = x.get('name'), x.get('input') or {}
                err, out = results.get(x.get('id'), (False, ''))
                step = {'n': len(steps) + 1, 'kind': 'tool', 'tool': name,
                        'input': _brief_input(name, inp), 'error': err}
                if err:
                    step['output'] = out[:400]
                steps.append(step)
                if name in FILE_TOOLS and isinstance(inp, dict) and inp.get('file_path'):
                    if inp['file_path'] not in files:
                        files.append(inp['file_path'])
    truncated = len(steps) > max_steps
    if truncated:
        # 保留开头与结尾，中间省略
        half = max_steps // 2
        steps = steps[:half] + [{'n': 0, 'kind': 'text', 'text': f'中间省略 {len(steps) - max_steps} 步'}] + steps[-half:]
    return {'steps': steps, 'files_changed': files, 'final_text': final,
            'tool_errors': sum(1 for s in steps if s.get('error')), 'truncated': truncated}


def summary_text(summary: dict) -> str:
    """把摘要转成给验收 skill 读的纯文本。"""
    lines = []
    for s in summary['steps']:
        if s['kind'] == 'text':
            lines.append(f"#{s['n']} 模型文字 {s['text']}")
        else:
            flag = ' 报错' if s.get('error') else ''
            lines.append(f"#{s['n']} {s['tool']}{flag} {s['input']}")
            if s.get('output'):
                lines.append(f"    输出 {s['output']}")
    lines.append('改动文件 ' + ', '.join(summary['files_changed']))
    lines.append('最终回复 ' + summary['final_text'])
    return '\n'.join(lines)
