# -*- coding: utf-8 -*-
"""Codex 通道：官方额度用完时切换到三方 API、到点切回官方，以及只用官方、只用三方。"""
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import autopilot, codex_runner, config

NOW = datetime(2026, 11, 20, 23, 30)
LIMIT_EVENT = ('{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit. Upgrade to Pro '
               '(https://chatgpt.com/explore/pro), visit https://chatgpt.com/settings/usage to purchase more '
               'credits or try again in 3 hours."}}')
FALLBACK_CFG = {'codex_channel_mode': 'auto', 'codex_fallback_base_url': 'https://relay.example.com/v1/',
                'codex_fallback_key': 'sk-relay-secret', 'codex_fallback_model': 'gpt-relay'}


def test_parse_reset_time():
    p = codex_runner.parse_reset_time
    # 只有钟点：还没到的是今天，已过去的是明天
    assert p('try again at 2:21 AM.', NOW) == datetime(2026, 11, 21, 2, 21)
    assert p('try again at 11:45 PM', NOW) == datetime(2026, 11, 20, 23, 45)
    assert p('try again at 12:05 PM', NOW) == datetime(2026, 11, 21, 12, 5)
    assert p('try again at 12:05 AM', NOW) == datetime(2026, 11, 21, 0, 5)
    # 刚过去几分钟（时钟误差）仍按今天算，不会多等一天
    assert p('try again at 11:25 PM', NOW) == datetime(2026, 11, 20, 23, 25)
    # 周额度：带日期
    assert p('purchase more credits or try again at Nov 24th, 2026 2:41 PM.', NOW) == datetime(2026, 11, 24, 14, 41)
    assert p('try again at Dec 1st, 2026 9:05 AM', NOW) == datetime(2026, 12, 1, 9, 5)
    # 相对时长
    assert p('Please try again in 2 hours 5 minutes.', NOW) == NOW + timedelta(hours=2, minutes=5)
    assert p('try again in 3 days', NOW) == NOW + timedelta(days=3)
    assert p('usage limit, try again later', NOW) is None
    assert p('', NOW) is None


def _cfg(**kw):
    return {**config.DEFAULTS, **FALLBACK_CFG, **kw}


def test_channel_switch_and_reset(data_dir):
    cfg = _cfg()
    assert codex_runner.channel_status(cfg, NOW)['channel'] == 'official'
    until = codex_runner.mark_official_blocked(cfg, 'ERROR: usage limit ... try again at 2:21 AM.', NOW)
    assert until == datetime(2026, 11, 21, 2, 22)
    s = codex_runner.channel_status(cfg, NOW)
    assert s['channel'] == 'fallback' and s['official_blocked_until'] == '2026-11-21 02:22'
    assert 'usage limit' in s['reason']
    # 到点后回到官方
    assert codex_runner.channel_status(cfg, datetime(2026, 11, 21, 2, 22))['channel'] == 'official'
    # 三方没填完整或只用官方时仍用官方，但显示官方额度用完；只用三方时始终走三方
    s = codex_runner.channel_status(_cfg(codex_fallback_key=''), NOW)
    assert s['channel'] == 'official' and s['official_blocked']
    s = codex_runner.channel_status(_cfg(codex_channel_mode='official'), NOW)
    assert s['channel'] == 'official' and s['official_blocked'] and s['mode'] == 'official'
    assert codex_runner.channel_status(_cfg(codex_channel_mode='fallback'), datetime(2026, 11, 22))['channel'] == 'fallback'
    # 未知取值按官方优先处理
    assert codex_runner.channel_status(_cfg(codex_channel_mode='x'), NOW)['mode'] == 'auto'
    # 并发运行先后报限流：不会把更晚的恢复时间改早
    codex_runner.mark_official_blocked(cfg, 'try again at Nov 24th, 2026 2:41 PM', NOW)
    codex_runner.mark_official_blocked(cfg, 'try again at 2:21 AM', NOW)
    assert codex_runner.channel_status(cfg, NOW)['official_blocked_until'] == '2026-11-24 14:42'
    # 人工切回官方
    codex_runner.reset_channel()
    assert codex_runner.channel_status(cfg, NOW)['channel'] == 'official'
    # 解析不出时间时按默认时长
    until = codex_runner.mark_official_blocked(_cfg(codex_fallback_retry_min=30), 'rate limit', NOW)
    assert until == NOW + timedelta(minutes=30)


def test_fallback_args_and_env(monkeypatch):
    import tomllib
    cfg = _cfg()
    args = codex_runner.fallback_args(cfg)
    assert args[:2] == ['-m', 'gpt-relay']
    kv = dict(args[i + 1].split('=', 1) for i in range(2, len(args), 2))
    back = {k: tomllib.loads(f'v = {v}')['v'] for k, v in kv.items()}
    p = f'model_providers.{codex_runner.FALLBACK_PROVIDER}'
    assert back['model_provider'] == codex_runner.FALLBACK_PROVIDER
    assert back[f'{p}.base_url'] == 'https://relay.example.com/v1'
    assert back[f'{p}.wire_api'] == 'responses'
    assert back[f'{p}.env_key'] == codex_runner.FALLBACK_KEY_ENV
    # key 不出现在命令行里
    cmd = codex_runner.build_cmd(cfg, 'qa-review', 'r1', 'fallback')
    assert not any('sk-relay-secret' in c for c in cmd)
    assert 'model_reasoning_effort="xhigh"' in cmd
    assert codex_runner.build_cmd(cfg, 'qa-review', 'r1')[cmd.index('-m') + 1] == config.DEFAULTS['model']
    # key 经环境变量与 WSLENV 交给 Windows 进程，原有的 WSLENV 保留
    monkeypatch.setenv('WSLENV', 'FOO/p:' + codex_runner.FALLBACK_KEY_ENV)
    env = codex_runner.fallback_env(cfg)
    assert env[codex_runner.FALLBACK_KEY_ENV] == 'sk-relay-secret'
    assert env['WSLENV'] == 'FOO/p:' + codex_runner.FALLBACK_KEY_ENV
    monkeypatch.delenv('WSLENV')
    assert codex_runner.fallback_env(cfg)['WSLENV'] == codex_runner.FALLBACK_KEY_ENV


class _Proc:
    """假的 codex 进程：按给定事件输出；成功时写出结果文件。"""

    def __init__(self, lines, code, result_file=None, result=None):
        self.lines, self.code, self.result_file, self.result = lines, code, result_file, result
        self.stdin = io.BytesIO()
        self.stderr = io.BytesIO(b'')
        self.stdout = iter([(s + '\n').encode('utf-8') for s in lines])
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self):
        if self.returncode is None:
            if self.code == 0 and self.result_file:
                self.result_file.write_text(json.dumps(self.result), encoding='utf-8')
            self.returncode = self.code
        return self.returncode


@pytest.fixture()
def workspace(data_dir, tmp_path):
    ws = tmp_path / 'ws'
    (ws / 'schemas').mkdir(parents=True)
    (ws / 'schemas' / 'smoke.json').write_text(json.dumps(
        {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}), encoding='utf-8')
    config.save({'agent_workspace_wsl': str(ws), **FALLBACK_CFG})
    return ws


def _patch_popen(monkeypatch, ws, plan):
    """plan：每次启动依次返回的 (事件, 退出码)；记录每次的命令与环境变量。"""
    calls = []

    def fake(cmd, **kw):
        lines, code = plan[len(calls)]
        run_key = cmd[cmd.index('-o') + 1].split('\\')[-2]
        calls.append({'cmd': cmd, 'env': kw.get('env')})
        return _Proc(lines, code, ws / 'runs' / run_key / 'result.json', {'ok': True})

    monkeypatch.setattr(codex_runner.subprocess, 'Popen', fake)
    monkeypatch.setattr(codex_runner, '_kill_windows', lambda k: None)
    return calls


def test_run_skill_switches_to_fallback(workspace, monkeypatch):
    calls = _patch_popen(monkeypatch, workspace, [([LIMIT_EVENT], 1), ([], 0), ([], 0)])
    logs = []
    assert codex_runner.run_skill('smoke', '输入', 'r1', logs.append) == {'ok': True}
    # 官方报限流后当场用三方重跑，key 只在环境变量里
    assert len(calls) == 2
    assert calls[0]['env'] is None and 'model_provider="feedback_fallback"' not in calls[0]['cmd']
    assert 'model_provider="feedback_fallback"' in calls[1]['cmd']
    assert calls[1]['env'][codex_runner.FALLBACK_KEY_ENV] == 'sk-relay-secret'
    assert not any('sk-relay-secret' in s for s in logs)
    assert any('本次立即用三方重跑' in s for s in logs)
    st = codex_runner.channel_status()
    assert st['channel'] == 'fallback' and st['official_blocked']
    # 恢复时间之前的新运行直接走三方
    assert codex_runner.run_skill('smoke', '输入', 'r2', logs.append) == {'ok': True}
    assert len(calls) == 3 and 'model_provider="feedback_fallback"' in calls[2]['cmd']
    # 全自动状态里带上通道
    assert autopilot.status()['codex']['channel'] == 'fallback'


def test_run_skill_fallback_failure_is_labeled(workspace, monkeypatch):
    calls = _patch_popen(monkeypatch, workspace, [([LIMIT_EVENT], 1), ([LIMIT_EVENT], 1)])
    with pytest.raises(codex_runner.CodexError) as e:
        codex_runner.run_skill('smoke', '输入', 'r1', lambda s: None)
    # 三方也限流：不再重试，原因里注明三方 API，处理建议里提示检查三方
    assert len(calls) == 2 and str(e.value).startswith('三方 API：额度限流')
    hints = autopilot.hints_for('待验收', str(e.value))
    assert any('三方 API' in h for h in hints) and any('额度恢复' in h for h in hints)


def test_run_skill_without_fallback_keeps_old_behavior(workspace, monkeypatch):
    config.save({'codex_channel_mode': 'official'})
    calls = _patch_popen(monkeypatch, workspace, [([LIMIT_EVENT], 1)])
    with pytest.raises(codex_runner.RateLimitError, match='^额度限流'):
        codex_runner.run_skill('smoke', '输入', 'r1', lambda s: None)
    # 只用官方时也不记录切换
    assert len(calls) == 1 and not codex_runner.channel_status()['official_blocked']
    # 其他失败不切换
    config.save({'codex_channel_mode': 'auto'})
    calls = _patch_popen(monkeypatch, workspace, [(['{"type":"turn.failed","error":{"message":"boom"}}'], 2)])
    with pytest.raises(codex_runner.CodexError, match='退出码 2'):
        codex_runner.run_skill('smoke', '输入', 'r2', lambda s: None)
    assert len(calls) == 1


def test_run_skill_fallback_only(workspace, monkeypatch):
    # 只用三方：直接走三方，不经过官方，也不记录官方额度用完
    config.save({'codex_channel_mode': 'fallback'})
    calls = _patch_popen(monkeypatch, workspace, [([], 0)])
    assert codex_runner.run_skill('smoke', '输入', 'r1', lambda s: None) == {'ok': True}
    assert len(calls) == 1 and 'model_provider="feedback_fallback"' in calls[0]['cmd']
    assert not codex_runner.channel_status()['official_blocked']
    # 三方没填完整时启动前报错
    config.save({'codex_fallback_model': ''})
    with pytest.raises(codex_runner.CodexError, match='没有填完整'):
        codex_runner.run_skill('smoke', '输入', 'r2', lambda s: None)
    assert len(calls) == 1


def test_config_save_drops_defaults_and_stale_keys(data_dir):
    # 旧版本留下的配置项与等于默认值的项不落盘，读取时也忽略旧项
    config.CONFIG_FILE.write_text(json.dumps({'seq_base': 10, 'codex_follow_toml': False, 'port_base': 25000,
                                              'concurrency': 4}), encoding='utf-8')
    assert 'seq_base' not in config.load()
    config.save({'do_timeout_min': 480})
    assert json.loads(config.CONFIG_FILE.read_text(encoding='utf-8')) == {'concurrency': 4, 'do_timeout_min': 480}
    # 页面只展示 SETTINGS，固定项不在其中
    assert 'codex_path' not in config.SETTINGS and 'codex_path' in config.DEFAULTS


def test_config_api_hides_key(data_dir):
    from app.main import app
    with TestClient(app) as c:
        assert c.put('/api/config', json={'codex_channel_mode': 'x'}).status_code == 400
        r = c.put('/api/config', json={'codex_fallback_key': 'sk-abc', 'codex_channel_mode': 'auto'}).json()
        assert r['codex_fallback_key'] == '' and r['codex_fallback_key_set'] is True
        assert c.get('/api/config').json()['codex_fallback_key'] == ''
        # 留空保存不覆盖已存的 key
        c.put('/api/config', json={'codex_fallback_key': '', 'codex_fallback_model': 'm'})
        assert config.load()['codex_fallback_key'] == 'sk-abc'
        # 通道状态与切回官方
        codex_runner.mark_official_blocked(dict(config.load(), codex_fallback_base_url='u'),
                                           'try again in 2 hours')
        assert c.get('/api/codex/channel').json()['official_blocked'] is True
        assert c.post('/api/codex/channel/reset').json()['official_blocked'] is False
