# -*- coding: utf-8 -*-
"""SQLite 访问层：建表迁移、题目与运行记录的读写、每日备份。"""
import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

from . import config

SQL_DIR = Path(__file__).resolve().parent / 'sql'
OPERATOR = 'pipeline'

# 以 JSON 文本存储的字段
JSON_FIELDS = {
    'acceptance_points', 'materials', 'env_requirements', 'ignored_before', 'client_check',
    'env_check', 'net_check', 'voided_sessions', 'traj_summary', 'evidence', 'record_steps',
    'review_self_check',
}

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now() -> str:
    """本地时间字符串，精确到秒。"""
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def db_path() -> Path:
    return config.DATA_DIR / 'pipeline.db'


def conn() -> sqlite3.Connection:
    """全局单连接，配合锁串行写入。"""
    global _conn
    with _lock:
        if _conn is None:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(db_path(), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute('PRAGMA journal_mode=WAL')
            migrate(_conn)
        return _conn


def reset_for_test():
    """测试用：关闭连接，下次访问时按新的 DATA_DIR 重新打开。"""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


def migrate(c: sqlite3.Connection):
    """按文件名顺序执行未执行过的初始化与增量脚本。"""
    c.executescript((SQL_DIR / '001_init.sql').read_text(encoding='utf-8'))
    done = {r[0] for r in c.execute('SELECT script FROM schema_version WHERE deleted = 0')}
    for f in sorted(SQL_DIR.glob('*.sql')):
        if f.name in done:
            continue
        if f.name != '001_init.sql':
            c.executescript(f.read_text(encoding='utf-8'))
        t = now()
        c.execute('INSERT INTO schema_version(script, creator, create_time, updater, update_time) '
                  'VALUES (?, ?, ?, ?, ?)', (f.name, OPERATOR, t, OPERATOR, t))
    c.commit()


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        if d[k]:
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        out[k] = json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v
    return out


def insert_task(fields: dict) -> int:
    t = now()
    f = _encode(fields)
    f.update(creator=OPERATOR, create_time=t, updater=OPERATOR, update_time=t)
    cols = ', '.join(f)
    marks = ', '.join('?' for _ in f)
    with _lock:
        c = conn()
        cur = c.execute(f'INSERT INTO qa_task({cols}) VALUES ({marks})', list(f.values()))
        c.commit()
        return cur.lastrowid


def update_task(task_id: int, **fields) -> dict:
    if not fields:
        return get_task(task_id)
    f = _encode(fields)
    f.update(updater=OPERATOR, update_time=now())
    sets = ', '.join(f'{k} = ?' for k in f)
    with _lock:
        c = conn()
        c.execute(f'UPDATE qa_task SET {sets} WHERE id = ?', [*f.values(), task_id])
        c.commit()
    return get_task(task_id)


def get_task(task_id: int) -> dict | None:
    with _lock:
        return _decode(conn().execute('SELECT * FROM qa_task WHERE id = ? AND deleted = 0',
                                      (task_id,)).fetchone())


def find_task_by_branch(branch: str) -> dict | None:
    with _lock:
        return _decode(conn().execute('SELECT * FROM qa_task WHERE branch = ? AND deleted = 0',
                                      (branch,)).fetchone())


def list_tasks(status: str | None = None) -> list[dict]:
    sql = 'SELECT * FROM qa_task WHERE deleted = 0'
    args: list = []
    if status:
        sql += ' AND status = ?'
        args.append(status)
    sql += ' ORDER BY id DESC'
    with _lock:
        return [_decode(r) for r in conn().execute(sql, args).fetchall()]


def delete_task(task_id: int):
    """逻辑删除（分支名唯一索引只约束未删除记录）。"""
    with _lock:
        c = conn()
        c.execute('UPDATE qa_task SET deleted = 1, updater = ?, update_time = ? WHERE id = ?',
                  (OPERATOR, now(), task_id))
        c.commit()


def max_seq() -> int:
    with _lock:
        r = conn().execute('SELECT MAX(seq) FROM qa_task WHERE deleted = 0').fetchone()
        return r[0] or 0


def insert_run(run_key: str, kind: str, task_id: int | None, log_path: str) -> int:
    t = now()
    with _lock:
        c = conn()
        cur = c.execute(
            'INSERT INTO qa_run(run_key, task_id, kind, status, log_path, started_at, '
            'creator, create_time, updater, update_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (run_key, task_id, kind, 'running', log_path, t, OPERATOR, t, OPERATOR, t))
        c.commit()
        return cur.lastrowid


def finish_run(run_key: str, status: str, error: str | None = None):
    t = now()
    with _lock:
        c = conn()
        c.execute('UPDATE qa_run SET status = ?, error = ?, finished_at = ?, updater = ?, '
                  'update_time = ? WHERE run_key = ?', (status, error, t, OPERATOR, t, run_key))
        c.commit()


def get_run(run_key: str) -> dict | None:
    with _lock:
        r = conn().execute('SELECT * FROM qa_run WHERE run_key = ?', (run_key,)).fetchone()
        return dict(r) if r else None


def list_runs(task_id: int | None = None, limit: int = 50) -> list[dict]:
    sql = 'SELECT * FROM qa_run WHERE deleted = 0'
    args: list = []
    if task_id is not None:
        sql += ' AND task_id = ?'
        args.append(task_id)
    sql += ' ORDER BY id DESC LIMIT ?'
    args.append(limit)
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def mark_stale_runs():
    """服务重启时，把仍处于 running 的记录标记为失败。"""
    with _lock:
        c = conn()
        c.execute("UPDATE qa_run SET status = 'failed', error = '服务重启，运行中断', "
                  "finished_at = ? WHERE status = 'running'", (now(),))
        c.commit()


def backup(keep: int = 14) -> Path:
    """用 SQLite 在线备份接口备份数据库，保留最近 keep 份。"""
    bdir = config.DATA_DIR / 'backups'
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"pipeline-{dt.date.today():%Y%m%d}.db"
    with _lock:
        target = sqlite3.connect(dest)
        conn().backup(target)
        target.close()
    files = sorted(bdir.glob('pipeline-*.db'))
    for f in files[:-keep]:
        f.unlink(missing_ok=True)
    return dest
