# -*- coding: utf-8 -*-
"""git 操作：孤儿分支初始化、初始快照、重做重置、产物快照、全新克隆。"""
import shutil
import subprocess
from pathlib import Path

GITIGNORE = """# 凭据文件
.env
.env.*
!.env.example
*.key
*.pem
*.p12
*.pfx
id_rsa*
credentials*.json
secrets*.json
# 缓存目录
.cache/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
"""

# 补交被忽略文件时排除的依赖与缓存目录
DEP_DIRS = {'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'target',
            '.cache', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.gradle', '.next',
            '.nuxt', '.turbo', '.parcel-cache', 'out', 'bin', 'obj', '.idea', '.vscode'}


class GitError(RuntimeError):
    pass


def git(cwd: Path, *args: str, check: bool = True, log=None, strip: bool = True) -> str:
    # 关闭路径转义，中文等非 ASCII 文件名按原样输出，避免把转义串当路径传回 git
    cmd = ['git', '-c', 'core.quotepath=off', *args]
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    if log:
        log(f"$ git {' '.join(args)}")
        if p.stdout.strip():
            log(p.stdout.strip()[-2000:])
        if p.stderr.strip():
            log(p.stderr.strip()[-2000:])
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)} 失败：{p.stderr.strip()[-500:]}")
    return p.stdout.strip() if strip else p.stdout


def remote_head(cwd: Path, branch: str, log=None) -> str | None:
    out = git(cwd, 'ls-remote', '--heads', 'origin', branch, log=log)
    for line in out.splitlines():
        sha, ref = line.split('\t', 1)
        if ref == f'refs/heads/{branch}':
            return sha
    return None


def is_ancestor(cwd: Path, a: str, b: str) -> bool:
    p = subprocess.run(['git', 'merge-base', '--is-ancestor', a, b], cwd=cwd, capture_output=True)
    return p.returncode == 0


def init_task_dir(task_dir: Path, branch: str, repo: str, log=None):
    """创建做题目录并初始化为孤儿分支，此时不推送。"""
    if task_dir.exists() and any(task_dir.iterdir()):
        raise GitError(f'做题目录已存在且非空：{task_dir}')
    task_dir.mkdir(parents=True, exist_ok=True)
    git(task_dir, 'init', '-b', branch, log=log)
    git(task_dir, 'remote', 'add', 'origin', repo, log=log)
    if remote_head(task_dir, branch, log=log):
        raise GitError(f'远端已存在分支 {branch}，请换一个分支名')
    (task_dir / '.gitignore').write_text(GITIGNORE, encoding='utf-8')


def _ls_ignored(task_dir: Path, *extra: str) -> list[str]:
    """用 -z 列出被忽略的路径：空字节分隔，任何文件名都不会被加引号转义。"""
    out = git(task_dir, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', *extra,
              strip=False)
    return [x for x in out.split('\0') if x]


_C_ESCAPES = {'a': 7, 'b': 8, 't': 9, 'n': 10, 'v': 11, 'f': 12, 'r': 13, '"': 34, '\\': 92}
_OCTAL = '01234567'


def _unquote(path: str) -> str:
    """还原 git 按 core.quotepath 输出的 C 风格引号路径（兼容旧任务记录里的转义串）。"""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path
    s, out, i = path[1:-1], bytearray(), 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            oct3 = s[i + 1:i + 4]
            if len(oct3) == 3 and all(ch in _OCTAL for ch in oct3):
                out.append(int(oct3, 8) & 0xFF)
                i += 4
                continue
            if s[i + 1] in _C_ESCAPES:
                out.append(_C_ESCAPES[s[i + 1]])
                i += 2
                continue
        out += c.encode('utf-8')
        i += 1
    return out.decode('utf-8', errors='replace')


def ignored_entries(task_dir: Path) -> list[str]:
    """当前被忽略的文件与目录（目录以 / 结尾，不展开）。"""
    return sorted(_ls_ignored(task_dir, '--directory'))


def commit_initial(task_dir: Path, branch: str, log=None) -> str:
    """提交并推送初始快照，返回 40 位 SHA。"""
    git(task_dir, 'add', '-A', log=log)
    git(task_dir, 'commit', '-m', 'chore: 初始化仓库', log=log)
    git(task_dir, 'push', '-u', 'origin', f'HEAD:refs/heads/{branch}', log=log)
    return git(task_dir, 'rev-parse', 'HEAD')


def reset_to_initial(task_dir: Path, branch: str, init_sha: str, log=None):
    """重做：本地恢复到初始快照；远端若被作废会话推送过，退回到初始快照。"""
    git(task_dir, 'reset', '--hard', init_sha, log=log)
    git(task_dir, 'clean', '-xfd', log=log)
    head = remote_head(task_dir, branch, log=log)
    if head and head != init_sha:
        git(task_dir, 'fetch', 'origin', branch, log=log)
        if not is_ancestor(task_dir, init_sha, head):
            raise GitError('远端分支历史已被改写，初始快照不再是其祖先，请人工处理')
        # 作废会话的提交未被记录，退回分支指针不影响初始快照可达性
        git(task_dir, 'push', '--force-with-lease=refs/heads/%s:%s' % (branch, head),
            'origin', f'{init_sha}:refs/heads/{branch}', log=log)


def _is_dep_entry(entry: str) -> bool:
    parts = [p for p in entry.rstrip('/').split('/') if p]
    return any(p in DEP_DIRS for p in parts)


# 凭据文件名模式：即使是会话期间新出现的被忽略文件，也绝不补交
SECRET_PATTERNS = ('.env', '.env.*', '*.key', '*.pem', '*.p12', '*.pfx', 'id_rsa*',
                   'credentials*.json', 'secrets*.json', '*.keystore', '.npmrc', '.pypirc')


def _is_secret_entry(entry: str) -> bool:
    import fnmatch
    name = entry.rstrip('/').split('/')[-1]
    if name == '.env.example':
        return False
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def _forced_ignored(task_dir: Path, ignored_before: list[str]) -> list[str]:
    """会话期间新出现、需要用 -f 补交的被忽略文件（排除依赖缓存与凭据）。"""
    # 旧任务记录的可能是带引号的转义路径，统一还原后再比较
    before = {_unquote(x) for x in (ignored_before or [])}
    forced = []
    for e in ignored_entries(task_dir):
        if e in before or _is_dep_entry(e):
            continue
        if e.endswith('/'):
            # 目录展开成文件逐个过滤，避免把目录里的凭据一起带进去
            forced += [f for f in _ls_ignored(task_dir, '--', e)
                       if not _is_dep_entry(f) and not _is_secret_entry(f)]
        elif not _is_secret_entry(e):
            forced.append(e)
    # git 可能同时列出目录与目录里的文件，去重并保持顺序
    return list(dict.fromkeys(forced))


def commit_artifact(task_dir: Path, branch: str, init_sha: str, ignored_before: list[str],
                    summary: str = '', log=None, title: str = 'feat: 模型产物') -> dict:
    """提交产物快照：校验远端、补交会话期间新出现的被忽略文件、推送。

    返修时在修复目录（产物快照的全新克隆）里复用本函数，title 改为返修的提交标题。
    """
    if not is_ancestor(task_dir, init_sha, 'HEAD'):
        raise GitError('本地 HEAD 不再以初始快照为祖先（模型可能改写了历史）')
    head = remote_head(task_dir, branch, log=log)
    if head is None:
        raise GitError('远端分支不存在（可能被模型删除）')
    if head != init_sha:
        git(task_dir, 'fetch', 'origin', branch, log=log)
        if not is_ancestor(task_dir, init_sha, head):
            raise GitError('远端分支被强推改写，初始快照不再是其祖先')
        if not is_ancestor(task_dir, head, 'HEAD'):
            raise GitError('远端分支有本地没有的提交，请人工检查')
    git(task_dir, 'add', '-A', log=log)
    forced = _forced_ignored(task_dir, ignored_before)
    for e in forced:
        git(task_dir, 'add', '-f', '--', e, log=log)
    staged = git(task_dir, 'diff', '--cached', '--name-only')
    if staged:
        if log:
            log('本次提交的文件：' + ' '.join(staged.split()))
        git(task_dir, 'commit', '-m', f"{title} - {summary or branch}", log=log)
    git(task_dir, 'push', 'origin', f'HEAD:refs/heads/{branch}', log=log)
    sha = git(task_dir, 'rev-parse', 'HEAD')
    if not is_ancestor(task_dir, init_sha, sha):
        raise GitError('初始快照不是产物快照的祖先')
    return {'sha': sha, 'forced_ignored': forced, 'model_commits':
            int(git(task_dir, 'rev-list', '--count', f'{init_sha}..{sha}'))}


def fresh_clone(repo: str, branch: str, sha: str, dest: Path, log=None):
    """把指定提交全新克隆到 dest（先删除旧目录）。"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(dest.parent, 'clone', '--branch', branch, '--single-branch', repo, dest.name, log=log)
    git(dest, 'checkout', '--detach', sha, log=log)


def diff_text(task_dir: Path, a: str, b: str, limit: int = 60000) -> str:
    """产物 diff：先给统计，再给正文（超长截断）。"""
    stat = git(task_dir, 'diff', '--stat', a, b)
    body = git(task_dir, 'diff', a, b, '--', '.', ':(exclude)*.lock', ':(exclude)package-lock.json')
    if len(body) > limit:
        body = body[:limit] + f'\n...（diff 超长，已截断，总长 {len(body)} 字符）'
    return stat + '\n\n' + body


def large_files(task_dir: Path, warn_mb: int = 50, max_mb: int = 100) -> dict:
    """统计工作区内超限的大文件（不含 .git）。"""
    warn, block = [], []
    for f in task_dir.rglob('*'):
        if '.git' in f.parts or not f.is_file():
            continue
        mb = f.stat().st_size / 1024 / 1024
        rel = str(f.relative_to(task_dir))
        if mb > max_mb:
            block.append((rel, round(mb, 1)))
        elif mb > warn_mb:
            warn.append((rel, round(mb, 1)))
    return {'warn': warn, 'block': block}
