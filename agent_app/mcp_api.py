"""Strict MCP-facing HTTP actions sharing the web worker and native project lock."""
from __future__ import annotations

import hashlib
import json

from .schemas import TaskSpec, TaskValidationError


def _fields(value, allowed):
    if set(value) - set(allowed):
        raise TaskValidationError('MCP_FIELDS', 'Unknown MCP request fields.')
    for name in ('live', 'auto_recover'):
        if name in value and type(value[name]) is not bool:
            raise TaskValidationError('MCP_FLAG', name + ' must be a boolean.')


def dispatch(server, path, value):
    from .web import _result, run_workflow, resume_workflow
    from .design_evaluation import evaluate_design
    from .tools import execute_task
    if path == '/api/mcp/evaluate-design':
        _fields(value, ('parameters', 'optimization_policy', 'live', 'request_id'))
        # Validate even before queueing, including explicit policy/profile and bounds.
        preview = evaluate_design(value.get('parameters'), optimization_policy=value.get('optimization_policy'),
                                  settings=server.settings)
        if not value.get('live', False) or not preview.ok:
            return 200, _result(preview)
        operation = evaluate_design
        kwargs = {'parameters': value['parameters'], 'optimization_policy': value.get('optimization_policy'), 'live': True}
    elif path == '/api/mcp/submit-optimization':
        _fields(value, ('task', 'live', 'auto_recover', 'request_id'))
        task = TaskSpec.from_dict(value.get('task'))
        if task.operation != 'optimize':
            raise TaskValidationError('MCP_TASK', 'submit_optimization requires an optimize TaskSpec.')
        # Replay must precede engine validation: a completed continuation may have
        # already reached its original budget. First validate the immutable schema.
        if not value.get('live', False):
            return 200, _result(execute_task(task, live=False, settings=server.settings))
        operation = run_workflow
        kwargs = {'task': task.to_dict(), 'live': True, 'use_llm': False,
                  'auto_recover': value.get('auto_recover', False)}
    elif path == '/api/mcp/resume-workflow':
        _fields(value, ('workflow_id', 'live', 'auto_recover', 'request_id'))
        server.store.load(value.get('workflow_id'))
        if not value.get('live', False):
            return 200, _result(resume_workflow(value['workflow_id'], live=False, settings=server.settings))
        operation = resume_workflow
        kwargs = {'workflow_id': value['workflow_id'], 'live': True,
                  'auto_recover': value.get('auto_recover', False)}
    else:
        raise TaskValidationError('MCP_NOT_FOUND', 'Unknown MCP action.')
    # A timeout/reconnect with identical request_id returns the original job.
    # Changed arguments conflict; there is no automatic retry of a native solve.
    digest = hashlib.sha256(json.dumps({'path': path, 'args': kwargs}, sort_keys=True,
                                      ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()
    request_id = value.get('request_id')
    server.job_store.request_path(request_id)
    return 202, server.submit(operation, request_id=request_id, request_digest=digest, **kwargs)
