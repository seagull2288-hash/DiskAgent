"""Recoverable list deletion preserves original paths used by audit evidence."""
from pathlib import Path

from python_control import runtime
from .schemas import TaskValidationError


DELETION_FILE = '.diskagent-deleted.json'


def is_deleted(directory):
    marker = Path(directory) / DELETION_FILE
    return marker.exists() or marker.is_symlink()


def mark_deleted(directory, kind):
    directory = Path(directory)
    marker = directory / DELETION_FILE
    if marker.resolve() != marker:
        raise TaskValidationError('RECORD_PATH', 'Deletion marker cannot redirect outside its record.')
    if not marker.exists():
        runtime.write_json(marker, {
            'schema_version': 'disk-agent.deleted-record.v1',
            'kind': kind, 'record_id': directory.name,
            'deleted_at': runtime.local_time(), 'original_directory': str(directory),
        })
    return {'ok': True, 'deleted': True, 'kind': kind, 'record_id': directory.name,
            'deletion_mode': 'recoverable_soft_delete', 'marker_file': str(marker)}
