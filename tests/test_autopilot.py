# -*- coding: utf-8 -*-
"""全自动：轨迹进度判断、确认框按键、补充题目池循环、自动做题调度、自动查验、录屏辅助函数。"""
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import autopilot, claude_driver, db, jobs, pipeline as P, recorder, trajectory
from conftest import write_jsonl


@pytest.fixture()
def client(data_dir):
    from app.main import app
    with TestClient(app) as c:
        yield c


def new_task(**kw):
    f = dict(branch=f"t{time.time_ns()}", status=P.S_INIT, user_prompt='做个东西', domain='游戏、图形与多媒体',
             subdomain='其他', task_type='功能开发', difficulty='困难', voided_sessions=[])
    f.update(kw)
    return db.insert_task(f)


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------- 轨迹进度 ----------

def _user(text):
    return {'type': 'user', 'message': {'role': 'user', 'content': text}, 'promptId': 'p1', 'uuid': 'u0'}


def _assistant(content, stop='tool_use', **kw):
    return {'type': 'assistant', 'message': {'content': content, 'stop_reason': stop}, **kw}


def test_turn_state(tmp_path):
    p = tmp_path / 's.jsonl'
    tool = [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}}]
    result = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}}
    write_jsonl(p, [_user('做个东西'), _assistant(tool)])
    assert trajectory.turn_state(p) == 'running'
    # 回复结束但还没写入本轮结束标记
    write_jsonl(p, [_user('做个东西'), _assistant(tool), result, _assistant([{'type': 'text', 'text': '好了'}], 'end_turn')])
    assert trajectory.turn_state(p) == 'running'
    end = {'type': 'system', 'subtype': 'turn_duration', 'durationMs': 1}
    write_jsonl(p, [_user('做个东西'), _assistant(tool), result,
                    _assistant([{'type': 'text', 'text': '好了'}], 'end_turn'), end, {'type': 'ai-title'}])
    assert trajectory.turn_state(p) == 'done'
    # 子代理的记录不影响主链
    write_jsonl(p, [_user('做个东西'), _assistant([{'type': 'text', 'text': '好了'}], 'end_turn'), end,
                    _assistant(tool, isSidechain=True)])
    assert trajectory.turn_state(p) == 'done'
    write_jsonl(p, [_user('做个东西'), _assistant([{'type': 'text', 'text': 'API Error: 500'}], 'stop_sequence',
                                                 isApiErrorMessage=True), end])
    assert trajectory.turn_state(p) == 'api_error'
    ask = [{'type': 'tool_use', 'id': 'q1', 'name': 'AskUserQuestion', 'input': {}}]
    write_jsonl(p, [_user('做个东西'), _assistant(ask)])
    assert trajectory.turn_state(p) == 'asking'


def test_parse_api_errors_and_forbidden_calls(tmp_path):
    """接口报错单独记录、不计入模型与最终回复；违规调用逐项列出。"""
    p = tmp_path / 's.jsonl'
    sub = [{'type': 'tool_use', 'id': 'a1', 'name': 'Agent', 'input': {}},
           {'type': 'tool_use', 'id': 'm1', 'name': 'mcp__x__y', 'input': {}}]
    write_jsonl(p, [_user('做个东西'), _assistant(sub),
                    _assistant([{'type': 'text', 'text': 'API Error: bad response status code 504'}],
                               'stop_sequence', isApiErrorMessage=True)])
    info = trajectory.parse(p)
    assert info['api_errors'] == ['API Error: bad response status code 504']
    assert 'API Error' not in info['final_text']
    bad = trajectory.forbidden_calls(info, [])
    assert any('MCP' in b for b in bad) and any('子代理' in b for b in bad)


def test_interrupt_remark():
    assert P.interrupt_remark({'api_errors': []}, 0) == ''
    r = P.interrupt_remark({'api_errors': ['API Error: bad response status code 504',
                                           'API Error: 529 overloaded']}, 2)
    assert '接口报错504 529中断2次' in r and '继续2次' in r and 'promptid取第一轮' in r
    assert '无报错停止' in P.interrupt_remark({'api_errors': []}, 1)


@pytest.fixture()
def fake_session(monkeypatch, tmp_path):
    """wait_done 用：轨迹进度按给定序列返回，屏幕空闲，等待不真睡。"""
    p = tmp_path / 's.jsonl'
    write_jsonl(p, [_user('做个东西')])
    sent = []
    monkeypatch.setattr(claude_driver, 'alive', lambda name: True)
    monkeypatch.setattr(claude_driver, 'capture', lambda name: '')
    monkeypatch.setattr(claude_driver, '_sleep', lambda sec, stop: None)
    monkeypatch.setattr(claude_driver, 'send_continue', lambda name, path, log, stop: sent.append(name))
    monkeypatch.setattr(claude_driver.time, 'sleep', lambda s: None)

    def states(seq):
        it = iter(seq)
        monkeypatch.setattr(trajectory, 'turn_state', lambda path: next(it))
    return p, sent, states


def _cfg(**kw):
    from app import config
    return {**config.DEFAULTS, 'do_idle_sec': 0, **kw}


def test_wait_done_auto_continue_on_api_error(fake_session):
    p, sent, states = fake_session
    # 报错一次后自动「继续」，再正常做完
    states(['api_error', 'running', 'done'])
    claude_driver.wait_done('s', p, _cfg(do_auto_continue_max=3), lambda m: None, threading.Event())
    assert sent == ['s']
    # 超过次数上限判失败，原因里带上已继续的次数
    sent.clear()
    states(['api_error', 'running', 'api_error'])
    with pytest.raises(claude_driver.DriverError, match='已自动输入「继续」1 次'):
        claude_driver.wait_done('s', p, _cfg(do_auto_continue_max=1), lambda m: None, threading.Event())
    assert sent == ['s']
    # 设为 0 时不自动继续，直接判失败
    sent.clear()
    states(['api_error'])
    with pytest.raises(claude_driver.DriverError, match='接口报错中断'):
        claude_driver.wait_done('s', p, _cfg(do_auto_continue_max=0), lambda m: None, threading.Event())
    assert sent == []


def test_wait_done_stops_on_forbidden_call(fake_session):
    p, sent, states = fake_session
    write_jsonl(p, [_user('做个东西'), _assistant([{'type': 'tool_use', 'id': 'a1', 'name': 'Agent', 'input': {}}])])
    states(['running'] * 5)
    with pytest.raises(claude_driver.DriverError, match='不允许的调用'):
        claude_driver.wait_done('s', p, _cfg(), lambda m: None, threading.Event())
    assert '重试' in autopilot.hints_for(P.S_DO, '做题中出现不允许的调用')[0]


def test_pick_option():
    screen = (' Quick safety check: Is this a project you created or one you trust?\n'
              ' ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel')
    assert claude_driver.pick_option(screen, 'yes, i trust this folder') == ['Down', 'Enter']
    screen2 = ' ❯ 1. No, exit\n   2. Yes, I accept\n'
    assert claude_driver.pick_option(screen2, 'yes, i accept') == ['Down', 'Enter']
    assert claude_driver.pick_option(' ❯ Yes, I accept\n   No', 'yes, i accept') == ['Enter']
    assert claude_driver.pick_option('没有确认框', 'yes, i accept') == []


def test_clean_env_drops_backend_vars(monkeypatch):
    monkeypatch.setenv('PIPELINE_DATA', '/x')
    monkeypatch.setenv('WSL_DISTRO_NAME', 'Ubuntu-24.04')
    prefix = claude_driver.clean_env_prefix()
    assert prefix[:2] == ['env', '-i'] and 'WSL_DISTRO_NAME=Ubuntu-24.04' in prefix
    assert not any(x.startswith('PIPELINE_DATA=') for x in prefix)


# ---------- 补充题目池 ----------

def _fake_gen(monkeypatch, per_round=2, fail_on=None, on_call=None):
    calls = []

    def fake(params):
        calls.append(params)
        n = len(calls)
        key = f'gen-{n}-{time.time_ns()}'
        db.insert_run(key, 'qa-gen', None, '/dev/null')
        if fail_on == n:
            db.finish_run(key, 'failed', 'codex 认证失败')
        else:
            for i in range(per_round):
                new_task(gen_run_id=key)
            db.finish_run(key, 'success')
        if on_call:
            on_call(n)
        return key
    monkeypatch.setattr(P, 'start_gen', fake)
    return calls


def test_gen_loop_runs_until_stopped(data_dir, monkeypatch):
    loop = autopilot.GenLoop()
    calls = _fake_gen(monkeypatch, on_call=lambda n: n == 2 and loop.stop())
    loop.start()
    assert wait(lambda: loop.status()['state'] == 'idle')
    st = loop.status()
    # 第二轮里点了停止：本轮跑完后停下，不再开第三轮
    assert len(calls) == 2 and st['rounds'] == 2 and st['added'] == 4
    assert calls[0]['count'] == 10 and calls[0]['subdomain'] == '' and calls[0]['difficulty'] == ''
    assert calls[0]['stack'] == '' and calls[0]['extra'] == ''


def test_gen_loop_stops_on_failure(data_dir, monkeypatch):
    loop = autopilot.GenLoop()
    _fake_gen(monkeypatch, fail_on=2)
    loop.start()
    assert wait(lambda: loop.status()['state'] == 'failed')
    st = loop.status()
    assert st['rounds'] == 1 and 'codex 认证失败' in st['error'] and st['last_run'].startswith('gen-2')


def test_gen_loop_stops_after_empty_rounds(data_dir, monkeypatch):
    loop = autopilot.GenLoop()
    _fake_gen(monkeypatch, per_round=0)
    loop.start()
    assert wait(lambda: loop.status()['state'] == 'failed')
    assert '连续两轮' in loop.status()['error']


def test_auto_gen_api(client, monkeypatch):
    """补充题目池暂停使用：开始一律拒绝；已在运行的仍可停止。"""
    started = threading.Event()
    monkeypatch.setattr(autopilot.gen_loop, '_loop', lambda: started.set())
    r = client.post('/api/auto/gen', json={'action': 'start'})
    assert r.status_code == 400 and '暂停使用' in r.json()['detail']
    assert not started.is_set() and autopilot.gen_loop.status()['state'] == 'idle'
    autopilot.gen_loop.state = 'running'
    assert client.post('/api/auto/gen', json={'action': 'stop'}).json()['gen']['state'] == 'stopping'
    autopilot.gen_loop.state = 'idle'


# ---------- 自动做题调度 ----------

class Calls(list):
    """记录调用的列表，另带一个可改的 busy 集合模拟在跑的任务。"""


@pytest.fixture()
def fake_steps(monkeypatch):
    """各环节的启动函数换成只记录调用。"""
    calls = Calls()
    for mod, name in ((P, 'start_prepare'), (P, 'start_collect'), (P, 'start_review'),
                      (claude_driver, 'start_auto_do'), (recorder, 'start_auto_record')):
        monkeypatch.setattr(mod, name, lambda tid, _n=name: calls.append((_n, tid)) or f'run-{_n}')
    busy: set[int] = set()
    monkeypatch.setattr(jobs, 'busy_tasks', lambda: set(busy))
    calls.busy = busy
    return calls


@pytest.fixture(autouse=True)
def _reset_started():
    claude_driver._started.clear()
    yield
    claude_driver._started.clear()


def test_tick_picks_next_after_claude_started(data_dir, fake_steps):
    a, b, c = new_task(), new_task(), new_task()
    autopilot.tick()
    assert fake_steps == [('start_prepare', a)]
    assert db.get_task(a)['auto_run'] == 1 and db.get_task(b)['auto_run'] == 0
    # 第一道准备好进入待做题：Claude Code 启动中、还没发出 prompt，仍算在准备线上，不取第二道
    db.update_task(a, status=P.S_DO)
    autopilot.tick()
    assert ('start_auto_do', a) in fake_steps and db.get_task(b)['auto_run'] == 0
    fake_steps.busy.add(a)
    autopilot.tick()
    assert db.get_task(b)['auto_run'] == 0
    # prompt 发出、开始做题后，取第二道开始初始化；第二道准备中时不取第三道
    claude_driver._mark_started(a, True)
    fake_steps.clear()
    autopilot.tick()
    assert fake_steps == [('start_prepare', b)] and db.get_task(c)['auto_run'] == 0


def test_tick_joined_init_tasks_queue_on_prep_line(data_dir, fake_steps):
    """人工交给自动的多道待初始化题按 id 排队，一次只准备一道，前一道开始做题后才准备下一道。"""
    a, b, c = (new_task(auto_run=1) for _ in range(3))
    pool = new_task()
    autopilot.tick()
    assert fake_steps == [('start_prepare', a)] and db.get_task(pool)['auto_run'] == 0
    # 准备中（任务在跑）：其余排队
    fake_steps.busy.add(a)
    fake_steps.clear()
    autopilot.tick()
    assert fake_steps == []
    # 准备好进入待做题、Claude Code 还没开始做：启动做题，仍占准备线
    fake_steps.busy.discard(a)
    db.update_task(a, status=P.S_DO)
    autopilot.tick()
    assert fake_steps == [('start_auto_do', a)]
    # prompt 发出后准备下一道
    fake_steps.busy.add(a)
    claude_driver._mark_started(a, True)
    fake_steps.clear()
    autopilot.tick()
    assert fake_steps == [('start_prepare', b)]
    assert db.get_task(c)['auto_run'] == 1 and db.get_task(pool)['auto_run'] == 0
    # 排队中的题在工具栏的准备线显示时排在正在准备的题后面
    fake_steps.busy.add(b)
    assert [x['id'] for x in autopilot.status()['do']['preparing']] == [b, c]


def test_tick_paused_prep_does_not_block_queue(data_dir, fake_steps):
    """停在待准备环境的题不占准备线，排队的下一道照常开始准备。"""
    e = new_task(status=P.S_ENV, auto_run=1, materials=[{'path': 'a.png', 'state': 'missing', 'note': ''}],
                 env_check=[], net_check=[], client_check={'items': []})
    nxt = new_task(auto_run=1)
    autopilot.tick()
    assert db.get_task(e)['auto_pause'] and ('start_prepare', nxt) in fake_steps


def test_run_do_kills_leftover_session_before_reset(data_dir, monkeypatch):
    """上次残留的做题会话先关闭，再作废会话、恢复初始快照。"""
    order = []
    tid = new_task(status=P.S_DO, auto_run=1)
    monkeypatch.setattr(claude_driver, 'alive', lambda name: 'kill' not in order)
    monkeypatch.setattr(claude_driver, 'kill', lambda name: order.append('kill'))
    monkeypatch.setattr(trajectory, 'list_sessions', lambda home, d: ['old'])

    def fake_reset(t, log):
        order.append('reset')
        raise claude_driver.DriverError('到此为止')

    monkeypatch.setattr(P, 'void_and_reset', fake_reset)
    with pytest.raises(claude_driver.DriverError):
        claude_driver.run_do(tid, lambda m: None, threading.Event())
    assert order == ['kill', 'reset']


def test_tick_respects_parallel_cap(data_dir, fake_steps):
    from app import config
    config.save({'do_parallel_max': 2})
    a, b = new_task(status=P.S_DO, auto_run=1), new_task(status=P.S_DO, auto_run=1)
    pool = new_task()
    fake_steps.busy.update({a, b})
    claude_driver._mark_started(a, True)
    claude_driver._mark_started(b, True)
    autopilot.tick()
    # 两个会话都在做题，已到上限，不取新题
    assert db.get_task(pool)['auto_run'] == 0
    # 已满时人工重试回到待做题的题先等着，不启动第三个会话
    w = new_task(status=P.S_DO, auto_run=1)
    autopilot.tick()
    assert ('start_auto_do', w) not in fake_steps
    fake_steps.busy.discard(b)
    db.update_task(b, status=P.S_COLLECT)
    autopilot.tick()
    assert ('start_auto_do', w) in fake_steps


def test_tick_without_pick(data_dir, fake_steps):
    pool = new_task()
    autopilot.tick(pick=False)
    assert db.get_task(pool)['auto_run'] == 0 and fake_steps == []


@pytest.fixture()
def fast_loop(monkeypatch, fake_steps):
    """调度线程每轮间隔缩短，工具检查直接通过。"""
    from app import envcheck
    monkeypatch.setattr(autopilot, 'TICK_SEC', 0.05)
    monkeypatch.setattr(envcheck, 'auto_tools_check', lambda audio: [])
    return fake_steps


def test_finish_mode_completes_current_then_stops(data_dir, fast_loop):
    """做完当前题后停止：不取新题，已接管的题继续推进，完成后自动关闭。"""
    cur = new_task(status=P.S_REVIEW, auto_run=1)
    pool = new_task()
    loop = autopilot.DoLoop()
    loop.finish()
    assert loop.state == 'finishing'
    assert wait(lambda: ('start_review', cur) in fast_loop)
    time.sleep(0.2)
    assert loop.state == 'finishing' and db.get_task(pool)['auto_run'] == 0
    # 当前题走完：推进时释放接管标记，随后调度关闭
    db.update_task(cur, status=P.S_DONE)
    assert wait(lambda: loop.state == 'idle')
    assert db.get_task(cur)['auto_run'] == 0 and db.get_task(pool)['auto_run'] == 0


def test_finish_mode_stops_when_remaining_paused(data_dir, fast_loop):
    cur = new_task(status=P.S_FAIL, failed_from=P.S_COLLECT, auto_run=1, last_error='collect：轨迹校验不通过')
    loop = autopilot.DoLoop()
    loop.finish()
    # 失败的题被停下后，没有未完成的题，调度关闭；停下的题保留接管标记待处理
    assert wait(lambda: loop.state == 'idle')
    t = db.get_task(cur)
    assert t['auto_pause'] and t['auto_run'] == 1


def test_mode_switches(data_dir, fast_loop):
    new_task(status=P.S_REVIEW, auto_run=1)
    loop = autopilot.DoLoop()
    loop.start()
    with pytest.raises(P.ActionError):
        loop.start()
    loop.finish()
    assert loop.state == 'finishing'
    # 做完当前题后停止时点开始，恢复正常取题
    loop.start()
    assert loop.state == 'running'
    loop.stop()
    assert loop.state == 'stopping'
    assert wait(lambda: loop.state == 'idle')


def test_auto_do_finish_api(client, monkeypatch):
    from app import envcheck
    monkeypatch.setattr(envcheck, 'auto_tools_check', lambda audio: [])
    started = threading.Event()
    monkeypatch.setattr(autopilot.do_loop, '_loop', lambda: started.set())
    r = client.post('/api/auto/do', json={'action': 'finish'}).json()
    assert r['do']['state'] == 'finishing' and started.wait(2)
    assert client.post('/api/auto/do', json={'action': 'stop'}).json()['do']['state'] == 'stopping'
    autopilot.do_loop.state = 'idle'


def test_tick_respects_backlog(data_dir, fake_steps):
    from app import config
    config.save({'auto_backlog_max': 2})
    for _ in range(2):
        new_task(status=P.S_REVIEW, auto_run=1)
    pool = new_task()
    autopilot.tick()
    assert db.get_task(pool)['auto_run'] == 0
    config.save({'auto_backlog_max': 3})
    autopilot.tick()
    assert db.get_task(pool)['auto_run'] == 1


def test_tick_pauses_on_failure_and_env_blockers(data_dir, fake_steps):
    f = new_task(status=P.S_FAIL, failed_from=P.S_COLLECT, auto_run=1, last_error='collect：轨迹校验不通过：x')
    autopilot.tick()
    p = db.get_task(f)['auto_pause']
    assert p['step'] == P.S_COLLECT and '轨迹校验不通过' in p['reason']
    assert any('重做' in h for h in p['hints'])
    # 出错只停下这道题：停下的题不占准备线与积压名额，照常取下一道
    from app import config
    config.save({'auto_backlog_max': 1})
    pool = new_task()
    autopilot.tick()
    assert db.get_task(pool)['auto_run'] == 1 and ('start_prepare', pool) in fake_steps
    autopilot.release(pool)
    autopilot.release(f)
    e = new_task(status=P.S_ENV, auto_run=1, materials=[{'path': 'a.png', 'state': 'missing', 'note': ''}],
                 env_check=[], net_check=[], client_check={'items': []})
    autopilot.tick()
    p = db.get_task(e)['auto_pause']
    assert p['step'] == P.S_ENV and 'a.png' in p['reason']


def test_tick_resumes_interrupted_prepare(data_dir, fake_steps):
    """待准备环境但没有待处理项（准备中途被打断）：接着准备，不停下。"""
    never = new_task(status=P.S_ENV, auto_run=1)  # 还没跑到环境检查
    autopilot.tick()
    assert ('start_prepare', never) in fake_steps and not db.get_task(never)['auto_pause']
    autopilot.release(never)
    fake_steps.clear()
    done = new_task(status=P.S_ENV, auto_run=1, materials=[], env_check=[], net_check=[],
                    client_check={'items': []})
    autopilot.tick()
    assert fake_steps == [('start_prepare', done)] and not db.get_task(done)['auto_pause']


def test_do_loop_clears_error_after_successful_tick(data_dir, fast_loop, monkeypatch):
    calls = {'n': 0}
    real_tick = autopilot.tick

    def flaky(pick=True):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('偶发错误')
        return real_tick(pick)

    monkeypatch.setattr(autopilot, 'tick', flaky)
    loop = autopilot.DoLoop()
    loop.start()
    assert wait(lambda: calls['n'] >= 3)
    assert loop.error == ''
    loop.stop()
    assert wait(lambda: loop.state == 'idle')


def test_tick_auto_check(data_dir, fake_steps):
    good = dict(delivery_score=3, reproducibility=P.REPRO_LEVELS[0], feedback='', score_reason='x',
                record_steps=[{'action': '打开页面点渲染', 'command': '', 'expect': '画面全黑'}],
                record_prep={'setup': 'npm i', 'cleanup': ''})
    ok = new_task(status=P.S_CHECK, auto_run=1, **good)
    bad = new_task(status=P.S_CHECK, auto_run=1, **{**good, 'record_steps': []})
    autopilot.tick()
    assert db.get_task(ok)['status'] == P.S_RECORD and not db.get_task(ok)['auto_pause']
    p = db.get_task(bad)['auto_pause']
    assert db.get_task(bad)['status'] == P.S_CHECK and '没有录屏步骤' in p['reason']


def test_tick_records_one_at_a_time_and_releases_done(data_dir, fake_steps):
    r1 = new_task(status=P.S_RECORD, auto_run=1)
    new_task(status=P.S_RECORD, auto_run=1)
    d = new_task(status=P.S_DONE, auto_run=1)
    autopilot.tick()
    recs = [c for c in fake_steps if c[0] == 'start_auto_record']
    assert recs == [('start_auto_record', r1)]
    assert db.get_task(d)['auto_run'] == 0


def test_manual_action_clears_pause_and_retry(client, monkeypatch):
    tid = new_task(status=P.S_FAIL, failed_from=P.S_DO, auto_run=1, last_error='auto-do：模型在向人提问')
    autopilot.pause(db.get_task(tid), '模型在向人提问')
    assert db.get_task(tid)['auto_pause']
    r = client.post(f'/api/tasks/{tid}/action/auto_retry', json={})
    assert r.status_code == 200
    t = db.get_task(tid)
    # 做题环节失败：回到待做题，由调度器重新做题（自动做题会先作废残留会话）
    assert t['status'] == P.S_DO and not t['auto_pause']
    autopilot.pause(t, 'x')
    monkeypatch.setattr(P, 'start_redo', lambda i: 'run-redo')
    client.post(f'/api/tasks/{tid}/action/redo', json={})
    assert not db.get_task(tid)['auto_pause']
    # 转人工与交给自动
    client.post(f'/api/tasks/{tid}/action/auto_release', json={})
    assert db.get_task(tid)['auto_run'] == 0
    client.post(f'/api/tasks/{tid}/action/auto_join', json={})
    assert db.get_task(tid)['auto_run'] == 1
    done = new_task(status=P.S_DONE)
    assert client.post(f'/api/tasks/{done}/action/auto_join', json={}).status_code == 400


def test_retry_regen_clears_record_script(client):
    tid = new_task(status=P.S_FAIL, failed_from=P.S_RECORD, auto_run=1, record_script={'key': 'k', 'script': 'x'})
    autopilot.pause(db.get_task(tid), 'auto-record：录屏脚本执行失败：找不到元素')
    assert any('重新生成录屏脚本' in h for h in db.get_task(tid)['auto_pause']['hints'])
    client.post(f'/api/tasks/{tid}/action/auto_retry', json={'regen_script': True})
    t = db.get_task(tid)
    assert t['status'] == P.S_RECORD and t['record_script'] is None and not t['auto_pause']


def test_auto_status_and_list_flags(client):
    tid = new_task(status=P.S_REVIEW, auto_run=1)
    new_task()
    autopilot.pause(db.get_task(tid), '验收失败')
    s = client.get('/api/auto').json()
    assert s['do']['state'] == 'idle' and s['do']['pool'] == 1
    # 停下的题列在需处理里，不计入后段积压
    assert [x['id'] for x in s['do']['paused']] == [tid] and s['do']['backlog'] == []
    assert s['do']['doing'] == [] and s['do']['doing_max'] == 10
    row = next(r for r in client.get('/api/tasks').json() if r['id'] == tid)
    assert row['auto_run'] == 1 and row['auto_paused'] is True
    d = client.get(f'/api/tasks/{tid}').json()
    assert d['do_attach'].startswith('tmux -L feedback attach -r -t fbdo-')


def test_do_loop_refuses_when_tools_missing(client, monkeypatch):
    from app import envcheck
    monkeypatch.setattr(envcheck, 'auto_tools_check',
                        lambda audio: [{'name': 'tmux', 'ok': False, 'detail': '缺失', 'fix': 'apt install tmux'}])
    r = client.post('/api/auto/do', json={'action': 'start'})
    assert r.status_code == 400 and 'tmux' in r.json()['detail']


def test_busy_task_blocks_status_only_actions(client, monkeypatch):
    tid = new_task(status=P.S_DO)
    monkeypatch.setattr(jobs, 'busy_tasks', lambda: {tid})
    r = client.post(f'/api/tasks/{tid}/action/mark_done', json={})
    assert r.status_code == 400 and '运行' in r.json()['detail']


# ---------- 录屏辅助 ----------

def test_script_key_changes_with_inputs():
    t = {'artifact_sha': 'a', 'record_steps': [{'action': '点'}], 'record_prep': {'setup': 'x'}}
    k = recorder.script_key(t)
    assert recorder.script_key(dict(t)) == k
    assert recorder.script_key({**t, 'artifact_sha': 'b'}) != k
    assert recorder.script_key({**t, 'record_steps': []}) != k


def test_check_video(tmp_path, monkeypatch):
    from app import submission
    f = tmp_path / 'a.mp4'
    with pytest.raises(recorder.RecordError):
        recorder.check_video(f, 90)
    f.write_bytes(b'x')
    monkeypatch.setattr(submission, '_probe', lambda p: {'duration': 61.0, 'width': 1280, 'height': 720})
    assert recorder.check_video(f, 90)['duration'] == 61.0
    monkeypatch.setattr(submission, '_probe', lambda p: {'duration': 95.0, 'width': 1920, 'height': 1080})
    with pytest.raises(recorder.RecordError, match='时长.*分辨率'):
        recorder.check_video(f, 90)


def test_record_manual_needed_from_cache(data_dir, monkeypatch):
    from app import envcheck
    monkeypatch.setattr(envcheck, 'record_tools_missing', lambda audio: [])
    t = dict(status=P.S_RECORD, artifact_sha='a', record_steps=[{'action': '点', 'command': '', 'expect': '黑'}],
             record_prep={'setup': '', 'cleanup': ''})
    tid = new_task(**t)
    key = recorder.script_key(db.get_task(tid))
    db.update_task(tid, record_script={'key': key, 'script': '', 'manual_needed': True, 'reason': '需要真显卡'})
    with pytest.raises(recorder.ManualNeeded, match='需要人工录屏'):
        recorder.run_record(tid, 'rk', lambda m: None, threading.Event())


def test_script_input_has_dryrun_and_paths(data_dir, tmp_path):
    tid = new_task(status=P.S_RECORD, feedback='渲染后画面全黑', delivery_score=2, lang_framework='TypeScript',
                   record_steps=[{'action': '点渲染', 'command': '', 'expect': '全黑'}],
                   record_prep={'setup': 'npm i && npm run dev &', 'cleanup': 'kill %1'},
                   evidence=[{'phenomenon': '全黑', 'command': 'x', 'output_excerpt': 'y', 'recordable': True}])
    text = recorder.script_input(db.get_task(tid), tmp_path / 'rec', tmp_path / 'dryrun.sh', 'listening on 20100', 'rk1')
    assert 'dryrun.sh' in text and 'record.mjs' in text and '点渲染' in text and 'listening on 20100' in text
    assert 'kill %1' in text and '1280x720' in text


def test_complete_clears_old_error(data_dir):
    """自动录屏失败后重试成功：标记完成时清掉之前的报错。"""
    tid = new_task(status=P.S_RECORD, trajectory_path='/data/t.jsonl', last_error='auto-record：找不到文件')
    t = P.complete(tid, '/tmp/a.mp4')
    assert t['status'] == P.S_DONE and t['last_error'] is None and t['failed_from'] is None


def test_auto_check_issues():
    t = {'delivery_score': 3, 'reproducibility': P.REPRO_LEVELS[0], 'feedback': '',
         'record_steps': [{'action': '执行 wsl.exe -d Ubuntu 打开', 'command': '', 'expect': ''}],
         'record_prep': {}, 'review_self_check': {'has_process': True, 'has_product': False, 'note': '缺产物'}}
    issues = P.auto_check_issues(t)
    assert any('wsl.exe' in i for i in issues) and any('产物' in i for i in issues)
    assert P.auto_check_issues({**t, 'delivery_score': None})[0].startswith('交付完整性')


def test_migrate_adds_auto_columns(data_dir):
    import sqlite3
    c = db.conn()
    cols = {r[1] for r in c.execute('PRAGMA table_info(qa_task)')}
    assert {'auto_run', 'auto_pause', 'record_script'} <= cols
    # 模拟只执行到 005 的存量库
    for col in ('auto_run', 'auto_pause', 'record_script'):
        c.execute(f'ALTER TABLE qa_task DROP COLUMN {col}')
    c.execute("DELETE FROM schema_version WHERE script = '006_auto.sql'")
    c.commit()
    db.reset_for_test()
    c = sqlite3.connect(db.db_path())
    db.migrate(c)
    assert {'auto_run', 'auto_pause', 'record_script'} <= {r[1] for r in c.execute('PRAGMA table_info(qa_task)')}
    c.close()


def test_migrate_updates_harness_and_os(data_dir):
    """存量库补执行 008：旧的 Harness 与操作系统取值改为新取值，其他取值不动。"""
    import sqlite3
    old = new_task(harness='Claude Code', os='MacOS/Linux')
    other = new_task(harness='Codex CLI', os='Windows')
    c = db.conn()
    c.execute("DELETE FROM schema_version WHERE script = '008_harness_os.sql'")
    c.commit()
    db.reset_for_test()
    c = sqlite3.connect(db.db_path())
    db.migrate(c)
    c.close()
    t, o = db.get_task(old), db.get_task(other)
    assert (t['harness'], t['os']) == ('Claude Code CLI', 'Linux')
    assert (o['harness'], o['os']) == ('Codex CLI', 'Windows')


def test_harness_file_exists():
    assert Path(recorder.HARNESS).is_file()


def test_windows_cursor_theme(tmp_path):
    """光标主题：Xcursor 文件头与图像块、热点、常用光标名齐全；箭头白色填充黑色描边。"""
    import struct
    from app import wincursor
    root = wincursor.ensure_theme(tmp_path)
    d = root / wincursor.THEME / 'cursors'
    for name in ('left_ptr', 'default', 'pointer', 'hand2', 'xterm', 'text'):
        assert (d / name).is_file(), name
    data = (d / 'hand2').read_bytes()
    magic, hsize, _, ntoc = struct.unpack('<4sIII', data[:16])
    assert magic == b'Xcur' and hsize == 16 and ntoc == 1
    _, size, pos = struct.unpack('<III', data[16:28])
    w, h, xhot, yhot = struct.unpack('<IIII', data[pos + 16:pos + 32])
    assert size == w == h == wincursor.SIZE and (xhot, yhot) == (5, 0)
    assert len(data) == pos + 36 + w * h * 4
    px = wincursor.render(wincursor.ARROW)
    assert px[0] == wincursor.BLACK and px[2 * wincursor.SIZE + 1] == wincursor.WHITE
