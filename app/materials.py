# -*- coding: utf-8 -*-
"""素材准备：按清单下载、执行生成脚本、人工确认、大小校验。"""
import shutil
import subprocess
import urllib.request
from pathlib import Path

MAX_MB = 100


def _safe_target(task_dir: Path, rel: str) -> Path:
    """素材路径必须位于做题目录内。"""
    target = (task_dir / rel).resolve()
    if task_dir.resolve() not in target.parents and target != task_dir.resolve():
        raise ValueError(f'素材路径越界：{rel}')
    return target


def download(task_dir: Path, item: dict, log) -> dict:
    target = _safe_target(task_dir, item['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    log(f"下载 {item['url']} -> {item['path']}")
    req = urllib.request.Request(item['url'], headers={'User-Agent': 'Mozilla/5.0'})
    tmp = target.with_suffix(target.suffix + '.part')
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f, length=1024 * 1024)
    tmp.replace(target)
    return check(task_dir, item)


def run_script(task_dir: Path, item: dict, log, timeout: int = 1800) -> dict:
    """执行生成脚本（准备环境时自动执行），工作目录为做题目录。"""
    log(f"执行生成脚本 {item['path']}\n{item['script']}")
    # 新会话执行，避免交互式 bash 抢占后端的控制终端（原因见 envcheck._bash）
    p = subprocess.run(['bash', '-ic', item['script']], cwd=task_dir, capture_output=True,
                       text=True, timeout=timeout, encoding='utf-8', errors='replace',
                       stdin=subprocess.DEVNULL, start_new_session=True)
    log(p.stdout[-3000:])
    if p.returncode != 0:
        log(p.stderr[-3000:])
        return {**item, 'state': 'failed', 'note': f'脚本退出码 {p.returncode}'}
    return check(task_dir, item)


def check(task_dir: Path, item: dict) -> dict:
    target = _safe_target(task_dir, item['path'])
    if not target.exists():
        return {**item, 'state': 'missing', 'note': '文件不存在'}
    size = sum(f.stat().st_size for f in target.rglob('*') if f.is_file()) if target.is_dir() \
        else target.stat().st_size
    mb = round(size / 1024 / 1024, 1)
    if target.is_file() and mb > MAX_MB:
        return {**item, 'state': 'too_large', 'note': f'{mb}MB 超过 {MAX_MB}MB', 'actual_mb': mb}
    note = f'{mb}MB' + ('（超过 50MB）' if mb > 50 else '')
    return {**item, 'state': 'ready', 'note': note, 'actual_mb': mb}
