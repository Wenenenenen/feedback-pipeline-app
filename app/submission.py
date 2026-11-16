# -*- coding: utf-8 -*-
"""提交表：按提交表的列顺序整理每道题的取值，供页面展示、逐格复制和导出 Excel。"""
import io
import json
import re
import subprocess
from pathlib import Path

from . import config

# 语言/框架：中文、分隔符、括号都当作分隔；「+」两侧有空格或紧跟字母时才是分隔（保留 c++ 这类名字）
_LANG_SPLIT = re.compile(r'[　-〿＀-￯一-鿿㐀-䶿,;/|()\[\]{}]+'
                         r'|\s+\+\s+|(?<=[a-z0-9])\+(?=[a-z])')
# 版本号：比较符版本（>=5.4）、独立的数字串（可带 v 或 r 前缀、.x 后缀）、
# 紧贴在名字后的点分版本（python3.12）、c++ 后的标准号（c++17）
_LANG_VERSION = re.compile(r'(?:>=|<=|>|<|~|\^)\s*\d+(?:\.\d+)*(?:\.x)?'
                           r'|(?:^|\s)[vr]?\d+(?:\.\d+)*(?:\.x)?(?=\s|$)'
                           r'|(?<=[a-z])\d+(?:\.\d+)+$'
                           r'|(?<=\+\+)\d+$')


def normalize_lang(text: str | None) -> str:
    """提交表的语言/框架：去掉版本号，英文小写，用英文逗号分隔，如 java,python,nodejs。"""
    out = []
    for tok in _LANG_SPLIT.split((text or '').lower()):
        tok = _LANG_VERSION.sub(' ', tok.strip())
        tok = re.sub(r'[^a-z0-9+#\-]', '', tok)  # 去掉空格与点，node.js 记为 nodejs
        if tok and not tok.isdigit() and tok not in out:
            out.append(tok)
    return ','.join(out)

# 提交表列顺序
COLUMNS = ['序号', '分支', 'User Prompt', '所属领域', '子方向', '任务类型', '任务难度', '语言/框架',
           'Harness', 'Harness 版本', '操作系统', '环境可复现等级', '初始环境快照', 'SessionID',
           'TurnID/PromptID', '轨迹文件', '产物快照', '运行录屏', '交付完整性', 'Feedback', '备注']

# 页面上占满整行的长字段
WIDE = {'User Prompt', 'Feedback', '备注'}
# 需要上传文件的列：单元格点击复制文件所在文件夹
UPLOAD_COLUMNS = {'轨迹文件', '运行录屏'}


def _s(v) -> str:
    return '' if v is None else str(v)


def row(t: dict) -> list[dict]:
    """一道题在提交表中的一行，按列顺序返回 {name, value, wide}。"""
    vals = {
        # 序号即甲方序号（出题时分配）
        '序号': t['client_seq'], '分支': t['branch'], 'User Prompt': t['user_prompt'],
        '所属领域': t['domain'], '子方向': t['subdomain'], '任务类型': t['task_type'],
        '任务难度': t['difficulty'], '语言/框架': normalize_lang(t['lang_framework']), 'Harness': t['harness'],
        'Harness 版本': t['harness_version'], '操作系统': t['os'],
        '环境可复现等级': t['reproducibility'], '初始环境快照': config.permalink(t['init_sha']),
        'SessionID': t['session_id'], 'TurnID/PromptID': t['prompt_id'],
        # 轨迹文件即收集环节留存的待上传轨迹，写成 Windows 侧路径方便上传时定位
        '轨迹文件': config.unc(t['trajectory_path']) if t['trajectory_path'] else '',
        '产物快照': config.permalink(t['artifact_sha']),
        '运行录屏': t['recording_link'], '交付完整性': t['delivery_score'],
        'Feedback': t['feedback'], '备注': t['remark'],
    }
    cells = []
    for c in COLUMNS:
        cell = {'name': c, 'value': _s(vals[c]), 'wide': c in WIDE}
        # 要上传的文件点单元格时复制所在文件夹，文件选择框里粘贴即可定位
        if c in UPLOAD_COLUMNS and cell['value']:
            cell['copy'] = config.win_parent(cell['value'])
        cells.append(cell)
    return cells


# ---------- 导出 Excel ----------

# 导出时按数字写入的列，其余一律按文本写入（避免长 ID 被转成科学计数法）
NUMERIC_COLUMNS = {'序号', '交付完整性'}
# 各列的导出宽度（字符数），长文本列放宽并自动换行
_XLSX_WIDTH = {'User Prompt': 60, 'Feedback': 60, '备注': 40, '初始环境快照': 36, '产物快照': 36,
               '轨迹文件': 36, '运行录屏': 30, 'SessionID': 24, 'TurnID/PromptID': 24, '分支': 24}


def to_xlsx(rows: list[list[dict]]) -> bytes:
    """把提交表的若干行（每行为 row() 的结果）写成 xlsx：首行表头，列顺序同 COLUMNS。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = '提交表'
    ws.append(COLUMNS)
    for cells in rows:
        vals = []
        for c in cells:
            v = c['value']
            vals.append(int(v) if c['name'] in NUMERIC_COLUMNS and v.isdigit() else v)
        ws.append(vals)
    head_fill = PatternFill('solid', fgColor='EFEAE0')
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = head_fill
        cell.alignment = Alignment(vertical='center')
    wrap = Alignment(wrap_text=True, vertical='top')
    for line in ws.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = wrap
            # 快照链接写成可点击的超链接
            if isinstance(cell.value, str) and cell.value.startswith(('http://', 'https://')):
                cell.hyperlink = cell.value
                cell.font = Font(color='1F5FBF', underline='single')
    for i, name in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = _XLSX_WIDTH.get(name, 14)
    ws.freeze_panes = 'A2'
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- 录屏文件 ----------

# WSL 挂载路径转 Windows 路径，实现放在 config 中与 Codex 工作区共用
win_path = config.win_path


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
