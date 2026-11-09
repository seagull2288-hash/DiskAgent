"""Artifact freshness helpers adapted from ANSYS-Workbench-mcp (MIT).

Copyright (c) 2026 hongwenwang36-eng. See third_party/ANSYS-Workbench-mcp/LICENSE.
Upstream commit: 2950add0f46023c685d897135970c9e691b2f255.
Original names: _path_state / _state_changed in mcp_server.py.
DiskAgent also requires a nonempty regular file; native verification is separate.
"""
from pathlib import Path


def path_state(path: Path) -> dict:
    if not path.exists():
        return {'exists': False, 'size_bytes': None, 'modified_time_ns': None}
    stat = path.stat()
    return {'exists': path.is_file(), 'size_bytes': stat.st_size if path.is_file() else None,
            'modified_time_ns': stat.st_mtime_ns}


def state_changed(before: dict, after: dict) -> bool:
    if not after['exists'] or not after['size_bytes']:
        return False
    if not before['exists']:
        return True
    return (before['size_bytes'] != after['size_bytes'] or
            before['modified_time_ns'] != after['modified_time_ns'])
