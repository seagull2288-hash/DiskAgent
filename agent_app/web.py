"""Loopback-only browser workbench over the existing workflow API.

Starting this server and reading its endpoints never launches an API request or
Workbench. A single background worker accepts explicitly submitted work.
"""
from __future__ import annotations

from datetime import datetime
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


MAX_BODY = 16 * 1024
_JOB_ID = re.compile(r'job_[a-f0-9]{24}')
_HTML = Path(__file__).parent / 'static' / 'index.html'


# Lazy imports keep server startup independent of expensive tool modules.
def run_workflow(*args, **kwargs):
    from .supervisor import run_workflow as operation
    return operation(*args, **kwargs)


def resume_workflow(*args, **kwargs):
    from .supervisor import resume_workflow as operation
    return operation(*args, **kwargs)


def get_workflow_status(*args, **kwargs):
    from .supervisor import get_workflow_status as operation
    return operation(*args, **kwargs)


def analyze_run(*args, **kwargs):
    from .result_analysis import analyze_run as operation
    return operation(*args, **kwargs)


def inspect_run(*args, **kwargs):
    from .performance_predictor import inspect_run as operation
    return operation(*args, **kwargs)


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


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
        self.csrf_token = secrets.token_urlsafe(32)
        self.jobs = {}
        self.active_job_id = None
        self.jobs_lock = threading.Lock()
        self.worker_lock = threading.Lock()
        self.cache = {}
        self.cache_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), _Handler)

    def submit(self, operation, *, workflow_id=None, **kwargs):
        if not self.worker_lock.acquire(blocking=False):
            raise TaskValidationError('WEB_BUSY', '已有任务正在执行，请等待完成后再提交。')
        job_id = 'job_' + uuid.uuid4().hex[:24]
        job = {'job_id': job_id, 'status': 'queued', 'created_at': _now(),
               'workflow_id': workflow_id, 'result': None, 'ok': None}
        with self.jobs_lock:
            # Retain a bounded session job list; workflow evidence remains on disk.
            while len(self.jobs) >= 200:
                oldest = next((key for key, item in self.jobs.items() if item['status'] == 'completed'), None)
                if oldest is None:
                    break
                del self.jobs[oldest]
            self.jobs[job_id] = job
            self.active_job_id = job_id

        def work():
            with self.jobs_lock:
                job['status'], job['started_at'] = 'running', _now()
            try:
                if workflow_id is not None:
                    kwargs['workflow_id'] = workflow_id
                else:
                    def created(created_id):
                        self.store.directory(created_id)  # Trusted callback still requires a directory name.
                        with self.jobs_lock:
                            job['workflow_id'] = created_id
                    kwargs['on_workflow_created'] = created
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
                self.worker_lock.release()

        try:
            threading.Thread(target=work, name='disk-workflow-' + job_id, daemon=True).start()
        except Exception:
            with self.jobs_lock:
                self.jobs.pop(job_id, None)
                self.active_job_id = None
            self.worker_lock.release()
            raise
        return {'ok': True, 'job_id': job_id, 'status': 'queued', 'workflow_id': workflow_id}

    def job(self, job_id):
        if _JOB_ID.fullmatch(job_id) is None:
            return None
        with self.jobs_lock:
            value = self.jobs.get(job_id)
            return json.loads(json.dumps(value)) if value is not None else None

    def run_directory(self, run_id):
        TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
        root = Path(self.settings.output_root).resolve()
        directory = (root / run_id).resolve()
        if directory.parent != root or directory.name != run_id:
            raise TaskValidationError('RUN_PATH', 'Run must be a direct child of the configured output directory.')
        if not directory.is_dir():
            raise TaskValidationError('RUN_NOT_FOUND', 'Requested run does not exist.')
        return directory

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
            return dict(cached)
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
        return dict(summary)

    def workflows(self):
        root = self.store._root()
        entries, warnings = [], []
        if root.is_dir():
            folders = sorted((p for p in root.iterdir() if p.name.startswith('wf_')),
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
            folders = sorted((p for p in root.iterdir() if p.name.startswith('run_')),
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
                  409 if code == 'WEB_BUSY' else 413 if code == 'WEB_TOO_LARGE' else
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
            if path == '/api/config':
                with self.server.jobs_lock:
                    active = self.server.active_job_id
                return self._json(200, {'ok': True, 'app_id': 'disk-agent.workbench.v1', 'csrf_token': self.server.csrf_token,
                                        'active_job_id': active, 'startup_execution': False})
            if path == '/api/workflows':
                return self._json(200, self.server.workflows())
            if path == '/api/runs':
                return self._json(200, self.server.runs())
            parts = path.strip('/').split('/')
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
            if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'predictions':
                self.server.run_directory(parts[2])
                # Explicit inspection only. No cached model or audit view can
                # conceal newly committed observations or prediction records.
                data = inspect_run(parts[2], settings=self.server.settings, include_records=True)
                return self._json(200, _result({'ok': True, 'data': data}))
            return self._json(404, _error('WEB_NOT_FOUND', 'Unknown endpoint.'))
        except Exception as exc:
            self._failed(exc)

    def do_POST(self):
        self._body_consumed = False
        try:
            self._local(write=True)
            path, value = self._path(), self._body()
            if path == '/api/workflows':
                allowed = {'request', 'live', 'use_llm', 'context_run_id'}
                if set(value) - allowed:
                    raise TaskValidationError('WEB_FIELDS', 'Unknown request fields.')
                text = value.get('request')
                if not isinstance(text, str) or not text.strip() or len(text) > 4000 or '\0' in text:
                    raise TaskValidationError('REQUEST_TEXT', '请输入1–4000字的任务，明确候选数量。')
                for name in ('live', 'use_llm'):
                    if type(value.get(name, False)) is not bool:
                        raise TaskValidationError('WEB_FLAG', name + ' must be a boolean.')
                context = value.get('context_run_id')
                if context is not None:
                    TaskSpec.from_dict({'operation': 'status', 'run_id': context})
                job = self.server.submit(run_workflow, request=text, live=value.get('live', False),
                                         use_llm=value.get('use_llm', False), context_run_id=context)
                return self._json(202, job)
            parts = path.strip('/').split('/')
            if len(parts) == 4 and parts[:2] == ['api', 'workflows'] and parts[3] == 'resume':
                if set(value) - {'live'} or type(value.get('live', False)) is not bool:
                    raise TaskValidationError('WEB_FIELDS', 'Resume accepts only a boolean live field.')
                self.server.store.load(parts[2])
                job = self.server.submit(resume_workflow, workflow_id=parts[2], live=value.get('live', False))
                return self._json(202, job)
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
    print('轮盘优化工作台：' + url, flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
