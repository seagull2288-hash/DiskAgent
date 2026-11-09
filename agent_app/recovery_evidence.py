"""Read-only eligibility and HTTP accounting for bounded workflow recovery.

Only canonical transport events and saved public configuration are inspected.
Execution permission, scheduling, reservations and sidecar writes are owned by
the recovery coordinator.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from python_control import runtime
from python_control.sampling import signature
from python_control.search import validate_search_config
from . import supervisor
from .schemas import TaskSpec, TaskValidationError
from .state import WorkflowStore
from .validation import read_checkpoint, resolve_run, validate_task


RETRYABLE_HTTP = frozenset((500, 502, 504))
RETRYABLE_CANDIDATE = {
    'BOUNDS': '越界', 'DUPLICATE': '重复',
    'CANDIDATE_SCHEMA': '字段格式错误', 'INVALID_JSON': 'JSON格式错误',
    'CANDIDATE_JSON': 'JSON封装格式错误', 'PARAMETERS': '参数字段不完整',
    'INVALID_NUMBER': '参数数值无效',
}
_TERMINAL = frozenset(('success', 'http_error', 'network_error_unknown_delivery'))


def _fail(message):
    raise TaskValidationError('RECOVERY_EVIDENCE', message)


def _inside(path, root):
    root = Path(root).absolute()
    path = Path(path).absolute()
    if (root.resolve() != root or path.resolve() != path or
            not path.is_relative_to(root)):
        _fail('Recovery evidence path is redirected or outside its owner.')
    return path


def _json(path, root, *, limit=8 * 1024 * 1024):
    path = _inside(path, root)
    if not path.is_file() or path.stat().st_size > limit:
        _fail('Recovery evidence is missing or exceeds its size limit.')
    raw = path.read_bytes()

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('Recovery evidence contains duplicate JSON keys.')
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=unique,
                           parse_constant=lambda _: _fail('Recovery evidence contains nonfinite JSON.'))
    except (UnicodeError, ValueError) as exc:
        _fail('Cannot parse recovery evidence: ' + runtime.safe_text(exc))
    return value, hashlib.sha256(raw).hexdigest()


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        _fail(label + ' must be an integer >= ' + str(minimum) + '.')
    return value


def _started_time(value):
    if not isinstance(value, str) or len(value) > 100:
        _fail('HTTP start time is missing or invalid.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail('HTTP start time requires a timezone.')
    return parsed.astimezone(timezone.utc)


def _transport(path, run):
    events, digest = _json(path, run, limit=256 * 1024)
    if not isinstance(events, list) or not events:
        _fail('Canonical transport events must be a nonempty list.')
    starts, terminals, started_order = {}, {}, []
    for event in events:
        if not isinstance(event, dict):
            _fail('A transport event must be an object.')
        attempt = _integer(event.get('attempt'), 'HTTP attempt', 1)
        status = event.get('status')
        if not isinstance(status, str):
            _fail('Transport status must be a string.')
        if status == 'started':
            if attempt in starts:
                _fail('HTTP attempt has more than one start event.')
            if set(starts) != set(terminals):
                _fail('A new HTTP attempt started before the prior attempt terminated.')
            starts[attempt] = _started_time(event.get('started_utc'))
            started_order.append(attempt)
        elif status in _TERMINAL:
            if attempt not in starts or attempt in terminals:
                _fail('HTTP terminal event has no unique preceding start.')
            if status == 'http_error':
                code = _integer(event.get('http_status'), 'HTTP status', 100)
                if code > 599:
                    _fail('HTTP status is outside the supported range.')
            terminals[attempt] = event
        else:
            _fail('Unknown transport event status.')
    if started_order != list(range(1, len(starts) + 1)):
        _fail('Transport attempt numbers are not a contiguous sequence.')
    if set(starts) != set(terminals):
        _fail('An HTTP attempt has no terminal event; delivery is unresolved.')
    if any(event['status'] == 'network_error_unknown_delivery' for event in terminals.values()):
        _fail('A network request has unknown delivery status.')
    last_attempt = started_order[-1]
    if any(starts[a] > starts[b] for a, b in zip(started_order, started_order[1:])):
        _fail('HTTP start timestamps are out of order.')
    return {
        'path': str(path.relative_to(run)), 'digest': digest,
        'attempts': len(starts), 'last_started': starts[last_attempt],
        'mtime_ns': path.stat().st_mtime_ns, 'terminal': terminals[last_attempt],
    }


def _config(run):
    value, _ = _json(run / 'search_config.json', run)
    return validate_search_config(value)


def _scope(run, config, iteration, done):
    if config['strategy'] == 'direct':
        return 'direct_iteration', None, run / 'iterations' / ('iter_%03d' % iteration) / 'api'
    initial, size = config['initial_lhs'], config['batch_size']
    if done < initial or (done - initial) % size:
        _fail('A center request must belong to a frozen batch boundary.')
    generation = (done - initial) // size + 1
    return 'center_generation', generation, run / 'search' / ('generation_%03d' % generation) / 'center_api'


def _scan(run, scope):
    scope = _inside(scope, run)
    if not scope.is_dir():
        _fail('Canonical HTTP scope is missing.')
    records = []
    for request in scope.glob('request_*'):
        request = _inside(request, run)
        if not request.is_dir():
            _fail('Canonical request path must be a directory.')
        for proposal in request.glob('proposal_*'):
            proposal = _inside(proposal, run)
            if not proposal.is_dir():
                _fail('Canonical proposal path must be a directory.')
            records.append(_transport(proposal / 'transport.json', run))
    if not records:
        _fail('No canonical transport evidence exists for this pending request.')
    newest_key = max((r['last_started'], r['mtime_ns']) for r in records)
    newest = [r for r in records if (r['last_started'], r['mtime_ns']) == newest_key]
    if len(newest) != 1:
        _fail('The most recent HTTP request cannot be determined uniquely.')
    digests = sorted((r['path'], r['digest']) for r in records)
    return {
        'attempts': sum(r['attempts'] for r in records), 'latest': newest[0],
        'log_digest': signature(digests), 'file_count': len(records),
    }


def _episode(context):
    identity = {key: context[key] for key in (
        'workflow_id', 'run_id', 'pending_iteration', 'history_signature',
        'child_target', 'target_total_candidates')}
    return 'episode_' + signature(identity)[:24]


def _pending_paths(run, pending):
    iteration = _integer(pending['iteration'], 'Pending iteration', 1)
    directory = _inside(run / 'iterations' / ('iter_%03d' % iteration), run)
    if not isinstance(pending.get('directory'), str) or _inside(pending['directory'], run) != directory:
        _fail('Pending iteration directory does not belong to this run.')
    if not isinstance(pending.get('api_output'), str):
        _fail('Pending request has no durable API output path.')
    output = _inside(pending['api_output'], run)
    if output.parent != directory / 'api' or not output.name.startswith('request_'):
        _fail('Pending API output is outside its iteration request directory.')
    return output


def _center_basis(run, scope, config, history):
    generation_folder = scope.parent
    basis, _ = _json(generation_folder / 'basis.json', run)
    if (not isinstance(basis, dict) or set(basis) != {'config_signature', 'history_signature'} or
            basis['config_signature'] != signature(config) or basis['history_signature'] != signature(history)):
        _fail('Saved center-generation basis differs from the frozen configuration/history.')
    for name in ('center.json', 'plan.json'):
        path = _inside(generation_folder / name, run)
        if path.exists():
            _fail('A center or batch plan already exists; this is not a pure unresolved center request.')


def _latest_source(scan, output, kind):
    request_name = Path(scan['latest']['path']).parent.parent.name
    allowed = {output.name}
    if kind == 'center_generation':
        # The feedback adapter uses this deterministic private staging name.
        allowed.add('request_s' + signature(str(output))[:16])
    if request_name not in allowed:
        _fail('Latest HTTP evidence does not belong to the checkpoint pending request.')


def _candidate_rejection_evidence(run, output, scan, checkpoint, workflow_error, limits):
    """Authenticate a rejected direct proposal before permitting another request."""
    if scan['latest']['terminal']['status'] != 'success':
        _fail('A candidate rejection requires a successfully returned HTTP response.')
    proposal = _inside(run / scan['latest']['path'], run).parent
    final_attempt = _integer(limits.get('max_proposal_attempts'), 'Proposal attempt limit', 1)
    if proposal != output / ('proposal_%02d' % final_attempt):
        _fail('Candidate rejection is not the final configured proposal attempt.')
    iteration = _inside(Path(checkpoint['pending']['directory']), run)
    if ((output / 'candidate.json').exists() or (iteration / 'candidate.json').exists()):
        _fail('An accepted candidate already exists for this pending request.')
    manifest, manifest_digest = _json(output / 'manifest.json', run)
    answer, answer_digest = _json(proposal / 'answer.json', run)
    rejection, rejection_digest = _json(proposal / 'rejection.json', run)
    if not all(isinstance(value, dict) for value in (manifest, answer, rejection)):
        _fail('Candidate rejection evidence must contain JSON objects.')
    if (manifest.get('command') != 'propose' or manifest.get('mode') != 'live'
            or manifest.get('status') != 'failed'
            or type(manifest.get('finite_element_calls')) is not int
            or manifest['finite_element_calls'] != 0):
        _fail('Candidate manifest does not describe an unresolved live proposal with zero FEA calls.')
    text = answer.get('text')
    error = checkpoint.get('lastError')
    code = workflow_error.get('code')
    if code not in RETRYABLE_CANDIDATE:
        _fail('Candidate rejection is outside the automatic recovery whitelist.')
    expected_error = {'code': code, 'message': rejection.get('message')}
    if (not isinstance(text, str) or rejection.get('previous_response') != text
            or rejection.get('code') != code
            or manifest.get('error') != expected_error or workflow_error != expected_error
            or not isinstance(error, dict) or error.get('type') != 'BridgeError'
            or error.get('message') != expected_error['message']):
        _fail('Candidate answer, rejection, manifest and saved validation error disagree.')
    request_http = sum(_transport(folder / 'transport.json', run)['attempts']
                       for folder in output.glob('proposal_*') if folder.is_dir())
    if (type(manifest.get('http_attempts')) is not int
            or manifest['http_attempts'] != request_http):
        _fail('Candidate manifest HTTP count differs from its canonical transports.')
    problem = runtime.bridge.load_problem(_inside(run / 'problem.json', run))
    try:
        runtime.bridge.parse_candidate(text, problem, checkpoint['history'])
    except runtime.bridge.BridgeError as exc:
        if exc.code != code or str(exc) != expected_error['message']:
            _fail('Replaying the rejected proposal does not reproduce the saved validation error.')
    else:
        _fail('The recorded rejected proposal now validates successfully.')
    return signature({'manifest': manifest_digest, 'answer': answer_digest,
                      'rejection': rejection_digest})


def _base(workflow_id):
    return {
        'eligible': False, 'category': 'evidence_invalid', 'reason_zh': '恢复证据尚未通过核验。',
        'workflow_id': workflow_id, 'run_id': None, 'completed_candidates': None,
        'target_total_candidates': None, 'remaining_candidates': None,
        'pending_iteration': None, 'pending_phase': None, 'child_target': None,
        'generation': None, 'scope_kind': None, 'episode_id': None,
        'history_signature': None, 'trigger_signature': None,
        'http_attempts': None, 'call_http_reserve': None, 'last_error': None,
    }


def inspect_failure(workflow_id, settings):
    """Describe retry eligibility without changing workflow or engine files."""
    result = _base(workflow_id)
    stage = 'evidence_invalid'
    try:
        store = WorkflowStore(settings)
        workflow = store.load(workflow_id)
        policy = (workflow.task or {}).get('optimization_policy')
        if policy is not None:
            settings = settings.for_policy(policy)
        folder = store.directory(workflow_id)
        workflow_raw, workflow_digest = _json(folder / 'state.json', folder)
        if workflow_raw != workflow.to_dict():
            _fail('Workflow changed while its snapshot was being loaded.')
        result.update(run_id=workflow.run_id, target_total_candidates=workflow.target_total_candidates,
                      last_error=workflow.error)
        run = checkpoint = pending = None
        checkpoint_digest = None
        if workflow.run_id is not None:
            run = resolve_run(settings, workflow.run_id)
            _, checkpoint_digest = _json(run / 'checkpoint.json', run)
            checkpoint = read_checkpoint(run)
            done = checkpoint['nCompleted']
            target = workflow.target_total_candidates
            result.update(completed_candidates=done,
                          remaining_candidates=target - done if type(target) is int else None,
                          history_signature=signature(checkpoint['history']))
            pending = checkpoint.get('pending')
            if isinstance(pending, dict):
                result.update(pending_iteration=pending.get('iteration'), pending_phase=pending.get('phase'))
            if isinstance(workflow.pending_batch, dict):
                result['child_target'] = workflow.pending_batch.get('target_total_candidates')
        if workflow.status == 'completed':
            result.update(category='completed', reason_zh='工作流已经完成。')
            return result
        if (workflow.status != 'failed' or not isinstance(workflow.error, dict) or
                (workflow.error.get('code') != 'HTTP_ERROR'
                 and workflow.error.get('code') not in RETRYABLE_CANDIDATE) or workflow.run_id is None):
            result.update(category='not_retryable', reason_zh='当前工作流未处于可自动恢复的HTTP或候选校验失败状态。')
            return result
        done = checkpoint['nCompleted']
        target = _integer(workflow.target_total_candidates, 'Frozen parent target', 1)
        result.update(completed_candidates=done, remaining_candidates=target - done,
                      history_signature=signature(checkpoint['history']))
        if (checkpoint['status'] != 'failed' or not isinstance(pending, dict) or
                pending.get('phase') != 'requesting' or pending.get('proposal') is not None or
                pending.get('detail') is not None or type(pending.get('iteration')) is not int or
                pending['iteration'] != done + 1):
            result.update(category='not_retryable', reason_zh='断点不是尚未生成候选的请求阶段，须检查当前执行状态。')
            return result
        if not isinstance(workflow.pending_batch, dict):
            _fail('Workflow has no frozen pending child batch.')
        output = _pending_paths(run, pending)
        if workflow.pending_batch.get('run_id') != run.name:
            _fail('Pending workflow batch belongs to another run.')
        child = _integer(workflow.pending_batch.get('target_total_candidates'), 'Child target', 1)
        result['child_target'] = child
        if checkpoint['nLLM'] != child or not done < child <= target:
            _fail('Engine and workflow child targets disagree.')
        # Compatibility checks are intentionally read-only. resume_workflow can
        # persist failures, so it is not used as an inspection shortcut.
        stage = 'compatibility'
        supervisor._check_definition(workflow, settings)
        supervisor._check_budget(workflow)
        supervisor._progress(workflow, settings)
        validate_task({'operation': 'optimize', 'run_id': run.name,
                       'total_candidate_budget': child}, settings=settings)
        stage = 'evidence_invalid'
        config = _config(run)
        kind, generation, scope = _scope(run, config, pending['iteration'], done)
        result.update(scope_kind=kind, generation=generation)
        if kind == 'center_generation':
            _center_basis(run, scope, config, checkpoint['history'])
        # load_config validates public retry settings. It never reads a key
        # value or constructs request headers.
        api_path = _inside(run / 'api_config.json', run)
        limits = runtime.bridge.load_config(api_path)
        reserve = (_integer(limits.get('max_http_attempts'), 'HTTP attempt limit', 1) *
                   _integer(limits.get('max_proposal_attempts'), 'Proposal attempt limit', 1))
        result['call_http_reserve'] = reserve
        scan = _scan(run, scope)
        _latest_source(scan, output, kind)
        result.update(http_attempts=scan['attempts'], episode_id=_episode(result))
        trigger = {'last_error': checkpoint.get('lastError'), 'log_digest': scan['log_digest']}
        terminal = scan['latest']['terminal']
        error = checkpoint.get('lastError')
        message = error.get('message') if isinstance(error, dict) else None
        if workflow.error['code'] in RETRYABLE_CANDIDATE:
            if kind != 'direct_iteration':
                result.update(category='not_retryable', reason_zh='候选校验恢复仅适用于直接LLM提案的请求阶段。')
                return result
            trigger['candidate_evidence'] = _candidate_rejection_evidence(
                run, output, scan, checkpoint, workflow.error, limits)
            category = 'eligible_candidate_' + workflow.error['code'].removeprefix('CANDIDATE_').lower()
            reason = ('已核验直接LLM提案' + RETRYABLE_CANDIDATE[workflow.error['code']]
                      + '且未进入仿真，可在原预算内恢复同一候选请求。')
        else:
            matched = re.match(r'^HTTP ([1-5][0-9]{2})(?:[; .]|$)', message or '')
            if (terminal['status'] != 'http_error' or not matched or
                    int(matched.group(1)) != terminal.get('http_status')):
                _fail('Checkpoint HTTP error and the most recent terminal transport event disagree.')
            code = terminal['http_status']
            if code not in RETRYABLE_HTTP:
                result.update(category='not_retryable', reason_zh='HTTP '+str(code)+'不在本版自动恢复白名单内。')
                return result
            category = 'eligible_http'
            reason = '已确认HTTP '+str(code)+'失败，可在持久预算及等待策略内恢复同一工作流。'
        result['trigger_signature'] = signature(trigger)
        # Recheck both owners after compatibility/native validation and scans.
        if (_json(run / 'checkpoint.json', run)[1] != checkpoint_digest or
                _json(folder / 'state.json', folder)[1] != workflow_digest):
            result.update(category='state_changed', reason_zh='核验期间运行状态发生变化，请重新读取。')
            return result
        result.update(eligible=True, category=category, reason_zh=reason)
        return result
    except Exception as exc:
        result.update(eligible=False, category=stage,
                      reason_zh='自动恢复核验未通过。'+runtime.safe_text(exc))
        return result


def _count_episode_http(context, settings):
    """Count one captured episode after resume, even if the run has advanced.

    Unavailable or unresolved evidence raises instead of turning unknown cost
    into zero. The caller retains its durable reservation in that case.
    """
    if not isinstance(context, dict):
        _fail('Recovery context must be an object.')
    workflow_id, run_id = context.get('workflow_id'), context.get('run_id')
    if run_id is None:
        _fail('Recovery accounting requires an explicit bound run.')
    TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
    store = WorkflowStore(settings)
    workflow = store.load(workflow_id)
    policy = (workflow.task or {}).get('optimization_policy')
    if policy is not None:
        settings = settings.for_policy(policy)
    if workflow.run_id != run_id or workflow.target_total_candidates != context.get('target_total_candidates'):
        _fail('Recovery episode no longer belongs to its workflow and parent budget.')
    run = resolve_run(settings, run_id)
    checkpoint = read_checkpoint(run)
    iteration = _integer(context.get('pending_iteration'), 'Pending iteration', 1)
    done = _integer(context.get('completed_candidates'), 'Episode source count')
    child = _integer(context.get('child_target'), 'Episode child target', 1)
    parent = _integer(context.get('target_total_candidates'), 'Episode parent target', 1)
    if not done < child <= parent:
        _fail('Recovery episode budget is invalid.')
    if iteration != done + 1 or len(checkpoint['history']) < done + 1:
        _fail('Recovery episode has an invalid source candidate count.')
    if signature(checkpoint['history'][:done + 1]) != context.get('history_signature'):
        _fail('Recovery episode source history changed.')
    if _episode(context) != context.get('episode_id'):
        _fail('Recovery episode identity changed.')
    kind, generation, scope = _scope(run, _config(run), iteration, done)
    if kind != context.get('scope_kind') or generation != context.get('generation'):
        _fail('Recovery episode HTTP scope changed.')
    return _scan(run, scope)['attempts']


def count_episode_http(context, settings):
    """Return captured-scope HTTP starts or a structured evidence error."""
    try:
        return _count_episode_http(context, settings)
    except TaskValidationError:
        raise
    except Exception as exc:
        _fail('Cannot account for recovery HTTP attempts: ' + runtime.safe_text(exc))
