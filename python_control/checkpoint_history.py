"""Verified committed snapshots and explicit, locked checkpoint restoration."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from .runtime import ControllerError, local_time, project_lock, read_json, safe_text, write_json


KEEP_BACKUPS = 5
SCHEMA = 'disk-agent.checkpoint-backup.v1'
BACKUP_ID = re.compile(r'checkpoint_[0-9]{6,}\.json')
_UNSET = object()


def _backup_directory(run_dir, *, create=False):
    directory = Path(run_dir).resolve() / 'checkpoint_backups'
    if directory.is_symlink() or directory.resolve() != directory:
        raise ControllerError('Checkpoint backup directory must stay inside its run.')
    if create:
        directory.mkdir(exist_ok=True)
    return directory


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_committed(state, verify_native, require_metrics):
    from .controller import SCHEMA as CHECKPOINT_SCHEMA, observation
    count = state.get('nCompleted')
    if (state.get('schema') != CHECKPOINT_SCHEMA or type(count) is not int or count < 0
            or type(state.get('nLLM')) is not int or state['nLLM'] < count
            or not isinstance(state.get('baseline'), dict) or state.get('pending') is not None
            or len(state.get('records', [])) != count or len(state.get('history', [])) != count + 1):
        raise ControllerError('Committed checkpoint structure is inconsistent.')
    details = [state['baseline']] + [record['detail'] for record in state['records']]
    for index, (detail, point) in enumerate(zip(details, state['history'])):
        if observation(detail) != point or (index and state['records'][index - 1]['iteration'] != index):
            raise ControllerError('Committed checkpoint results and observations disagree.')
        verify_native(detail, state['baseline'] if index else None)
        require_metrics(detail, state.get('optimization_policy'))


def save_committed_backup(state, *, verify_native, require_metrics):
    """A backup failure is diagnostic only after the primary commit has succeeded."""
    if (state.get('baseline') is None or state.get('pending') is not None
            or state.get('status') not in ('running', 'complete')):
        return
    root = Path(state['runDir']).resolve()
    status_path = root / 'backup_status.json'
    try:
        directory = _backup_directory(root, create=True)
        backup_id = 'checkpoint_%06d.json' % state['nCompleted']
        path = directory / backup_id
        metadata_path = directory / (backup_id + '.meta.json')
        if path.is_symlink() or metadata_path.is_symlink():
            raise ControllerError('Checkpoint backup files cannot be symbolic links.')
        if path.is_file() and metadata_path.is_file():
            metadata = read_json(metadata_path)
            if metadata.get('sha256') == _hash(path):
                return
            raise ControllerError('Existing committed backup failed its digest check.')
        _verify_committed(state, verify_native, require_metrics)
        write_json(path, state)
        metadata = {'schema_version': SCHEMA, 'backup_id': backup_id,
                    'run_id': root.name, 'created_at': local_time(), 'sha256': _hash(path),
                    'completed_candidates': state['nCompleted'], 'target_total_candidates': state['nLLM'],
                    'baseline_completed': True, 'verification': 'native_and_structure_passed'}
        write_json(metadata_path, metadata)
        entries = list_backups(root)['backups']
        for item in entries[KEEP_BACKUPS:]:
            (directory / item['backup_id']).unlink()
            (directory / (item['backup_id'] + '.meta.json')).unlink()
        write_json(status_path, {'status': 'passed', 'latest_backup_id': backup_id,
                                'completed_candidates': state['nCompleted'], 'updated_at': local_time()})
    except Exception as error:
        try:
            write_json(status_path, {'status': 'warning', 'message': safe_text(str(error)),
                                    'completed_candidates': state.get('nCompleted'), 'updated_at': local_time()})
        except Exception:
            pass


def list_backups(run_dir):
    root = Path(run_dir).resolve()
    directory = _backup_directory(root)
    entries, warnings = [], []
    if directory.is_dir():
        for path in directory.glob('checkpoint_*.json.meta.json'):
            try:
                if path.is_symlink():
                    raise ControllerError('Backup metadata cannot be a symbolic link.')
                metadata = read_json(path)
                backup_id = metadata.get('backup_id', '')
                if (metadata.get('schema_version') != SCHEMA or metadata.get('run_id') != root.name
                        or not BACKUP_ID.fullmatch(backup_id) or path.name != backup_id + '.meta.json'):
                    raise ControllerError('Invalid backup metadata.')
                snapshot = directory / backup_id
                if snapshot.is_symlink() or _hash(snapshot) != metadata.get('sha256'):
                    raise ControllerError('Backup digest verification failed.')
                entries.append(metadata)
            except Exception as error:
                warnings.append(path.name + ': ' + safe_text(str(error)))
    entries.sort(key=lambda item: item['completed_candidates'], reverse=True)
    diagnostic = None
    status_path = root / 'backup_status.json'
    if status_path.is_file() and not status_path.is_symlink():
        try:
            diagnostic = read_json(status_path)
        except Exception as error:
            warnings.append(safe_text(str(error)))
    return {'ok': True, 'schema_version': SCHEMA, 'run_id': root.name,
            'status': 'warning' if warnings or (diagnostic or {}).get('status') == 'warning' else 'passed',
            'backups': entries, 'diagnostic': diagnostic, 'warnings': warnings}


def _load_backup(settings, run_dir, backup_id):
    root = Path(run_dir).resolve()
    if root.parent != settings.output_root.resolve() or not root.is_dir():
        raise ControllerError('Backup restoration requires a run inside the configured output directory.')
    if not isinstance(backup_id, str) or not BACKUP_ID.fullmatch(backup_id):
        raise ControllerError('Invalid checkpoint backup identifier.')
    directory = _backup_directory(root)
    path = directory / backup_id
    metadata_path = directory / (backup_id + '.meta.json')
    if path.is_symlink() or metadata_path.is_symlink():
        raise ControllerError('Checkpoint backup cannot redirect outside its run.')
    metadata = read_json(metadata_path)
    if (metadata.get('schema_version') != SCHEMA or metadata.get('run_id') != root.name
            or metadata.get('backup_id') != backup_id or metadata.get('sha256') != _hash(path)):
        raise ControllerError('Checkpoint backup identity or digest verification failed.')
    snapshot = read_json(path)
    if (snapshot.get('pending') is not None
            or snapshot.get('nCompleted') != metadata.get('completed_candidates')
            or snapshot.get('nLLM') != metadata.get('target_total_candidates')):
        raise ControllerError('Checkpoint backup counts or pending state disagree with its metadata.')
    bound = replace(settings, candidate_count=snapshot['nLLM'], optimization_policy=None)
    policy = snapshot.get('definition', {}).get('optimization_policy')
    if policy is not None:
        bound = bound.for_policy(policy)
    from .controller import restore_state
    checked = restore_state(bound, root, checkpoint=snapshot)
    return root, checked, metadata


def _restore_eligibility(root, state, backup_id):
    backups = list_backups(root)['backups']
    if not backups or backups[0]['backup_id'] != backup_id:
        return False, '仅允许恢复最新已核验备份，较早备份可用于查看与核对。'
    for path in _backup_directory(root).glob('checkpoint_*.json'):
        if BACKUP_ID.fullmatch(path.name) and int(path.stem.split('_')[1]) > state['nCompleted']:
            return False, '存在候选数更高的备份文件，请先核验该备份现场。'
    checkpoint = root / 'checkpoint.json'
    try:
        current = read_json(checkpoint)
    except Exception:
        current = None
    if isinstance(current, dict):
        if current.get('status') in ('running', 'initializing'):
            return False, '当前断点仍标记为计算中，请停止并核对运行现场后恢复。'
        completed = current.get('nCompleted')
        if type(completed) is int and completed > state['nCompleted']:
            return False, '当前断点已提交更多候选，恢复此备份会回退已提交结果。'
        if current.get('pending') is not None:
            return False, '当前断点含未结算候选，请先核验并认领该现场。'
    directory = root / 'iterations' / ('iter_%03d' % (state['nCompleted'] + 1))
    if directory.exists() or directory.is_symlink():
        return False, '存在下一轮未结算现场 ' + directory.name + '，请先核验并认领该现场。'
    return True, '最新备份已通过核验，可以恢复已提交断点。'


def preview_restore(settings, run_dir, backup_id):
    root, state, metadata = _load_backup(settings, run_dir, backup_id)
    checkpoint = root / 'checkpoint.json'
    if checkpoint.is_symlink():
        raise ControllerError('Current checkpoint cannot redirect outside its run.')
    can_restore, reason = _restore_eligibility(root, state, backup_id)
    return {'ok': True, 'run_id': root.name, 'backup_id': backup_id, 'verification': 'passed',
            'can_restore': can_restore, 'reason_zh': reason,
            'completed_candidates': state['nCompleted'], 'target_total_candidates': state['nLLM'],
            'created_at': metadata['created_at'],
            'current_checkpoint_sha256': _hash(checkpoint) if checkpoint.is_file() else None}


def restore_backup(settings, run_dir, backup_id, *, expected_current_sha256=_UNSET):
    """Explicit restoration replays all compatibility/native checks under the engine lock."""
    from .controller import summary
    from .workbench import project_unlocked
    with project_lock(settings.root):
        root, state, metadata = _load_backup(settings, run_dir, backup_id)
        checkpoint = root / 'checkpoint.json'
        if checkpoint.is_symlink():
            raise ControllerError('Current checkpoint cannot redirect outside its run.')
        if expected_current_sha256 is not _UNSET:
            if expected_current_sha256 is None:
                unchanged = not checkpoint.exists()
            else:
                unchanged = checkpoint.is_file() and _hash(checkpoint) == expected_current_sha256
            if not unchanged:
                raise ControllerError('Current checkpoint changed after the recovery preview.')
        can_restore, reason = _restore_eligibility(root, state, backup_id)
        if not can_restore:
            raise ControllerError(reason)
        project_unlocked(Path(state['definition']['project_file']))
        archive = _backup_directory(root) / ('before_restore_' + uuid.uuid4().hex + '.json')
        if checkpoint.is_file():
            shutil.copyfile(checkpoint, archive)
        state['status'] = 'interrupted'
        state['lastError'] = {'type': 'CHECKPOINT_RESTORED', 'time': local_time(),
                              'message': 'Restored verified backup ' + backup_id}
        write_json(checkpoint, state)
        write_json(root / 'status.json', summary(state))
        return {'ok': True, 'run_id': root.name, 'backup_id': backup_id, 'restored': True,
                'completed_candidates': state['nCompleted'], 'target_total_candidates': state['nLLM'],
                'verification': 'passed',
                'previous_checkpoint_file': str(archive) if archive.is_file() else None}
