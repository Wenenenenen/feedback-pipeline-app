# -*- coding: utf-8 -*-
"""飞书多维表格：把已完成题目的提交表行写入飞书，按 UID 列（即出题时分配的甲方序号）新增或更新。

读写经由 app/feishu_bridge.mjs 调用 plugin/feishu-table-tool，使用 WSL 中 lark-cli 已登录的用户身份。
"""
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from . import config, db, jobs, submission

BRIDGE = config.APP_DIR / 'feishu_bridge.mjs'
# 飞书表里存甲方序号的列，全局只用这一个 ID 对应一道题
UID_FIELD = 'UID'
# 附件列取本地文件：轨迹为收集留存的 jsonl，录屏为完成录屏时选定的 mp4
ATTACHMENT_COLUMNS = ('轨迹文件', '运行录屏')
# 同一张表的写入串行执行，避免自动写入与手动写入同时新增出重复记录
_lock = threading.Lock()


class FeishuError(RuntimeError):
    pass


# ---------- 运行环境 ----------

def _version_key(p: Path) -> tuple:
    return tuple(int(x) for x in re.findall(r'\d+', p.parent.parent.name)) or (0,)


def node_path() -> Path:
    """Node.js 可执行文件：优先 PATH，其次 nvm 安装的最新版本（后端启动时 PATH 不一定带 nvm）。"""
    found = shutil.which('node')
    if found:
        return Path(found)
    cands = sorted(Path('~/.nvm/versions/node').expanduser().glob('v*/bin/node'), key=_version_key)
    if not cands:
        raise FeishuError('找不到 node，请在 WSL 中安装 Node.js 22 以上版本')
    return cands[-1]


def _env() -> dict:
    """子进程环境：把 node 所在目录放到 PATH 前面（lark-cli 由 npm 全局安装在同一目录）。"""
    node = node_path()
    env = dict(os.environ)
    env['PATH'] = f'{node.parent}{os.pathsep}{env.get("PATH", "")}'
    cli = node.parent / 'lark-cli'
    if not env.get('LARK_CLI_BIN') and cli.exists():
        env['LARK_CLI_BIN'] = str(cli)
    return env


def call(req: dict, timeout: int = 900) -> dict:
    """调用桥接脚本，返回结果；失败时抛出 FeishuError，附带 CLI 给出的提示。"""
    try:
        p = subprocess.run([str(node_path()), str(BRIDGE)], input=json.dumps(req, ensure_ascii=False),
                           capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=_env())
    except subprocess.TimeoutExpired as e:
        raise FeishuError(f'调用飞书超时（{timeout} 秒），写入可能已部分生效，请先到飞书表里核对') from e
    out = None
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict):
        raise FeishuError(f'桥接脚本没有返回结果：{(p.stderr or p.stdout).strip()[-500:]}')
    if not out.get('ok'):
        msg = out.get('error') or '调用飞书失败'
        if out.get('hint'):
            msg += f'（{out["hint"]}）'
        raise FeishuError(msg)
    return out


def table_url() -> str:
    url = (config.load().get('feishu_url') or '').strip()
    if not url:
        raise FeishuError('配置页还没有填写飞书表格链接')
    return url


# ---------- 字段映射 ----------

def column_of(field_name: str) -> str:
    """飞书字段名对应的提交表列名：飞书字段名里的「/」会显示成「|」，如 语言|框架 对应 语言/框架。"""
    return field_name.replace('|', '/').strip()


def uid_of(t: dict) -> int:
    seq = str(t.get('client_seq') or '').strip()
    if not seq.isdigit():
        raise FeishuError(f'{t["branch"]} 没有甲方序号，不能写入飞书')
    return int(seq)


def _number(v: str):
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def build_record(t: dict, fields: list[dict]) -> dict:
    """按飞书表的真实字段，把一道题的提交表行转成写入请求。

    返回 {uid, fields, attachments, skipped}：只填飞书表里有、提交表里也有的列；空值不写（更新时保留飞书里的原值）；
    飞书表里有但提交表对应不上的列记在 skipped 里。
    """
    if not any(f['name'] == UID_FIELD for f in fields):
        raise FeishuError(f'飞书表里没有「{UID_FIELD}」列（存甲方序号）')
    uid = uid_of(t)
    cells = {c['name']: c['value'] for c in submission.row(t)}
    files = {'轨迹文件': t.get('trajectory_path') or '',
             '运行录屏': config.wsl_path(t['recording_link']) if t.get('recording_link') else ''}
    out_fields, attachments, skipped = {}, {}, []
    for f in fields:
        name, typ = f['name'], f['type']
        col = column_of(name)
        if f.get('readonly') or name == UID_FIELD:
            continue
        if typ == 'attachment':
            if col in files:
                if files[col]:
                    attachments[name] = [files[col]]
            else:
                skipped.append(name)
            continue
        if col not in cells:
            skipped.append(name)
            continue
        v = cells[col].strip()
        if not v:
            continue
        if typ == 'text':
            out_fields[name] = v
        elif typ == 'number':
            n = _number(v)
            if n is None:
                skipped.append(name)
            else:
                out_fields[name] = n
        elif typ == 'select':
            # 单选、多选都只填一个选项，选项不在飞书表里时由插件报错
            out_fields[name] = [v]
        else:
            skipped.append(name)
    return {'uid': uid, 'fields': out_fields, 'attachments': attachments, 'skipped': skipped}


# ---------- 写入 ----------

def sync(task_ids: list[int], log) -> list[dict]:
    """把若干道题写入飞书：按 UID 找记录，有则更新、无则新增。逐题处理，有失败时汇总后抛出。"""
    url = table_url()
    results, failed = [], []
    with _lock:
        info = call({'op': 'inspect', 'url': url})
        log(f"飞书数据表 {info['tableName']}（{info['tableId']}） {len(info['fields'])} 个字段")
        for tid in task_ids:
            t = db.get_task(tid)
            if not t:
                continue
            try:
                rec = build_record(t, info['fields'])
                if rec['skipped']:
                    log(f"{t['branch']} 飞书列没有对应的提交表内容 不填：{'、'.join(rec['skipped'])}")
                r = call({'op': 'upsert', 'url': url, 'tableId': info['tableId'], 'uidField': UID_FIELD,
                          'uid': rec['uid'], 'fields': rec['fields'], 'attachments': rec['attachments']})
                what = '新增' if r['action'] == 'create' else '更新'
                extra = f"；附件沿用 {'、'.join(r['kept'])}" if r.get('kept') else ''
                log(f"{t['branch']} UID {rec['uid']} 已{what} {r.get('recordId') or ''}"
                    f"；上传附件 {'、'.join(r.get('uploaded') or []) or '无'}{extra}")
                results.append({'id': tid, 'uid': rec['uid'], **r})
            except (FeishuError, OSError, ValueError) as e:
                log(f"{t['branch']} 写入失败 {e}")
                failed.append(f"{t['branch']}：{e}")
    if failed:
        raise FeishuError(f'{len(failed)} 题写入飞书失败：' + '；'.join(failed)[:1500])
    return results


def start_sync(task_ids: list[int], task_id: int | None = None) -> str:
    """后台写入飞书，返回运行 ID。旁路任务：不占题目互斥，失败也不改题目状态。"""
    ids = list(dict.fromkeys(int(i) for i in task_ids))
    if not ids:
        raise FeishuError('没有要写入的题目')
    table_url()
    return jobs.submit('feishu', task_id, lambda log, run_key, stop: sync(ids, log), side=True)


def on_done(task_id: int) -> str | None:
    """题目进入已完成后自动写入飞书（填了表格链接且开启自动写入时）；出错只记在运行日志里，不影响流水线。"""
    cfg = config.load()
    if not cfg.get('feishu_auto_sync') or not (cfg.get('feishu_url') or '').strip():
        return None
    try:
        return start_sync([task_id], task_id=task_id)
    except Exception:  # noqa: BLE001 自动写入失败不能打断完成操作
        return None


def status(task_ids: list[int]) -> dict:
    """查飞书表里已有哪些 UID，返回 {task_id: recordId 或 None}，供提交表页标记是否已写入。"""
    url = table_url()
    recs = call({'op': 'list', 'url': url, 'uidField': UID_FIELD}, timeout=120)['records']
    by_uid: dict[int, list[str]] = {}
    for r in recs:
        if isinstance(r['uid'], (int, float)):
            by_uid.setdefault(int(r['uid']), []).append(r['recordId'])
    out = {}
    for tid in task_ids:
        t = db.get_task(tid)
        seq = str((t or {}).get('client_seq') or '')
        out[tid] = by_uid.get(int(seq), []) if seq.isdigit() else []
    return out
