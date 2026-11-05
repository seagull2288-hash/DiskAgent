"""Local configuration, durable files, secret handling and shared project lock."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'api_bridge'))
import llm_bridge as bridge


class ControllerError(RuntimeError):
    pass


def local_time():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def safe_text(value):
    text = str(value)
    for name in ('DEEPSEEK_API_KEY', 'ZY_KEYS1'):
        secret = os.environ.get(name, '')
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)


def read_json(path):
    return bridge.read_json(path)


def write_json(path, value):
    # All controller state must be JSON serializable; never serialize credentials.
    cleaned = json.loads(safe_text(json.dumps(value, ensure_ascii=False, allow_nan=False)))
    bridge.write_json(path, cleaned)


def atomic_text(path, text, encoding='utf-8'):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding=encoding, newline='') as stream:
            stream.write(safe_text(text))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_csv(path, rows, fields):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, stream.getvalue(), 'utf-8-sig')


@dataclass(frozen=True)
class Settings:
    root: Path
    candidate_count: int
    project_file: Path
    workbench_executable: Path
    output_root: Path
    progress_log: Path
    api_config: Path
    problem_file: Path
    key_environment_variable: str
    plot_on_complete: bool

    def definition(self):
        """Only the evaluation budget may change during a resume."""
        return {name: str(getattr(self, name)) for name in (
            'root', 'project_file', 'workbench_executable', 'output_root',
            'progress_log', 'api_config', 'problem_file', 'key_environment_variable')}


def load_settings(path=None, rounds=None):
    path = Path(path) if path else ROOT / 'python_control' / 'optimization_config.json'
    value = read_json(path)
    names = {'candidate_count', 'project_file', 'workbench_executable', 'output_root',
             'progress_log', 'api_config', 'problem_file', 'key_environment_variable', 'plot_on_complete'}
    if not isinstance(value, dict) or set(value) != names:
        raise ControllerError('Optimization config must contain exactly the documented settings.')
    count = value['candidate_count'] if rounds is None else rounds
    if type(count) is not int or count < 1:
        raise ControllerError('candidate_count must be a positive integer.')
    if type(value['plot_on_complete']) is not bool:
        raise ControllerError('plot_on_complete must be true or false.')
    key_name = value['key_environment_variable']
    if not isinstance(key_name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key_name):
        raise ControllerError('Invalid key environment variable name.')
    paths = {}
    for name in names - {'candidate_count', 'key_environment_variable', 'plot_on_complete'}:
        raw = value[name]
        if not isinstance(raw, str) or not raw or any(c in raw for c in '\0\r\n'):
            raise ControllerError('Invalid path setting: ' + name)
        paths[name] = (ROOT / raw).resolve()
    if paths['project_file'] != (ROOT / 'project_v2' / '2dmodel.wbpj').resolve():
        raise ControllerError('The existing verified macro targets project_v2/2dmodel.wbpj; review the macro before changing it.')
    return Settings(ROOT, count, **paths, key_environment_variable=key_name,
                    plot_on_complete=value['plot_on_complete'])


def fingerprint(settings):
    paths = [settings.project_file, settings.problem_file, settings.api_config,
             settings.root / 'make_verified_journal.py', settings.root / 'mechanical_template.py.txt',
             settings.root / 'api_bridge' / 'llm_bridge.py', settings.root / 'run_python_optimization.py']
    paths += list((settings.root / 'python_control').glob('*.py'))
    search_config = settings.root / 'python_control' / 'search_config.json'
    if search_config.exists():
        paths.append(search_config)
    paths += list((settings.root / 'cad').glob('*.prt'))
    material_root = settings.project_file.parent / (settings.project_file.stem + '_files')
    paths += list(material_root.rglob('*.engd')) + list(material_root.rglob('EngineeringData.xml'))
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}


def saved_key(name):
    """Read only the requested Windows User value; never enumerate environment data."""
    if sys.platform == 'win32':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as handle:
                value = winreg.QueryValueEx(handle, name)[0]
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except FileNotFoundError:
            pass
    return os.environ.get(name, '').strip()


@contextmanager
def api_key(settings, target_name):
    secret = saved_key(settings.key_environment_variable)
    if not secret or any(c in secret for c in '\0\r\n'):
        raise ControllerError('Configure a single-line API key in ' + settings.key_environment_variable + ' first.')
    previous = os.environ.get(target_name)
    os.environ[target_name] = secret
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(target_name, None)
        else:
            os.environ[target_name] = previous


@contextmanager
def project_lock(root):
    """Use the same OS byte-range lock as the MATLAB controller on Windows."""
    path = Path(root) / 'llm_optimization.lock'
    with path.open('a+b') as handle:
        acquired = False
        try:
            if sys.platform == 'win32':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise ControllerError('Another MATLAB/Python optimization holds this project. Do not start a second session.') from exc
        try:
            yield
        finally:
            if acquired:
                if sys.platform == 'win32':
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
