# -*- coding: utf-8 -*-
"""后台任务：线程池执行、AI 环节全局并发上限、同一题目互斥、日志落盘。"""
import datetime as dt
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config, db

_pool = ThreadPoolExecutor(max_workers=8)
_task_locks: set[int] = set()
_locks_guard = threading.Lock()
_ai_sem_size = None
_ai_sem: threading.BoundedSemaphore | None = None
_stop_flags: dict[str, threading.Event] = {}


class Busy(RuntimeError):
    pass


def _ai_semaphore() -> threading.BoundedSemaphore:
    """按配置的并发数创建信号量（配置变化后对新任务生效）。"""
    global _ai_sem, _ai_sem_size
    n = max(1, int(config.load()['concurrency']))
    if _ai_sem is None or n != _ai_sem_size:
        _ai_sem, _ai_sem_size = threading.BoundedSemaphore(n), n
    return _ai_sem


def new_run_key(kind: str) -> str:
    return f"{dt.datetime.now():%Y%m%d-%H%M%S}-{kind}-{uuid.uuid4().hex[:6]}"


def log_dir():
    d = config.DATA_DIR / 'logs'
    d.mkdir(parents=True, exist_ok=True)
    return d


def submit(kind: str, task_id: int | None, fn, ai: bool = False) -> str:
    """提交后台任务。fn(log, run_key, stop_flag) 抛异常即视为失败。返回运行 ID。"""
    if task_id is not None:
        with _locks_guard:
            if task_id in _task_locks:
                raise Busy('该题目已有任务在运行')
            _task_locks.add(task_id)
    run_key = new_run_key(kind)
    log_path = log_dir() / f'{run_key}.log'
    db.insert_run(run_key, kind, task_id, str(log_path))
    stop = threading.Event()
    _stop_flags[run_key] = stop

    def log(msg: str):
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"{dt.datetime.now():%H:%M:%S} {msg}\n")

    def _run():
        sem = _ai_semaphore() if ai else None
        try:
            if sem:
                log('等待并发名额')
                sem.acquire()
            log(f'开始 {kind}')
            fn(log, run_key, stop)
            db.finish_run(run_key, 'success')
            log('完成')
        except Exception as e:  # noqa: BLE001 统一记录失败原因
            log('失败 ' + str(e))
            log(traceback.format_exc()[-3000:])
            db.finish_run(run_key, 'failed', str(e)[:2000])
            if task_id is not None:
                t = db.get_task(task_id)
                if t and t['status'] != '失败':
                    db.update_task(task_id, status='失败', failed_from=t['status'],
                                   last_error=f'{kind}：{e}'[:2000])
                elif t:
                    db.update_task(task_id, last_error=f'{kind}：{e}'[:2000])
        finally:
            if sem:
                sem.release()
            _stop_flags.pop(run_key, None)
            if task_id is not None:
                with _locks_guard:
                    _task_locks.discard(task_id)

    _pool.submit(_run)
    return run_key


def stop(run_key: str) -> bool:
    f = _stop_flags.get(run_key)
    if f:
        f.set()
        return True
    return False


def busy_tasks() -> set[int]:
    with _locks_guard:
        return set(_task_locks)
