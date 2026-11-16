# -*- coding: utf-8 -*-
"""全自动：补充题目池（不停出题）与自动做题调度器。

两个开关的状态只保存在内存里，后端重启后都是停止状态；题目上的接管标记（auto_run）保存在数据库，
重新开始自动做题后接着推进已接管的题。

自动做题的节奏：
- 做题线（待初始化、待准备环境、待做题、待收集，含在这些环节失败的）同时只有 1 道自动题；
- 验收受 AI 并发上限约束，录屏同时只录 1 道；
- 做题线空闲且后段（待验收、待查验、待录屏）积压少于配置的上限时，从题目池按 id 顺序取下一道。
任一环节出错，题目停下（auto_pause 记下环节、原因、处理建议与出错运行），不影响其他题继续推进。

两种关闭方式：
- 做完当前题后停止：不再取新题，已接管的题继续走完所有环节，全部完成或停下后自动关闭；
- 停止：不再取新题、不再启动下一环节，正在跑的环节跑完就关闭，已接管的题停在当前状态。
"""
import threading
import time
import traceback

from . import claude_driver, config, db, envcheck, jobs, pipeline as P, recorder

FRONT = {P.S_INIT, P.S_ENV, P.S_DO, P.S_COLLECT}
BACK = {P.S_REVIEW, P.S_CHECK, P.S_RECORD}
TICK_SEC = 5


def stage(t: dict) -> str:
    """题目所处的环节：失败的题按失败前的状态算。"""
    return (t.get('failed_from') or P.S_INIT) if t['status'] == P.S_FAIL else t['status']


def last_run_key(task_id: int) -> str | None:
    runs = db.list_runs(task_id, 1)
    return runs[0]['run_key'] if runs else None


# ---------- 停下时的处理建议 ----------

# 按失败原因匹配的建议：(原因里出现的文字, 建议)，自上而下取所有命中的
REASON_HINTS = [
    ('codex 认证失败', '检查 Windows 上 codex 的登录，或 CC Switch 当前供应商的 key 与地址，处理后点「重试」'),
    ('额度限流', '等 codex 额度恢复后点「重试」'),
    ('连续重连', '检查网络或代理，确认 codex 能连上供应商后点「重试」'),
    ('轨迹校验不通过', '这次做题的轨迹不能提交：点「重做」作废会话重新做题，自动流程会接着做'),
    ('首条输入', '点「重试」会作废这次会话并重新做题'),
    ('接口报错', '检查 WSL 中 Claude Code 的接入配置（用 attach 命令或手动开一个 claude 看报错），处理后点「重试」，会作废这次会话重新做题'),
    ('向人提问', '模型中途向人提问，按做题规则这次会话作废：点「重试」重新做题'),
    ('没有进入可输入状态', '在 WSL 终端手动执行一次做题命令，看 Claude Code 卡在什么提示（登录、更新、首次使用说明等），处理掉后点「重试」'),
    ('启动后立即退出', '在 WSL 终端手动执行一次做题命令看报错，处理后点「重试」'),
    ('会话意外退出', '点「重试」会作废这次会话并重新做题'),
    ('仍未做完', '做题超时：点「重试」重新做题，或在配置页调大做题超时'),
    ('分钟未完成', 'codex 运行超时：在配置页调大单次运行超时后点「重试」'),
    ('秒仍未结束', '录屏脚本超过时长：点「重新生成录屏脚本并重试」，或转人工录屏'),
    ('tmux', '执行 scripts/setup-verify-tools.sh 安装工具后点「重试」'),
    ('录屏工具缺失', '执行 scripts/setup-verify-tools.sh 安装工具（或在配置页关闭录制声音）后点「重试」'),
    ('录屏前准备失败', '查看日志里录屏前准备的输出；在录屏目录手动执行排查，必要时在「验收与 Feedback」里改录屏前准备后点「重试」，或转人工录屏'),
    ('需要人工录屏', '点「转人工」后照录屏卡片的步骤手动录制，选好录屏文件后标记完成'),
    ('录屏脚本执行失败', '查看日志与试跑截图；点「重新生成录屏脚本并重试」，或转人工录屏'),
    ('录屏文件不合格', '点「重新生成录屏脚本并重试」（让脚本缩短时长），或转人工录屏'),
    ('没有生成录屏文件', '查看日志里 ffmpeg 的输出后重试，或转人工录屏'),
    ('push', '检查 WSL 中 GitHub 的 ssh 凭据与网络后点「重试」'),
    ('推送', '检查 WSL 中 GitHub 的 ssh 凭据与网络后点「重试」'),
]

# 没有命中原因时按环节给的建议
STEP_HINTS = {
    P.S_INIT: '查看日志定位原因，处理后点「重试」',
    P.S_ENV: '按上面列出的问题逐项处理（需要人工放的素材、缺失的工具、显卡确认等），处理完点「重试」或「继续准备」',
    P.S_DO: '点「重试」会作废这次会话并重新做题',
    P.S_COLLECT: '查看日志：轨迹不合规时点「重做」重新做题，其他原因处理后点「重试」',
    P.S_REVIEW: '查看日志定位原因后点「重试」重新验收',
    P.S_CHECK: '在「验收与 Feedback」里修改后点「通过」，或填写打回意见重新验收；改完也可以直接点「重试」重新检查',
    P.S_RECORD: '查看日志定位原因后点「重试」，或点「转人工」手动录屏',
}
COMMON_HINT = '也可以点「转人工」，之后按手动流程处理这道题'


def hints_for(step: str, reason: str) -> list[str]:
    out = [h for mark, h in REASON_HINTS if mark in (reason or '')]
    if not out:
        out.append(STEP_HINTS.get(step, '查看日志定位原因，处理后点「重试」'))
    out.append(COMMON_HINT)
    return list(dict.fromkeys(out))


def pause(t: dict, reason: str, hints: list[str] | None = None, run_key: str | None = None) -> dict:
    step = stage(t)
    info = {'step': step, 'reason': (reason or '未知原因')[:3000], 'hints': hints or hints_for(step, reason),
            'run_key': run_key or last_run_key(t['id']), 'time': db.now()}
    return db.update_task(t['id'], auto_pause=info)


# ---------- 补充题目池 ----------

class GenLoop:
    """不停出题：一轮跑完再开下一轮；停止只在本轮结束后生效；出错即终止。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'idle'          # idle / running / stopping / failed
        self.rounds = 0
        self.added = 0
        self.current_run = None
        self.last_run = None
        self.error = ''
        self.started_at = None
        self._stop = threading.Event()

    def status(self) -> dict:
        with self.lock:
            return {'state': self.state, 'rounds': self.rounds, 'added': self.added,
                    'current_run': self.current_run, 'last_run': self.last_run, 'error': self.error,
                    'started_at': self.started_at, 'count': int(config.load()['pool_gen_count'])}

    def start(self) -> dict:
        with self.lock:
            if self.state in ('running', 'stopping'):
                raise P.ActionError('补充题目池已在运行')
            self.state, self.rounds, self.added, self.error = 'running', 0, 0, ''
            self.current_run, self.last_run, self.started_at = None, None, db.now()
            self._stop.clear()
        threading.Thread(target=self._loop, daemon=True, name='gen-loop').start()
        return self.status()

    def stop(self) -> dict:
        with self.lock:
            if self.state == 'running':
                self.state = 'stopping'
                self._stop.set()
        return self.status()

    def _params(self) -> dict:
        return {'domain': next(iter(P.DOMAINS)), 'count': int(config.load()['pool_gen_count']),
                'subdomain': '', 'difficulty': '', 'stack': '', 'extra': ''}

    def _finish(self, state: str, error: str = ''):
        with self.lock:
            self.state, self.error = state, error
            self.last_run = self.current_run or self.last_run
            self.current_run = None

    def _loop(self):
        empty = 0
        try:
            while not self._stop.is_set():
                run_key = P.start_gen(self._params())
                with self.lock:
                    self.current_run = run_key
                while (r := db.get_run(run_key)) and r['status'] == 'running':
                    time.sleep(3)
                if not r or r['status'] != 'success':
                    return self._finish('failed', (r or {}).get('error') or '出题运行失败')
                n = sum(1 for t in db.list_tasks() if t.get('gen_run_id') == run_key)
                with self.lock:
                    self.rounds += 1
                    self.added += n
                    self.last_run, self.current_run = run_key, None
                empty = empty + 1 if n == 0 else 0
                if empty >= 2:
                    return self._finish('failed', '连续两轮没有入库任何题目，查看出题日志')
            self._finish('idle')
        except Exception as e:  # noqa: BLE001 循环异常统一记为失败
            self._finish('failed', f'{e}\n{traceback.format_exc()[-1500:]}')


# ---------- 自动做题 ----------

class DoLoop:
    """自动做题调度器：后台线程每隔几秒推进一次所有接管的题。"""

    # 三种运行方式：
    #   running    正常推进已接管的题，并按规则从题目池取新题
    #   finishing  做完当前题后停止：不再取新题，已接管的题继续走完所有环节，全部完成或停下后自动关闭
    #   stopping   停止：不再取新题、不再启动下一环节，正在跑的环节跑完就关闭
    ACTIVE = ('running', 'finishing', 'stopping')

    def __init__(self):
        self.lock = threading.Lock()
        self.state = 'idle'
        self.error = ''
        self.started_at = None

    def _check_tools(self):
        missing = [i for i in envcheck.auto_tools_check(bool(config.load().get('record_audio'))) if not i['ok']]
        if missing:
            raise P.ActionError('自动做题需要的工具缺失：' +
                                '；'.join(f"{i['name']}（{i['fix']}）" for i in missing))

    def _launch(self, mode: str) -> dict:
        """未开启时检查工具后启动后台线程；已开启时只切换运行方式（线程每轮按当前方式推进）。"""
        with self.lock:
            if self.state in self.ACTIVE:
                self.state = mode
                return status()
        self._check_tools()
        with self.lock:
            if self.state in self.ACTIVE:
                self.state = mode
                return status()
            self.state, self.error, self.started_at = mode, '', db.now()
        threading.Thread(target=self._loop, daemon=True, name='do-loop').start()
        return status()

    def start(self) -> dict:
        """开始做题；做完当前题后停止、停止中时点开始，恢复为正常取题。"""
        with self.lock:
            if self.state == 'running':
                raise P.ActionError('自动做题已在运行')
        return self._launch('running')

    def finish(self) -> dict:
        """做完当前题后停止；未开启时直接以这种方式开启，只推进已接管的题。"""
        return self._launch('finishing')

    def stop(self) -> dict:
        with self.lock:
            if self.state in ('running', 'finishing'):
                self.state = 'stopping'
        return status()

    def _loop(self):
        while True:
            with self.lock:
                mode = self.state
            try:
                if mode == 'stopping':
                    # 停止后不再启动新环节，等已在跑的自动任务结束
                    if not auto_busy():
                        break
                else:
                    tick(pick=mode == 'running')
                    # 做完当前题后停止：已接管的题都完成（已释放）或停下，且没有在跑的任务时关闭
                    if mode == 'finishing' and not unfinished() and not auto_busy():
                        break
            except Exception as e:  # noqa: BLE001 单次推进出错不终止调度
                with self.lock:
                    self.error = f'{e}'[:500]
            time.sleep(TICK_SEC)
        with self.lock:
            self.state = 'idle'


def unfinished() -> list[dict]:
    """已接管且没停下的题（完成的题在推进时已释放接管标记）。"""
    return [t for t in auto_tasks() if not t.get('auto_pause')]


def auto_tasks() -> list[dict]:
    """接管的题，按 id 从小到大（先取的题先推进，录屏名额也先给它）。"""
    return sorted((t for t in db.list_tasks() if t.get('auto_run')), key=lambda t: t['id'])


def auto_busy() -> list[int]:
    busy = jobs.busy_tasks()
    return [t['id'] for t in auto_tasks() if t['id'] in busy]


def advance(t: dict, busy: set[int], recording: bool) -> bool:
    """按题目当前状态启动下一个环节。返回是否启动了录屏。"""
    if t['id'] in busy or t.get('auto_pause'):
        return False
    s = t['status']
    tid = t['id']
    try:
        if s == P.S_DONE:
            db.update_task(tid, auto_run=0)
        elif s == P.S_FAIL:
            pause(t, t.get('last_error') or '环节失败')
        elif s == P.S_INIT:
            P.start_prepare(tid)
        elif s == P.S_ENV:
            # 准备已经跑过仍停在这里，说明有需要人工处理的项
            blockers = P.env_blockers(t)
            pause(t, '准备环境有需要人工处理的项：\n' + '\n'.join(blockers or ['见环境检查']))
        elif s == P.S_DO:
            claude_driver.start_auto_do(tid)
        elif s == P.S_COLLECT:
            P.start_collect(tid)
        elif s == P.S_REVIEW:
            P.start_review(tid)
        elif s == P.S_CHECK:
            issues = P.auto_check_issues(t)
            if issues:
                pause(t, '自动查验未通过：\n' + '\n'.join(issues))
            else:
                P.approve(tid)
        elif s == P.S_RECORD:
            if recording:
                return False
            recorder.start_auto_record(tid)
            return True
    except jobs.Busy:
        pass
    except Exception as e:  # noqa: BLE001 启动环节出错时停下这道题
        pause(db.get_task(tid) or t, f'启动下一环节失败：{e}')
    return False


def tick(pick: bool = True):
    """推进一次：先推进已接管的题，再按规则从题目池取新题（pick 为 False 时不取新题）。"""
    cfg = config.load()
    busy = jobs.busy_tasks()
    tasks = auto_tasks()
    recording = any(t['status'] == P.S_RECORD and t['id'] in busy for t in tasks)
    for t in tasks:
        recording = advance(t, busy, recording) or recording
    if not pick:
        return
    tasks = auto_tasks()
    front = [t for t in tasks if stage(t) in FRONT]
    backlog = [t for t in tasks if stage(t) in BACK]
    if front or len(backlog) >= int(cfg['auto_backlog_max']):
        return
    nxt = next_pool_task()
    if nxt:
        t = db.update_task(nxt['id'], auto_run=1, auto_pause=None)
        advance(t, jobs.busy_tasks(), recording)


def next_pool_task() -> dict | None:
    pool = [t for t in db.list_tasks(P.S_INIT) if not t.get('auto_run')]
    return min(pool, key=lambda t: t['id']) if pool else None


# ---------- 单题操作 ----------

def retry(task_id: int, regen_script: bool = False):
    """清除停下状态并按环节重试；返回启动的运行 ID（交给调度器下一轮推进时返回 None）。"""
    t = P.need(db.get_task(task_id))
    if not t.get('auto_run'):
        raise P.ActionError('这道题不在自动流程中')
    fields = {'auto_pause': None}
    if regen_script:
        fields['record_script'] = None
    t = db.update_task(task_id, **fields)
    if t['status'] == P.S_FAIL:
        return P.retry(task_id)
    if t['status'] == P.S_ENV:
        return P.start_prepare(task_id)
    return None


def release(task_id: int) -> dict:
    """转人工：退出自动流程，之后按原来的手动流程处理。"""
    P.need(db.get_task(task_id))
    return db.update_task(task_id, auto_run=0, auto_pause=None)


def join(task_id: int) -> dict:
    """交给自动：未完成的题由调度器接着推进。"""
    t = P.need(db.get_task(task_id))
    if t['status'] in (P.S_DONE, P.S_REWORK):
        raise P.ActionError('已完成或返修中的题不能交给自动做题')
    return db.update_task(task_id, auto_run=1, auto_pause=None)


def clear_pause(task_id: int):
    """人工在详情里执行了原有操作：清除停下状态，自动流程从新状态接着走。"""
    t = db.get_task(task_id)
    if t and t.get('auto_pause'):
        db.update_task(task_id, auto_pause=None)


# ---------- 状态 ----------

gen_loop = GenLoop()
do_loop = DoLoop()


def brief(t: dict) -> dict:
    return {'id': t['id'], 'branch': t['branch'], 'status': t['status'], 'stage': stage(t),
            'paused': bool(t.get('auto_pause'))}


def status() -> dict:
    tasks = auto_tasks()
    busy = jobs.busy_tasks()
    with do_loop.lock:
        do = {'state': do_loop.state, 'error': do_loop.error, 'started_at': do_loop.started_at}
    do.update(
        front=[brief(t) for t in tasks if stage(t) in FRONT],
        backlog=[brief(t) for t in tasks if stage(t) in BACK],
        paused=[brief(t) for t in tasks if t.get('auto_pause')],
        running=[t['id'] for t in tasks if t['id'] in busy],
        pool=len([t for t in db.list_tasks(P.S_INIT) if not t.get('auto_run')]),
        backlog_max=int(config.load()['auto_backlog_max']),
    )
    return {'gen': gen_loop.status(), 'do': do}
