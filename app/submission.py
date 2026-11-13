# -*- coding: utf-8 -*-
"""提交表：按提交表的列顺序整理每道题的取值，供页面展示和逐格复制。"""
import json
import subprocess
from pathlib import Path

from . import config

# 提交表列顺序
COLUMNS = ['序号', '分支', 'User Prompt', '所属领域', '子方向', '任务类型', '任务难度', '语言/框架',
           'Harness', 'Harness 版本', '操作系统', '环境可复现等级', '初始环境快照', 'SessionID',
           'TurnID/PromptID', '轨迹文件', '产物快照', '运行录屏', '交付完整性', 'Feedback', '备注']

# 页面上占满整行的长字段
WIDE = {'User Prompt', 'Feedback', '备注'}


def _s(v) -> str:
    return '' if v is None else str(v)


def row(t: dict) -> list[dict]:
    """一道题在提交表中的一行，按列顺序返回 {name, value, wide}。"""
    vals = {
        '序号': t['seq'], '分支': t['branch'], 'User Prompt': t['user_prompt'],
        '所属领域': t['domain'], '子方向': t['subdomain'], '任务类型': t['task_type'],
        '任务难度': t['difficulty'], '语言/框架': t['lang_framework'], 'Harness': t['harness'],
        'Harness 版本': t['harness_version'], '操作系统': t['os'],
        '环境可复现等级': t['reproducibility'], '初始环境快照': config.permalink(t['init_sha']),
        'SessionID': t['session_id'], 'TurnID/PromptID': t['prompt_id'],
        # 轨迹文件即收集环节留存的待上传轨迹
        '轨迹文件': t['trajectory_path'], '产物快照': config.permalink(t['artifact_sha']),
        '运行录屏': t['recording_link'], '交付完整性': t['delivery_score'],
        'Feedback': t['feedback'], '备注': t['remark'],
    }
    return [{'name': c, 'value': _s(vals[c]), 'wide': c in WIDE} for c in COLUMNS]


# ---------- 录屏文件 ----------

def win_path(p: Path) -> str:
    """WSL 挂载路径转换为 Windows 路径，如 /mnt/e/a/b.mp4 → E:\\a\\b.mp4。"""
    parts = Path(p).parts
    if len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1:
        return parts[2].upper() + ':\\' + '\\'.join(parts[3:])
    return str(p)


def _probe(p: Path) -> dict:
    """用 ffprobe 读取时长与分辨率，失败时返回空值，不影响列出文件。"""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=width,height:format=duration', '-of', 'json', str(p)],
            capture_output=True, text=True, timeout=15, check=True).stdout
        d = json.loads(out)
        s = (d.get('streams') or [{}])[0]
        return {'duration': round(float(d['format']['duration']), 1),
                'width': s.get('width'), 'height': s.get('height')}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {'duration': None, 'width': None, 'height': None}


def recordings(limit: int = 5) -> list[dict]:
    """录屏目录下最新的若干个 mp4，按修改时间倒序，附带时长与分辨率供人工核验。"""
    d = Path(config.load()['recording_dir']).expanduser()
    if not d.is_dir():
        raise FileNotFoundError(f'录屏目录不存在：{win_path(d)}')
    files = sorted((f for f in d.iterdir() if f.is_file() and f.suffix.lower() == '.mp4'),
                   key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    import datetime as dt
    result = []
    for f in files:
        st = f.stat()
        result.append({
            'path': win_path(f), 'name': f.name, 'size_mb': round(st.st_size / 1024 / 1024, 1),
            'mtime': dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            **_probe(f),
        })
    return result
