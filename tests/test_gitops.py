# -*- coding: utf-8 -*-
import pytest

from app import gitops as G
from tests.conftest import run


@pytest.fixture()
def remote(tmp_path, git_identity):
    r = tmp_path / 'remote.git'
    run(['git', 'init', '--bare', str(r)], tmp_path)
    return r


def setup_task(tmp_path, remote, branch='demo'):
    d = tmp_path / 'tasks' / branch
    G.init_task_dir(d, branch, str(remote))
    (d / 'samples').mkdir()
    (d / 'samples' / 'in.txt').write_text('素材', encoding='utf-8')
    sha = G.commit_initial(d, branch)
    return d, sha, G.ignored_entries(d)


def test_init_and_initial_snapshot(tmp_path, remote):
    d, sha, ignored = setup_task(tmp_path, remote)
    assert len(sha) == 40
    assert G.remote_head(d, 'demo') == sha
    assert run(['git', 'rev-list', '--max-parents=0', 'HEAD'], d) == sha  # 孤儿分支只有一个根提交
    assert '.env' in (d / '.gitignore').read_text()
    with pytest.raises(G.GitError):
        G.init_task_dir(tmp_path / 'tasks' / 'other', 'demo', str(remote))  # 远端已有同名分支


def test_artifact_with_new_ignored_files(tmp_path, remote):
    d, init, ignored = setup_task(tmp_path, remote)
    # 模拟模型：写代码、自己加了忽略规则产生的输出文件、装了依赖、写了凭据
    (d / 'main.go').write_text('package main', encoding='utf-8')
    (d / '.gitignore').write_text((d / '.gitignore').read_text() + 'output/\nnode_modules/\n')
    (d / 'output').mkdir()
    (d / 'output' / 'result.mp4').write_bytes(b'x')
    (d / 'output' / 'server.pem').write_text('私钥')
    (d / 'node_modules' / 'pkg').mkdir(parents=True)
    (d / 'node_modules' / 'pkg' / 'index.js').write_text('x')
    (d / '.env').write_text('TOKEN=secret')
    run(['git', 'add', 'main.go'], d)
    run(['git', 'commit', '-m', '模型自己的提交'], d)
    art = G.commit_artifact(d, 'demo', init, ignored, summary='直播服务')
    assert run(['git', 'log', '-1', '--format=%s'], d) == 'feat: 模型产物 - 直播服务'
    assert run(['git', 'log', '--format=%s', init, '-1'], d) == 'chore: 初始化仓库'
    files = run(['git', 'ls-tree', '-r', '--name-only', art['sha']], d).splitlines()
    assert 'main.go' in files and 'output/result.mp4' in files
    assert not any(f.startswith('node_modules') for f in files)
    # .env 是会话期间新出现的被忽略文件，但属于凭据，不得补交
    assert '.env' not in files
    assert '.env' not in art['forced_ignored']
    assert 'output/server.pem' not in files
    assert G.is_ancestor(d, init, art['sha'])
    assert G.remote_head(d, 'demo') == art['sha']
    assert art['model_commits'] == 2


def test_redo_resets_local_and_remote(tmp_path, remote):
    d, init, ignored = setup_task(tmp_path, remote)
    (d / 'a.txt').write_text('作废会话的产物')
    run(['git', 'add', '-A'], d)
    run(['git', 'commit', '-m', 'x'], d)
    run(['git', 'push', 'origin', 'HEAD:refs/heads/demo'], d)
    (d / 'untracked.txt').write_text('y')
    G.reset_to_initial(d, 'demo', init)
    assert not (d / 'a.txt').exists() and not (d / 'untracked.txt').exists()
    assert (d / 'samples' / 'in.txt').exists()
    assert G.remote_head(d, 'demo') == init


def test_detect_force_push(tmp_path, remote):
    d, init, ignored = setup_task(tmp_path, remote)
    other = tmp_path / 'other'
    run(['git', 'clone', str(remote), str(other)], tmp_path)
    run(['git', 'checkout', '--orphan', 'x'], other)
    (other / 'z.txt').write_text('z')
    run(['git', 'add', '-A'], other)
    run(['git', 'commit', '-m', '改写'], other)
    run(['git', 'push', '-f', 'origin', 'HEAD:refs/heads/demo'], other)
    with pytest.raises(G.GitError, match='强推'):
        G.commit_artifact(d, 'demo', init, ignored)


def test_rework_commit_in_fresh_clone(tmp_path, remote):
    """返修：在产物快照的全新克隆（修复目录）里改动后提交推送，产物快照前进且仍以初始快照为祖先。"""
    d, init, ignored = setup_task(tmp_path, remote)
    (d / 'main.go').write_text('package main', encoding='utf-8')
    art = G.commit_artifact(d, 'demo', init, ignored, summary='初版')
    fix = tmp_path / 'verify' / 'demo-rework'
    G.fresh_clone(str(remote), 'demo', art['sha'], fix)
    before = G.ignored_entries(fix)
    # 修复目录没有改动时不产生新提交
    assert G.commit_artifact(fix, 'demo', init, before, title='fix: 返修')['sha'] == art['sha']
    # AI 补上漏交的测试和基准文件，其中一个被忽略规则挡住，依赖目录不应带进去
    (fix / '.gitignore').write_text((fix / '.gitignore').read_text() + '*.stl\n')
    (fix / 'tests' / 'data').mkdir(parents=True)
    (fix / 'tests' / 'data' / 'baseline.stl').write_bytes(b'solid')
    (fix / 'tests' / 'test_main.py').write_text('def test_ok(): pass', encoding='utf-8')
    (fix / 'node_modules' / 'x').mkdir(parents=True)
    (fix / 'node_modules' / 'x' / 'i.js').write_text('x')
    (fix / '.gitignore').write_text((fix / '.gitignore').read_text() + 'node_modules/\n')
    new = G.commit_artifact(fix, 'demo', init, before, summary='补交测试', title='fix: 返修')
    assert run(['git', 'log', '-1', '--format=%s'], fix) == 'fix: 返修 - 补交测试'
    files = run(['git', 'ls-tree', '-r', '--name-only', new['sha']], fix).splitlines()
    assert 'tests/data/baseline.stl' in files and 'tests/test_main.py' in files
    assert not any(f.startswith('node_modules') for f in files)
    assert G.is_ancestor(fix, art['sha'], new['sha']) and G.remote_head(fix, 'demo') == new['sha']


def test_fresh_clone(tmp_path, remote):
    d, init, ignored = setup_task(tmp_path, remote)
    dest = tmp_path / 'verify' / 'demo'
    G.fresh_clone(str(remote), 'demo', init, dest)
    assert (dest / 'samples' / 'in.txt').exists()
    assert run(['git', 'rev-parse', 'HEAD'], dest) == init


def test_dep_entry_filter():
    assert G._is_dep_entry('web/node_modules/')
    assert G._is_dep_entry('__pycache__/')
    assert not G._is_dep_entry('output/result.mp4')


def test_secret_entry_filter():
    for e in ('.env', 'server/.env.local', 'certs/server.pem', 'id_rsa', 'config/credentials.json'):
        assert G._is_secret_entry(e), e
    assert not G._is_secret_entry('.env.example')
    assert not G._is_secret_entry('output/result.mp4')
