"""Durable local jobs and idempotent MCP submissions; no solver imports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from python_control import runtime
from .schemas import TaskValidationError

JOB_ID = re.compile(r'job_[a-f0-9]{24}')


class JobStore:
    def __init__(self, root):
        self.root = Path(root).resolve() / 'agent_jobs'
        self.instance = uuid.uuid4().hex
        self.owner_url = None

    def path(self, job_id):
        if not isinstance(job_id, str) or JOB_ID.fullmatch(job_id) is None:
            raise TaskValidationError('JOB_ID', 'Use a job ID, not a file path.')
        path = self.root / (job_id + '.json')
        if path.resolve() != path:
            raise TaskValidationError('JOB_PATH', 'Job files cannot redirect outside their store.')
        return path

    def save(self, job):
        runtime.write_json(self.path(job['job_id']), dict(job, owner_instance=self.instance, owner_url=self.owner_url))

    def load(self, job_id):
        path = self.path(job_id)
        if not path.is_file():
            return None
        value = runtime.read_json(path)
        if value.get('job_id') != job_id:
            raise TaskValidationError('JOB_RECORD', 'Job record ID disagrees with its filename.')
        if value.get('status') in ('queued', 'running'):
            owner = value.get('owner_url')
            if owner and owner != self.owner_url:
                try:
                    parsed = urlsplit(owner)
                    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path
                            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port is None):
                        raise ValueError('Invalid task owner URL.')
                    opener = build_opener(ProxyHandler({}))
                    with opener.open(owner + '/api/config', timeout=1) as response:
                        config = json.load(response)
                    if config.get('job_server_instance') == value.get('owner_instance'):
                        with opener.open(owner + '/api/jobs/' + job_id, timeout=2) as response:
                            remote = json.load(response).get('job')
                        if isinstance(remote, dict) and remote.get('job_id') == job_id:
                            return dict(remote, owner_url=owner)
                except (OSError, ValueError):
                    pass
            value['recorded_status'] = value['status']
            value['status'] = 'detached'
            value['warning'] = ('Job is outside this server session. Inspect its workflow/native evidence; '
                                'do not resubmit a solve automatically.')
        value.pop('owner_instance', None)
        return value

    def request_path(self, request_id):
        if (not isinstance(request_id, str) or
                re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', request_id) is None):
            raise TaskValidationError('REQUEST_ID', 'Live calls require a stable 1-100 character request_id (letters, digits, _, ., -).')
        path = self.root / 'requests' / (hashlib.sha256(request_id.encode()).hexdigest() + '.json')
        if path.resolve() != path:
            raise TaskValidationError('JOB_PATH', 'Request files cannot redirect outside their store.')
        return path

    def request_lock(self):
        # A separate ledger lock, not the engineering project lock held by FEA.
        self.root.mkdir(parents=True, exist_ok=True)
        return runtime.project_lock(self.root)

    def replay(self, request_id, digest):
        path = self.request_path(request_id)
        if not path.is_file():
            return None
        value = runtime.read_json(path)
        if value.get('digest') != digest:
            raise TaskValidationError('REQUEST_CONFLICT', 'request_id was already used with different arguments.')
        return value['job_id']

    def bind(self, request_id, digest, job_id):
        runtime.write_json(self.request_path(request_id), {'digest': digest, 'job_id': job_id})
