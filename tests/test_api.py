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


def _prepare_env(client, tmp_path, monkeypatch, gpu_ok=None):
    """准备环境测试的公共设置：临时裸仓库当远端，工具链、外网、客户端体检返回固定结果。"""
    from app import envcheck
    from tests.conftest import run
    remote = tmp_path / 'remote.git'
    run(['git', 'init', '--bare', str(remote)], tmp_path)
    client.put('/api/config', json={'github_repo': str(remote), 'tasks_root': str(tmp_path / 'tasks')})
    monkeypatch.setattr(envcheck, 'toolchain_check', lambda tc: [
        {'name': 'python', 'ok': True, 'detail': 'Python 3.12.3', 'fix': ''}])
    monkeypatch.setattr(envcheck, 'net_check', lambda urls: [])
    monkeypatch.setattr(envcheck, 'client_check', lambda home, d: {
        'ok': True, 'items': [{'name': '全局 CLAUDE.md', 'ok': True, 'detail': '不存在', 'fix': ''}],
        'expected_model': 'auto'})
    monkeypatch.setattr(envcheck, 'gpu_items', lambda req: [
        {'name': '显卡能力', 'ok': False, 'manual': True, 'detail': 'webgpu', 'fix': '在chrome://gpu确认'}]
        if req.get('needs_gpu') else [])


def test_prepare_runs_everything_and_stops_only_for_manual(client, tmp_path, monkeypatch, git_identity):
    """一次跑完初始化与准备：脚本素材自动生成；人工素材缺失时停下并说明，放入后继续准备即提交初始快照。"""
    _prepare_env(client, tmp_path, monkeypatch)
    mats = [
        # 生成脚本每执行一次就在计数文件里追加一行，用来确认已就绪的素材不会重跑
        {'path': 'samples/gen.txt', 'source': 'script', 'url': '', 'purpose': '生成',
         'script': 'mkdir -p samples && echo ok > samples/gen.txt && echo run >> ../gen-count.txt',
         'state': 'pending', 'note': ''},
        {'path': 'samples/manual.bin', 'source': 'manual', 'url': '', 'script': '', 'purpose': '人工',
         'state': 'pending', 'note': ''},
    ]
    tid = new_task(branch='prep', status=pipeline.S_INIT, materials=mats,
                   env_requirements={'toolchain': [], 'needs_gpu': False, 'external_urls': []})
    key = client.post(f'/api/tasks/{tid}/action/prepare', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_ENV and t['init_sha'] is None
    assert [m['state'] for m in t['materials']] == ['ready', 'missing']
    blockers = client.get(f'/api/tasks/{tid}').json()['env_blockers']
    assert len(blockers) == 1 and 'samples/manual.bin' in blockers[0] and '人工' in blockers[0]
    # 人工放入素材后继续准备：全部通过，提交初始快照进入待做题，脚本素材不重跑
    (tmp_path / 'tasks' / 'prep' / 'samples' / 'manual.bin').write_bytes(b'x')
    key = client.post(f'/api/tasks/{tid}/action/prepare', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_DO and len(t['init_sha']) == 40 and t['expected_model'] == 'auto'
    assert (tmp_path / 'tasks' / 'gen-count.txt').read_text().count('run') == 1


def test_prepare_gpu_needs_confirm(client, tmp_path, monkeypatch, git_identity):
    """依赖显卡的题停下等人工确认，确认后自动继续并提交初始快照；脚本失败时停下并给出原因。"""
    _prepare_env(client, tmp_path, monkeypatch)
    tid = new_task(branch='gpu', status=pipeline.S_INIT, materials=[
        {'path': 'a.txt', 'source': 'script', 'url': '', 'purpose': '', 'script': 'exit 3',
         'state': 'pending', 'note': ''}],
        env_requirements={'toolchain': [], 'needs_gpu': True, 'external_urls': []})
    key = client.post(f'/api/tasks/{tid}/action/prepare', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    blockers = client.get(f'/api/tasks/{tid}').json()['env_blockers']
    assert any('显卡能力' in b and '人工确认' in b for b in blockers)
    assert any('a.txt' in b and '退出码 3' in b for b in blockers)
    # 修好脚本后确认显卡：一次继续准备完成
    m = db.get_task(tid)['materials']
    m[0]['script'] = 'echo ok > a.txt'
    db.update_task(tid, materials=m)
    key = client.post(f'/api/tasks/{tid}/action/confirm_gpu', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    assert db.get_task(tid)['status'] == pipeline.S_DO


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
    for name in ('qa-gen', 'qa-review', 'qa-rework'):
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


# ---------- 质检登记与返修 ----------
# 质检原文属于作业方材料，测试只用格式相同的虚构文本

GOOD_FEEDBACK = ('模型写完src/app.js只跑了node --check就说完成 没有打开过页面 最终提交里src/app.js第40行'
                 'render在数据为空时直接读items.length 页面白屏报typeerror 问题仍在 给2分')
REWORK_TEXT = ('【AI质检｜待返修】\n质检时间：2026-11-13 15:07:35\n问题：\n过程段把某个环境现象归因成模型问题。\n'
               '证据：\n1. 原轨第12行只是环境提示。\n修改建议：\n删除这条过程归因 保留产物缺陷。')
VOID_TEXT = '【AI质检｜不通过】\n质检时间：2026-11-12 13:32:38\n轨迹里有不允许的内容 需重新生成本轮轨迹。'
NEW_FB = GOOD_FEEDBACK.replace('没有打开过页面', '没有在浏览器里打开过页面')


def wait_run(key):
    import time
    for _ in range(100):
        if db.get_run(key)['status'] != 'running':
            break
        time.sleep(0.1)
    return db.get_run(key)


def done_task(**kw):
    f = dict(status=pipeline.S_DONE, seq=11, feedback=GOOD_FEEDBACK, delivery_score=2,
             score_reason='核心功能白屏', reproducibility='无外部依赖', artifact_sha='b' * 40,
             init_sha='a' * 40, recording_link='E:\\r\\a.mp4', trajectory_path='/data/t.jsonl',
             record_steps=[{'action': '启动', 'command': 'npm start', 'expect': '白屏'}])
    f.update(kw)
    return new_task(**f)


def test_parse_inspection():
    p = pipeline.parse_inspection(REWORK_TEXT)
    assert p['verdict'] == '待返修' and p['inspect_time'] == '2026-11-13 15:07:35'
    assert p['problem'].startswith('过程段') and '原轨第12行' in p['evidence']
    assert p['suggestion'].startswith('删除这条')
    # 不带 AI 的旧写法照样识别；没有分段的质检，正文整体记为问题
    p = pipeline.parse_inspection('【质检｜待返修】\n质检时间：2026-11-14 11:46:57\nfeedback缺代码锚点 需补写')
    assert p['problem'].startswith('feedback缺') and not p['evidence']
    # 不通过的数据直接放弃，不进流水线
    with pytest.raises(pipeline.ActionError, match='直接放弃'):
        pipeline.parse_inspection(VOID_TEXT)
    with pytest.raises(pipeline.ActionError, match='一次只处理一条'):
        pipeline.parse_inspection(REWORK_TEXT + '\n' + REWORK_TEXT)
    with pytest.raises(pipeline.ActionError, match='质检结论'):
        pipeline.parse_inspection('质检时间：2026-11-13 15:07:35 没有结论')


def fake_rework(monkeypatch, result: dict, new_sha: str | None = None):
    """替换 codex 与 git：AI 返回 result；new_sha 不为空时模拟修复目录有改动、推送了新的产物快照。"""
    from app import gitops
    seen = {}

    def fake(skill, text, run_key, log, stop=None):
        seen['skill'], seen['text'] = skill, text
        base = {'summary': '删掉了环境现象的过程归因', 'delivery_score': 2, 'feedback': NEW_FB,
                'remark': '', 'reproducibility': '无外部依赖', 'need_rerecord': False,
                'record_steps': [{'action': '启动', 'command': 'npm start', 'expect': '白屏'}],
                'self_check': {'has_process': True, 'has_product': True, 'has_code_anchor': True,
                               'score_consistent': True, 'note': 'ok'}}
        return {**base, **result}
    monkeypatch.setattr(codex_runner, 'run_skill', fake)
    monkeypatch.setattr(gitops, 'fresh_clone', lambda *a, **k: None)
    monkeypatch.setattr(gitops, 'ignored_entries', lambda *a, **k: [])
    monkeypatch.setattr(gitops, 'git', lambda *a, **k: '')
    monkeypatch.setattr(gitops, 'diff_text', lambda *a, **k: 'diff')
    monkeypatch.setattr(gitops, 'commit_artifact',
                        lambda d, b, init, ign, summary='', log=None, title='': {'sha': new_sha or 'b' * 40})
    return seen


def test_rework_flow_with_fake_codex(client, monkeypatch):
    """粘贴质检原文即由 AI 处理，处理完直接回到已完成，列出提交表要改的格，序号与录屏沿用。"""
    tid = done_task(lang_framework='Node.js 22 + 原生JavaScript')
    seen = fake_rework(monkeypatch, {})
    # 不通过的质检不进流水线，题目状态不变
    r = client.post(f'/api/tasks/{tid}/action/rework', json={'text': VOID_TEXT})
    assert r.status_code == 400 and db.get_task(tid)['status'] == pipeline.S_DONE and not db.list_inspections(tid)
    key = client.post(f'/api/tasks/{tid}/action/rework', json={'text': REWORK_TEXT}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    assert seen['skill'] == 'qa-rework' and '本次质检（第1轮）' in seen['text']
    assert '原轨第12行' in seen['text'] and '修复目录' in seen['text'] and GOOD_FEEDBACK in seen['text']
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_DONE and t['feedback'] == NEW_FB
    assert t['seq'] == 11 and t['recording_link'] == 'E:\\r\\a.mp4'
    i = db.list_inspections(tid)[0]
    assert i['state'] == pipeline.I_DONE and i['summary'].startswith('删掉了') and i['need_record'] == 0
    d = client.get(f'/api/tasks/{tid}').json()
    assert d['inspections'][0]['changed_columns'] == ['Feedback']
    # 提交表弹窗：返修过的题带轮次与改过的格（列名到原值），没返修过的为空
    done_task(branch='plain', seq=12)
    rows = {r['branch']: r for r in client.post('/api/submission', json={}).json()['rows']}
    assert rows['demo']['rework'] == {'round': 1, 'state': pipeline.I_DONE, 'changed': {'Feedback': GOOD_FEEDBACK}}
    assert rows['plain']['rework'] is None
    # 只有已完成的题可以返修
    tid2 = new_task(branch='other')
    assert client.post(f'/api/tasks/{tid2}/action/rework', json={'text': REWORK_TEXT}).status_code == 400


def test_rework_artifact_changed_needs_record(client, monkeypatch):
    """修复目录有改动时推送新的产物快照，回到待录屏，录完回到已完成并结束本轮质检。"""
    tid = done_task()
    fake_rework(monkeypatch, {'summary': '补交了tests目录'}, new_sha='c' * 40)
    key = client.post(f'/api/tasks/{tid}/action/rework', json={'text': REWORK_TEXT}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_RECORD and t['recording_link'] is None and t['artifact_sha'] == 'c' * 40
    cols = client.get(f'/api/tasks/{tid}').json()['inspections'][0]['changed_columns']
    assert cols == ['产物快照', '运行录屏', 'Feedback']
    assert db.open_inspection(tid)['need_record'] == 1
    rec = 'E:\\r\\b.mp4'
    assert client.post(f'/api/tasks/{tid}/action/complete', json={'recording_link': rec}).status_code == 200
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_DONE and t['seq'] == 11
    assert db.list_inspections(tid)[0]['state'] == pipeline.I_DONE


def test_rework_changed_columns_per_round(client, monkeypatch):
    """多轮返修时每轮只列自己改过的列：前一轮对比下一轮登记时的快照，最后一轮对比当前内容。"""
    tid = done_task()
    fake_rework(monkeypatch, {})
    key = client.post(f'/api/tasks/{tid}/action/rework', json={'text': REWORK_TEXT}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    fake_rework(monkeypatch, {'remark': '补充了素材说明'})
    key = client.post(f'/api/tasks/{tid}/action/rework', json={'text': REWORK_TEXT}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    rounds = client.get(f'/api/tasks/{tid}').json()['inspections']
    assert [(r['round'], r['changed_columns']) for r in rounds] == [(1, ['Feedback']), (2, ['备注'])]


def test_rework_gate_failure_then_retry(client, monkeypatch):
    """AI 返修后的 Feedback 过不了规则检查时置为失败、不改提交内容，重试重新跑 AI。"""
    tid = done_task()
    fake_rework(monkeypatch, {'feedback': '页面白屏，控制台报错'})
    key = client.post(f'/api/tasks/{tid}/action/rework', json={'text': REWORK_TEXT}).json()['run_key']
    assert wait_run(key)['status'] == 'failed'
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_FAIL and t['failed_from'] == pipeline.S_REWORK
    assert t['feedback'] == GOOD_FEEDBACK and '规则检查' in t['last_error']
    fake_rework(monkeypatch, {})
    key = client.post(f'/api/tasks/{tid}/action/retry', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    assert db.get_task(tid)['status'] == pipeline.S_DONE and len(db.list_inspections(tid)) == 1


def test_rework_schema_matches():
    ws = Path(config.DEFAULTS['agent_workspace_wsl'])
    if not ws.exists():
        pytest.skip('Codex 工作区不存在')
    s = json.loads((ws / 'schemas' / 'qa-rework.json').read_text(encoding='utf-8'))
    assert s['properties']['reproducibility']['enum'] == pipeline.REPRO_LEVELS


def test_normalize_lang_in_submission(client):
    """提交表的语言/框架不带版本号，英文小写，英文逗号分隔；编辑保存时同样规整。"""
    from app import submission
    n = submission.normalize_lang
    assert n('Java, Python, Node.js') == 'java,python,nodejs'
    assert n('Node.js 22 + 原生JavaScript（Web Worker、Web Audio、Canvas）；Python 3.12标准库生成素材') == \
        'nodejs,javascript,webworker,webaudio,canvas,python'
    assert n('C++17 + OpenGL 4.5') == 'c++,opengl'
    assert n('TypeScript>=5.4 + React 18') == 'typescript,react'
    assert n('Go 1.22, FFmpeg 6.x, H.264') == 'go,ffmpeg,h264'
    assert n('') == '' and n(None) == ''
    tid = done_task(lang_framework='Python 3.12 + SQLite')
    cells = {c['name']: c['value'] for c in client.post('/api/submission', json={}).json()['rows'][0]['cells']}
    assert cells['语言/框架'] == 'python,sqlite'
    r = client.put(f'/api/tasks/{tid}', json={'lang_framework': 'Rust 1.80 / wgpu'}).json()
    assert r['lang_framework'] == 'rust,wgpu'


def test_migrate_fresh_and_existing(data_dir):
    """新库由初始化脚本直接建出最新结构；存量库补执行缺的增量脚本。"""
    import sqlite3
    c = db.conn()
    cols = {r[1] for r in c.execute('PRAGMA table_info(qa_inspection)')}
    assert 'summary' in cols
    done = {r[0] for r in c.execute('SELECT script FROM schema_version')}
    assert {'001_init.sql', '002_inspection.sql', '003_rework_summary.sql'} <= done
    # 模拟只执行到 002 的存量库
    c.execute('ALTER TABLE qa_inspection DROP COLUMN summary')
    c.execute("DELETE FROM schema_version WHERE script = '003_rework_summary.sql'")
    c.commit()
    db.reset_for_test()
    c = sqlite3.connect(db.db_path())
    db.migrate(c)
    assert 'summary' in {r[1] for r in c.execute('PRAGMA table_info(qa_inspection)')}
    c.close()
