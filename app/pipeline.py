# -*- coding: utf-8 -*-
"""各环节业务逻辑：出题、初始化、准备环境、重做、收集、验收、查验、录屏。"""
import datetime as dt
import json
import re
import shutil
from pathlib import Path

from . import codex_runner, config, db, envcheck, gitops, jobs, materials, style_check, trajectory

# 状态
S_INIT = '待初始化'
S_ENV = '待准备环境'
S_DO = '待做题'
S_COLLECT = '待收集'
S_REVIEW = '待验收'
S_CHECK = '待查验'
S_RECORD = '待录屏'
S_DONE = '已完成'
S_FAIL = '失败'
STATUSES = [S_INIT, S_ENV, S_DO, S_COLLECT, S_REVIEW, S_CHECK, S_RECORD, S_DONE, S_FAIL]

# 出题只限定在「游戏、图形与多媒体」这一主领域，作业文档中的其他领域不再出题
DOMAINS = {
    '游戏、图形与多媒体': ['音视频处理', '图形渲染', '其他'],
}
OS_LINUX = 'MacOS/Linux'
# 环境可复现等级只能取这三档，与 qa-review schema 的枚举保持一致
REPRO_LEVELS = ['无外部依赖', '有外部依赖，未容器化', '已容器化，可一键起环境']


class ActionError(RuntimeError):
    pass


# ---------- 公共 ----------

def task_dir(t: dict) -> Path:
    return config.path_of('tasks_root') / t['branch']


def unc(path: Path) -> str:
    """WSL 路径转换为 Windows 侧可读的 UNC 路径。"""
    distro = config.load()['wsl_distro']
    return f'\\\\wsl.localhost\\{distro}' + str(path).replace('/', '\\')


def need(t: dict | None, *statuses: str) -> dict:
    if not t:
        raise ActionError('题目不存在')
    if statuses and t['status'] not in statuses:
        raise ActionError(f"当前状态为 {t['status']}，不能执行该操作")
    return t


def port_range(t: dict) -> tuple[int, int]:
    cfg = config.load()
    start = int(cfg['port_base']) + (t['id'] % 50) * int(cfg['port_span'])
    return start, start + int(cfg['port_span']) - 1


def due_flag(t: dict) -> str:
    """当天 20:00 前完成的题当天必须提交，20:00 后完成的题次日提交。"""
    if not t.get('done_time') or t['status'] == S_DONE:
        return ''
    done = dt.datetime.strptime(t['done_time'], '%Y-%m-%d %H:%M:%S')
    due = done.date() if done.hour < 20 else done.date() + dt.timedelta(days=1)
    today = dt.date.today()
    if due == today:
        return '今日必须提交'
    if due < today:
        return '已超过提交日'
    return ''


def feedback_issues(text: str, score=None) -> list[dict]:
    """Feedback 的口吻检查加内容门禁（原分依据、代码锚点、质检会打回的表达）。"""
    if not text:
        return []
    return style_check.check_feedback(text) + style_check.check_feedback_content(text, score)


def view(t: dict) -> dict:
    """附加页面需要的派生字段。"""
    t = dict(t)
    t['feedback_issues'] = feedback_issues(t.get('feedback'), t.get('delivery_score'))
    t['prompt_issues'] = style_check.check_prompt(t.get('user_prompt'))
    t['due_flag'] = due_flag(t)
    t['init_permalink'] = config.permalink(t.get('init_sha'))
    t['artifact_permalink'] = config.permalink(t.get('artifact_sha'))
    t['task_dir'] = str(task_dir(t))
    # 做题以 bypass 模式启动，避免权限确认打断单轮执行
    t['do_command'] = f"cd {task_dir(t)} && claude --dangerously-skip-permissions"
    # 录屏目录：「准备录屏环境」会把产物快照全新克隆到这里
    t['record_dir'] = str(config.path_of('record_root') / t['branch'])
    t['busy'] = t['id'] in jobs.busy_tasks()
    return t


# ---------- 出题 ----------

def run_dir_lines(run_key: str) -> list[str]:
    """告诉 skill 本次运行目录，临时文件只放这里。"""
    ws = config.load()['agent_workspace_win'].rstrip('\\')
    return ['## 本次运行', f'运行 ID {run_key}',
            f'运行目录 {ws}\\runs\\{run_key}（草稿、验证脚本等临时文件只能放在这里）', '']


def subdomain_ok(domain: str, subdomain: str) -> bool:
    return subdomain in DOMAINS.get(domain, [])


def _gen_input(params: dict, run_key: str = '预览') -> str:
    lines = [*run_dir_lines(run_key),
             '## 本次出题参数',
             f"数量 {params['count']}",
             f"所属领域 {params['domain']}",
             f"子方向 {params.get('subdomain') or '由你从该领域的子方向中选一个 原样填写表中的取值'}",
             f"难度 {params.get('difficulty') or '由你判断 只能是困难或地狱'}",
             f"技术栈偏好 {params.get('stack') or '无'}",
             f"补充要求 {params.get('extra') or '无'}",
             '',
             '领域与子方向表（所属领域与子方向必须从这里取值）']
    for d, subs in DOMAINS.items():
        lines.append(f"{d}：{'、'.join(subs)}")
    lines += ['', '库内已有题目（用于查重 不得与其雷同）']
    existing = db.list_tasks()
    if not existing:
        lines.append('无')
    for t in existing:
        lines.append(f"- {t['branch']} | {t['domain']}/{t['subdomain']} | "
                     f"{re.sub(r'\s+', ' ', t['user_prompt'])[:150]}")
    return '\n'.join(lines)


def _unique_branch(name: str) -> str:
    base = re.sub(r'[^a-z0-9-]', '-', (name or 'task').lower()).strip('-') or 'task'
    b, i = base, 2
    while db.find_task_by_branch(b):
        b, i = f'{base}-{i}', i + 1
    return b


def start_gen(params: dict) -> str:
    if params.get('domain') not in DOMAINS:
        raise ActionError('所属领域无效')
    count = int(params.get('count') or 1)
    if not 1 <= count <= 10:
        raise ActionError('数量需在 1 到 10 之间')
    params['count'] = count
    if params.get('difficulty') not in (None, '', '困难', '地狱'):
        raise ActionError('难度只能是困难或地狱')
    if params.get('subdomain') and not subdomain_ok(params['domain'], params['subdomain']):
        raise ActionError('子方向不属于所选领域')

    def work(log, run_key, stop):
        text = _gen_input(params, run_key)
        result = codex_runner.run_skill('qa-gen', text, run_key, log, stop)
        n = 0
        for it in result['tasks']:
            if it['domain'] not in DOMAINS or it['difficulty'] not in ('困难', '地狱'):
                log(f"跳过不合规题目 {it.get('branch')}：领域或难度无效")
                continue
            if not subdomain_ok(it['domain'], it['subdomain']):
                log(f"跳过不合规题目 {it.get('branch')}：子方向 {it['subdomain']} 不属于 {it['domain']}")
                continue
            mats = [{**m, 'state': 'pending', 'note': ''} for m in it['materials']]
            db.insert_task({
                'branch': _unique_branch(it['branch']), 'status': S_INIT, 'gen_run_id': run_key,
                'user_prompt': it['user_prompt'], 'domain': it['domain'],
                'subdomain': it['subdomain'], 'task_type': '功能开发',
                'difficulty': it['difficulty'], 'lang_framework': it['lang_framework'],
                'acceptance_points': it['acceptance_points'], 'materials': mats,
                'env_requirements': it['env_requirements'], 'remark': it['remark'],
                'similarity_note': it['similarity_note'], 'harness': 'Claude Code', 'os': OS_LINUX,
                'voided_sessions': [],
            })
            n += 1
        log(f'入库 {n} 道题')

    return jobs.submit('qa-gen', None, work, ai=True)


# ---------- 初始化与准备环境 ----------

def start_init(task_id: int) -> str:
    t = need(db.get_task(task_id), S_INIT)

    def work(log, run_key, stop):
        gitops.init_task_dir(task_dir(t), t['branch'], config.load()['github_repo'], log=log)
        db.update_task(task_id, status=S_ENV, failed_from=None, last_error=None)

    return jobs.submit('init', task_id, work)


def start_fetch_materials(task_id: int) -> str:
    t = need(db.get_task(task_id), S_ENV)

    def work(log, run_key, stop):
        d = task_dir(t)
        mats = []
        for m in db.get_task(task_id)['materials'] or []:
            if m.get('source') == 'url' and m.get('url') and m.get('state') != 'ready':
                try:
                    m = materials.download(d, m, log)
                except Exception as e:  # noqa: BLE001 单个素材失败不影响其他
                    m = {**m, 'state': 'failed', 'note': str(e)[:300]}
            elif m.get('state') != 'pending':
                m = materials.check(d, m) if m.get('state') in ('ready', 'confirmed') else m
            mats.append(m)
            log(f"{m['path']} {m['state']} {m.get('note', '')}")
        db.update_task(task_id, materials=mats)

    return jobs.submit('materials', task_id, work)


def start_run_script(task_id: int, idx: int) -> str:
    t = need(db.get_task(task_id), S_ENV)
    mats = t['materials'] or []
    if not 0 <= idx < len(mats) or not mats[idx].get('script'):
        raise ActionError('该素材没有生成脚本')

    def work(log, run_key, stop):
        cur = db.get_task(task_id)['materials']
        cur[idx] = materials.run_script(task_dir(t), cur[idx], log)
        db.update_task(task_id, materials=cur)

    return jobs.submit('material-script', task_id, work)


def confirm_material(task_id: int, idx: int) -> dict:
    t = need(db.get_task(task_id), S_ENV)
    mats = t['materials'] or []
    if not 0 <= idx < len(mats):
        raise ActionError('素材序号无效')
    m = materials.check(task_dir(t), mats[idx])
    if m['state'] != 'ready':
        raise ActionError(f"素材检查未通过：{m['note']}")
    mats[idx] = {**m, 'state': 'confirmed'}
    return db.update_task(task_id, materials=mats)


def start_envcheck(task_id: int) -> str:
    t = need(db.get_task(task_id), S_ENV)

    def work(log, run_key, stop):
        cfg = config.load()
        req = t['env_requirements'] or {}
        items = envcheck.toolchain_check(req.get('toolchain') or [])
        old_gpu = {i['name']: i for i in (t['env_check'] or []) if i.get('manual')}
        for g in envcheck.gpu_items(req):
            if old_gpu.get(g['name'], {}).get('ok'):
                g['ok'] = True
            items.append(g)
        net = envcheck.net_check(req.get('external_urls') or [])
        client = envcheck.client_check(Path(cfg['claude_home']).expanduser(), str(task_dir(t)))
        for i in items + net + client['items']:
            log(f"{'通过' if i['ok'] else '不通过'} {i['name']} {i['detail']}")
        db.update_task(task_id, env_check=items, net_check=net, client_check=client,
                       expected_model=client['expected_model'])

    return jobs.submit('envcheck', task_id, work)


def confirm_gpu(task_id: int) -> dict:
    t = need(db.get_task(task_id), S_ENV)
    items = t['env_check'] or []
    for i in items:
        if i.get('manual'):
            i['ok'] = True
    return db.update_task(task_id, env_check=items)


def env_blockers(t: dict) -> list[str]:
    out = []
    for m in t['materials'] or []:
        if m.get('state') not in ('ready', 'confirmed'):
            out.append(f"素材未就绪 {m['path']}（{m.get('state')}）")
    if t['env_check'] is None or t['client_check'] is None:
        out.append('尚未执行环境检查')
    for i in (t['env_check'] or []) + (t['net_check'] or []):
        if not i['ok']:
            out.append(f"检查未通过 {i['name']}")
    for i in (t['client_check'] or {}).get('items', []):
        if not i['ok']:
            out.append(f"做题客户端 {i['name']}")
    if t['status'] == S_ENV and task_dir(t).exists():
        big = gitops.large_files(task_dir(t))
        out += [f'文件超过 100MB {p}（{mb}MB）' for p, mb in big['block']]
    return out


def start_finalize_env(task_id: int) -> str:
    t = need(db.get_task(task_id), S_ENV)
    blockers = env_blockers(t)
    if blockers:
        raise ActionError('；'.join(blockers[:8]))

    def work(log, run_key, stop):
        d = task_dir(t)
        sha = gitops.commit_initial(d, t['branch'], log=log)
        db.update_task(task_id, init_sha=sha, ignored_before=gitops.ignored_entries(d),
                       status=S_DO, failed_from=None, last_error=None)
        log(f'初始快照 {config.permalink(sha)}')

    return jobs.submit('finalize-env', task_id, work)


# ---------- 做题与重做 ----------

def start_redo(task_id: int) -> str:
    t = db.get_task(task_id)
    if not t or not (t['status'] in (S_DO, S_COLLECT) or
                     (t['status'] == S_FAIL and t['failed_from'] in (S_DO, S_COLLECT))):
        raise ActionError('只有待做题、待收集或收集失败的题目可以重做')

    def work(log, run_key, stop):
        cfg = config.load()
        sessions = trajectory.list_sessions(Path(cfg['claude_home']).expanduser(), str(task_dir(t)))
        voided = sorted(set(t['voided_sessions'] or []) | set(sessions))
        gitops.reset_to_initial(task_dir(t), t['branch'], t['init_sha'], log=log)
        db.update_task(task_id, voided_sessions=voided, redo_count=(t['redo_count'] or 0) + 1,
                       status=S_DO, failed_from=None, last_error=None, session_id=None,
                       prompt_id=None, done_time=None)
        log(f'已作废会话 {len(voided)} 个，做题目录已恢复到初始快照')

    return jobs.submit('redo', task_id, work)


def mark_done(task_id: int) -> dict:
    need(db.get_task(task_id), S_DO)
    return db.update_task(task_id, status=S_COLLECT)


# ---------- 收集 ----------

def _local_time(ts: str | None) -> str | None:
    if not ts:
        return None
    t = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
    return t.astimezone().strftime('%Y-%m-%d %H:%M:%S')


def start_collect(task_id: int, summary: str = '') -> str:
    t = need(db.get_task(task_id), S_COLLECT)

    def work(log, run_key, stop):
        cfg = config.load()
        home = Path(cfg['claude_home']).expanduser()
        d = task_dir(t)
        path = trajectory.locate(home, str(d), t['user_prompt'], t['voided_sessions'] or [])
        if not path:
            raise ActionError('未找到首条输入与 prompt 原文一致的有效会话')
        log(f'轨迹 {path}')
        info = trajectory.parse(path)
        v = trajectory.validate(info, t['user_prompt'], t['expected_model'], cfg['builtin_skills'])
        if v['errors']:
            raise ActionError('轨迹校验不通过：' + '；'.join(v['errors']))
        art = gitops.commit_artifact(d, t['branch'], t['init_sha'], t['ignored_before'],
                                     summary=(summary or '').strip(), log=log)
        log(f"产物快照 {config.permalink(art['sha'])} 补交被忽略文件 {art['forced_ignored']}")
        tdir = config.DATA_DIR / 'trajectories'
        tdir.mkdir(parents=True, exist_ok=True)
        copy = tdir / f"{t['branch']}-{info['session_id']}.jsonl"
        shutil.copy2(path, copy)
        traj_summary = trajectory.summarize(path)
        remark = t['remark'] or ''
        if v['continue_count']:
            remark = (remark + f" 模型中途无报错停止 人工输入继续{v['continue_count']}次后完成 "
                      'prompt与promptid取第一轮').strip()
        models = info['requested_models'] or info['models']
        db.update_task(
            task_id, session_id=info['session_id'], prompt_id=info['prompts'][0]['prompt_id'],
            harness='Claude Code', harness_version=info['version'], os=OS_LINUX,
            model_name=', '.join(models), continue_count=v['continue_count'],
            done_time=_local_time(info['last_timestamp']), trajectory_path=str(copy),
            traj_summary=traj_summary, artifact_sha=art['sha'], remark=remark,
            status=S_REVIEW, failed_from=None, last_error=None)

    return jobs.submit('collect', task_id, work)


# ---------- 验收 ----------

def _materials_lines(t: dict) -> list[str]:
    """把素材路径、来源、用途和生成脚本拼进验收输入。"""
    lines = []
    for m in t.get('materials') or []:
        lines.append(f"- {m['path']} 来源 {m['source']} 用途 {m.get('purpose', '')}")
        if m.get('url'):
            lines.append(f"  下载地址 {m['url']}")
        if m.get('script'):
            lines += ['  生成脚本（在仓库根目录执行，可在验收目录内换参数重新生成，不要覆盖已提交的素材）：',
                      '```bash', m['script'], '```']
    return lines


def _review_input(t: dict, verify_dir: Path, run_key: str = '预览') -> str:
    distro = config.load()['wsl_distro']
    p0, p1 = port_range(t)
    env_lines = [f"{'通过' if i['ok'] else '不通过'} {i['name']} {i['detail']}"
                 for i in (t['env_check'] or []) + (t['net_check'] or [])]
    summary = t['traj_summary'] or {'steps': [], 'files_changed': [], 'final_text': ''}
    diff = gitops.diff_text(task_dir(t), t['init_sha'], t['artifact_sha'])
    parts = [
        *run_dir_lines(run_key),
        '## 题目',
        f"分支 {t['branch']}",
        f"所属领域 {t['domain']} 子方向 {t['subdomain']} 难度 {t['difficulty']}",
        f"语言/框架 {t['lang_framework']}",
        'User Prompt 原文：', t['user_prompt'],
        '', '## 内部验收要点', *[f'- {x}' for x in t['acceptance_points'] or []],
        '', '## 环境检查结果', *(env_lines or ['无']),
        '', '## 出题备注草稿', t['remark'] or '无',
        # 素材生成脚本不在产物仓库里，验收需要换参数重新生成时从这里取
        '', '## 素材清单与生成脚本', *(_materials_lines(t) or ['无']),
        '', '## 验收目录',
        f'WSL 路径 {verify_dir}',
        f'Windows 读取路径 {unc(verify_dir)}',
        f"执行命令方式 wsl.exe -d {distro} --cd {verify_dir} -- bash -ic '<命令>'",
        f'可用端口 {p0}-{p1}',
        '', '## 轨迹',
        f"完整轨迹 {unc(Path(t['trajectory_path']))}",
        f"SessionID {t['session_id']} 「继续」次数 {t['continue_count']}",
        trajectory.summary_text(summary),
        '', '## 产物 diff（初始快照到产物快照）', diff,
        '', '## 人工意见', t['review_comment'] or '无',
    ]
    return '\n'.join(parts)


def start_review(task_id: int) -> str:
    t = need(db.get_task(task_id), S_REVIEW)

    def work(log, run_key, stop):
        cfg = config.load()
        vdir = config.path_of('verify_root') / t['branch']
        gitops.fresh_clone(cfg['github_repo'], t['branch'], t['artifact_sha'], vdir, log=log)
        text = _review_input(t, vdir, run_key)
        r = codex_runner.run_skill('qa-review', text, run_key, log, stop)
        issues = feedback_issues(r['feedback'], r['delivery_score'])
        log(f"评分 {r['delivery_score']} 口吻与内容问题 {len(issues)} 条 "
            f"自检一致 {r['self_check']['score_consistent']}")
        db.update_task(task_id, evidence=r['evidence'], delivery_score=r['delivery_score'],
                       score_reason=r['score_reason'], feedback=r['feedback'],
                       record_steps=r['record_steps'], reproducibility=r['reproducibility'],
                       remark=r['remark'], review_self_check=r['self_check'],
                       status=S_CHECK, failed_from=None, last_error=None)

    return jobs.submit('qa-review', task_id, work, ai=True)


# ---------- 查验与录屏 ----------

def approve(task_id: int) -> dict:
    t = need(db.get_task(task_id), S_CHECK)
    if not t.get('delivery_score') or not 1 <= int(t['delivery_score']) <= 5:
        raise ActionError('交付完整性需为 1 到 5 的整数')
    if t.get('reproducibility') not in REPRO_LEVELS:
        raise ActionError('请选择环境可复现等级')
    issues = feedback_issues(t['feedback'], t['delivery_score'])
    if issues:
        raise ActionError(f'Feedback 口吻或内容检查有 {len(issues)} 处问题，修正后再通过')
    return db.update_task(task_id, status=S_RECORD)


def reject(task_id: int, comment: str) -> str:
    need(db.get_task(task_id), S_CHECK)
    if not (comment or '').strip():
        raise ActionError('请填写打回意见')
    db.update_task(task_id, review_comment=comment.strip(), status=S_REVIEW)
    return start_review(task_id)


def start_prepare_record(task_id: int) -> str:
    t = need(db.get_task(task_id), S_RECORD)

    def work(log, run_key, stop):
        cfg = config.load()
        rdir = config.path_of('record_root') / t['branch']
        gitops.fresh_clone(cfg['github_repo'], t['branch'], t['artifact_sha'], rdir, log=log)
        log(f'录屏目录已就绪 {rdir}')

    return jobs.submit('prepare-record', task_id, work)


def complete(task_id: int, recording_link: str) -> dict:
    t = need(db.get_task(task_id), S_RECORD)
    if not (recording_link or '').strip():
        raise ActionError('请先选定录屏文件')
    if not t.get('trajectory_path'):
        raise ActionError('缺少收集环节留存的轨迹文件')
    fields = {'recording_link': recording_link.strip(), 'status': S_DONE}
    # 完成时编号，接续提交表已有的最大序号
    if not t.get('seq'):
        fields['seq'] = max(int(config.load()['seq_base'] or 0), db.max_seq()) + 1
    return db.update_task(task_id, **fields)


# ---------- 失败重试 ----------

RETRY = {S_INIT: start_init, S_COLLECT: start_collect, S_REVIEW: start_review}


def retry(task_id: int, summary: str = ''):
    t = need(db.get_task(task_id), S_FAIL)
    back = t['failed_from'] or S_INIT
    db.update_task(task_id, status=back, failed_from=None)
    # 收集重试时带上产物概述，提交信息才不会退化成分支名
    if back == S_COLLECT:
        return start_collect(task_id, summary)
    fn = RETRY.get(back)
    return fn(task_id) if fn else None


def describe_input(kind: str, task_id: int | None, params: dict | None = None) -> str:
    """调试用：预览将传给 skill 的输入文本。"""
    if kind == 'qa-gen':
        return _gen_input(params or {'count': 1, 'domain': '游戏、图形与多媒体'})
    t = db.get_task(task_id)
    return _review_input(t, config.path_of('verify_root') / t['branch'])


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
