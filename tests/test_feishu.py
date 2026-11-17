# -*- coding: utf-8 -*-
"""飞书多维表格写入：字段映射、完成后自动写入、旁路任务不影响题目状态。不调用真实飞书。"""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, db, feishu, jobs, pipeline

URL = 'https://example.feishu.cn/base/TestBaseToken'


@pytest.fixture()
def client(data_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c


def new_task(**kw):
    f = dict(branch='demo', status=pipeline.S_DONE, user_prompt='做个东西', domain='游戏、图形与多媒体',
             subdomain='音视频处理', task_type='功能开发', difficulty='困难', voided_sessions=[],
             client_seq='12109', lang_framework='Python 3.12, Flask', harness=pipeline.HARNESS,
             harness_version='2.1.325', os=pipeline.OS_LINUX, reproducibility='无外部依赖',
             init_sha='a' * 40, artifact_sha='b' * 40, session_id='s-1', prompt_id='p-1',
             trajectory_path='/mnt/e/up/12109-demo.jsonl', recording_link='E:\\up\\12109-demo.mp4',
             delivery_score=3, feedback='反馈正文', remark='备注不进飞书')
    f.update(kw)
    return db.insert_task(f)


def field(name, typ, **kw):
    return {'id': 'fld' + name, 'name': name, 'type': typ, 'multiple': True, 'readonly': False,
            'options': [], 'style': {}, **kw}


SCHEMA = [
    field('UID', 'number'), field('User Prompt', 'text'), field('所属领域', 'select', multiple=False),
    field('环境可复现等级', 'select'), field('语言|框架', 'text'), field('TurnID|PromptID', 'text'),
    field('交付完整性', 'number'), field('产物快照', 'text', style={'type': 'url'}),
    field('轨迹文件', 'attachment'), field('运行录屏', 'attachment'), field('截图', 'attachment'),
    field('格式校验', 'formula', readonly=True), field('自定义列', 'text'), field('Feedback', 'text'),
]


def test_build_record_maps_schema(data_dir):
    """按飞书字段名对上提交表列：| 对应 /，选项包成数组，数字转数值，附件取 WSL 路径，UID 取甲方序号。"""
    t = db.get_task(new_task(feedback=''))
    rec = feishu.build_record(t, SCHEMA)
    assert rec['uid'] == 12109
    f = rec['fields']
    assert 'UID' not in f and '格式校验' not in f
    assert f['所属领域'] == ['游戏、图形与多媒体'] and f['环境可复现等级'] == ['无外部依赖']
    assert f['语言|框架'] == 'python,flask' and f['TurnID|PromptID'] == 'p-1'
    assert f['交付完整性'] == 3 and isinstance(f['交付完整性'], int)
    assert f['产物快照'].endswith('/commit/' + 'b' * 40)
    # 空值不写，更新时保留飞书里的原值
    assert 'Feedback' not in f
    assert rec['attachments'] == {'轨迹文件': ['/mnt/e/up/12109-demo.jsonl'],
                                  '运行录屏': ['/mnt/e/up/12109-demo.mp4']}
    assert set(rec['skipped']) == {'截图', '自定义列'}


def test_build_record_requires_uid(data_dir):
    t = db.get_task(new_task(client_seq=None))
    with pytest.raises(feishu.FeishuError, match='甲方序号'):
        feishu.build_record(t, SCHEMA)
    with pytest.raises(feishu.FeishuError, match='UID'):
        feishu.build_record(db.get_task(new_task(branch='b2')), [f for f in SCHEMA if f['name'] != 'UID'])


def test_on_done_only_when_configured(data_dir, monkeypatch):
    """没填飞书链接或关闭自动写入时不写；开启时完成即提交旁路写入任务。"""
    calls = []
    monkeypatch.setattr(feishu, 'start_sync', lambda ids, task_id=None: calls.append((ids, task_id)) or 'rk')
    tid = new_task()
    assert feishu.on_done(tid) is None and not calls
    config.save({'feishu_url': URL, 'feishu_auto_sync': False})
    assert feishu.on_done(tid) is None and not calls
    config.save({'feishu_auto_sync': True})
    assert feishu.on_done(tid) == 'rk' and calls == [([tid], tid)]


def test_complete_triggers_feishu(client, monkeypatch):
    """标记完成后自动写入飞书；写入出错不影响完成操作。"""
    config.save({'feishu_url': URL})
    calls = []

    def boom(ids, task_id=None):
        calls.append(ids)
        raise feishu.FeishuError('飞书不可用')
    monkeypatch.setattr(feishu, 'start_sync', boom)
    tid = new_task(status=pipeline.S_RECORD)
    r = client.post(f'/api/tasks/{tid}/action/complete', json={'recording_link': 'E:\\up\\a.mp4'})
    assert r.status_code == 200 and db.get_task(tid)['status'] == pipeline.S_DONE and calls == [[tid]]


def test_sync_failure_keeps_task_status(data_dir, monkeypatch):
    """写入飞书失败只记在运行记录里，题目状态不变，也不占题目互斥。"""
    config.save({'feishu_url': URL})

    def fake_call(req, timeout=900):
        if req['op'] == 'inspect':
            return {'ok': True, 'tableId': 'tbl1', 'tableName': '提交表', 'fields': SCHEMA}
        raise feishu.FeishuError('选项不存在')
    monkeypatch.setattr(feishu, 'call', fake_call)
    tid = new_task()
    rk = feishu.start_sync([tid], task_id=tid)
    assert tid not in jobs.busy_tasks()
    for _ in range(100):
        run = db.get_run(rk)
        if run['status'] != 'running':
            break
        time.sleep(0.05)
    assert run['status'] == 'failed' and '选项不存在' in run['error'] and run['task_id'] == tid
    assert db.get_task(tid)['status'] == pipeline.S_DONE and not db.get_task(tid)['last_error']


def test_sync_api_requires_url(client):
    tid = new_task()
    r = client.post('/api/feishu/sync', json={'ids': [tid]})
    assert r.status_code == 400 and '飞书表格链接' in r.json()['detail']
    assert client.post('/api/feishu/status', json={'ids': [tid]}).json() == {'configured': False, 'records': {}}
