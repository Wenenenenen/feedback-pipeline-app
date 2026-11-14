# -*- coding: utf-8 -*-
"""各环节业务逻辑：出题、初始化、准备环境、重做、收集、验收、查验、录屏、质检登记与返修。"""
import datetime as dt
import json
import re
import shutil
from pathlib import Path

from . import (codex_runner, config, db, envcheck, gitops, jobs, materials, style_check, submission,
               trajectory)

# 状态
S_INIT = '待初始化'
S_ENV = '待准备环境'
S_DO = '待做题'
S_COLLECT = '待收集'
S_REVIEW = '待验收'
S_CHECK = '待查验'
S_RECORD = '待录屏'
S_DONE = '已完成'
# 甲方质检待返修：AI 按质检原文处理后回到已完成（产物改动过或需要重录时先回到待录屏）
# 质检不通过的数据直接放弃，不进流水线
S_REWORK = '待返修'
S_FAIL = '失败'
STATUSES = [S_INIT, S_ENV, S_DO, S_COLLECT, S_REVIEW, S_CHECK, S_RECORD, S_DONE, S_REWORK, S_FAIL]

# 质检记录的处理状态
I_OPEN = '返修中'
I_DONE = '已返修'
# 返修前留存快照的提交字段，用于列出提交表里改了哪些格
SNAPSHOT_FIELDS = ('feedback', 'delivery_score', 'score_reason', 'remark', 'reproducibility',
                   'record_steps', 'artifact_sha', 'recording_link', 'lang_framework')

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
    return config.unc(path)


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
    # 已编号的题已经提交过（返修中的题同样如此），不再提示提交日
    if not t.get('done_time') or t['status'] == S_DONE or t.get('seq'):
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
    t['record_issues'] = record_issues(t.get('record_steps'))
    # 轨迹要在 Windows 文件选择框里上传，页面展示 Windows 侧路径
    t['trajectory_win'] = config.unc(t['trajectory_path']) if t.get('trajectory_path') else ''
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
    cfg = config.load()
    ws = cfg['agent_workspace_win'].rstrip('\\')
    # 发行版名随运行环境配置传入，skill 与 AGENTS.md 中不写死
    return ['## 本次运行', f'运行 ID {run_key}',
            f'运行目录 {ws}\\runs\\{run_key}（草稿、验证脚本等临时文件只能放在这里）',
            f"WSL 发行版 {cfg['wsl_distro']}（Windows 读取 WSL 文件用 \\\\wsl.localhost\\{cfg['wsl_distro']}\\ 开头的路径）", '']


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
                'difficulty': it['difficulty'],
                'lang_framework': submission.normalize_lang(it['lang_framework']),
                'acceptance_points': it['acceptance_points'], 'materials': mats,
                'env_requirements': it['env_requirements'], 'remark': it['remark'],
                'similarity_note': it['similarity_note'], 'harness': 'Claude Code', 'os': OS_LINUX,
                'voided_sessions': [],
            })
            n += 1
        log(f'入库 {n} 道题')

    return jobs.submit('qa-gen', None, work, ai=True)


# ---------- 初始化与准备环境 ----------

def _prepare_material(d: Path, m: dict, log) -> dict:
    """准备单个素材：已就绪的只核对；下载类自动下载，脚本类自动执行；人工素材只检查是否已放入。"""
    if m.get('state') in ('ready', 'confirmed'):
        c = materials.check(d, m)
        if c['state'] == 'ready':
            return {**c, 'state': m['state']}
        m = c  # 已就绪的文件不见了，按来源重新获取
    src = m.get('source')
    try:
        if src == 'url' and m.get('url'):
            return materials.download(d, m, log)
        if src == 'script' and m.get('script'):
            return materials.run_script(d, m, log)
    except Exception as e:  # noqa: BLE001 单个素材失败不影响其他素材和检查
        return {**m, 'state': 'failed', 'note': str(e)[:300]}
    c = materials.check(d, m)
    if c['state'] == 'missing':
        c['note'] = '需要人工放到做题目录对应路径'
    return c


def start_prepare(task_id: int) -> str:
    """初始化并准备环境：一次跑完初始化、工具链、素材、外网、做题客户端检查与初始快照提交。

    全部通过直接进入待做题；有需要人工处理的项时停在待准备环境，处理完再次执行即可（已就绪的素材只核对不重跑）。
    """
    need(db.get_task(task_id), S_INIT, S_ENV)

    def work(log, run_key, stop):
        cfg = config.load()
        t = db.get_task(task_id)
        d = task_dir(t)
        if t['status'] == S_INIT:
            gitops.init_task_dir(d, t['branch'], cfg['github_repo'], log=log)
            t = db.update_task(task_id, status=S_ENV, failed_from=None, last_error=None)
            log(f'做题目录已初始化 {d}')
        req = t['env_requirements'] or {}
        # 先查工具链，素材生成脚本可能依赖这些工具
        items = envcheck.toolchain_check(req.get('toolchain') or [], cwd=str(d))
        old_gpu = {i['name']: i for i in (t['env_check'] or []) if i.get('manual')}
        for g in envcheck.gpu_items(req):
            if old_gpu.get(g['name'], {}).get('ok'):
                g['ok'] = True
            items.append(g)
        mats = []
        for m in t['materials'] or []:
            m = _prepare_material(d, m, log)
            log(f"素材 {m['path']} {m['state']} {m.get('note', '')}")
            mats.append(m)
        net = envcheck.net_check(req.get('external_urls') or [])
        client = envcheck.client_check(Path(cfg['claude_home']).expanduser(), str(d))
        for i in items + net + client['items']:
            log(f"{'通过' if i['ok'] else '不通过'} {i['name']} {i['detail']}")
        t = db.update_task(task_id, materials=mats, env_check=items, net_check=net, client_check=client,
                           expected_model=client['expected_model'])
        blockers = env_blockers(t)
        if blockers:
            log('需要人工处理，处理完点「继续准备」：\n' + '\n'.join(blockers))
            return
        sha = gitops.commit_initial(d, t['branch'], log=log)
        db.update_task(task_id, init_sha=sha, ignored_before=gitops.ignored_entries(d),
                       status=S_DO, failed_from=None, last_error=None)
        log(f'初始快照 {config.permalink(sha)}')

    return jobs.submit('prepare', task_id, work)


def confirm_gpu(task_id: int) -> str:
    """人工确认显卡能力后继续准备（没有其他问题时直接提交初始快照）。"""
    t = need(db.get_task(task_id), S_ENV)
    items = t['env_check'] or []
    for i in items:
        if i.get('manual'):
            i['ok'] = True
    db.update_task(task_id, env_check=items)
    return start_prepare(task_id)


_MATERIAL_HINT = {
    'missing': '文件不存在',
    'failed': '获取失败 看运行日志里的报错',
    'too_large': '超过 100MB 换小一点的素材或改脚本参数',
    'pending': '尚未准备',
}


def env_blockers(t: dict) -> list[str]:
    """需要人工处理的项，每项带上处理方式。"""
    out = []
    for m in t['materials'] or []:
        st = m.get('state')
        if st not in ('ready', 'confirmed'):
            out.append(f"素材 {m['path']} {_MATERIAL_HINT.get(st, st)} {m.get('note', '')}".strip())
    if t['env_check'] is None or t['client_check'] is None:
        out.append('尚未执行环境检查')
    for i in (t['env_check'] or []) + (t['net_check'] or []):
        if not i['ok']:
            label = '需要人工确认' if i.get('manual') else '不通过'
            out.append(f"{i['name']} {label}" + (f" 处理 {i['fix']}" if i.get('fix') else ''))
    for i in (t['client_check'] or {}).get('items', []):
        if not i['ok']:
            out.append(f"做题客户端 {i['name']} {i['detail']}" + (f" 处理 {i['fix']}" if i.get('fix') else ''))
    if t['status'] == S_ENV and task_dir(t).exists():
        big = gitops.large_files(task_dir(t))
        out += [f'文件超过 100MB {p}（{mb}MB）' for p, mb in big['block']]
    return out


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


def _verify_lines(t: dict, verify_dir: Path) -> list[str]:
    """验收目录、命令写法与端口段，验收与返修共用。"""
    distro = config.load()['wsl_distro']
    p0, p1 = port_range(t)
    return ['## 验收目录',
            f'WSL 路径 {verify_dir}',
            f'Windows 读取路径 {unc(verify_dir)}',
            f"执行命令方式 wsl.exe -d {distro} --cd {verify_dir} -- bash -ic '<命令>'",
            f'可用端口 {p0}-{p1}']


def _record_lines(t: dict) -> list[str]:
    """录屏环境说明，验收与返修共用：录屏步骤是人在录屏目录的终端里照着执行的。"""
    rdir = config.path_of('record_root') / t['branch']
    return ['## 录屏环境',
            f'录屏目录 {rdir}（产物快照的全新克隆，不含依赖）',
            '录屏时人在 WSL 终端里先 cd 到录屏目录，再照录屏步骤逐条粘贴执行；浏览器用 Windows 本机浏览器按 localhost 加端口访问',
            '录屏步骤的命令直接在这个终端里执行：用相对路径，不加 wsl.exe 或 bash -ic 包装，不出现验收目录、修复目录的路径']


def record_issues(steps) -> list[dict]:
    """录屏步骤的规则检查：命令要能在录屏目录的终端里直接执行，操作要人工能快速完成。"""
    work_dirs = '|'.join(re.escape(config.path_of(k).name) for k in ('verify_root', 'tasks_root'))
    rules = [
        (r'wsl(\.exe)?\s+-d|bash\s+-ic', '带了 wsl.exe 或 bash -ic 包装 录屏时直接在录屏目录的终端里执行'),
        (rf'/({work_dirs})/', '用了验收目录或做题目录的路径 应在录屏目录下用相对路径'),
        (r'(点击|点选|单击|点)[^。；;\n]{0,10}(像素|坐标)\s*[（(]?\s*\d+\s*[,，\s]\s*\d+',
         '要求手动点击精确的像素坐标 人工做不到 应改成可粘贴的控制台脚本或按画面上可见的元素描述'),
    ]
    issues = []
    for n, s in enumerate(steps or [], 1):
        text = ' '.join(str(s.get(k) or '') for k in ('action', 'command', 'expect'))
        for pat, rule in rules:
            m = re.search(pat, text)
            if m:
                issues.append({'rule': f'第{n}步 {rule}', 'excerpt': m.group(0)[:60]})
    return issues


def _review_input(t: dict, verify_dir: Path, run_key: str = '预览') -> str:
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
        '', *_verify_lines(t, verify_dir),
        '', *_record_lines(t),
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
            f"录屏步骤问题 {len(record_issues(r['record_steps']))} 条 "
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
    # 返修后重新录屏完成，本轮质检随之处理完毕（序号沿用原来的）
    insp = db.open_inspection(task_id)
    if insp:
        db.update_inspection(insp['id'], state=I_DONE, resolved_time=db.now())
    return db.update_task(task_id, **fields)


# ---------- 质检登记与返修 ----------

# 质检原文开头形如【AI质检｜待返修】，兼容不带 AI 的写法
_VERDICT = re.compile(r'【\s*(?:ai\s*)?质检\s*[｜|]\s*([^】]+?)\s*】', re.I)
_INSPECT_TIME = re.compile(r'质检时间\s*[：:]\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)')
_SECTION = re.compile(r'^[ \t]*(问题|证据|修改建议)[ \t]*[：:][ \t]*', re.M)
_SECTION_KEYS = {'问题': 'problem', '证据': 'evidence', '修改建议': 'suggestion'}


def parse_inspection(text: str) -> dict:
    """解析一条待返修的质检原文：质检时间，以及问题、证据、修改建议三段（没有分段时正文记为问题）。"""
    text = (text or '').strip()
    if not text:
        raise ActionError('请粘贴质检原文')
    marks = _VERDICT.findall(text)
    if len(marks) > 1:
        raise ActionError('一次只处理一条质检结果，请分开粘贴')
    if marks and marks[0] == '不通过':
        raise ActionError('质检不通过的数据直接放弃，不进流水线返修')
    if not marks or marks[0] != '待返修':
        raise ActionError('没有识别到质检结论，原文开头应为【AI质检｜待返修】')
    out = {'verdict': marks[0], 'inspect_time': None, 'raw_text': text,
           'problem': '', 'evidence': '', 'suggestion': ''}
    body = _VERDICT.sub('', text, count=1)
    m = _INSPECT_TIME.search(body)
    if m:
        out['inspect_time'] = m.group(1).replace('T', ' ')
        body = body[:m.start()] + body[m.end():]
    parts = _SECTION.split(body)
    if len(parts) == 1:
        out['problem'] = body.strip()
    else:
        out['problem'] = parts[0].strip()
        for i in range(1, len(parts), 2):
            key = _SECTION_KEYS[parts[i]]
            out[key] = (out[key] + '\n' + parts[i + 1].strip()).strip()
    return out


def start_rework_from_text(task_id: int, text: str) -> str:
    """粘贴质检原文即返修：登记本轮质检、留存当前提交内容，随后由 AI 直接处理。"""
    t = need(db.get_task(task_id), S_DONE)
    p = parse_inspection(text)
    if task_id in jobs.busy_tasks():
        raise ActionError('该题目已有任务在运行')
    rnd = len(db.list_inspections(task_id)) + 1
    db.insert_inspection({**p, 'task_id': task_id, 'round': rnd, 'state': I_OPEN,
                          'before_snapshot': {k: t.get(k) for k in SNAPSHOT_FIELDS}})
    db.update_task(task_id, status=S_REWORK)
    return start_rework(task_id)


def rework_changes(t: dict, insp: dict | None, after: dict | None = None) -> list[dict]:
    """提交表里这一轮返修改动过的格：对比本轮快照与改完后的内容，按提交表列顺序给出。

    after 为改完后的提交字段快照（下一轮登记时的快照），不传时取题目当前内容。
    """
    if not insp or not insp.get('before_snapshot'):
        return []
    before = {**t, **insp['before_snapshot']}
    now = {**t, **after} if after else t
    old = {c['name']: c['value'] for c in submission.row(before)}
    out = []
    for c in submission.row(now):
        if c['value'] != old[c['name']]:
            out.append({'column': c['name'], 'before': old[c['name']], 'after': c['value']})
    return out


def inspections_view(t: dict) -> list[dict]:
    """题目详情用的历次质检：每轮附上这一轮改动过的提交表列名。"""
    rows = db.list_inspections(t['id'])
    out = []
    for n, i in enumerate(rows):
        nxt = rows[n + 1].get('before_snapshot') if n + 1 < len(rows) else None
        out.append({**i, 'changed_columns': [c['column'] for c in rework_changes(t, i, nxt)]})
    return out


def _snapshot_lines(snap: dict) -> list[str]:
    steps = snap.get('record_steps') or []
    return [f"交付完整性 {snap.get('delivery_score')}",
            f"环境可复现等级 {snap.get('reproducibility') or '未填'}",
            '评分理由：', snap.get('score_reason') or '无',
            'Feedback：', snap.get('feedback') or '无',
            '备注：', snap.get('remark') or '无',
            '录屏步骤：', *([dumps(s) for s in steps] or ['无'])]


def _inspection_text(i: dict) -> list[str]:
    head = f"第{i['round']}轮 {i.get('inspect_time') or ''} {i['verdict']} {i['state']}"
    return [head, i['raw_text'], f"当时的处理说明：{i.get('summary') or '无'}"]


def _rework_input(t: dict, insp: dict, verify_dir: Path, fix_dir: Path, repo_dir: Path,
                  run_key: str = '预览') -> str:
    """repo_dir 是读取 diff 与提交记录的仓库（运行时为验收目录的全新克隆）。"""
    snap = insp.get('before_snapshot') or {}
    history = [x for x in db.list_inspections(t['id']) if x['id'] != insp['id']]
    summary = t['traj_summary'] or {'steps': [], 'files_changed': [], 'final_text': ''}
    art = t.get('artifact_sha')
    parts = [
        *run_dir_lines(run_key),
        '## 题目',
        f"分支 {t['branch']}",
        f"所属领域 {t['domain']} 子方向 {t['subdomain']} 难度 {t['difficulty']}",
        f"语言/框架 {t['lang_framework']}",
        'User Prompt 原文：', t['user_prompt'],
        '', '## 内部验收要点', *[f'- {x}' for x in t['acceptance_points'] or []],
        '', f"## 本次质检（第{insp['round']}轮）", insp['raw_text'],
        '', '## 历次质检', *([y for x in history for y in _inspection_text(x)] or ['无']),
        '', '## 当前提交的内容', *_snapshot_lines(snap),
        '', '## 验收时的证据清单',
        *([f"- {e['phenomenon']} | {e['command']} | {e['output_excerpt']}"
           for e in t.get('evidence') or []] or ['无']),
        '', '## 产物',
        f"初始快照 {t['init_sha']}",
        f'当前产物快照 {art}',
        '提交记录（标题以 fix: 返修 开头的是之前返修的改动 不是模型的产出）：',
        gitops.git(repo_dir, 'log', '--format=%h %s', f"{t['init_sha']}..{art}") or '无',
        '', '## 产物 diff（初始快照到当前产物快照）', gitops.diff_text(repo_dir, t['init_sha'], art),
        '', *_verify_lines(t, verify_dir),
        '', '## 修复目录',
        f'WSL 路径 {fix_dir}',
        f'Windows 读取路径 {unc(fix_dir)}',
        '当前产物快照的另一份全新克隆 只用来放对产物的修改 后端会把这里的改动提交并推送为新的产物快照',
        '', *_record_lines(t),
        '', '## 轨迹',
        f"完整轨迹 {unc(Path(t['trajectory_path']))}",
        f"WSL 路径 {t['trajectory_path']}（质检说的第N行指这个 jsonl 文件的物理行号）",
        f"SessionID {t['session_id']} 「继续」次数 {t['continue_count']}",
        trajectory.summary_text(summary),
    ]
    return '\n'.join(parts)


def _rework_dirs(t: dict) -> tuple[Path, Path]:
    """验收目录与修复目录（都在验收目录根下）。"""
    root = config.path_of('verify_root')
    return root / t['branch'], root / f"{t['branch']}-rework"


def start_rework(task_id: int) -> str:
    """AI 按质检原文直接处理：改写提交字段，需要时修改产物并推送为新的产物快照。

    处理完回到已完成；产物改动过或 AI 判断需要重录时先回到待录屏，录完再回到已完成。
    """
    t = need(db.get_task(task_id), S_REWORK)
    insp = db.open_inspection(task_id)
    if not insp:
        raise ActionError('没有找到正在返修的质检记录')

    def work(log, run_key, stop):
        cfg = config.load()
        vdir, fdir = _rework_dirs(t)
        gitops.fresh_clone(cfg['github_repo'], t['branch'], t['artifact_sha'], vdir, log=log)
        gitops.fresh_clone(cfg['github_repo'], t['branch'], t['artifact_sha'], fdir, log=log)
        ignored_before = gitops.ignored_entries(fdir)
        text = _rework_input(t, insp, vdir, fdir, vdir, run_key)
        r = codex_runner.run_skill('qa-rework', text, run_key, log, stop)
        log(f"处理说明 {r['summary']}")
        issues = feedback_issues(r['feedback'], r['delivery_score'])
        if issues:
            raise ActionError(f'返修后的 Feedback 规则检查有 {len(issues)} 处问题：' +
                              '；'.join(f"{i['rule']}「{i['excerpt']}」" for i in issues[:5]))
        # 修复目录有改动时提交推送为新的产物快照（提交标题注明返修，与模型产出区分）
        art = gitops.commit_artifact(fdir, t['branch'], t['init_sha'], ignored_before,
                                     summary=r['summary'].splitlines()[0][:60] if r['summary'] else '',
                                     log=log, title='fix: 返修')
        changed = art['sha'] != t['artifact_sha']
        fields = dict(feedback=r['feedback'], delivery_score=r['delivery_score'], remark=r['remark'],
                      reproducibility=r['reproducibility'], record_steps=r['record_steps'],
                      review_self_check=r['self_check'], failed_from=None, last_error=None)
        if changed:
            log(f"产物快照 {config.permalink(t['artifact_sha'])} → {config.permalink(art['sha'])}")
            fields['artifact_sha'] = art['sha']
            # 做题目录取回新提交，后续 diff 能找到对象
            if (task_dir(t) / '.git').exists():
                gitops.git(task_dir(t), 'fetch', 'origin', t['branch'], check=False, log=log)
        need_record = changed or bool(r['need_rerecord'])
        db.update_inspection(insp['id'], summary=r['summary'], need_record=int(need_record))
        if need_record:
            db.update_task(task_id, **fields, status=S_RECORD, recording_link=None)
            log('需要重新录屏 已回到待录屏')
        else:
            db.update_inspection(insp['id'], state=I_DONE, resolved_time=db.now())
            db.update_task(task_id, **fields, status=S_DONE)

    return jobs.submit('qa-rework', task_id, work, ai=True)


# ---------- 失败重试 ----------

RETRY = {S_INIT: start_prepare, S_ENV: start_prepare, S_COLLECT: start_collect, S_REVIEW: start_review,
         S_REWORK: start_rework}


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
    vdir = config.path_of('verify_root') / t['branch']
    if kind == 'qa-rework':
        insp = db.open_inspection(task_id)
        if not insp:
            raise ActionError('没有正在返修的质检记录')
        vdir, fdir = _rework_dirs(t)
        # 预览时验收目录未必有当前快照，diff 与提交记录从做题目录读取
        return _rework_input(t, insp, vdir, fdir, task_dir(t))
    return _review_input(t, vdir)


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
