"""API-free candidate evaluation shared by the web UI and MCP clients."""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from .schemas import TaskValidationError
from python_control import runtime

_ID = re.compile(r'candidate_[a-f0-9]{32}')


class ExternalService:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(settings.output_root) / 'external_candidates'
        if self.root.resolve().parent != Path(settings.output_root).resolve():
            raise TaskValidationError('CANDIDATE_PATH', 'External output root cannot redirect outside configured output.')
        self.threads = {}
        self.lock = threading.RLock()

    def save(self, directory, record):
        # Windows readers briefly hold a handle that can deny atomic replace.
        # Retry only persistence, never retry the solver itself.
        with self.lock:
            for attempt in range(20):
                try:
                    runtime.write_json(directory / 'status.json', record)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(.05)

    def case(self):
        problem = runtime.bridge.load_problem(self.settings.problem_file)
        return {'ok': True, 'mode': 'external', 'problem': problem,
                'internal_model_api_calls': 0,
                'instructions': 'Read variable bounds first. Preview parameters, then explicitly submit live=true. Poll candidate ID. Never interpret predictions as measured stress.'}

    def preview(self, parameters):
        problem = runtime.bridge.load_problem(self.settings.problem_file)
        values = runtime.bridge.ordered_values(parameters, problem)
        return {'ok': True, 'parameters': dict(zip(problem['input_order'], values)),
                'internal_model_api_calls': 0, 'solver_started': False}

    def directory(self, candidate_id):
        if not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id):
            raise TaskValidationError('CANDIDATE_ID', 'Invalid candidate ID.')
        directory = (self.root / candidate_id).resolve()
        if directory.parent != self.root.resolve():
            raise TaskValidationError('CANDIDATE_PATH', 'Candidate path escaped output root.')
        return directory

    def status(self, candidate_id):
        directory = self.directory(candidate_id)
        if (directory / 'status.json').resolve() != directory / 'status.json':
            raise TaskValidationError('CANDIDATE_PATH', 'Status file cannot redirect outside candidate directory.')
        with self.lock:
            value = runtime.read_json(directory / 'status.json')
        # A crashed process must never silently resubmit a paid solver operation.
        return value

    def list(self):
        if not self.root.exists():
            return {'ok': True, 'candidates': []}
        paths = sorted(self.root.glob('candidate_*/status.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]
        return {'ok': True, 'candidates': [self.status(p.parent.name) for p in paths]}

    def submit(self, parameters, candidate_id, *, live=False):
        if type(live) is not bool:
            raise TaskValidationError('LIVE_FLAG', 'live must be boolean.')
        preview = self.preview(parameters)
        if not live:
            return preview
        directory = self.directory(candidate_id)
        with self.lock:
            if directory.exists():
                previous = self.status(candidate_id)
                if previous['parameters'] != preview['parameters']:
                    raise TaskValidationError('CANDIDATE_CONFLICT', 'ID already belongs to different parameters.')
                return previous
            # Validate local solver availability without loading model credentials.
            for path in (self.settings.project_file, self.settings.workbench_executable):
                if not Path(path).is_file():
                    raise TaskValidationError('SOLVER_MISSING', 'Required solver/project file missing: ' + str(path))
            directory.mkdir(parents=True, exist_ok=False)
            record = dict(preview, candidate_id=candidate_id, status='queued', result=None,
                          created_at=runtime.local_time())
            self.save(directory, record)

            def work():
                try:
                    from python_control.workbench import evaluate
                    with runtime.project_lock(self.settings.root):
                        record['status'] = 'running'
                        record['solver_started'] = None  # Not yet known whether native launch succeeded.
                        self.save(directory, record)
                        problem = runtime.bridge.load_problem(self.settings.problem_file)
                        values = runtime.bridge.ordered_values(record['parameters'], problem)
                        result = evaluate(values, self.settings, problem, directory / 'evaluations')
                        record.update(status='completed', result=result, solver_started=True)
                except Exception as exc:
                    record.update(ok=False, status='failed', error=runtime.safe_text(str(exc)))
                finally:
                    record['finished_at'] = runtime.local_time()
                    self.save(directory, record)

            thread = threading.Thread(target=work, daemon=False, name=candidate_id)
            self.threads[candidate_id] = thread
            thread.start()
            return self.status(candidate_id)
