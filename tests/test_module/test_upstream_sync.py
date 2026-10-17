"""Real Git fixtures exercise selective merge, rollback and interrupted recovery."""
import json
import subprocess
from pathlib import Path

import pytest

from module.update import upstream_sync as sync


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL).decode().strip()


def put(root, path, data):
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data if isinstance(data, bytes) else data.encode())


def commit(root):
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'fixture')
    return git(root, 'rev-parse', 'HEAD')


@pytest.fixture
def repository(tmp_path, monkeypatch):
    upstream, project = tmp_path / 'upstream', tmp_path / 'mac'
    upstream.mkdir()
    project.mkdir()
    git(upstream, 'init', '-b', 'main')
    git(upstream, 'config', 'user.name', 'Fixture')
    git(upstream, 'config', 'user.email', 'fixture@example.invalid')
    base = ''.join(f'value_{i} = {i}\n' for i in range(40))
    put(upstream, 'utils/logic.py', base)
    put(upstream, 'utils/second.py', 'old = 1\n')
    baseline = commit(upstream)
    put(project, 'utils/logic.py', base)
    put(project, 'utils/second.py', 'old = 1\n')
    put(project, 'config.yaml', 'private_fixture: preserve\n')
    put(project, 'settings/terminal.json', '{"quality":"2"}')
    put(project, 'terminal.py', 'mac_only = True\n')
    monkeypatch.setattr(sync, 'SOURCE_URL', str(upstream))
    monkeypatch.setattr(sync, 'INITIAL_BASELINE', baseline)
    return upstream, project, baseline, base


def info(repository):
    upstream, root, baseline, _ = repository
    return sync.check_for_updates(root)


def test_check_reads_real_branch_without_downloading_package(repository):
    upstream, root, baseline, _ = repository
    result = info(repository)
    assert result == sync.UpdateInfo(baseline, baseline, False)
    assert not (root / 'settings/updates').exists()


def test_three_way_merge_preserves_mac_changes_and_excludes_gui(repository):
    upstream, root, baseline, base = repository
    put(root, 'utils/logic.py', base.replace('value_2 = 2', 'value_2 = 200'))
    put(upstream, 'utils/logic.py', base.replace('value_35 = 35', 'value_35 = 3500'))
    put(upstream, 'assets/images/zh_CN/new.png', b'new-resource')
    put(upstream, 'app/window.py', 'import PySide6\n')
    put(upstream, 'terminal.py', 'overwrite_mac = True\n')
    put(upstream, 'config.yaml', 'private_fixture: overwritten\n')
    put(upstream, 'assets/binary/program.exe', b'windows')
    target = commit(upstream)
    result = sync.apply_update(info(repository), root)
    merged = (root / 'utils/logic.py').read_text()
    assert 'value_2 = 200' in merged and 'value_35 = 3500' in merged
    assert (root / 'assets/images/zh_CN/new.png').read_bytes() == b'new-resource'
    assert not (root / 'app').exists() and not (root / 'assets/binary').exists()
    assert (root / 'terminal.py').read_text() == 'mac_only = True\n'
    assert (root / 'config.yaml').read_text() == 'private_fixture: preserve\n'
    assert json.loads((root / 'settings/terminal.json').read_text()) == {'quality': '2'}
    assert result.changed == 2 and result.ignored == 4
    assert (Path(result.backup_dir) / 'utils/logic.py').read_text().find('value_2 = 200') >= 0
    assert sync._baseline(root) == target
    assert not info(repository).available


def test_conflict_aborts_all_changes_and_keeps_baseline(repository):
    upstream, root, baseline, base = repository
    local = base.replace('value_2 = 2', 'value_2 = 200')
    put(root, 'utils/logic.py', local)
    put(upstream, 'utils/logic.py', base.replace('value_2 = 2', 'value_2 = 300'))
    put(upstream, 'utils/second.py', 'old = 2\n')
    commit(upstream)
    with pytest.raises(sync.UpdateConflictError) as failure:
        sync.apply_update(info(repository), root)
    assert 'utils/logic.py' in failure.value.paths
    assert (root / 'utils/logic.py').read_text() == local
    assert (root / 'utils/second.py').read_text() == 'old = 1\n'
    assert sync._baseline(root) == baseline
    assert not (root / sync.STATE_PATH).exists()


def test_write_failure_rolls_back_files_and_state(repository, monkeypatch):
    upstream, root, baseline, base = repository
    put(upstream, 'utils/logic.py', base.replace('value_35 = 35', 'value_35 = 3500'))
    put(upstream, 'utils/second.py', 'old = 2\n')
    commit(upstream)
    real_write = sync._atomic_write
    failed = False
    def fail_once(path, data, mode=0o644):
        nonlocal failed
        if path == root / 'utils/second.py' and not failed:
            failed = True
            raise OSError('simulated disk failure')
        return real_write(path, data, mode)
    monkeypatch.setattr(sync, '_atomic_write', fail_once)
    with pytest.raises(OSError, match='simulated'):
        sync.apply_update(info(repository), root)
    assert (root / 'utils/logic.py').read_text() == base
    assert (root / 'utils/second.py').read_text() == 'old = 1\n'
    assert sync._baseline(root) == baseline
    journals = list((root / 'settings/updates').glob('update-*/transaction.json'))
    assert json.loads(journals[0].read_text())['status'] == 'rolled_back'


def test_next_check_recovers_interrupted_transaction(repository):
    upstream, root, baseline, base = repository
    put(upstream, 'utils/logic.py', base.replace('value_35 = 35', 'value_35 = 3500'))
    commit(upstream)
    result = sync.apply_update(info(repository), root)
    journal_path = Path(result.backup_dir).parent / 'transaction.json'
    journal = json.loads(journal_path.read_text())
    journal['status'] = 'applying'  # simulate crash after writing state, before completion marker
    journal_path.write_text(json.dumps(journal))
    checked = info(repository)
    assert checked.baseline == baseline and checked.available
    assert (root / 'utils/logic.py').read_text() == base
    assert json.loads(journal_path.read_text())['status'] == 'rolled_back'


def test_recovery_does_not_overwrite_post_crash_user_edit(repository):
    upstream, root, baseline, base = repository
    put(upstream, 'utils/logic.py', base.replace('value_35 = 35', 'value_35 = 3500'))
    commit(upstream)
    result = sync.apply_update(info(repository), root)
    journal_path = Path(result.backup_dir).parent / 'transaction.json'
    journal = json.loads(journal_path.read_text()); journal['status'] = 'applying'
    journal_path.write_text(json.dumps(journal))
    put(root, 'utils/logic.py', 'user_edit_after_crash = True\n')
    with pytest.raises(sync.UpdateRecoveryError, match='中断后文件又有改动'):
        info(repository)
    assert (root / 'utils/logic.py').read_text() == 'user_edit_after_crash = True\n'


def test_update_is_blocked_while_game_runner_holds_lock(repository):
    import fcntl
    upstream, root, baseline, base = repository
    put(upstream, 'utils/second.py', 'old = 2\n'); commit(upstream)
    with (root / '.terminal_runner.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(sync.UpdateError, match='正在运行'):
            sync.apply_update(info(repository), root)
    assert (root / 'utils/second.py').read_text() == 'old = 1\n'


def test_unmodified_upstream_deletion_is_applied(repository):
    upstream, root, baseline, base = repository
    (upstream / 'utils/second.py').unlink(); commit(upstream)
    assert sync.apply_update(info(repository), root).changed == 1
    assert not (root / 'utils/second.py').exists()


def test_intentional_local_deletion_is_retained(repository):
    upstream, root, baseline, base = repository
    (root / 'utils/second.py').unlink()
    put(upstream, 'utils/second.py', 'old = 2\n'); commit(upstream)
    assert sync.apply_update(info(repository), root).changed == 0
    assert not (root / 'utils/second.py').exists()


def test_new_windows_dependency_requires_compatibility(repository):
    upstream, root, baseline, base = repository
    put(upstream, 'utils/new_feature.py', 'import PySide6\n'); commit(upstream)
    with pytest.raises(sync.UpdateConflictError, match='Windows'):
        sync.apply_update(info(repository), root)
    assert not (root / 'utils/new_feature.py').exists()
    assert sync._baseline(root) == baseline


def test_unsafe_source_symlink_never_writes_outside_project(repository, tmp_path):
    upstream, root, baseline, base = repository
    external = tmp_path / 'outside'; external.mkdir()
    put(external, 'logic.py', 'outside = True\n')
    (root / 'utils/logic.py').unlink()
    (root / 'utils/logic.py').symlink_to(external / 'logic.py')
    put(upstream, 'utils/logic.py', base.replace('value_35 = 35', 'value_35 = 3500')); commit(upstream)
    with pytest.raises(sync.UpdateError, match='符号链接'):
        sync.apply_update(info(repository), root)
    assert (external / 'logic.py').read_text() == 'outside = True\n'


def test_invalid_recovery_journal_blocks_check(repository):
    upstream, root, baseline, base = repository
    path = root / 'settings/updates/update-broken/transaction.json'
    put(root, str(path.relative_to(root)), '{"status":"applying"}')
    with pytest.raises(sync.UpdateRecoveryError):
        info(repository)


def test_binary_conflict_is_never_overwritten(repository):
    upstream, root, baseline, base = repository
    put(upstream, 'assets/images/zh_CN/item.png', b'base\0data')
    second_base = commit(upstream)
    sync.INITIAL_BASELINE = second_base  # fixture's monkeypatch restores it afterwards
    put(root, 'assets/images/zh_CN/item.png', b'local\0data')
    put(upstream, 'assets/images/zh_CN/item.png', b'remote\0data'); commit(upstream)
    with pytest.raises(sync.UpdateConflictError):
        sync.apply_update(info(repository), root)
    assert (root / 'assets/images/zh_CN/item.png').read_bytes() == b'local\0data'


def test_parent_file_is_detected_before_any_replacement(repository):
    upstream, root, baseline, base = repository
    with pytest.raises(sync.UpdateError, match='父路径不是目录'):
        sync._path(root, 'utils/logic.py/new.py')


def test_text_merge_normalizes_windows_eol_without_losing_mac_patch(tmp_path):
    base_lf = ''.join(f'value_{i} = {i}\n' for i in range(40))
    base = base_lf.replace('\n', '\r\n').encode()
    local = base_lf.replace('value_2 = 2', 'value_2 = 200').encode()
    upstream = base.replace(b'value_35 = 35', b'value_35 = 3500')
    merged, conflict = sync._merge(local, base, upstream, tmp_path)
    assert conflict is None
    assert b'value_2 = 200' in merged and b'value_35 = 3500' in merged
    assert b'\r\n' not in merged


def test_binary_resources_without_nul_are_still_protected(tmp_path):
    merged, conflict = sync._merge(b'local', b'base', b'upstream', tmp_path, text=False)
    assert merged is None and '二进制' in conflict
