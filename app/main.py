# -*- coding: utf-8 -*-
"""FastAPI 入口：题目管理、环节操作、运行日志、配置、提交表与录屏文件。"""
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, db, jobs, pipeline as P, submission

@asynccontextmanager
async def lifespan(_app):
    """启动时打开数据库、清理中断的运行记录，并启动每日备份线程。"""
    db.conn()
    db.mark_stale_runs()

    def _backup_loop():
        while True:
            try:
                db.backup()
            except Exception:  # noqa: BLE001 备份失败不影响服务
                pass
            time.sleep(24 * 3600)

    threading.Thread(target=_backup_loop, daemon=True).start()
    yield


app = FastAPI(title='出题与评测流水线', lifespan=lifespan)
STATIC = Path(__file__).resolve().parent / 'static'


@app.middleware('http')
async def _no_stale_static(request, call_next):
    """页面资源每次都向后端确认是否有更新（未变化时返回 304），改了前端不用强制刷新。"""
    resp = await call_next(request)
    if not request.url.path.startswith('/api/'):
        resp.headers.setdefault('Cache-Control', 'no-cache')
    return resp

# 页面可编辑的字段
EDITABLE = {
    'user_prompt', 'domain', 'subdomain', 'difficulty', 'lang_framework', 'acceptance_points',
    'materials', 'env_requirements', 'remark', 'reproducibility', 'feedback', 'score_reason',
    'delivery_score', 'record_steps', 'record_prep', 'evidence', 'recording_link',
}


@app.exception_handler(P.ActionError)
@app.exception_handler(jobs.Busy)
async def _action_error(_, exc):
    return JSONResponse(status_code=400, content={'detail': str(exc)})


# ---------- 元信息与配置 ----------

@app.get('/api/meta')
def meta():
    return {'statuses': P.STATUSES, 'domains': P.DOMAINS, 'repro_levels': P.REPRO_LEVELS}


@app.get('/api/config')
def get_config():
    return config.load()


@app.put('/api/config')
def put_config(body: dict = Body(...)):
    return config.save(body)


# ---------- 题目 ----------

@app.get('/api/tasks')
def list_tasks(status: str | None = None):
    out = []
    for t in db.list_tasks(status):
        v = P.view(t)
        out.append({k: v.get(k) for k in (
            'id', 'seq', 'branch', 'domain', 'subdomain', 'difficulty', 'status', 'failed_from',
            'done_time', 'due_flag', 'update_time', 'busy', 'last_error')})
    return out


@app.get('/api/tasks/{task_id}')
def get_task(task_id: int):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, '题目不存在')
    v = P.view(t)
    v['env_blockers'] = P.env_blockers(t) if t['status'] == P.S_ENV else []
    v['port_range'] = P.port_range(t)
    v['runs'] = db.list_runs(task_id, 20)
    # 质检与返修：历次质检记录，每轮附上改动过的提交表列名
    v['inspections'] = P.inspections_view(t)
    return v


@app.put('/api/tasks/{task_id}')
def put_task(task_id: int, body: dict = Body(...)):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, '题目不存在')
    fields = {k: v for k, v in body.items() if k in EDITABLE}
    if 'branch' in body and t['status'] == P.S_INIT:
        fields['branch'] = P._unique_branch(body['branch']) if body['branch'] != t['branch'] else t['branch']
    if 'domain' in fields or 'subdomain' in fields:
        dom = fields.get('domain', t['domain'])
        if not P.subdomain_ok(dom, fields.get('subdomain', t['subdomain'])):
            raise HTTPException(400, '子方向不属于所属领域')
    if fields.get('reproducibility') not in (None, '', *P.REPRO_LEVELS):
        raise HTTPException(400, '环境可复现等级取值无效')
    # 语言/框架统一为不带版本号、英文逗号分隔的写法
    if 'lang_framework' in fields:
        fields['lang_framework'] = submission.normalize_lang(fields['lang_framework'])
    if 'delivery_score' in fields and fields['delivery_score'] not in (None, ''):
        fields['delivery_score'] = int(fields['delivery_score'])
    return P.view(db.update_task(task_id, **fields))


@app.delete('/api/tasks/{task_id}')
def delete_task(task_id: int):
    db.delete_task(task_id)
    return {'ok': True}


@app.post('/api/gen')
def gen(body: dict = Body(...)):
    return {'run_key': P.start_gen(body)}


ACTIONS = {
    # 初始化并准备环境一次跑完，只在需要人工处理时停下
    'prepare': lambda i, b: P.start_prepare(i),
    'confirm_gpu': lambda i, b: P.confirm_gpu(i),
    'redo': lambda i, b: P.start_redo(i),
    'mark_done': lambda i, b: P.mark_done(i),
    'collect': lambda i, b: P.start_collect(i, b.get('summary', '')),
    'review': lambda i, b: P.start_review(i),
    'approve': lambda i, b: P.approve(i),
    'reject': lambda i, b: P.reject(i, b.get('comment', '')),
    'prepare_record': lambda i, b: P.start_prepare_record(i),
    'complete': lambda i, b: P.complete(i, b.get('recording_link', '')),
    'retry': lambda i, b: P.retry(i, b.get('summary', '')),
    # 返修：粘贴质检原文，AI 直接处理
    'rework': lambda i, b: P.start_rework_from_text(i, b.get('text', '')),
}


@app.post('/api/tasks/{task_id}/action/{name}')
def action(task_id: int, name: str, body: dict = Body(default={})):
    fn = ACTIONS.get(name)
    if not fn:
        raise HTTPException(404, '未知操作')
    r = fn(task_id, body or {})
    return {'run_key': r} if isinstance(r, str) else {'ok': True}


@app.get('/api/tasks/{task_id}/review-input')
def review_input(task_id: int):
    """预览传给 qa-review 的输入文本（调试用）。"""
    return {'text': P.describe_input('qa-review', task_id)}


@app.get('/api/tasks/{task_id}/rework-input')
def rework_input(task_id: int):
    """预览传给 qa-rework 的输入文本（调试用）。"""
    return {'text': P.describe_input('qa-rework', task_id)}


# ---------- 运行记录 ----------

@app.get('/api/runs')
def runs(task_id: int | None = None):
    return db.list_runs(task_id, 50)


@app.get('/api/runs/{run_key}/log')
def run_log(run_key: str, offset: int = 0):
    r = db.get_run(run_key)
    if not r:
        raise HTTPException(404, '运行不存在')
    text, size = '', offset
    p = Path(r['log_path'])
    if p.exists():
        with open(p, 'rb') as f:
            f.seek(offset)
            data = f.read()
        size = offset + len(data)
        text = data.decode('utf-8', 'replace')
    return {'text': text, 'offset': size, 'status': r['status'], 'error': r['error']}


@app.post('/api/runs/{run_key}/stop')
def stop_run(run_key: str):
    return {'ok': jobs.stop(run_key)}


# ---------- 提交表与录屏文件 ----------

def _submission_tasks(body: dict | None) -> list[dict]:
    """提交表的题目范围：勾选了题目时取勾选的，否则取全部已完成的；已编号的按序号排在前面。"""
    ids = [int(x) for x in (body or {}).get('ids') or []]
    tasks = [db.get_task(i) for i in ids] if ids else db.list_tasks(P.S_DONE)
    return sorted((t for t in tasks if t), key=lambda t: (t['seq'] is None, t['seq'] or 0, t['id']))


@app.post('/api/submission/xlsx')
def submission_xlsx(body: dict = Body(default={})):
    """按与提交表弹窗相同的范围导出 xlsx，列顺序同提交表。"""
    tasks = _submission_tasks(body)
    if not tasks:
        raise HTTPException(400, '没有可导出的题目')
    data = submission.to_xlsx([submission.row(t) for t in tasks])
    name = f"提交表-{time.strftime('%Y%m%d-%H%M%S')}.xlsx"
    # 中文文件名按 RFC 5987 编码，同时给出 ASCII 兜底名
    disp = f"attachment; filename=\"submission.xlsx\"; filename*=UTF-8''{quote(name)}"
    return Response(data, headers={'Content-Disposition': disp},
                    media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.post('/api/submission')
def submission_rows(body: dict = Body(default={})):
    """勾选了题目时返回勾选的题目，否则返回全部已完成的题目，按序号排列。

    返修过的题附上最近一轮返修的轮次与改动过的格（列名到原值），页面据此高亮。
    """
    rows = []
    for t in _submission_tasks(body):
        insps = db.list_inspections(t['id'])
        rework = None
        if insps:
            last = insps[-1]
            rework = {'round': last['round'], 'state': last['state'],
                      'changed': {c['column']: c['before'] for c in P.rework_changes(t, last)}}
        rows.append({'id': t['id'], 'branch': t['branch'], 'status': t['status'],
                     'rework': rework, 'cells': submission.row(t)})
    return {'columns': submission.COLUMNS, 'rows': rows}


@app.get('/api/recordings')
def recordings(limit: int = 5):
    try:
        return submission.recordings(max(1, min(limit, 20)))
    except FileNotFoundError as e:
        raise HTTPException(400, str(e)) from e


app.mount('/', StaticFiles(directory=STATIC, html=True), name='static')
