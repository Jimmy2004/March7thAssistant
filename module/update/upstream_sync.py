"""Selective, transactional upstream source updates for the Mac cloud fork."""
from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass

SOURCE_URL = 'https://github.com/moesnow/March7thAssistant.git'
INITIAL_BASELINE = '7c94facb070850895710263b1c72a37e32da92bf'
STATE_PATH = 'settings/upstream-sync.json'
UPDATES_PATH = 'settings/updates'
SHA = re.compile(r'^[0-9a-f]{40}$')
FORBIDDEN_IMPORTS = {'PySide6', 'PySide2', 'PyQt5', 'PyQt6', 'qfluentwidgets',
                     'win32api', 'win32con', 'win32gui', 'win32ui', 'winreg',
                     'pyautogui', 'keyboard', 'pygetwindow'}


class UpdateError(RuntimeError):
    pass


class UpdateRecoveryError(UpdateError):
    """An incomplete transaction could not be restored; startup must stop."""


class UpdateConflictError(UpdateError):
    def __init__(self, paths, report_dir):
        self.paths = list(paths)
        self.report_dir = str(report_dir)
        super().__init__('以下文件需要兼容处理，项目未改动：' + '、'.join(paths)
                         + f'；待处理内容：{report_dir}')


@dataclass(frozen=True)
class UpdateInfo:
    baseline: str
    target: str
    available: bool


@dataclass(frozen=True)
class UpdateResult:
    changed: int
    ignored: int
    backup_dir: str


def _git(directory, *args, timeout=60, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    try:
        proc = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(directory), *args],
                              capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError(f'Git 操作失败：{exc}') from exc
    if check and proc.returncode:
        detail = proc.stderr.decode('utf-8', errors='replace').strip()[-600:]
        raise UpdateError(f'Git 操作失败：{detail}')
    return proc


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise UpdateError('更新版本标识无效')
    return value


def _path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or '..' in parts or '\\' in relative:
        raise UpdateError(f'不安全的更新路径：{relative}')
    result = root
    for index, part in enumerate(parts):
        result = result / part
        if index < len(parts) - 1 and result.exists() and not result.is_dir():
            raise UpdateError(f"更新父路径不是目录：{relative}")
        if result.is_symlink():
            raise UpdateError(f'更新路径含符号链接，未修改：{relative}')
    if not result.resolve().is_relative_to(root.resolve()):
        raise UpdateError(f'更新路径超出项目：{relative}')
    return result


def _atomic_write(path: Path, data: bytes, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_write(path, payload):
    _atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode())


def _digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def _read(path):
    if path.exists() and not path.is_file():
        raise UpdateError(f'预期为文件：{path}')
    return path.read_bytes() if path.exists() else None


def _baseline(root):
    path = _path(root, STATE_PATH)
    if not path.exists():
        return INITIAL_BASELINE
    try:
        state = json.loads(path.read_text())
        if state.get('repository') != SOURCE_URL:
            raise ValueError('repository')
        return _sha(state['baseline'])
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateError('上游更新状态文件损坏，请从备份恢复 settings/upstream-sync.json') from exc


@contextlib.contextmanager
def _lock(root):
    import fcntl
    path = _path(root, '.terminal_runner.lock')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError('另一个任务或更新正在运行，请结束后再更新') from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _restore(root, transaction, journal):
    """Restore a journal only if files still match its before/after snapshots."""
    from .sync_policy import is_managed_path
    for record in journal['files']:
        relative = record['path']
        if not is_managed_path(relative):
            raise UpdateError(f'恢复清单含不允许的路径：{relative}')
        actual = _digest(_read(_path(root, relative)))
        if actual not in (record['before'], record['after']):
            raise UpdateError(f'更新中断后文件又有改动，未覆盖：{relative}；备份：{transaction}')
        if record['before'] is not None:
            data = _read(_path(transaction / 'backup', relative))
            if _digest(data) != record['before']:
                raise UpdateError(f'更新备份校验失败：{relative}')
    state_path = _path(root, STATE_PATH)
    state = _read(state_path)
    if _digest(state) not in (journal['state_before'], journal['state_after']):
        raise UpdateError(f'更新状态在中断后被修改，备份：{transaction}')
    state_backup = transaction / 'state-before.json'
    if journal['state_before'] is not None and _digest(_read(state_backup)) != journal['state_before']:
        raise UpdateError(f'更新状态备份校验失败：{transaction}')
    for record in reversed(journal['files']):
        path = _path(root, record['path'])
        if record['before'] is None:
            path.unlink(missing_ok=True)
        else:
            _atomic_write(path, _path(transaction / 'backup', record['path']).read_bytes(), record['mode'])
    if journal['state_before'] is None:
        state_path.unlink(missing_ok=True)
    else:
        backup = transaction / 'state-before.json'
        if _digest(_read(backup)) != journal['state_before']:
            raise UpdateError(f'更新状态备份校验失败：{transaction}')
        _atomic_write(state_path, backup.read_bytes())
    journal['status'] = 'rolled_back'
    _json_write(transaction / 'transaction.json', journal)


def _recover(root, locked=False):
    updates = _path(root, UPDATES_PATH)
    pending = []
    if updates.exists():
        for path in sorted(updates.glob('update-*/transaction.json')):
            if path.is_symlink() or path.parent.is_symlink():
                raise UpdateError('更新恢复目录不能为符号链接')
            try:
                journal = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise UpdateError(f'更新恢复记录损坏：{path}') from exc
            if journal.get('status') not in {'applying', 'complete', 'rolled_back'}:
                raise UpdateError(f'更新恢复记录状态无效：{path}')
            if journal.get('status') == 'applying':
                pending.append((path.parent, journal))
    if not pending:
        return
    if not locked:
        with _lock(root):
            _recover(root, locked=True)
        return
    for transaction, journal in pending:
        _restore(root, transaction, journal)


def check_for_updates(root: Path) -> UpdateInfo:
    """Check only the upstream branch head; download no release packages."""
    root = Path(root).resolve()
    try:
        _recover(root)
    except Exception as exc:
        raise UpdateRecoveryError(str(exc)) from exc
    baseline = _baseline(root)
    result = _git(root, 'ls-remote', '--exit-code', '--heads', SOURCE_URL, 'main', timeout=12)
    lines = result.stdout.decode().splitlines()
    if len(lines) != 1 or len(lines[0].split()) != 2 or lines[0].split()[1] != 'refs/heads/main':
        raise UpdateError('未能确认上游 main 分支')
    target = _sha(lines[0].split()[0])
    return UpdateInfo(baseline, target, target != baseline)


def _cache(root, target):
    cache = _path(root, UPDATES_PATH + '/upstream.git')
    if not cache.exists():
        cache.mkdir(parents=True)
        _git(cache, 'init', '--bare')
        _git(cache, 'remote', 'add', 'origin', SOURCE_URL)
        _git(cache, 'config', 'remote.origin.promisor', 'true')
        _git(cache, 'config', 'remote.origin.partialclonefilter', 'blob:none')
    remote = _git(cache, 'remote', 'get-url', 'origin').stdout.decode().strip()
    if remote != SOURCE_URL:
        raise UpdateError('更新缓存来源与官方仓库不一致')
    print(f'正在下载上游源码 {target[:12]}...', flush=True)
    _git(cache, 'fetch', '--filter=blob:none', '--no-tags', 'origin', target, timeout=180)
    return cache


def _blob(cache, commit, relative):
    result = _git(cache, 'ls-tree', '-z', commit, '--', relative)
    if not result.stdout:
        return None
    metadata, path = result.stdout.split(b'\t', 1)
    mode, kind, oid = metadata.split()
    if kind != b'blob' or mode not in (b'100644', b'100755'):
        raise UpdateError(f'不支持上游符号链接或子模块：{relative}')
    return _git(cache, 'show', f'{commit}:{relative}').stdout


def _merge(local, base, incoming, directory, text=True):
    if local == incoming or incoming == base:
        return local, None
    if local is None and base is not None:
        return None, None  # Keep intentional deletions in this fork.
    if local == base:
        return incoming, None
    if base is None or incoming is None:
        return None, '本地和上游存在不同的新增或删除'
    if not text or any(b'\0' in data for data in (local, base, incoming)):
        return None, '二进制资源两边均有修改'
    try:
        for data in (local, base, incoming):
            data.decode('utf-8-sig')
    except UnicodeError:
        return None, '二进制资源两边均有修改'
    crlf = b'\r\n' in local and b'\n' not in local.replace(b'\r\n', b'')
    normalized = [data.replace(b'\r\n', b'\n') for data in (local, base, incoming)]
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        paths = [Path(temporary) / name for name in ('local', 'base', 'upstream')]
        for path, data in zip(paths, normalized):
            path.write_bytes(data)
        result = _git(directory, 'merge-file', '-p', '--diff3', *map(str, paths), check=False)
    merged = result.stdout.replace(b'\n', b'\r\n') if crlf else result.stdout
    return (merged, None) if result.returncode == 0 else (merged, '三方合并冲突')


def _imports(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split('.')[0])
    return names


def _validate_python(relative, data, old):
    if data is None or not relative.endswith('.py'):
        return
    try:
        tree = ast.parse(data, filename=relative)
        previous = _imports(ast.parse(old, filename=relative)) if old else set()
    except (SyntaxError, UnicodeError) as exc:
        raise UpdateError(f'更新后的 Python 代码无法解析：{relative}：{exc}') from exc
    for name in _imports(tree) - previous:
        if name in FORBIDDEN_IMPORTS:
            raise UpdateError(f'新增桌面或 Windows 依赖，需先适配：{relative}：{name}')
        if name in sys.stdlib_module_names or name in {'module', 'tasks', 'utils'}:
            continue
        if importlib.util.find_spec(name) is None:
            raise UpdateError(f'新增依赖尚未安装：{name}（{relative}），请先完成 Mac 依赖适配')


def apply_update(info: UpdateInfo, root: Path) -> UpdateResult:
    from .sync_policy import is_managed_path
    root = Path(root).resolve()
    _sha(info.baseline)
    _sha(info.target)
    with _lock(root):
        try:
            _recover(root, locked=True)
        except Exception as exc:
            raise UpdateRecoveryError(str(exc)) from exc
        if _baseline(root) != info.baseline:
            raise UpdateError('更新基线已变化，请重新检查更新')
        if info.baseline == info.target:
            return UpdateResult(0, 0, '')
        cache = _cache(root, info.target)
        if _git(cache, 'merge-base', '--is-ancestor', info.baseline, info.target, check=False).returncode:
            raise UpdateError('上游历史发生分叉或回退，未自动降级，请先进行兼容处理')
        changed = _git(cache, 'diff', '--name-only', '--no-renames', '-z', info.baseline, info.target)
        names = [path.decode('utf-8') for path in changed.stdout.split(b'\0') if path]
        transaction = Path(tempfile.mkdtemp(prefix=f'update-{info.target[:8]}-', dir=cache.parent))
        (transaction / 'backup').mkdir()
        plans, conflicts, ignored = [], [], 0
        print('正在筛选并三方合并云游戏代码和资源...', flush=True)
        for relative in names:
            if not is_managed_path(relative):
                ignored += 1
                continue
            destination = _path(root, relative)
            local = _read(destination)
            base = _blob(cache, info.baseline, relative)
            incoming = _blob(cache, info.target, relative)
            text = Path(relative).suffix.lower() in {'.py', '.json', '.jsonc', '.yaml', '.yml', '.txt', '.toml', '.csv', '.md', '.po', '.ini', '.cfg'}
            merged, conflict = _merge(local, base, incoming, transaction, text=text)
            if conflict:
                conflicts.append(relative)
                if merged is not None:
                    _atomic_write(_path(transaction / 'conflicts', relative), merged)
                continue
            try:
                _validate_python(relative, merged, local)
            except UpdateError as exc:
                conflicts.append(f'{relative}（{exc}）')
                continue
            if merged == local:
                continue
            mode = stat.S_IMODE(destination.stat().st_mode) if destination.exists() else 0o644
            record = {'path': relative, 'before': _digest(local), 'after': _digest(merged), 'mode': mode}
            if local is not None:
                _atomic_write(_path(transaction / 'backup', relative), local, mode)
            if merged is not None:
                _atomic_write(_path(transaction / 'staged', relative), merged, mode)
            plans.append(record)
        if conflicts:
            _json_write(transaction / 'conflicts.json', {'paths': conflicts, 'target': info.target})
            raise UpdateConflictError(conflicts, transaction)
        state_path = _path(root, STATE_PATH)
        state_before = _read(state_path)
        if state_before is not None:
            _atomic_write(transaction / 'state-before.json', state_before)
        state_after = (json.dumps({'repository': SOURCE_URL, 'baseline': info.target}, indent=2) + '\n').encode()
        journal = {'status': 'applying', 'files': plans, 'state_before': _digest(state_before),
                   'state_after': _digest(state_after), 'baseline': info.baseline, 'target': info.target}
        _json_write(transaction / 'transaction.json', journal)
        print(f'检查通过，正在应用 {len(plans)} 个文件，跳过 {ignored} 个无关文件...', flush=True)
        try:
            for record in plans:
                destination = _path(root, record['path'])
                if _digest(_read(destination)) != record['before']:
                    raise UpdateError(f'文件在更新准备后又有改动：{record["path"]}')
                if record['after'] is None:
                    destination.unlink()
                else:
                    _atomic_write(destination, _path(transaction / 'staged', record['path']).read_bytes(), record['mode'])
            _atomic_write(state_path, state_after)
            journal['status'] = 'complete'
            _json_write(transaction / 'transaction.json', journal)
        except BaseException as exc:
            try:
                _restore(root, transaction, journal)
            except Exception as rollback:
                raise UpdateRecoveryError(f'更新中断且回滚未完成：{rollback}；备份：{transaction}') from exc
            raise
        return UpdateResult(len(plans), ignored, str(transaction / 'backup'))
