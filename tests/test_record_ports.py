# -*- coding: utf-8 -*-
"""录屏端口避开 Windows 保留端口段：本机桌面的浏览器经 WSL 转发访问，落在保留段里的端口会被拒。"""
from app import db, pipeline, recorder, wintools

NETSH = """协议 tcp 端口排除范围

开始端口    结束端口
----------    --------
      5357        5357
     21071       21170
     21442       21541
     50000       50059     *

* - 管理的端口排除。
"""


def test_parse_excluded():
    rngs = wintools.parse_excluded(NETSH)
    assert rngs == [(5357, 5357), (21071, 21170), (21442, 21541), (50000, 50059)]
    assert wintools.port_excluded(21500, rngs) and not wintools.port_excluded(21400, rngs)
    assert wintools.parse_excluded('') == []


def _task(**kw):
    f = dict(branch='tree', status=pipeline.S_RECORD, user_prompt='做个东西', domain='游戏、图形与多媒体',
             subdomain='其他', task_type='功能开发', difficulty='困难', voided_sessions=[], artifact_sha='abc',
             record_prep={'setup': 'nohup python3 -m http.server 21500 > .qa/record.log 2>&1 &\n'
                                   "echo 'http://localhost:21500'", 'cleanup': 'kill x'},
             record_steps=[{'action': '打开http://localhost:21500 点击运行', 'command': '',
                            'expect': '结果为215000与1215001'}])
    f.update(kw)
    return db.insert_task(f)


def test_remap_excluded_ports(data_dir, monkeypatch):
    monkeypatch.setattr(recorder, '_port_busy', lambda p: False)
    tid = _task()
    t = db.get_task(tid)
    script = "export async function prepare(ctx) { await ctx.page.goto('http://localhost:21500/'); }"
    db.update_task(tid, record_script={'key': recorder.script_key(t), 'script': script, 'manual_needed': False})
    logs = []
    rngs = [(21442, 21541)]
    m = recorder.remap_excluded_ports(tid, logs.append, rngs)
    p0, _ = pipeline.port_range(db.get_task(tid))
    assert m == {21500: p0}
    t = db.get_task(tid)
    assert f'http.server {p0} ' in t['record_prep']['setup'] and '21500' not in t['record_prep']['setup']
    assert f'localhost:{p0}' in t['record_steps'][0]['action']
    # 更长数字里的同样几位不动
    assert t['record_steps'][0]['expect'] == '结果为215000与1215001'
    # 脚本缓存一起换端口并按新内容重算键，不用重新生成
    assert f'localhost:{p0}/' in t['record_script']['script']
    assert t['record_script']['key'] == recorder.script_key(t)
    assert '21500→' in logs[0]
    # 已经不在保留段里：不再改动
    assert recorder.remap_excluded_ports(tid, logs.append, rngs) == {}
    assert len(logs) == 1


def test_remap_skips_stale_cache_and_clean_ports(data_dir, monkeypatch):
    monkeypatch.setattr(recorder, '_port_busy', lambda p: False)
    tid = _task()
    # 缓存键与当前内容对不上：只换准备与步骤，不动缓存（流水线会重新生成）
    db.update_task(tid, record_script={'key': 'old', 'script': "goto('http://localhost:21500/')"})
    recorder.remap_excluded_ports(tid, lambda s: None, [(21442, 21541)])
    t = db.get_task(tid)
    assert t['record_script']['script'] == "goto('http://localhost:21500/')"
    assert '21500' not in t['record_prep']['setup']
    # 没有保留段或端口不在保留段里：不改
    tid2 = _task(branch='tree2')
    assert recorder.remap_excluded_ports(tid2, lambda s: None, []) == {}
    assert recorder.remap_excluded_ports(tid2, lambda s: None, [(30000, 30100)]) == {}
