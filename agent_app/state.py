"""Durable supervisor state, independent of engine checkpoints and live permission.

A saved workflow describes work and evidence; it never authorizes execution.
Callers must supply ``live=True`` again when resuming a workflow.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys
import uuid
from typing import Any

from python_control import runtime
from .schemas import TaskSpec, TaskValidationError


WORKFLOW_SCHEMA = 'disk-agent.workflow.v1'
WORKFLOW_STATUSES = frozenset({
    'received', 'planning', 'clarification_needed', 'planned', 'validating',
    'validated', 'checking', 'running', 'analyzing', 'completed', 'failed',
    'interrupted', 'needs_review',
})
_ID = re.compile(r'wf_[A-Za-z0-9][A-Za-z0-9_-]{0,180}')


def _now():
    return datetime.now().astimezone().isoformat(timespec='microseconds')


def _workflow_id(value):
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TaskValidationError('WORKFLOW_ID', 'workflow_id must be a wf_ directory name, not a path.')
    return value


def _timestamp(value, name):
    if not isinstance(value, str) or not value or len(value) > 80:
        raise TaskValidationError('WORKFLOW_TIME', name + ' must be a timezone-aware ISO8601 timestamp.')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('Timezone required.')
        return parsed
    except (ValueError, OverflowError):
        raise TaskValidationError('WORKFLOW_TIME', name + ' must be a timezone-aware ISO8601 timestamp.') from None


def _json_value(value, path='state', active=None, depth=0):
    """Copy only native finite JSON values, rejecting cycles and coerced keys."""
    if depth > 64:
        raise TaskValidationError('WORKFLOW_VALUE', path + ' exceeds the maximum JSON nesting depth.')
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if math.isfinite(value):
            return value
        raise TaskValidationError('WORKFLOW_VALUE', path + ' must contain only finite numbers.')
    if type(value) not in (list, dict):
        raise TaskValidationError('WORKFLOW_VALUE', path + ' must contain only JSON values.')
    active = set() if active is None else active
    if id(value) in active:
        raise TaskValidationError('WORKFLOW_VALUE', path + ' contains a circular reference.')
    active.add(id(value))
    try:
        if isinstance(value, list):
            return [_json_value(item, path + '[' + str(i) + ']', active, depth + 1)
                    for i, item in enumerate(value)]
        if any(type(key) is not str for key in value):
            raise TaskValidationError('WORKFLOW_VALUE', path + ' requires string JSON object keys.')
        return {key: _json_value(item, path + '.' + key, active, depth + 1)
                for key, item in value.items()}
    finally:
        active.remove(id(value))


@dataclass
class WorkflowState:
    workflow_id: str
    created_at: str
    updated_at: str
    revision: int = 0
    status: str = 'received'
    request_text: str | None = None
    task: dict[str, Any] | None = None
    planning: dict[str, Any] | None = None
    text_plan_only: bool = False
    run_id: str | None = None
    start_completed_candidates: int = 0
    target_total_candidates: int | None = None
    batch_size: int = 4
    engine_definition: dict[str, Any] | None = None
    engine_fingerprint: dict[str, Any] | None = None
    feedback_policy: dict[str, Any] | None = None
    decision_trace: list[dict[str, Any]] = field(default_factory=list)
    batches: list[dict[str, Any]] = field(default_factory=list)
    pending_batch: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = WORKFLOW_SCHEMA

    @classmethod
    def from_dict(cls, value):
        if isinstance(value, cls):
            value = {item.name: getattr(value, item.name) for item in fields(cls)}
        if not isinstance(value, dict):
            raise TaskValidationError('WORKFLOW_FORMAT', 'Workflow state must be a JSON object.')
        allowed = {item.name for item in fields(cls)}
        if set(value) - allowed:
            raise TaskValidationError('WORKFLOW_FIELDS', 'Unsupported workflow fields: ' +
                                      ', '.join(sorted(map(str, set(value) - allowed))))
        if any(name not in value for name in ('workflow_id', 'created_at', 'updated_at')):
            raise TaskValidationError('WORKFLOW_FORMAT', 'workflow_id, created_at and updated_at are required.')
        raw = dict(value)
        if isinstance(raw.get('task'), TaskSpec):
            raw['task'] = raw['task'].to_dict()
        raw = _json_value(raw)
        _workflow_id(raw['workflow_id'])
        created = _timestamp(raw['created_at'], 'created_at')
        updated = _timestamp(raw['updated_at'], 'updated_at')
        if updated < created:
            raise TaskValidationError('WORKFLOW_TIME', 'updated_at cannot precede created_at.')
        if raw.get('schema_version', WORKFLOW_SCHEMA) != WORKFLOW_SCHEMA:
            raise TaskValidationError('WORKFLOW_SCHEMA', 'Unsupported workflow schema version.')
        if not isinstance(raw.get('status', 'received'), str) or raw.get('status', 'received') not in WORKFLOW_STATUSES:
            raise TaskValidationError('WORKFLOW_STATUS', 'Unsupported workflow status.')
        for name, default, minimum in (('revision', 0, 0), ('start_completed_candidates', 0, 0),
                                       ('batch_size', 4, 1)):
            number = raw.get(name, default)
            if type(number) is not int or number < minimum:
                raise TaskValidationError('WORKFLOW_COUNTS', name + ' must be an integer >= ' + str(minimum) + '.')
        target = raw.get('target_total_candidates')
        if target is not None and (type(target) is not int or target < 1 or
                                   target < raw.get('start_completed_candidates', 0)):
            raise TaskValidationError('WORKFLOW_COUNTS', 'target_total_candidates must be a positive integer >= the start count.')
        if type(raw.get('text_plan_only', False)) is not bool:
            raise TaskValidationError('WORKFLOW_VALUE', 'text_plan_only must be a boolean.')
        request = raw.get('request_text')
        if request is not None and (not isinstance(request, str) or not request.strip() or
                                    len(request) > 4000 or '\0' in request):
            raise TaskValidationError('REQUEST_TEXT', 'request_text must contain 1-4000 characters without NUL.')
        if raw.get('task') is not None:
            raw['task'] = TaskSpec.from_dict(raw['task']).to_dict()
        run_id = raw.get('run_id')
        if run_id is not None:
            TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
            if raw.get('task') is not None and raw['task'].get('run_id') not in (None, run_id):
                raise TaskValidationError('WORKFLOW_RUN', 'Workflow and task run_id disagree.')
        for name in ('planning', 'engine_definition', 'engine_fingerprint', 'feedback_policy',
                     'pending_batch', 'analysis', 'result', 'error'):
            if raw.get(name) is not None and not isinstance(raw[name], dict):
                raise TaskValidationError('WORKFLOW_VALUE', name + ' must be a JSON object or null.')
        for name in ('batches', 'events', 'decision_trace'):
            entries = raw.get(name, [])
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise TaskValidationError('WORKFLOW_VALUE', name + ' must be a list of JSON objects.')
        if len(raw.get('events', [])) > 500:
            raise TaskValidationError('WORKFLOW_VALUE', 'events may retain at most the last 500 entries.')
        warnings = raw.get('warnings', [])
        if not isinstance(warnings, list) or any(not isinstance(entry, str) for entry in warnings):
            raise TaskValidationError('WORKFLOW_VALUE', 'warnings must be a list of strings.')
        return cls(**raw)

    def to_dict(self):
        checked = type(self).from_dict(self)
        return {item.name: getattr(checked, item.name) for item in fields(checked)}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaskValidationError('WORKFLOW_FORMAT', 'Workflow JSON contains duplicate object keys.')
        result[key] = value
    return result


class WorkflowStore:
    """Atomic snapshots with revision CAS and a separate nonblocking run lock."""

    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.root).resolve() / 'agent_workflows'

    def _root(self):
        try:
            resolved = self.root.resolve()
        except (OSError, RuntimeError) as exc:
            raise TaskValidationError('WORKFLOW_PATH', 'Cannot resolve workflow storage safely.') from exc
        if resolved != self.root or resolved.parent != Path(self.settings.root).resolve():
            raise TaskValidationError('WORKFLOW_PATH', 'Workflow storage cannot escape the configured project root.')
        return resolved

    def directory(self, workflow_id):
        workflow_id = _workflow_id(workflow_id)
        root = self._root()
        try:
            directory = (root / workflow_id).resolve()
        except (OSError, RuntimeError) as exc:
            raise TaskValidationError('WORKFLOW_PATH', 'Cannot resolve workflow directory safely.') from exc
        if directory.parent != root or directory.name != workflow_id:
            raise TaskValidationError('WORKFLOW_PATH', 'Workflow must be a direct child of agent_workflows.')
        return directory

    def _file(self, workflow_id, name):
        directory = self.directory(workflow_id)
        path = directory / name
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise TaskValidationError('WORKFLOW_PATH', 'Cannot resolve workflow file safely.') from exc
        if resolved != path:
            raise TaskValidationError('WORKFLOW_PATH', 'Workflow files cannot redirect outside their directory.')
        return path

    @contextmanager
    def _file_lock(self, workflow_id, filename):
        directory = self.directory(workflow_id)
        if not directory.is_dir():
            raise TaskValidationError('WORKFLOW_NOT_FOUND', 'Workflow directory does not exist.')
        path = self._file(workflow_id, filename)
        try:
            handle = path.open('a+b')
        except OSError as exc:
            raise TaskValidationError('WORKFLOW_IO', 'Cannot open workflow lock: ' + runtime.safe_text(exc)) from exc
        acquired = False
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            try:
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                raise TaskValidationError('WORKFLOW_BUSY', 'Another process holds this workflow lock.') from exc
            yield
        finally:
            try:
                if acquired:
                    if sys.platform == 'win32':
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def lock(self, workflow_id):
        """Hold only this workflow; engine/project serialization remains separate."""
        return self._file_lock(workflow_id, '.workflow.lock')

    def _write(self, state):
        path = self._file(state.workflow_id, 'state.json')
        try:
            text = json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False, indent=2) + '\n'
            runtime.atomic_text(path, text)
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, TaskValidationError):
                raise
            raise TaskValidationError('WORKFLOW_IO', 'Cannot write workflow state: ' + runtime.safe_text(exc)) from exc

    def create(self, request_text=None):
        now = _now()
        # Validate user text before making any directory.
        state = WorkflowState.from_dict(WorkflowState('wf_validation', now, now, request_text=request_text))
        root = self._root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            for _ in range(10):
                state.workflow_id = 'wf_' + datetime.now().strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:16]
                directory = self.directory(state.workflow_id)
                try:
                    directory.mkdir()
                    break
                except FileExistsError:
                    continue
            else:
                raise TaskValidationError('WORKFLOW_ID', 'Could not allocate a unique workflow directory.')
            with self._file_lock(state.workflow_id, '.state-write.lock'):
                self._write(state)
            return state
        except OSError as exc:
            raise TaskValidationError('WORKFLOW_IO', 'Cannot create workflow: ' + runtime.safe_text(exc)) from exc

    def load(self, workflow_id):
        path = self._file(workflow_id, 'state.json')
        try:
            raw = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_object)
        except FileNotFoundError as exc:
            raise TaskValidationError('WORKFLOW_NOT_FOUND', 'Saved workflow state does not exist.') from exc
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, TaskValidationError):
                raise
            raise TaskValidationError('WORKFLOW_FORMAT', 'Cannot read workflow state: ' + runtime.safe_text(exc)) from exc
        state = WorkflowState.from_dict(raw)
        if state.workflow_id != workflow_id:
            raise TaskValidationError('WORKFLOW_ID', 'Saved workflow belongs to a different directory.')
        return state

    def save(self, state):
        if not isinstance(state, WorkflowState):
            raise TaskValidationError('WORKFLOW_FORMAT', 'save requires a WorkflowState.')
        checked = WorkflowState.from_dict(state)
        with self._file_lock(checked.workflow_id, '.state-write.lock'):
            current = self.load(checked.workflow_id)
            if checked.revision != current.revision:
                raise TaskValidationError('WORKFLOW_STALE', 'Workflow state changed; reload before saving.')
            if checked.created_at != current.created_at:
                raise TaskValidationError('WORKFLOW_TIME', 'created_at cannot change after workflow creation.')
            checked.revision += 1
            checked.updated_at = _now()
            self._write(checked)
        state.revision, state.updated_at = checked.revision, checked.updated_at
        return state

    def record(self, state, status, **details):
        if not isinstance(status, str) or status not in WORKFLOW_STATUSES:
            raise TaskValidationError('WORKFLOW_STATUS', 'Unsupported workflow status.')
        if not isinstance(state, WorkflowState):
            raise TaskValidationError('WORKFLOW_FORMAT', 'record requires a WorkflowState.')
        # Prepare an independent object so failed validation/atomic writes leave
        # the caller's last committed status and revision unchanged.
        checked = WorkflowState.from_dict(state)
        checked.status = status
        checked.events = (checked.events + [{'time': _now(), 'status': status,
                                             'details': _json_value(details)}])[-500:]
        self.save(checked)
        state.status, state.events = checked.status, checked.events
        state.revision, state.updated_at = checked.revision, checked.updated_at
        return state
