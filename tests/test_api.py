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
    monkeypatch.setattr(envcheck, 'toolchain_check', lambda tc, cwd=None: [
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
    """关闭自动确认时，依赖显卡的题停下等人工确认，确认后自动继续并提交初始快照；脚本失败时停下并给出原因。"""
    _prepare_env(client, tmp_path, monkeypatch)
    client.put('/api/config', json={'gpu_auto_confirm': False})
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


def test_prepare_gpu_auto_confirm(client, tmp_path, monkeypatch, git_identity):
    """默认自动确认显卡能力：依赖显卡的题一次准备完成进入待做题，环境检查明细注明已自动确认。"""
    _prepare_env(client, tmp_path, monkeypatch)
    assert client.get('/api/config').json()['gpu_auto_confirm'] is True
    tid = new_task(branch='gpu-auto', status=pipeline.S_INIT, materials=[],
                   env_requirements={'toolchain': [], 'needs_gpu': True, 'external_urls': []})
    key = client.post(f'/api/tasks/{tid}/action/prepare', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_DO
    gpu = next(i for i in t['env_check'] if i['name'] == '显卡能力')
    assert gpu['ok'] and gpu['auto_confirmed'] and '已按配置自动确认' in gpu['detail']


def test_wrong_state(client):
    tid = new_task(status=pipeline.S_INIT)
    r = client.post(f'/api/tasks/{tid}/action/collect', json={})
    assert r.status_code == 400 and '不能执行' in r.json()['detail']


def test_complete_requires_recording(client):
    """完成需要录屏文件与收集时留存的轨迹，甲方序号保持出题时分配的值。"""
    tid = new_task(status=pipeline.S_RECORD, trajectory_path='/data/t.jsonl', client_seq='12100')
    assert client.post(f'/api/tasks/{tid}/action/complete', json={}).status_code == 400
    rec = 'E:\\GSB-Recordings\\2026-11-13 22-52-36.mp4'
    r = client.post(f'/api/tasks/{tid}/action/complete', json={'recording_link': rec})
    t = db.get_task(tid)
    assert r.status_code == 200 and t['status'] == pipeline.S_DONE and t['client_seq'] == '12100'
    assert 'seq' not in t
    # 没有轨迹文件不能完成
    tid3 = new_task(branch='demo3', status=pipeline.S_RECORD)
    r = client.post(f'/api/tasks/{tid3}/action/complete', json={'recording_link': rec})
    assert r.status_code == 400 and '轨迹' in r.json()['detail']


def test_submission_rows(client):
    """提交表按列顺序给出取值；未勾选时只列已完成题目；轨迹文件取收集留存的轨迹。"""
    from app import submission
    client.put('/api/config', json={'github_repo': 'git@github.com:org/repo.git'})
    done = new_task(branch='done-task', status=pipeline.S_DONE, client_seq='11', init_sha='a' * 40,
                    trajectory_path='/data/t.jsonl', recording_link='E:\\r\\a.mp4', delivery_score=3)
    other = new_task(branch='check-task', status=pipeline.S_CHECK)
    body = client.post('/api/submission', json={}).json()
    assert body['columns'] == submission.COLUMNS
    assert [r['branch'] for r in body['rows']] == ['done-task']
    cells = {c['name']: c['value'] for c in body['rows'][0]['cells']}
    assert [c['name'] for c in body['rows'][0]['cells']] == submission.COLUMNS
    # 轨迹文件写成 Windows 侧路径，上传文件的两格点击时复制所在文件夹
    assert cells['序号'] == '11' and cells['轨迹文件'] == '\\\\wsl.localhost\\Ubuntu-24.04\\data\\t.jsonl'
    assert cells['初始环境快照'] == 'https://github.com/org/repo/commit/' + 'a' * 40
    assert cells['运行录屏'] == 'E:\\r\\a.mp4' and cells['交付完整性'] == '3'
    copies = {c['name']: c.get('copy') for c in body['rows'][0]['cells']}
    assert copies['轨迹文件'] == '\\\\wsl.localhost\\Ubuntu-24.04\\data\\'
    assert copies['运行录屏'] == 'E:\\r\\' and copies['分支'] is None
    # 勾选时按勾选的题目返回，按甲方序号数值从小到大，没有甲方序号的排在最后
    big = new_task(branch='big-seq', status=pipeline.S_DONE, client_seq='9')
    body = client.post('/api/submission', json={'ids': [other, done, big]}).json()
    assert [r['branch'] for r in body['rows']] == ['big-seq', 'done-task', 'check-task']
    # 按状态多选：取任一勾选状态的题；传空列表取全部状态
    body = client.post('/api/submission', json={'statuses': [pipeline.S_CHECK, pipeline.S_REWORK]}).json()
    assert [r['branch'] for r in body['rows']] == ['check-task']
    body = client.post('/api/submission', json={'statuses': [pipeline.S_DONE, pipeline.S_CHECK]}).json()
    assert [r['branch'] for r in body['rows']] == ['big-seq', 'done-task', 'check-task']
    body = client.post('/api/submission', json={'statuses': []}).json()
    assert len(body['rows']) == 3


def test_submission_xlsx(client):
    """导出 Excel 的范围与弹窗一致，表头为提交表列顺序，序号与交付完整性按数字写入，链接可点击。"""
    import io
    from urllib.parse import unquote
    from openpyxl import load_workbook
    from app import submission
    client.put('/api/config', json={'github_repo': 'git@github.com:org/repo.git'})
    # 没有已完成的题目时不导出空表
    assert client.post('/api/submission/xlsx', json={}).status_code == 400
    done = new_task(branch='done-task', status=pipeline.S_DONE, client_seq='11', init_sha='a' * 40,
                    delivery_score=3, session_id='0123456789012345678', feedback='模型没跑测试 给3分')
    other = new_task(branch='check-task', status=pipeline.S_CHECK)
    r = client.post('/api/submission/xlsx', json={})
    assert r.status_code == 200 and r.headers['content-type'].startswith(
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    assert unquote(r.headers['content-disposition'].split("UTF-8''")[1]).startswith('提交表-')
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = [list(x) for x in ws.iter_rows(values_only=True)]
    assert rows[0] == submission.COLUMNS and len(rows) == 2
    got = dict(zip(rows[0], rows[1]))
    assert got['分支'] == 'done-task' and got['序号'] == 11 and got['交付完整性'] == 3
    # 长数字 ID 按文本写入，不被转成科学计数法
    assert got['SessionID'] == '0123456789012345678' and got['Feedback'] == '模型没跑测试 给3分'
    link = ws.cell(row=2, column=submission.COLUMNS.index('初始环境快照') + 1)
    assert link.hyperlink.target == 'https://github.com/org/repo/commit/' + 'a' * 40
    # 勾选时只导出勾选的题目
    r = client.post('/api/submission/xlsx', json={'ids': [other, done]})
    names = [x[1] for x in load_workbook(io.BytesIO(r.content)).active.iter_rows(min_row=2, values_only=True)]
    assert names == ['done-task', 'check-task']


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
    key = client.post('/api/gen', json={'client_seqs': '12100', 'domain': '游戏、图形与多媒体'}).json()['run_key']
    import time
    for _ in range(50):
        if db.get_run(key)['status'] != 'running':
            break
        time.sleep(0.1)
    assert db.get_run(key)['status'] == 'success'
    t = db.find_task_by_branch('live-relay')
    assert t and t['status'] == pipeline.S_INIT and t['os'] == 'Linux' and t['client_seq'] == '12100'
    assert t['harness'] == 'Claude Code CLI'


def test_build_cmd():
    cfg = dict(config.DEFAULTS)
    cmd = codex_runner.build_cmd(cfg, 'qa-review', 'run1')
    assert '--ephemeral' in cmd and '--ignore-user-config' in cmd and cmd[-1] == '$qa-review'
    assert cmd[cmd.index('-o') + 1].endswith('\\runs\\run1\\result.json')
    # 默认不跟随 config.toml：模型参数与原来一致，紧跟推理强度
    i = cmd.index('-m')
    assert cmd[i:i + 4] == ['-m', cfg['model'], '-c', f'model_reasoning_effort="{cfg["reasoning_effort"]}"']
    assert sum(1 for c in cmd if c == '-c') == 1


CC_SWITCH_TOML = '''
model_provider = "relay"
model = "gpt-relay-1"
model_reasoning_effort = "high"

[model_providers.relay]
name = "Relay \\"中转\\""
base_url = "https://relay.example.com/v1"
wire_api = "responses"
requires_openai_auth = true
request_max_retries = 4
experimental_bearer_token = "sk-secret"

[model_providers.relay.http_headers]
X-Org = "team"

[mcp_servers.fetch]
command = "uvx"
'''


def _follow_cfg(tmp_path, text):
    p = tmp_path / 'config.toml'
    p.write_text(text, encoding='utf-8')
    return dict(config.DEFAULTS, codex_follow_toml=True, codex_config_toml=str(p))


def test_model_args_follow_toml(tmp_path):
    import tomllib
    args = codex_runner.model_args(_follow_cfg(tmp_path, CC_SWITCH_TOML))
    assert args[:2] == ['-m', 'gpt-relay-1']
    pairs = [args[i + 1] for i in range(2, len(args), 2)]
    assert all(args[i] == '-c' for i in range(2, len(args), 2))
    assert pairs[0] == 'model_provider="relay"'
    kv = dict(p.split('=', 1) for p in pairs)
    # 每个值都能被当成 TOML 字面量解析回原值
    back = {k: tomllib.loads(f'v = {v}')['v'] for k, v in kv.items()}
    assert back['model_providers.relay.name'] == 'Relay "中转"'
    assert back['model_providers.relay.base_url'] == 'https://relay.example.com/v1'
    assert back['model_providers.relay.requires_openai_auth'] is True
    assert back['model_providers.relay.request_max_retries'] == 4
    assert back['model_providers.relay.http_headers.X-Org'] == 'team'
    # 只透传供应商与模型，MCP 与推理强度不透传
    assert not any(k.startswith('mcp_servers') or 'reasoning' in k for k in kv)
    # 日志里密钥打码
    masked = codex_runner.mask_args(args)
    assert 'model_providers.relay.experimental_bearer_token=***' in masked
    assert 'model_providers.relay.base_url="https://relay.example.com/v1"' in masked
    # build_cmd 用同一组参数，推理强度仍来自配置页
    cmd = codex_runner.build_cmd(_follow_cfg(tmp_path, CC_SWITCH_TOML), 'qa-gen', 'r1')
    assert cmd[cmd.index('-m') + 1] == 'gpt-relay-1'
    assert 'model_reasoning_effort="xhigh"' in cmd


def test_model_args_follow_toml_edge_cases(tmp_path):
    # 未指定供应商（ChatGPT 官方登录）：只传模型；没有 model 时用配置页的模型名
    assert codex_runner.model_args(_follow_cfg(tmp_path, 'model = "m1"\n')) == ['-m', 'm1']
    assert codex_runner.model_args(_follow_cfg(tmp_path, '')) == ['-m', config.DEFAULTS['model']]
    # 内置供应商不需要 provider 表
    assert codex_runner.model_args(_follow_cfg(tmp_path, 'model_provider = "openai"\n'))[2:] == \
        ['-c', 'model_provider="openai"']
    with pytest.raises(codex_runner.CodexError, match='找不到'):
        codex_runner.model_args(_follow_cfg(tmp_path, 'model_provider = "relay"\n'))
    with pytest.raises(codex_runner.CodexError, match='TOML'):
        codex_runner.model_args(_follow_cfg(tmp_path, 'model = \n'))
    cfg = dict(config.DEFAULTS, codex_follow_toml=True, codex_config_toml=str(tmp_path / 'missing.toml'))
    with pytest.raises(codex_runner.CodexError, match='读取'):
        codex_runner.model_args(cfg)


def test_wsl_path():
    assert config.wsl_path('C:\\Users\\张三\\.codex') == '/mnt/c/Users/张三/.codex'
    assert config.wsl_path('D:\\') == '/mnt/d'
    assert config.wsl_path('/home/a') == '/home/a'


class _FakeProc:
    """假的 codex 进程：按给定事件逐行输出，被 kill 后不再输出。"""

    def __init__(self, lines, code=1):
        import io
        self.lines, self.code, self.killed = lines, code, False
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO(b'')
        self.returncode = None

    @property
    def stdout(self):
        for s in self.lines:
            if self.killed:
                return
            yield (s + '\n').encode('utf-8')

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self):
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode


def _run_fake(tmp_path, monkeypatch, lines, max_reconnects):
    ws = tmp_path / 'ws'
    (ws / 'schemas').mkdir(parents=True, exist_ok=True)
    src = Path(config.DEFAULTS['agent_workspace_wsl']) / 'schemas' / 'qa-gen.json'
    (ws / 'schemas' / 'qa-gen.json').write_text(src.read_text(encoding='utf-8'), encoding='utf-8')
    config.save({'agent_workspace_wsl': str(ws), 'codex_max_reconnects': max_reconnects})
    proc = _FakeProc(lines)
    kills = []
    monkeypatch.setattr(codex_runner.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(codex_runner, '_kill_windows', kills.append)
    logs = []
    with pytest.raises(codex_runner.CodexError) as e:
        codex_runner.run_skill('qa-gen', '输入', 'r1', logs.append)
    return str(e.value), proc, kills, logs


RECONNECT = '{"type":"error","message":"Reconnecting... waiting for network (Connection failed)"}'


def test_run_skill_stops_after_reconnects(data_dir, tmp_path, monkeypatch):
    lines = ['{"type":"thread.started"}', '{"type":"turn.started"}'] + [RECONNECT] * 50
    msg, proc, kills, logs = _run_fake(tmp_path, monkeypatch, lines, 5)
    assert '连续重连 5 次' in msg and proc.killed and kills == ['r1']
    # 第 5 次重连后立即结束，不再读后面的事件
    assert sum('Reconnecting' in s for s in logs) == 5


def test_run_skill_reconnect_counter_resets(data_dir, tmp_path, monkeypatch):
    # 重连之间穿插正常事件时重新计数，不触发上限；codex 自己失败时按原因报错
    lines = ([RECONNECT] * 4 + ['{"type":"item.completed","item":{"type":"reasoning","text":"x"}}']) * 3 + \
        ['{"type":"turn.failed","error":{"message":"401 Unauthorized"}}']
    msg, proc, kills, _ = _run_fake(tmp_path, monkeypatch, lines, 5)
    assert '认证失败' in msg and not proc.killed and kills == []
    # 上限为 0 时不限制
    msg, proc, _, _ = _run_fake(tmp_path, monkeypatch, [RECONNECT] * 30, 0)
    assert msg.startswith('codex 退出码 1') and not proc.killed


def test_failure_reason():
    auth = codex_runner.failure_reason(1, [], ['{"type":"turn.failed","error":{"message":"401 Unauthorized"}}'])
    assert '认证失败' in auth
    assert '额度限流' in codex_runner.failure_reason(1, ['ERROR: You have hit your usage limit'], [])
    # 耗时数字里的 401、429 不算状态码
    assert codex_runner.failure_reason(2, ['took 14010ms', 'x4291'], []).startswith('codex 退出码 2')
    # 命令输出里的 429 不在判断范围内（只看 stderr 与错误事件）
    assert codex_runner._is_error_event('{"type":"error","message":"stream error"}')
    assert codex_runner._is_error_event('{"type":"item.completed","item":{"type":"error","message":"x"}}')
    assert not codex_runner._is_error_event(
        '{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"HTTP 429"}}')


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
    for name in ('qa-gen', 'qa-review', 'qa-rework', 'qa-record'):
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
    key = client.post('/api/gen', json={'client_seqs': '12100-12102', 'domain': '游戏、图形与多媒体'}).json()['run_key']
    import time
    for _ in range(50):
        if db.get_run(key)['status'] != 'running':
            break
        time.sleep(0.1)
    assert db.find_task_by_branch('ok-task') and not db.find_task_by_branch('bad-task')
    assert not db.find_task_by_branch('off-domain')
    assert f'runs\\{key}' in seen['text']
    # 题目数量取甲方序号个数；入库的题按顺序分配，没用上的序号记在日志里
    assert '数量 3' in seen['text'] and db.find_task_by_branch('ok-task')['client_seq'] == '12100'
    log = client.get(f'/api/runs/{key}/log').json()['text']
    assert '未用上的甲方序号 12101、12102' in log
    # 必须填甲方序号；已被占用的序号不能再用
    assert '甲方序号' in client.post('/api/gen', json={'domain': '游戏、图形与多媒体'}).json()['detail']
    r = client.post('/api/gen', json={'client_seqs': '12099-12100', 'domain': '游戏、图形与多媒体'})
    assert r.status_code == 400 and '12100 已分配给 ok-task' in r.json()['detail']
    r = client.post('/api/gen', json={'client_seqs': '12200', 'domain': '游戏、图形与多媒体', 'subdomain': '漏洞检测与攻防'})
    assert r.status_code == 400
    # 主领域以外的领域不再出题
    assert client.post('/api/gen', json={'client_seqs': '12200', 'domain': '数据工程'}).status_code == 400


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
    f = dict(status=pipeline.S_DONE, client_seq='11', feedback=GOOD_FEEDBACK, delivery_score=2,
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
                'record_prep': {'setup': 'npm start &', 'cleanup': ''},
                'record_steps': [{'action': '打开页面', 'command': 'http://localhost:20400', 'expect': '白屏'}],
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


def test_static_no_cache(client):
    """页面资源要求浏览器每次确认更新，避免改了样式还用旧缓存。"""
    r = client.get('/style.css')
    assert r.status_code == 200 and r.headers['cache-control'] == 'no-cache'
    assert 'max-height: calc(100vh - var(--header-h)' in r.text


def test_record_issues(client):
    """录屏步骤要能在录屏目录终端里直接执行，且不要求手动点精确像素。"""
    bad = [
        {'action': '后台启动服务', 'expect': '页面可见',
         'command': "wsl.exe -d Ubuntu-24.04 --cd /home/u/jianzhi/feedback-verify/demo -- bash -ic 'bash scripts/start.sh'"},
        {'action': '查看像素', 'command': '浏览器选择gradient.json 点击像素192 300 查看RGBA', 'expect': '数值偏差'},
    ]
    rules = [i['rule'] for i in pipeline.record_issues(bad)]
    assert any(r.startswith('第1步') and 'wsl.exe' in r for r in rules)
    assert any(r.startswith('第1步') and '路径' in r for r in rules)
    assert any(r.startswith('第2步') and '像素' in r for r in rules)
    good = [
        {'action': '浏览器打开页面 在场景下拉框选 gradient.json', 'command': 'http://localhost:20400',
         'expect': '画面出现渐变 右侧面板红分量 0.6635'},
        {'action': '点导入按钮 在文件选择框里选录屏目录 .qa 下的 truncated.wav', 'command': '',
         'expect': '仍显示分析完成 没有截断提示'},
    ]
    prep = {'setup': 'nohup bash scripts/start.sh 20400 > .qa/record.log 2>&1 & echo $! > .qa/record.pid',
            'cleanup': 'kill "$(cat .qa/record.pid)"'}
    assert pipeline.record_issues(good, prep) == []
    assert pipeline.record_issues(None) == []
    # 录制部分不放启动结束服务、临时脚本和控制台注入
    scripted = [
        {'action': '后台启动服务', 'command': 'nohup python3 -m http.server 20800 &', 'expect': '服务运行'},
        {'action': '生成截断文件', 'command': "python3 - <<'PY'\nprint(1)\nPY", 'expect': '生成'},
        {'action': '控制台导入', 'command': "document.querySelector('#f').dispatchEvent(new Event('change'))",
         'expect': '导入'},
    ]
    rules = [i['rule'] for i in pipeline.record_issues(scripted)]
    assert any(r.startswith('第1步') and '录屏前准备' in r for r in rules)
    assert any(r.startswith('第2步') and '临时脚本' in r for r in rules)
    assert any(r.startswith('第3步') and '控制台脚本' in r for r in rules)
    # 写明在控制台只读取精确像素的一行脚本放行；带赋值、点击或改存储的控制台脚本仍算注入
    read_px = [{'action': '打开浏览器控制台执行一行只读命令 读取插入像素',
                'command': "(c=>Array.from(c.getContext('2d').getImageData(1,1,1,1).data))"
                           "(document.querySelector('#canvas'))",
                'expect': '控制台返回[128,128,128,255]'}]
    assert pipeline.record_issues(read_px) == []
    for cmd in ("document.querySelector('#w').value = 3", "document.querySelector('#go').click()",
                "window.localStorage.clear()"):
        rules = [i['rule'] for i in pipeline.record_issues([{'action': '控制台执行', 'command': cmd, 'expect': ''}])]
        assert any('控制台脚本' in r for r in rules), cmd
    # 没写明是在控制台读取的仍按注入处理
    assert pipeline.record_issues([{'action': '执行', 'command': 'document.title', 'expect': ''}])
    # 录屏前准备也要能在录屏目录终端里直接执行
    bad_prep = {'setup': "wsl.exe -d Ubuntu-24.04 -- bash -ic 'npm i'", 'cleanup': ''}
    assert any(i['rule'].startswith('录屏前准备') for i in pipeline.record_issues([], bad_prep))


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
    assert '## 录屏环境' in seen['text'] and 'feedback-record' in seen['text']
    assert '录屏前准备（不录）' in seen['text'] and 'Windows 访问路径' in seen['text']
    assert db.get_task(tid)['record_prep'] == {'setup': 'npm start &', 'cleanup': ''}
    assert '原轨第12行' in seen['text'] and '修复目录' in seen['text'] and GOOD_FEEDBACK in seen['text']
    t = db.get_task(tid)
    assert t['status'] == pipeline.S_DONE and t['feedback'] == NEW_FB
    assert t['client_seq'] == '11' and t['recording_link'] == 'E:\\r\\a.mp4'
    i = db.list_inspections(tid)[0]
    assert i['state'] == pipeline.I_DONE and i['summary'].startswith('删掉了') and i['need_record'] == 0
    d = client.get(f'/api/tasks/{tid}').json()
    assert d['inspections'][0]['changed_columns'] == ['Feedback']
    # 提交表弹窗：返修过的题带轮次与改过的格（列名到原值），没返修过的为空
    done_task(branch='plain', client_seq='12')
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
    assert t['status'] == pipeline.S_DONE and t['client_seq'] == '11'
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


def test_prepare_record_marks_ready(client, tmp_path, monkeypatch):
    """准备录屏环境成功后记下就绪标记；产物快照变化或录屏目录被删后视为未就绪。"""
    from app import gitops
    client.put('/api/config', json={'record_root': str(tmp_path / 'record')})
    monkeypatch.setattr(gitops, 'fresh_clone', lambda repo, br, sha, d, log=None: Path(d).mkdir(parents=True))
    tid = new_task(status=pipeline.S_RECORD, artifact_sha='b' * 40)
    assert client.get(f'/api/tasks/{tid}').json()['record_ready'] is False
    key = client.post(f'/api/tasks/{tid}/action/prepare_record', json={}).json()['run_key']
    assert wait_run(key)['status'] == 'success'
    d = client.get(f'/api/tasks/{tid}').json()
    assert d['record_ready'] is True and d['record_ready_sha'] == 'b' * 40 and d['record_ready_time']
    # 返修推送了新的产物快照，需要重新准备
    db.update_task(tid, artifact_sha='c' * 40)
    assert client.get(f'/api/tasks/{tid}').json()['record_ready'] is False
    db.update_task(tid, artifact_sha='b' * 40)
    (tmp_path / 'record' / 'demo').rmdir()
    assert client.get(f'/api/tasks/{tid}').json()['record_ready'] is False


def test_client_submit_mark(client):
    """有甲方序号且已完成的题可标记甲方提交，列表与提交表都带上；可取消。"""
    tid = new_task(status=pipeline.S_DONE, client_seq='1234')
    no_seq = new_task(branch='no-seq', status=pipeline.S_DONE)
    r = client.post(f'/api/tasks/{no_seq}/action/client_submit', json={'submitted': True})
    assert r.status_code == 400 and '甲方序号' in r.json()['detail']
    # 未完成的题不能标记
    doing = new_task(branch='doing', status=pipeline.S_RECORD, client_seq='1240')
    r = client.post(f'/api/tasks/{doing}/action/client_submit', json={'submitted': True})
    assert r.status_code == 400 and '标记完成' in r.json()['detail']
    assert client.post(f'/api/tasks/{tid}/action/client_submit', json={'submitted': True}).status_code == 200
    t = db.get_task(tid)
    assert t['client_seq'] == '1234' and t['client_submit_time']
    first = t['client_submit_time']
    row = client.post('/api/submission', json={'ids': [tid]}).json()['rows'][0]
    assert row['client_seq'] == '1234' and row['client_submit_time'] == first
    item = {x['id']: x for x in client.get('/api/tasks').json()}[tid]
    assert item['client_seq'] == '1234' and item['client_submit_time'] == first
    # 重复标记保留首次时间；已标记的不能清空甲方序号
    client.post(f'/api/tasks/{tid}/action/client_submit', json={'submitted': True})
    assert db.get_task(tid)['client_submit_time'] == first
    r = client.post(f'/api/tasks/{tid}/action/client_seq', json={'client_seq': ''})
    assert r.status_code == 400 and '取消提交标记' in r.json()['detail']
    client.post(f'/api/tasks/{tid}/action/client_submit', json={'submitted': False})
    t = db.get_task(tid)
    assert t['client_seq'] == '1234' and t['client_submit_time'] is None


def test_client_seq_edit(client):
    """详情里修改或补填甲方序号：去掉前导 # 与前导零，不能与其他题重复，只能填一个，留空清除。"""
    tid = new_task(client_seq='12100')
    other = new_task(branch='other')
    assert client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': ' #012101 '}).status_code == 200
    assert db.get_task(other)['client_seq'] == '12101'
    r = client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': '12100'})
    assert r.status_code == 400 and 'demo' in r.json()['detail']
    r = client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': '12102-12103'})
    assert r.status_code == 400 and '只能填一个' in r.json()['detail']
    assert client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': 'abc'}).status_code == 400
    # 改成自己当前的序号不算占用
    assert client.post(f'/api/tasks/{tid}/action/client_seq', json={'client_seq': '12100'}).status_code == 200
    client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': ''})
    assert db.get_task(other)['client_seq'] is None
    # 删除的题释放序号
    db.delete_task(tid)
    assert client.post(f'/api/tasks/{other}/action/client_seq', json={'client_seq': '12100'}).status_code == 200


def test_task_list_sorted_by_client_seq(client):
    """题目列表按甲方序号数值从大到小，没有甲方序号的排在最后。"""
    new_task(branch='s9', client_seq='9')
    new_task(branch='s12100', client_seq='12100')
    new_task(branch='none')
    new_task(branch='s100', client_seq='100')
    assert [x['branch'] for x in client.get('/api/tasks').json()] == ['s12100', 's100', 's9', 'none']
    assert 'seq' not in client.get('/api/tasks').json()[0]


def test_parse_client_seqs():
    """甲方序号批量写法：逗号、顿号、空白分隔，区间展开，保持填写顺序。"""
    assert pipeline.parse_client_seqs('12100-12103') == ['12100', '12101', '12102', '12103']
    assert pipeline.parse_client_seqs('#12105，12100、12102 ~ 12103\n12110') == [
        '12105', '12100', '12102', '12103', '12110']
    assert pipeline.parse_client_seqs(['12100', 12101]) == ['12100', '12101']
    assert pipeline.parse_client_seqs('') == [] and pipeline.parse_client_seqs(None) == []
    for bad, msg in (('12100,12100', '重复'), ('12100-12099', '无效'), ('12100-12110', '最多'),
                     ('a12', '格式'), ('0', '无效'), ('12100-12101, 12101', '重复')):
        with pytest.raises(pipeline.ActionError, match=msg):
            pipeline.parse_client_seqs(bad)


def test_migrate_drops_seq(data_dir):
    """存量库补执行 007：去掉自编序号列，甲方序号保留并在未删除的题目中唯一。"""
    import sqlite3
    c = db.conn()
    assert 'seq' not in {r[1] for r in c.execute('PRAGMA table_info(qa_task)')}
    # 模拟只执行到 006 的存量库
    c.execute('DROP INDEX uk_qa_task_client_seq')
    c.execute('ALTER TABLE qa_task ADD COLUMN seq INTEGER')
    c.execute("DELETE FROM schema_version WHERE script = '007_client_seq.sql'")
    c.commit()
    tid = new_task(seq=11, client_seq='5600')
    db.reset_for_test()
    c = sqlite3.connect(db.db_path())
    db.migrate(c)
    assert 'seq' not in {r[1] for r in c.execute('PRAGMA table_info(qa_task)')}
    assert c.execute('SELECT client_seq FROM qa_task WHERE id = ?', (tid,)).fetchone()[0] == '5600'
    c.close()
    db.reset_for_test()
    with pytest.raises(Exception, match='UNIQUE'):
        new_task(branch='dup', client_seq='5600')
    db.delete_task(tid)
    assert new_task(branch='reuse', client_seq='5600')


def test_upload_names_follow_client_seq(client, tmp_path):
    """待上传目录里的轨迹与录屏以「甲方序号-分支名」命名，改甲方序号时一起改名；目录外的文件不动。"""
    up = tmp_path / 'upload'
    client.put('/api/config', json={'upload_dir': str(up)})
    t = db.get_task(new_task(status=pipeline.S_DONE, client_seq='12100'))
    assert pipeline.upload_stem(t) == '12100-demo'
    assert pipeline.upload_stem(db.get_task(new_task(branch='no-seq'))) == 'no-seq'
    d = pipeline.upload_dir()
    (d / '12100-demo.jsonl').write_text('{}', encoding='utf-8')
    (d / '12100-demo.mp4').write_bytes(b'x')
    old_rec = tmp_path / 'old.mp4'
    old_rec.write_bytes(b'x')
    db.update_task(t['id'], trajectory_path=str(d / '12100-demo.jsonl'),
                   recording_link=config.win_path(d / '12100-demo.mp4'))
    r = client.post(f"/api/tasks/{t['id']}/action/client_seq", json={'client_seq': '12101'})
    assert r.status_code == 200
    t = db.get_task(t['id'])
    assert t['trajectory_path'] == str(d / '12101-demo.jsonl') and (d / '12101-demo.jsonl').exists()
    assert t['recording_link'] == config.win_path(d / '12101-demo.mp4') and (d / '12101-demo.mp4').exists()
    assert not (d / '12100-demo.jsonl').exists()
    # 清空甲方序号退回只用分支名；历史文件在目录外不改名
    db.update_task(t['id'], recording_link=str(old_rec))
    client.post(f"/api/tasks/{t['id']}/action/client_seq", json={'client_seq': ''})
    t = db.get_task(t['id'])
    assert t['trajectory_path'] == str(d / 'demo.jsonl') and t['recording_link'] == str(old_rec)
