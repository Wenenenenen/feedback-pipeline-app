# -*- coding: utf-8 -*-
import os
from pathlib import Path

import pytest

from app import config, trajectory as T
from tests.conftest import write_jsonl

CWD = '/home/u/jianzhi/feedback-tasks/live-relay'
PROMPT = '帮我用 Go 写一个 RTMP 直播服务。\n要支持多房间。'
SID = '11111111-2222-3333-4444-555555555555'


def base(extra_prompts=(), tools=(('Bash', {'command': 'go build ./...'}, False),),
         model='auto_model/urm', effort='high', skills=None):
    rows = [
        {'type': 'mode', 'mode': 'normal', 'sessionId': SID},
        {'type': 'system', 'subtype': 'local_command', 'content': '<command-name>/status</command-name>',
         'sessionId': SID, 'version': '2.1.322', 'cwd': CWD, 'timestamp': '2026-11-12T06:00:00.000Z'},
        {'type': 'attachment', 'attachment': {'type': 'skill_listing',
                                              'names': skills or list(config.BUILTIN_SKILLS)}},
        {'type': 'user', 'promptId': 'p-1', 'uuid': 'u-1', 'turnOrigin': 'human', 'promptSource': 'typed',
         'message': {'role': 'user', 'content': PROMPT}, 'sessionId': SID, 'version': '2.1.322',
         'cwd': CWD, 'timestamp': '2026-11-12T06:01:00.000Z'},
    ]
    for i, (name, inp, err) in enumerate(tools):
        rows.append({'type': 'assistant', 'requestedModel': model, 'effort': effort, 'sessionId': SID,
                     'timestamp': f'2026-11-12T06:02:{i:02d}.000Z',
                     'message': {'model': model, 'content': [
                         {'type': 'text', 'text': f'第 {i} 步'},
                         {'type': 'tool_use', 'id': f't{i}', 'name': name, 'input': inp}]}})
        rows.append({'type': 'user', 'promptId': 'p-1', 'toolUseResult': {}, 'sourceToolAssistantUUID': 'a',
                     'message': {'content': [{'type': 'tool_result', 'tool_use_id': f't{i}',
                                              'is_error': err, 'content': 'boom' if err else 'ok'}]}})
    for j, text in enumerate(extra_prompts):
        rows.append({'type': 'user', 'promptId': f'p-{j + 2}', 'turnOrigin': 'human',
                     'message': {'content': text}, 'sessionId': SID, 'timestamp': '2026-11-12T06:10:00.000Z'})
    rows.append({'type': 'assistant', 'requestedModel': model, 'effort': effort, 'sessionId': SID,
                 'timestamp': '2026-11-12T06:20:00.000Z',
                 'message': {'model': model, 'content': [{'type': 'text', 'text': '全部完成'}]}})
    return rows


def make(tmp_path, rows, name=SID) -> Path:
    p = tmp_path / '.claude' / 'projects' / T.encode_project_dir(CWD) / f'{name}.jsonl'
    write_jsonl(p, rows)
    return p


def check(tmp_path, rows, expected='auto_model/urm'):
    info = T.parse(make(tmp_path, rows))
    return info, T.validate(info, PROMPT, expected, config.BUILTIN_SKILLS)


def test_encode_project_dir():
    assert T.encode_project_dir('/home/wendy/jianzhi/feedback/sdf') == '-home-wendy-jianzhi-feedback-sdf'


def test_valid_single_turn(tmp_path):
    info, v = check(tmp_path, base())
    assert v['errors'] == []
    assert info['session_id'] == SID and info['version'] == '2.1.322'
    assert info['prompts'][0]['prompt_id'] == 'p-1'
    assert info['final_text'] == '全部完成'


def test_continue_allowed(tmp_path):
    _, v = check(tmp_path, base(extra_prompts=['继续', '继续']))
    assert v['errors'] == [] and v['continue_count'] == 2


def test_follow_up_rejected(tmp_path):
    _, v = check(tmp_path, base(extra_prompts=['你没做断流重连']))
    assert any('追问' in e for e in v['errors'])


@pytest.mark.parametrize('tool,inp,word', [
    ('Skill', {'skill': 'brainstorming'}, 'brainstorming'),
    ('Skill', {}, '非内置 skill'),
    ('mcp__fetch__fetch', {}, 'MCP'),
    ('Agent', {}, '子代理'),
])
def test_forbidden_tools(tmp_path, tool, inp, word):
    _, v = check(tmp_path, base(tools=[(tool, inp, False)]))
    assert any(word in e for e in v['errors'])


def test_builtin_skill_call_allowed(tmp_path):
    # 模型主动加载内置 skill（如画波形时加载 dataviz）不算违规
    info, v = check(tmp_path, base(tools=[('Skill', {'skill': 'dataviz'}, False)]))
    assert info['skill_calls'] == ['dataviz']
    assert v['errors'] == []


def test_extra_skill(tmp_path):
    _, v = check(tmp_path, base(skills=list(config.BUILTIN_SKILLS) + ['brainstorming']))
    assert any('brainstorming' in e for e in v['errors'])


def test_model_and_effort(tmp_path):
    _, v = check(tmp_path, base(model='other-model'))
    assert any('不一致' in e for e in v['errors'])
    _, v = check(tmp_path, base(effort='low'))
    assert any('high' in e for e in v['errors'])


def test_prompt_mismatch(tmp_path):
    info = T.parse(make(tmp_path, base()))
    v = T.validate(info, '另一道题', None, config.BUILTIN_SKILLS)
    assert any('不一致' in e for e in v['errors'])


def test_locate_skips_voided_and_other_prompts(tmp_path):
    home = tmp_path / '.claude'
    old = make(tmp_path, base(), name='old')
    new = make(tmp_path, base(), name='new')
    other_rows = base()
    other_rows[3]['message']['content'] = '别的题'
    make(tmp_path, other_rows, name='other')
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    assert T.locate(home, CWD, PROMPT, []).stem == 'new'
    assert T.locate(home, CWD, PROMPT, ['new']).stem == 'old'
    assert T.locate(home, CWD, PROMPT, ['new', 'old']) is None
    assert sorted(T.list_sessions(home, CWD)) == ['new', 'old', 'other']


def test_summarize(tmp_path):
    rows = base(tools=[('Write', {'file_path': '/x/main.go', 'content': '...'}, False),
                       ('Bash', {'command': 'go test ./...'}, True)])
    s = T.summarize(make(tmp_path, rows))
    assert s['files_changed'] == ['/x/main.go']
    assert s['tool_errors'] == 1
    assert s['final_text'] == '全部完成'
    text = T.summary_text(s)
    assert 'Bash 报错 command=go test ./...' in text and 'boom' in text


REAL = Path('~/.claude/projects/-home-wendy-jianzhi-feedback-sdf').expanduser()


@pytest.mark.skipif(not REAL.is_dir(), reason='本机没有真实轨迹样本')
def test_real_sample():
    f = sorted(REAL.glob('*.jsonl'))[0]
    info = T.parse(f)
    assert info['session_id'] == f.stem
    assert len(info['prompts']) == 1 and info['prompts'][0]['prompt_id']
    assert set(info['efforts']) == {'high'}
    assert set(info['skills']) <= set(config.BUILTIN_SKILLS)
    v = T.validate(info, info['prompts'][0]['text'], None, config.BUILTIN_SKILLS)
    assert v['errors'] == []
    assert T.summarize(f)['steps']
