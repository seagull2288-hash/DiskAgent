"""Loopback-only browser workbench over the existing workflow API.

Starting this server and reading its endpoints never launches an API request or
Workbench. A single background worker accepts explicitly submitted work.
"""
from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid

from python_control import runtime
from .schemas import TaskSpec, TaskValidationError
from .state import WorkflowStore
from .record_trash import is_deleted, mark_deleted
from .mcp_jobs import JobStore


MAX_BODY = 16 * 1024
_JOB_ID = re.compile(r'job_[a-f0-9]{24}')
_HTML = Path(__file__).parent / 'static' / 'index.html'


# Lazy imports keep server startup independent of expensive tool modules.
def run_workflow(*args, **kwargs):
    from .recovery import run_workflow as operation
    return operation(*args, **kwargs)


def resume_workflow(*args, **kwargs):
    from .recovery import resume_workflow as operation
    return operation(*args, **kwargs)


def get_workflow_status(*args, **kwargs):
    from .supervisor import get_workflow_status as operation
    from .recovery import inspect_recovery
    result = operation(*args, **kwargs)
    if result.ok:
        workflow_id = args[0] if args else kwargs['workflow_id']
        result.data['recovery'] = inspect_recovery(workflow_id, settings=kwargs.get('settings'))
    return result


def analyze_run(*args, **kwargs):
    from .result_analysis import analyze_run as operation
    return operation(*args, **kwargs)


def inspect_run(*args, **kwargs):
    from .performance_predictor import inspect_run as operation
    return operation(*args, **kwargs)


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _run_display_name(directory):
    path = directory / '.diskagent-name.json'
    if path.is_symlink() or not path.is_file():
        return ''
    try:
        value = runtime.read_json(path)
        if isinstance(value, dict) and value.get('run_id') == directory.name:
            name = value.get('display_name')
            return name if isinstance(name, str) else ''
    except (OSError, ValueError, runtime.bridge.BridgeError):
        pass
    return ''


def _error(code, message):
    return {'ok': False, 'error': {'code': code, 'message': runtime.safe_text(message)}}


def _result(value):
    value = value.to_dict() if hasattr(value, 'to_dict') else value
    if not isinstance(value, dict) or type(value.get('ok')) is not bool:
        raise TaskValidationError('WEB_RESULT', 'Workflow returned an invalid result.')
    if not isinstance(value.get('data', {}), dict):
        raise TaskValidationError('WEB_RESULT', 'Workflow result data must be an object.')
    # Reject non-finite values before publishing a background result.
    return json.loads(runtime.safe_text(json.dumps(value, ensure_ascii=False, allow_nan=False)))


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise TaskValidationError('WEB_JSON', 'JSON object keys must be unique.')
        result[name] = value
    return result


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, settings, port):
        self.settings = settings
        self.store = WorkflowStore(settings)
        self.job_store = JobStore(settings.root)
        self.csrf_token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.recovery_stops = {}
        self.active_job_id = None
        self.jobs_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.chat_lock = threading.Lock()
        self.chat = None
        super().__init__(('127.0.0.1', port), _Handler)
        self.job_store.owner_url = 'http://127.0.0.1:' + str(self.server_address[1])

    def conversations(self):
        with self.chat_lock:
            if self.chat is None:
                from .chat_agent import ChatService
                self.chat = ChatService(self.settings, 'http://127.0.0.1:' + str(self.server_address[1]), self.job)
            return self.chat

    def server_close(self):
        if self.chat is not None:
            self.chat.stop.set()
        super().server_close()

    def submit(self, operation, *, workflow_id=None, request_id=None, request_digest=None, **kwargs):
        if request_id is None:
            return self._submit(operation, workflow_id=workflow_id, **kwargs)
        self.job_store.request_path(request_id)
        with self.job_store.request_lock():
            previous = self.job_store.replay(request_id, request_digest)
            if previous is not None:
                job = self.job(previous)
                if job is None:
                    raise TaskValidationError('JOB_RECORD', 'Submission exists but its job record is missing; inspect before retrying.')
                return {'ok': True, 'job_id': previous, 'status': job['status'],
                        'workflow_id': job['workflow_id'], 'replayed': True}
            return self._submit(operation, workflow_id=workflow_id, request_id=request_id,
                                request_digest=request_digest, **kwargs)

    def _submit(self, operation, *, workflow_id=None, request_id=None, request_digest=None, **kwargs):
        if not self.worker_lock.acquire(blocking=False):
            raise TaskValidationError('WEB_BUSY', '已有任务正在执行，请等待完成后再提交。')
        job_id = 'job_' + uuid.uuid4().hex[:24]
        recovery_enabled = kwargs.get('auto_recover') is True and kwargs.get('live') is True
        stop = threading.Event() if recovery_enabled else None
        job = {'job_id': job_id, 'status': 'queued', 'created_at': _now(),
               'operation': getattr(operation, '__name__', 'workflow'), 'request_id': request_id,
               'workflow_id': workflow_id, 'result': None, 'ok': None,
               'recovery_enabled': recovery_enabled, 'recovery_stop_requested': False}
        try:
            self.job_store.save(job)
            if request_id is not None:
                self.job_store.bind(request_id, request_digest, job_id)
        except Exception:
            self.worker_lock.release()
            raise
        with self.jobs_lock:
            # Retain a bounded session job list; workflow evidence remains on disk.
            while len(self.jobs) >= 200:
                oldest = next((key for key, item in self.jobs.items() if item['status'] == 'completed'), None)
                if oldest is None:
                    break
                del self.jobs[oldest]
            self.jobs[job_id] = job
            if stop is not None:
                self.recovery_stops[job_id] = stop
            self.active_job_id = job_id

        def work():
            try:
                with self.jobs_lock:
                    job['status'], job['started_at'] = 'running', _now()
                    self.job_store.save(job)
                if workflow_id is not None:
                    kwargs['workflow_id'] = workflow_id
                else:
                    def created(created_id):
                        self.store.directory(created_id)  # Trusted callback still requires a directory name.
                        with self.jobs_lock:
                            job['workflow_id'] = created_id
                            self.job_store.save(job)
                    kwargs['on_workflow_created'] = created
                if stop is not None:
                    kwargs['recovery_stop'] = stop
                if job['operation'] == 'evaluate_design':
                    def evaluation_created(folder):
                        expected = self.settings.root / 'agent_jobs' / 'evaluations'
                        if Path(folder).resolve().parent != expected.resolve():
                            raise TaskValidationError('EVALUATION_PATH', 'Evaluation must stay in its registered directory.')
                        with self.jobs_lock:
                            job['evaluation_root'] = folder
                            self.job_store.save(job)
                    kwargs['on_evaluation_created'] = evaluation_created
                value = _result(operation(settings=self.settings, **kwargs))
            except (Exception, KeyboardInterrupt) as exc:
                value = dict(_error(str(getattr(exc, 'code', type(exc).__name__)), exc),
                             operation='workflow', status='failed')
            finally:
                # The result is published before another submission is accepted.
                with self.jobs_lock:
                    job.update(status='completed', completed_at=_now(), result=value, ok=value['ok'])
                    job['workflow_id'] = value.get('data', {}).get('workflow_id', job['workflow_id'])
                    self.active_job_id = None
                    self.recovery_stops.pop(job_id, None)
                    try:
                        self.job_store.save(job)
                    except Exception as exc:
                        job['persistence_error'] = runtime.safe_text(exc)
                self.worker_lock.release()

        try:
            threading.Thread(target=work, name='disk-workflow-' + job_id, daemon=True).start()
        except Exception:
            with self.jobs_lock:
                self.jobs.pop(job_id, None)
                self.recovery_stops.pop(job_id, None)
                self.active_job_id = None
            self.worker_lock.release()
            raise
        return {'ok': True, 'job_id': job_id, 'status': 'queued', 'workflow_id': workflow_id}

    def job(self, job_id):
        if _JOB_ID.fullmatch(job_id) is None:
            return None
        with self.jobs_lock:
            value = self.jobs.get(job_id)
            if value is not None:
                return json.loads(json.dumps(value))
        saved = self.job_store.load(job_id)
        if saved is not None and saved.get('status') == 'detached':
            from .job_recovery import recover_job
            recovered = recover_job(saved, self.settings)
            if recovered.get('status') == 'completed':
                self.job_store.save(recovered)
            return recovered
        return saved

    def stop_recovery(self, job_id):
        with self.jobs_lock:
            stop = self.recovery_stops.get(job_id)
            if job_id != self.active_job_id or stop is None:
                raise TaskValidationError('RECOVERY_NOT_ACTIVE', 'No active automatic recovery exists for this job.')
            self.jobs[job_id]['recovery_stop_requested'] = True
            stop.set()
        return {'ok': True, 'stop_requested': True}

    def run_directory(self, run_id, *, include_deleted=False):
        TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
        root = Path(self.settings.output_root).resolve()
        directory = (root / run_id).resolve()
        if directory.parent != root or directory.name != run_id:
            raise TaskValidationError('RUN_PATH', 'Run must be a direct child of the configured output directory.')
        if not directory.is_dir() or (not include_deleted and is_deleted(directory)):
            raise TaskValidationError('RUN_NOT_FOUND', 'Requested run does not exist.')
        return directory

    def delete_record(self, kind, record_id):
        if not self.worker_lock.acquire(blocking=False):
            raise TaskValidationError('WEB_BUSY', '已有任务正在执行，请完成后再删除记录。')
        try:
            directory = (self.store.directory(record_id) if kind == 'workflow' else
                         self.run_directory(record_id, include_deleted=True))
            if not directory.is_dir():
                raise TaskValidationError('RECORD_NOT_FOUND', '记录已不存在。')
            if is_deleted(directory):
                return mark_deleted(directory, kind)
            try:
                with runtime.project_lock(self.settings.root):
                    if kind == 'workflow':
                        result = self.store.soft_delete(record_id)
                    else:
                        root = self.store._root()
                        references = []
                        for folder in root.glob('wf_*'):
                            if not folder.is_dir() or is_deleted(folder):
                                continue
                            state = self.store.load(folder.name)
                            if state.run_id == record_id or (state.task or {}).get('run_id') == record_id:
                                references.append(state.request_text or state.workflow_id)
                        if references:
                            raise TaskValidationError('RECORD_REFERENCED',
                                '此运行仍被工作流引用，请先删除相关工作流。' + '、'.join(references[:3]))
                        snapshot = self.run_summary(record_id)
                        if snapshot.get('status') in ('running', 'initializing'):
                            raise TaskValidationError('WEB_BUSY', '此运行仍标记为计算中，请确认计算结束后再删除。')
                        result = mark_deleted(directory, kind)
            except runtime.ControllerError as exc:
                raise TaskValidationError('WEB_BUSY', '项目正在计算，请完成后再删除记录。') from exc
            with self.cache_lock:
                self.cache.clear()
            return result
        finally:
            self.worker_lock.release()

    def rename_run(self, run_id, name):
        if not isinstance(name, str) or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name):
            raise TaskValidationError('RUN_NAME', '名称须为文字，且不能包含换行或控制字符。')
        name = name.strip()
        if len(name) > 80:
            raise TaskValidationError('RUN_NAME', '名称最多可输入80个字符。')
        directory = self.run_directory(run_id)
        path = directory / '.diskagent-name.json'
        if path.is_symlink() or path.resolve() != path:
            raise TaskValidationError('RUN_PATH', '名称文件路径无效。')
        runtime.write_json(path, {'schema_version': 'disk-agent.run-name.v1',
                                 'run_id': run_id, 'display_name': name, 'updated_at': _now()})
        return {'ok': True, 'run_id': run_id, 'display_name': name}

    def run_summary(self, run_id):
        directory = self.run_directory(run_id)
        path = directory / 'checkpoint.json'
        if path.resolve() != path:
            raise TaskValidationError('RUN_PATH', 'Checkpoint cannot redirect outside its run.')
        stat = path.stat()
        key = ('run', run_id, stat.st_mtime_ns, stat.st_size)
        with self.cache_lock:
            cached = self.cache.get(key)
        if cached is not None:
            return dict(cached, display_name=_run_display_name(directory))
        value = runtime.read_json(path)
        done, target = value.get('nCompleted'), value.get('nLLM')
        if type(done) is not int or type(target) is not int or done < 0 or target < done:
            raise TaskValidationError('CHECKPOINT_COUNTS', 'Invalid committed candidate counts.')
        summary = {'run_id': run_id, 'status': str(value.get('status', 'unknown')),
                   'completed_candidates': done, 'target_total_candidates': target,
                   'baseline_completed': isinstance(value.get('baseline'), dict),
                   'updated_at': datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds')}
        with self.cache_lock:
            self.cache = {k: v for k, v in self.cache.items() if k[:2] != key[:2]}
            self.cache[key] = summary
        return dict(summary, display_name=_run_display_name(directory))

    def protection_status(self, run_id):
        from python_control.evaluation_lifecycle import inspect_monitor, read_optional
        root = self.run_directory(run_id)
        snapshot = read_optional(root / 'checkpoint.json')
        records = snapshot.get('records', [])
        detail = records[-1].get('detail', {}) if records else snapshot.get('baseline') or {}
        pending = snapshot.get('pending') or {}
        selected = pending.get('evaluation_dir')
        if snapshot.get('baseline') is None:
            selected = (snapshot.get('baseline_evaluation') or {}).get('evaluation_dir')
        selected = selected or detail.get('runDir')
        monitor = {'status': 'unavailable', 'message': '此运行尚无阶段监控记录。'}
        geometry = detail.get('geometry_checks')
        if selected:
            folder = Path(selected).resolve()
            if folder.parent != (root / 'evaluations').resolve():
                raise TaskValidationError('EVALUATION_PATH', '评价监控目录超出当前运行。')
            slot = read_optional(folder / 'evaluation_slot.json')
            if slot.get('current_attempt'):
                attempt = Path(slot['current_attempt']).resolve()
                if attempt.parent != folder.parent or not attempt.name.startswith(folder.name):
                    raise TaskValidationError('EVALUATION_PATH', '评价尝试目录与当前运行不一致。')
                folder = attempt
            monitor = inspect_monitor(folder)
            geometry = read_optional(folder / 'geometry_precheck.json') or geometry
        if pending.get('phase') == 'requesting' and not pending.get('evaluation_dir'):
            monitor = {'status': 'requesting', 'stage_label': '生成候选尺寸',
                       'message': '当前候选正在请求优化LLM。'}
        return {'ok': True, 'run_id': run_id, 'run_status': snapshot.get('status'),
                'monitor': monitor, 'geometry': geometry,
                'result_diagnostics': detail.get('engineering_checks'),
                'diagnostics_iteration': snapshot.get('nCompleted'),
                'result_reclaimed': detail.get('recoveredCompletedEvaluation') is True,
                'backup_status': read_optional(root / 'backup_status.json')}

    def restore_references(self, run_id):
        references = []
        for path in self.store._root().glob('wf_*'):
            if path.is_dir() and not is_deleted(path):
                workflow = self.store.load(path.name)
                if workflow.run_id == run_id or (workflow.task or {}).get('run_id') == run_id:
                    references.append(workflow.workflow_id)
        return references

    def restore_checkpoint(self, run_id, backup_id, expected_sha256):
        if not self.worker_lock.acquire(blocking=False):
            raise TaskValidationError('WEB_BUSY', '请等待当前任务完成后再恢复断点。')
        try:
            folder = self.run_directory(run_id)
            if self.restore_references(run_id):
                raise TaskValidationError('RECORD_REFERENCED', '此运行仍有关联工作流，请先移除关联工作流列表记录再恢复。')
            from python_control.checkpoint_history import restore_backup
            result = restore_backup(self.settings, folder, backup_id,
                                    expected_current_sha256=expected_sha256)
            with self.cache_lock:
                self.cache.clear()
            return result
        finally:
            self.worker_lock.release()

    def workflows(self):
        root = self.store._root()
        entries, warnings = [], []
        if root.is_dir():
            folders = sorted((p for p in root.iterdir() if p.name.startswith('wf_') and not is_deleted(p)),
                             key=lambda p: p.stat().st_mtime_ns, reverse=True)[:200]
            for folder in folders:
                try:
                    path = self.store.directory(folder.name) / 'state.json'
                    stat = path.stat()
                    key = ('workflow', folder.name, stat.st_mtime_ns, stat.st_size)
                    with self.cache_lock:
                        metadata = self.cache.get(key)
                    if metadata is None:
                        state = self.store.load(folder.name)
                        done = (state.batches[-1].get('completed_candidates', state.start_completed_candidates)
                                if state.batches else state.start_completed_candidates)
                        metadata = {'workflow_id': state.workflow_id, 'status': state.status,
                                    'created_at': state.created_at, 'updated_at': state.updated_at,
                                    'request_text': state.request_text, 'run_id': state.run_id,
                                    'operation': (state.task or {}).get('operation'),
                                    'completed_candidates': done, 'target_total_candidates': state.target_total_candidates}
                        with self.cache_lock:
                            self.cache = {k: v for k, v in self.cache.items() if k[:2] != key[:2]}
                            self.cache[key] = metadata
                    entry = dict(metadata)
                    done, target = entry.pop('completed_candidates'), entry.pop('target_total_candidates')
                    if entry['run_id'] is not None:
                        try:
                            done = self.run_summary(entry['run_id'])['completed_candidates']
                        except Exception as exc:
                            warnings.append(folder.name + ': ' + runtime.safe_text(exc))
                            entry['progress_error'] = runtime.safe_text(exc)
                    entry['progress'] = {'completed_candidates': done, 'target_total_candidates': target,
                                         'remaining_candidates': max(0, target - done) if target is not None else None,
                                         'ratio': min(1, done / target) if target else None}
                    entries.append(entry)
                except Exception as exc:
                    warnings.append(folder.name + ': ' + runtime.safe_text(exc))
        entries.sort(key=lambda item: item['updated_at'], reverse=True)
        with self.jobs_lock:
            active = self.active_job_id
        return {'ok': True, 'workflows': entries, 'warnings': warnings, 'active_job_id': active}

    def runs(self):
        root = Path(self.settings.output_root).resolve()
        entries, warnings = [], []
        if root.is_dir():
            folders = sorted((p for p in root.iterdir() if p.name.startswith('run_') and not is_deleted(p)),
                             key=lambda p: p.stat().st_mtime_ns, reverse=True)[:200]
            for folder in folders:
                try:
                    entries.append(self.run_summary(folder.name))
                except Exception as exc:
                    warnings.append(folder.name + ': ' + runtime.safe_text(exc))
        return {'ok': True, 'runs': entries, 'warnings': warnings}


class _Handler(BaseHTTPRequestHandler):
    server_version = 'DiskAgent'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass

    def _local(self, *, write=False):
        port = self.server.server_address[1]
        authorities = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if port == 80:
            authorities.update({'127.0.0.1', 'localhost'})
        hosts = self.headers.get_all('Host', [])
        if len(hosts) != 1 or hosts[0] not in authorities:
            raise TaskValidationError('WEB_ORIGIN', 'Host must be this local workbench.')
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (origins and origins[0] != 'http://' + hosts[0]):
            raise TaskValidationError('WEB_ORIGIN', 'Cross-site requests are not accepted.')
        if write:
            tokens = self.headers.get_all('X-CSRF-Token', [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.server.csrf_token):
                raise TaskValidationError('WEB_CSRF', 'A valid workbench CSRF token is required.')

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, value):
        body = runtime.safe_text(json.dumps(value, ensure_ascii=False, allow_nan=False)).encode('utf-8')
        self._send(status, body, 'application/json; charset=utf-8')

    def _body(self):
        if self.headers.get('Transfer-Encoding') is not None:
            raise TaskValidationError('WEB_BODY', 'Chunked request bodies are not accepted.')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or re.fullmatch(r'\d+', lengths[0]) is None:
            raise TaskValidationError('WEB_BODY', 'One valid Content-Length is required.')
        length = int(lengths[0])
        if length > MAX_BODY:
            raise TaskValidationError('WEB_TOO_LARGE', 'JSON request exceeds 16 KB.')
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise TaskValidationError('WEB_CONTENT_TYPE', 'Use application/json.')
        try:
            raw = self.rfile.read(length)
            self._body_consumed = len(raw) == length
            if len(raw) != length:
                raise ValueError('Incomplete body.')
            value = json.loads(raw.decode('utf-8'), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite number.')))
        except (ValueError, UnicodeError) as exc:
            raise TaskValidationError('WEB_JSON', 'Request must be valid finite UTF-8 JSON.') from exc
        if not isinstance(value, dict):
            raise TaskValidationError('WEB_JSON', 'Request body must be a JSON object.')
        return value

    def _path(self):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            raise TaskValidationError('WEB_PATH', 'Only local relative endpoints are accepted.')
        return unquote(parsed.path)

    def _failed(self, exc):
        code = str(getattr(exc, 'code', type(exc).__name__))
        status = (403 if code in ('WEB_ORIGIN', 'WEB_CSRF') else
                  409 if code in ('WEB_BUSY', 'WORKFLOW_BUSY', 'RECORD_REFERENCED', 'CHAT_BUSY', 'CHAT_REQUEST_CONFLICT') else 413 if code == 'WEB_TOO_LARGE' else
                  404 if code.endswith('NOT_FOUND') or isinstance(exc, FileNotFoundError) else
                  400 if isinstance(exc, (TaskValidationError, ValueError)) else 500)
        if status == 403:
            self._drain_rejected_body()
        self._json(status, _error(code, exc))

    def _drain_rejected_body(self):
        """Avoid Windows resets when rejecting headers before a small POST body.

        Discard bytes only; rejected requests never reach JSON parsing or tools.
        A total deadline also bounds clients that send the body a byte at a time.
        """
        if self.command != 'POST' or getattr(self, '_body_consumed', False):
            return
        lengths = self.headers.get_all('Content-Length', [])
        if (len(lengths) != 1 or re.fullmatch(r'[0-9]+', lengths[0]) is None or
                self.headers.get('Transfer-Encoding') is not None):
            return
        try:
            remaining = int(lengths[0])
        except ValueError:
            return
        if remaining > MAX_BODY:
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 0.5
        try:
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                self.connection.settimeout(timeout)
                chunk = self.rfile.read1(min(remaining, 4096))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._body_consumed = remaining == 0
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def do_GET(self):
        try:
            self._local()
            path = self._path()
            if path == '/':
                return self._send(200, _HTML.read_bytes(), 'text/html; charset=utf-8')
            if path == '/objectives.js':
                return self._send(200, (_HTML.parent / 'objectives.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/chat.js':
                return self._send(200, (_HTML.parent / 'chat.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/mesh.js':
                return self._send(200, (_HTML.parent / 'mesh.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/protection.js':
                return self._send(200, (_HTML.parent / 'protection.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/api/mesh-study':
                from .mesh_study import read_latest
                return self._json(200, read_latest(self.server.settings))
            if path == '/api/config':
                with self.server.jobs_lock:
                    active = self.server.active_job_id
                capabilities = {'mass': 'axisymmetric_revolved_section_v1', 'modal_profile': None,
                                'modal_frequency_collection': 'six_orders_v1'}
                profile_path = self.server.settings.root / 'project_modal' / 'engineering_profile.json'
                if profile_path.is_file():
                    try:
                        profile = runtime.read_json(profile_path)
                        if profile.get('status') == 'native_verified':
                            capabilities['modal_profile'] = profile.get('modal_analysis_name')
                    except (OSError, ValueError):
                        capabilities['modal_profile'] = None
                from python_control.search import load_search_config
                search = load_search_config(self.server.settings.root)
                return self._json(200, {'ok': True, 'app_id': 'disk-agent.workbench.v1', 'csrf_token': self.server.csrf_token,
                                        'active_job_id': active, 'startup_execution': False,
                                        'job_server_instance': self.server.job_store.instance,
                                        'optimization_engine': {'version': 'llm_direct_v1', 'strategy': search['strategy']},
                                        'record_deletion': 'recoverable_soft_delete_v1',
                                        'conversation': {'version': 'disk-agent.chat.v1', 'transport': 'mcp_stdio',
                                            'unified_tasks': 'v1',
                                            'instance': self.server.chat.instance if self.server.chat is not None else None,
                                            'available': (self.server.settings.root / '.venv-mcp' / 'Scripts' / 'python.exe').is_file()},
                                        'engineering_capabilities': capabilities,
                                        'mcp_bridge': {'version': 'disk-agent.mcp.v1', 'transport': 'stdio',
                                                       'persistent_jobs': True, 'idempotent_submissions': True}})
            if path == '/api/mcp/case':
                from .tools import read_case
                return self._json(200, _result(read_case(settings=self.server.settings)))
            if path == '/api/workflows':
                return self._json(200, self.server.workflows())
            if path == '/api/runs':
                return self._json(200, self.server.runs())
            if path == '/api/chat/sessions':
                return self._json(200, self.server.conversations().sessions())
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'protection':
                return self._json(200, self.server.protection_status(parts[2]))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'backups':
                from python_control.checkpoint_history import list_backups
                return self._json(200, list_backups(self.server.run_directory(parts[2])))
            if len(parts) == 4 and parts[:3] == ['api', 'chat', 'sessions']:
                return self._json(200, self.server.conversations().snapshot(parts[3]))
            if len(parts) == 3 and parts[:2] == ['api', 'jobs']:
                job = self.server.job(parts[2])
                return self._json(200, dict(ok=True, job=job)) if job else self._json(404, _error('JOB_NOT_FOUND', 'Unknown job.'))
            if len(parts) in (3, 4) and parts[:2] == ['api', 'workflows']:
                workflow_id = parts[2]
                self.server.store.load(workflow_id)
                if len(parts) == 3:
                    return self._json(200, _result(get_workflow_status(workflow_id, settings=self.server.settings)))
                if parts[3] == 'report':
                    folder = self.server.store.directory(workflow_id)
                    report = folder / 'report.md'
                    if report.resolve() != report:
                        raise TaskValidationError('WORKFLOW_PATH', 'Report cannot redirect outside its workflow.')
                    return self._send(200, report.read_bytes(), 'text/plain; charset=utf-8')
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'analysis':
                self.server.run_directory(parts[2])
                return self._json(200, _result(analyze_run(parts[2], settings=self.server.settings, verify_native=False)))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'status':
                self.server.run_directory(parts[2])
                from .tools import get_run_status
                return self._json(200, _result(get_run_status(parts[2], settings=self.server.settings)))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'optimization-trace':
                self.server.run_directory(parts[2])
                from .optimization_trace import inspect_optimization_trace
                return self._json(200, _result(inspect_optimization_trace(parts[2], settings=self.server.settings)))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'predictions':
                self.server.run_directory(parts[2])
                # Explicit inspection only. No cached model or audit view can
                # conceal newly committed observations or prediction records.
                data = inspect_run(parts[2], settings=self.server.settings, include_records=True)
                return self._json(200, _result({'ok': True, 'data': data}))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'search-diagnostics':
                self.server.run_directory(parts[2])
                from .search_diagnostics import inspect_search_diagnostics
                return self._json(200, _result(inspect_search_diagnostics(parts[2], settings=self.server.settings)))
            return self._json(404, _error('WEB_NOT_FOUND', 'Unknown endpoint.'))
        except Exception as exc:
            self._failed(exc)

    def do_POST(self):
        self._body_consumed = False
        try:
            self._local(write=True)
            path, value = self._path(), self._body()
            parts = path.strip('/').split('/')
            if path == '/api/preflight':
                if set(value) - {'run_id', 'optimization_policy'}:
                    raise TaskValidationError('WEB_FIELDS', '体检仅接受运行ID与目标约束配置。')
                from .preflight import inspect_preflight
                current = self.server.settings
                run_id = value.get('run_id')
                if run_id is not None:
                    folder = self.server.run_directory(run_id)
                    saved = runtime.read_json(folder / 'checkpoint.json')
                    current = replace(current, candidate_count=saved['nLLM'])
                    policy = saved.get('definition', {}).get('optimization_policy')
                    if 'optimization_policy' in value:
                        raise TaskValidationError('WEB_FIELDS', '所选运行体检使用保存的配置。')
                else:
                    policy = value.get('optimization_policy')
                if policy is not None:
                    try:
                        current = current.for_policy(policy)
                    except runtime.ControllerError as error:
                        raise TaskValidationError('OPTIMIZATION_POLICY', str(error)) from error
                return self._json(200, inspect_preflight(current, run_id))
            if (len(parts) == 5 and parts[:2] == ['api', 'runs'] and parts[3] == 'backups'
                    and parts[4] in ('preview', 'restore')):
                allowed = {'backup_id'} if parts[4] == 'preview' else {'backup_id', 'expected_current_sha256'}
                if set(value) != allowed:
                    raise TaskValidationError('WEB_FIELDS', '请提交备份编号，恢复时同时提交预览所得的断点摘要。')
                folder = self.server.run_directory(parts[2])
                if parts[4] == 'preview':
                    from python_control.checkpoint_history import preview_restore
                    preview = preview_restore(self.server.settings, folder, value['backup_id'])
                    if self.server.restore_references(parts[2]):
                        preview.update(can_restore=False, reason_zh='此运行仍有关联工作流，请先移除关联工作流列表记录再恢复。')
                    return self._json(200, preview)
                expected = value['expected_current_sha256']
                if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[a-f0-9]{64}', expected)):
                    raise TaskValidationError('WEB_FIELDS', '断点摘要无效，请重新预览备份。')
                return self._json(200, self.server.restore_checkpoint(parts[2], value['backup_id'], expected))
            if path == '/api/chat/sessions':
                if value:
                    raise TaskValidationError('CHAT_FIELDS', '新建对话请求应提交空对象。')
                return self._json(201, self.server.conversations().create())
            if len(parts) == 5 and parts[:3] == ['api', 'chat', 'sessions'] and parts[4] == 'messages':
                return self._json(202, self.server.conversations().submit(parts[3], value))
            if len(parts) == 5 and parts[:3] == ['api', 'chat', 'sessions'] and parts[4] == 'recover':
                if value:
                    raise TaskValidationError('CHAT_FIELDS', '恢复查询请求应提交空对象。')
                return self._json(200, self.server.conversations().recover(parts[3]))
            if path.startswith('/api/mcp/'):
                from .mcp_api import dispatch
                status, result = dispatch(self.server, path, value)
                return self._json(status, result)
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[0] == 'api' and parts[1] in ('workflows', 'runs') and parts[3] == 'delete':
                if value:
                    raise TaskValidationError('WEB_FIELDS', '删除记录请求应提交空 JSON 对象。')
                kind = 'workflow' if parts[1] == 'workflows' else 'run'
                return self._json(200, self.server.delete_record(kind, parts[2]))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'rename':
                if set(value) != {'display_name'}:
                    raise TaskValidationError('WEB_FIELDS', '重命名请求须包含display_name字段。')
                return self._json(200, self.server.rename_run(parts[2], value['display_name']))
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'objectives':
                from python_control.objectives import archive, selection_summary, validate_policy
                from .validation import read_checkpoint
                if set(value) != {'optimization_policy'}:
                    raise TaskValidationError('WEB_FIELDS', 'Supply an optimization_policy for archive re-evaluation.')
                try:
                    policy = validate_policy(value['optimization_policy'])
                except runtime.ControllerError as exc:
                    raise TaskValidationError('OPTIMIZATION_POLICY', str(exc)) from exc
                folder = self.server.run_directory(parts[2])
                from .tools import get_run_status
                checked = get_run_status(parts[2], settings=self.server.settings)
                if not checked.ok:
                    raise TaskValidationError('CHECKPOINT_RESULT', checked.error['message'])
                snapshot = read_checkpoint(folder)
                data = archive(snapshot['history'], policy)
                data['design_selection'] = selection_summary(snapshot['history'], data)
                data['observations'] = [dict(point, iteration=i) for i, point in enumerate(snapshot['history'])]
                data['basis'] = 'committed_records_without_new_solves'
                data['run_id'] = parts[2]
                if read_checkpoint(folder) != snapshot:
                    raise TaskValidationError('RUN_CHANGED', 'Run changed during re-evaluation.')
                return self._json(200, {'ok': True, 'data': data})
            if path == '/api/workflows':
                allowed = {'request', 'live', 'use_llm', 'context_run_id', 'auto_recover', 'optimization_policy'}
                if set(value) - allowed:
                    raise TaskValidationError('WEB_FIELDS', 'Unknown request fields.')
                text = value.get('request')
                if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\0' in text:
                    raise TaskValidationError('REQUEST_TEXT', '请输入1–4000字的任务，明确候选数量。')
                for name in ('live', 'use_llm', 'auto_recover'):
                    if type(value.get(name, False)) is not bool:
                        raise TaskValidationError('WEB_FLAG', name + ' must be a boolean.')
                context = value.get('context_run_id')
                if context is not None:
                    TaskSpec.from_dict({'operation': 'status', 'run_id': context})
                options = {'auto_recover': value['auto_recover']} if 'auto_recover' in value else {}
                if 'optimization_policy' in value:
                    options['optimization_policy'] = TaskSpec.from_dict({
                        'operation': 'optimize', 'total_candidate_budget': 1,
                        'optimization_policy': value['optimization_policy']}).optimization_policy
                job = self.server.submit(run_workflow, request=text, live=value.get('live', False),
                                         use_llm=value.get('use_llm', False), context_run_id=context, **options)
                return self._json(202, job)
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'workflows'] and parts[3] == 'resume':
                if (set(value) - {'live', 'auto_recover'}
                        or type(value.get('live', False)) is not bool
                        or type(value.get('auto_recover', False)) is not bool):
                    raise TaskValidationError('WEB_FIELDS', 'Resume accepts boolean live and auto_recover fields.')
                self.server.store.load(parts[2])
                options = {'auto_recover': value['auto_recover']} if 'auto_recover' in value else {}
                job = self.server.submit(resume_workflow, workflow_id=parts[2], live=value.get('live', False), **options)
                return self._json(202, job)
            if len(parts) == 4 and parts[:2] == ['api', 'jobs'] and parts[3] == 'stop-recovery':
                if value:
                    raise TaskValidationError('WEB_FIELDS', 'Stop recovery requires an empty JSON object.')
                return self._json(200, self.server.stop_recovery(parts[2]))
            return self._json(404, _error('WEB_NOT_FOUND', 'Unknown endpoint.'))
        except Exception as exc:
            self._failed(exc)

    def do_OPTIONS(self):
        self._json(405, _error('WEB_METHOD', 'Cross-origin access is not supported.'))


def create_server(settings=None, port=8765):
    """Build an unstarted loopback HTTP server; port=0 allocates a test port."""
    if type(port) is not int or port < 0 or port > 65535:
        raise TaskValidationError('WEB_PORT', 'port must be an integer from 0 to 65535.')
    current = settings if settings is not None else runtime.load_settings()
    try:
        return _Server(current, port)
    except OSError as exc:
        raise TaskValidationError('WEB_BIND', f'无法启动本地工作台（端口 {port}）：{runtime.safe_text(exc)}。请更换 --port。') from exc


def serve(settings=None, port=8765, open_browser=False):
    if type(open_browser) is not bool:
        raise TaskValidationError('WEB_FLAG', 'open_browser must be a boolean.')
    server = create_server(settings=settings, port=port)
    url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/'
    print('DiskAgent workbench: ' + url, flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
