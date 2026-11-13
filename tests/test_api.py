# -*- coding: utf-8 -*-
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codex_runner, config, db, pipeline


@pytest.fixture()
def client(data_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c


def new_task(**kw):
    f = dict(branch='demo', status=pipeline.S_CHECK, user_prompt='做个东西', domain='游戏、图形与多媒体',
             subdomain='其他', task_type='功能开发', difficulty='困难', voided_sessions=[])
    f.update(kw)
    return db.insert_task(f)


def test_meta_and_config(client):
    m = client.get('/api/meta').json()
    assert '待准备环境' in m['statuses'] and list(m['domains']) == ['游戏、图形与多媒体']
    r = client.put('/api/config', json={'concurrency': 3, 'unknown': 1}).json()
    assert r['concurrency'] == 3 and 'unknown' not in r
    # 未改动的项不落盘，内置 skill 清单继续跟随代码默认值
    saved = json.loads(config.CONFIG_FILE.read_text(encoding='utf-8'))
    assert saved == {'concurrency': 3}
    assert 'artifact-design' in r['builtin_skills']


def test_detail_and_edit(client):
    tid = new_task()
    d = client.get(f'/api/tasks/{tid}').json()
    assert d['do_command'].endswith('/demo && claude --dangerously-skip-permissions')
    r = client.put(f'/api/tasks/{tid}', json={'feedback': '页面白屏，控制台报错', 'status': '已完成'}).json()
    assert r['status'] == pipeline.S_CHECK  # 状态不可直接改
    assert r['feedback_issues']


def test_approve_blocked_by_style(client):
    tid = new_task(feedback='页面白屏，控制台报错', delivery_score=2)
    r = client.post(f'/api/tasks/{tid}/action/approve', json={})
    assert r.status_code == 400 and '可复现' in r.json()['detail']
    # 可复现等级只能取三档之一
    assert client.put(f'/api/tasks/{tid}', json={'reproducibility': '高'}).status_code == 400
    client.put(f'/api/tasks/{tid}', json={'reproducibility': '无外部依赖'})
    r = client.post(f'/api/tasks/{tid}/action/approve', json={})
    assert r.status_code == 400 and '口吻' in r.json()['detail']
    # 口吻合格但没有代码锚点和原分依据 仍不能通过
    client.put(f'/api/tasks/{tid}', json={'feedback': '页面白屏 控制台报typeerror'})
    r = client.post(f'/api/tasks/{tid}/action/approve', json={})
    assert r.status_code == 400 and '内容' in r.json()['detail']
    client.put(f'/api/tasks/{tid}', json={'feedback': (
        '模型写完src/app.js只跑了node --check就说完成 没有打开过页面 最终提交里src/app.js第40行'
        'render在数据为空时直接读items.length 页面白屏报typeerror 问题仍在 给2分')})
    assert client.post(f'/api/tasks/{tid}/action/approve', json={}).status_code == 200
    assert db.get_task(tid)['status'] == pipeline.S_RECORD


def test_review_schema_repro_enum_matches():
    from pathlib import Path
    ws = Path(config.DEFAULTS['agent_workspace_wsl'])
    if not ws.exists():
        pytest.skip('Codex 工作区不存在')
    s = json.loads((ws / 'schemas' / 'qa-review.json').read_text(encoding='utf-8'))
    assert s['properties']['reproducibility']['enum'] == pipeline.REPRO_LEVELS


def test_wrong_state(client):
    tid = new_task(status=pipeline.S_INIT)
    r = client.post(f'/api/tasks/{tid}/action/collect', json={})
    assert r.status_code == 400 and '不能执行' in r.json()['detail']


def test_complete_requires_recording_and_numbers(client):
    """完成需要录屏文件与收集时留存的轨迹，完成时接续已有最大序号编号。"""
    client.put('/api/config', json={'seq_base': 10})
    tid = new_task(status=pipeline.S_RECORD, trajectory_path='/data/t.jsonl')
    assert client.post(f'/api/tasks/{tid}/action/complete', json={}).status_code == 400
    rec = 'E:\\GSB-Recordings\\2026-11-13 22-52-36.mp4'
    r = client.post(f'/api/tasks/{tid}/action/complete', json={'recording_link': rec})
    t = db.get_task(tid)
    assert r.status_code == 200 and t['status'] == pipeline.S_DONE and t['seq'] == 11
    tid2 = new_task(branch='demo2', status=pipeline.S_RECORD, trajectory_path='/data/t2.jsonl')
    client.post(f'/api/tasks/{tid2}/action/complete', json={'recording_link': rec})
    assert db.get_task(tid2)['seq'] == 12
    # 没有轨迹文件不能完成
    tid3 = new_task(branch='demo3', status=pipeline.S_RECORD)
    r = client.post(f'/api/tasks/{tid3}/action/complete', json={'recording_link': rec})
    assert r.status_code == 400 and '轨迹' in r.json()['detail']


def test_submission_rows(client):
    """提交表按列顺序给出取值；未勾选时只列已完成题目；轨迹文件取收集留存的轨迹。"""
    from app import submission
    client.put('/api/config', json={'github_repo': 'git@github.com:org/repo.git'})
    done = new_task(branch='done-task', status=pipeline.S_DONE, seq=11, init_sha='a' * 40,
                    trajectory_path='/data/t.jsonl', recording_link='E:\\r\\a.mp4', delivery_score=3)
    other = new_task(branch='check-task', status=pipeline.S_CHECK)
    body = client.post('/api/submission', json={}).json()
    assert body['columns'] == submission.COLUMNS
    assert [r['branch'] for r in body['rows']] == ['done-task']
    cells = {c['name']: c['value'] for c in body['rows'][0]['cells']}
    assert [c['name'] for c in body['rows'][0]['cells']] == submission.COLUMNS
    assert cells['序号'] == '11' and cells['轨迹文件'] == '/data/t.jsonl'
    assert cells['初始环境快照'] == 'https://github.com/org/repo/commit/' + 'a' * 40
    assert cells['运行录屏'] == 'E:\\r\\a.mp4' and cells['交付完整性'] == '3'
    # 勾选时按勾选的题目返回，已编号的排在前面
    body = client.post('/api/submission', json={'ids': [other, done]}).json()
    assert [r['branch'] for r in body['rows']] == ['done-task', 'check-task']


def test_recordings_latest(client, tmp_path):
    """录屏目录按修改时间倒序列出 mp4，并给出 Windows 路径。"""
    import os
    from app import submission
    assert submission.win_path(Path('/mnt/e/GSB-Recordings/a b.mp4')) == 'E:\\GSB-Recordings\\a b.mp4'
    d = tmp_path / 'rec'
    d.mkdir()
    for i, name in enumerate(['old.mp4', 'new.mp4', 'note.txt']):
        f = d / name
        f.write_bytes(b'x')
        os.utime(f, (1_700_000_000 + i * 100, 1_700_000_000 + i * 100))
    client.put('/api/config', json={'recording_dir': str(d)})
    files = client.get('/api/recordings').json()
    assert [f['name'] for f in files] == ['new.mp4', 'old.mp4']
    client.put('/api/config', json={'recording_dir': str(d / 'missing')})
    assert client.get('/api/recordings').status_code == 400


def test_due_flag():
    import datetime as dt
    today = dt.date.today()
    early = {'status': pipeline.S_CHECK, 'done_time': f'{today} 10:00:00'}
    late_yesterday = {'status': pipeline.S_CHECK, 'done_time': f'{today - dt.timedelta(days=1)} 21:00:00'}
    old = {'status': pipeline.S_CHECK, 'done_time': f'{today - dt.timedelta(days=3)} 10:00:00'}
    assert pipeline.due_flag(early) == '今日必须提交'
    assert pipeline.due_flag(late_yesterday) == '今日必须提交'
    assert pipeline.due_flag(old) == '已超过提交日'
    assert pipeline.due_flag({**early, 'status': pipeline.S_DONE}) == ''


def test_gen_flow_with_fake_codex(client, monkeypatch):
    """用假的 codex 结果走一遍出题入库。"""
    def fake(skill, text, run_key, log, stop=None):
        assert skill == 'qa-gen' and '库内已有题目' in text
        return {'tasks': [{
            'branch': 'Live Relay', 'user_prompt': '帮我做个直播服务。', 'domain': '游戏、图形与多媒体',
            'subdomain': '音视频处理', 'task_type': '功能开发', 'difficulty': '地狱',
            'lang_framework': 'Go', 'acceptance_points': ['推流可播'], 'materials': [],
            'env_requirements': {'toolchain': [], 'needs_gpu': False, 'gpu_note': '', 'external_urls': []},
            'remark': '0-1 开发。', 'similarity_note': '无相近题目'}]}
    monkeypatch.setattr(codex_runner, 'run_skill', fake)
    key = client.post('/api/gen', json={'count': 1, 'domain': '游戏、图形与多媒体'}).json()['run_key']
    import time
    for _ in range(50):
        if db.get_run(key)['status'] != 'running':
            break
        time.sleep(0.1)
    assert db.get_run(key)['status'] == 'success'
    t = db.find_task_by_branch('live-relay')
    assert t and t['status'] == pipeline.S_INIT and t['os'] == 'MacOS/Linux'


def test_build_cmd():
    cfg = dict(config.DEFAULTS)
    cmd = codex_runner.build_cmd(cfg, 'qa-review', 'run1')
    assert '--ephemeral' in cmd and '--ignore-user-config' in cmd and cmd[-1] == '$qa-review'
    assert cmd[cmd.index('-o') + 1].endswith('\\runs\\run1\\result.json')


def test_schemas_are_strict():
    """codex 的结构化输出要求每个对象 additionalProperties=false 且字段全部 required。"""
    from pathlib import Path
    ws = Path(config.DEFAULTS['agent_workspace_wsl'])
    if not ws.exists():
        pytest.skip('Codex 工作区不存在')

    def walk(node, path):
        if node.get('type') == 'object':
            assert node.get('additionalProperties') is False, path
            assert set(node['required']) == set(node['properties']), path
            for k, v in node['properties'].items():
                walk(v, f'{path}.{k}')
        if node.get('type') == 'array':
            walk(node['items'], path + '[]')
    for name in ('qa-gen', 'qa-review'):
        walk(json.loads((ws / 'schemas' / f'{name}.json').read_text(encoding='utf-8')), name)


def test_gen_skips_wrong_subdomain(client, monkeypatch):
    """子方向不属于所属领域的题目不入库；输入里带有本次运行目录。"""
    seen = {}

    def fake(skill, text, run_key, log, stop=None):
        seen['text'], seen['key'] = text, run_key
        base = {'user_prompt': '做个东西。', 'task_type': '功能开发', 'difficulty': '困难',
                'lang_framework': 'Go', 'acceptance_points': [], 'materials': [],
                'env_requirements': {'toolchain': [], 'needs_gpu': False, 'gpu_note': '', 'external_urls': []},
                'remark': '', 'similarity_note': ''}
        return {'tasks': [
            {**base, 'branch': 'ok-task', 'domain': '游戏、图形与多媒体', 'subdomain': '图形渲染'},
            {**base, 'branch': 'bad-task', 'domain': '游戏、图形与多媒体', 'subdomain': '数据采集、爬虫与 RPA'},
            {**base, 'branch': 'off-domain', 'domain': '安全', 'subdomain': '其他'}]}
    monkeypatch.setattr(codex_runner, 'run_skill', fake)
    key = client.post('/api/gen', json={'count': 3, 'domain': '游戏、图形与多媒体'}).json()['run_key']
    import time
    for _ in range(50):
        if db.get_run(key)['status'] != 'running':
            break
        time.sleep(0.1)
    assert db.find_task_by_branch('ok-task') and not db.find_task_by_branch('bad-task')
    assert not db.find_task_by_branch('off-domain')
    assert f'runs\\{key}' in seen['text']
    r = client.post('/api/gen', json={'count': 1, 'domain': '游戏、图形与多媒体', 'subdomain': '漏洞检测与攻防'})
    assert r.status_code == 400
    # 主领域以外的领域不再出题
    assert client.post('/api/gen', json={'count': 1, 'domain': '数据工程'}).status_code == 400


def test_edit_rejects_wrong_subdomain(client):
    tid = new_task()
    r = client.put(f'/api/tasks/{tid}', json={'subdomain': '漏洞检测与攻防'})
    assert r.status_code == 400
    assert client.put(f'/api/tasks/{tid}', json={'subdomain': '图形渲染'}).status_code == 200


def test_schema_subdomain_enum_matches_domains():
    from pathlib import Path
    ws = Path(config.DEFAULTS['agent_workspace_wsl'])
    if not ws.exists():
        pytest.skip('Codex 工作区不存在')
    s = json.loads((ws / 'schemas' / 'qa-gen.json').read_text(encoding='utf-8'))
    enum = s['properties']['tasks']['items']['properties']['subdomain']['enum']
    assert set(enum) == {x for subs in pipeline.DOMAINS.values() for x in subs}


def test_materials_lines_include_script():
    """验收输入要带上素材生成脚本，便于换参数重新生成。"""
    t = {'materials': [
        {'path': 'samples/a.zip', 'source': 'script', 'purpose': '测试录音', 'url': '', 'script': 'python3 gen.py'},
        {'path': 'samples/b.mp4', 'source': 'url', 'purpose': '样片', 'url': 'https://example.com/b.mp4', 'script': ''}]}
    text = '\n'.join(pipeline._materials_lines(t))
    assert 'samples/a.zip' in text and 'python3 gen.py' in text
    assert 'https://example.com/b.mp4' in text
    assert pipeline._materials_lines({'materials': []}) == []
