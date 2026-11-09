"""Bounded recovery over the original workflow and immutable engine budget."""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import threading
import time

from python_control import runtime
from .schemas import TaskValidationError, ToolResult
from .state import WorkflowStore
from .recovery_evidence import inspect_failure, count_episode_http


SCHEMA = 'disk-agent.recovery.v1'
POLICY = {'max_attempts': 2, 'max_total_attempts': 6, 'max_http_attempts': 10,
          'delays_seconds': [30, 120], 'max_total_wait_seconds': 600}


def _now_epoch():
    return time.time()


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _wait_until(deadline, stop):
    while not stop.is_set():
        remaining = deadline - _now_epoch()
        if remaining <= 0:
            return True
        stop.wait(min(remaining, 1.0))
    return False


def _path(store, workflow_id):
    path = store.directory(workflow_id) / 'recovery.json'
    if path.resolve() != path:
        raise TaskValidationError('RECOVERY_PATH', 'Recovery record cannot redirect outside its workflow.')
    return path


def _load(store, workflow):
    path = _path(store, workflow.workflow_id)
    if not path.exists():
        return None
    try:
        data = runtime.read_json(path)
        if (not isinstance(data, dict) or data.get('schema_version') != SCHEMA
                or data.get('workflow_id') != workflow.workflow_id
                or data.get('run_id') != workflow.run_id
                or data.get('target_total_candidates') != workflow.target_total_candidates
                or data.get('policy') != POLICY):
            raise ValueError('Recovery binding or policy differs from the saved workflow.')
        for name in ('total_attempts', 'total_wait_seconds'):
            value = data.get(name)
            if type(value) is not int or value < 0:
                raise ValueError('Invalid recovery counters.')
        episode = data.get('episode')
        if episode is not None and (not isinstance(episode, dict)
                or type(episode.get('attempts')) is not int or episode['attempts'] < 0
                or not isinstance(episode.get('context'), dict)):
            raise ValueError('Invalid recovery episode.')
        if data.get('status') not in ('idle', 'waiting', 'retrying', 'recovered', 'retry_failed',
                                      'completed', 'stopped', 'needs_review', 'exhausted'):
            raise ValueError('Unknown recovery status.')
        if not isinstance(data.get('events'), list):
            raise ValueError('Missing recovery event history.')
        json.dumps(data, allow_nan=False)
        return data
    except (OSError, ValueError, TypeError, runtime.bridge.BridgeError) as error:
        raise TaskValidationError('RECOVERY_RECORD', 'Cannot trust the saved recovery record: ' + runtime.safe_text(error)) from error


def _new(workflow):
    return {'schema_version': SCHEMA, 'policy': dict(POLICY),
            'workflow_id': workflow.workflow_id, 'run_id': workflow.run_id,
            'target_total_candidates': workflow.target_total_candidates,
            'status': 'idle', 'reason_zh': '已启用有上限的故障恢复。',
            'total_attempts': 0, 'total_wait_seconds': 0, 'episode': None,
            'next_retry_at': None, 'last_error': None, 'events': []}


def _save(store, data, status, message):
    data['status'], data['reason_zh'] = status, message
    data['events'] = (data['events'] + [{'at': _iso(_now_epoch()), 'status': status, 'message': message}])[-100:]
    runtime.atomic_text(_path(store, data['workflow_id']), json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


def _remaining_budget(data, context):
    episode = data.get('episode')
    attempts = episode['attempts'] if episode and episode['context']['episode_id'] == context['episode_id'] else 0
    if attempts >= POLICY['max_attempts']:
        return '本次故障已达到自动恢复次数上限，请检查模型服务后手动处理。'
    if data['total_attempts'] >= POLICY['max_total_attempts']:
        return '本工作流已达到自动恢复总次数上限。'
    if context['http_attempts'] + context['call_http_reserve'] > POLICY['max_http_attempts']:
        return '当前中心请求的HTTP次数不足以预留一次完整恢复，已停止自动请求。'
    delay = POLICY['delays_seconds'][attempts]
    if data['total_wait_seconds'] + delay > POLICY['max_total_wait_seconds']:
        return '本工作流已达到自动恢复等待时间上限。'
    return None


def _finish(store, data, current):
    """Account only the failed episode's HTTP scope, even after later batches."""
    episode = data['episode']
    context = episode['context']
    after = count_episode_http(context, current)
    episode['http_attempts_after'] = after
    if after > POLICY['max_http_attempts']:
        raise TaskValidationError('RECOVERY_HTTP_BUDGET', 'Observed episode HTTP attempts exceeded the recovery cap.')
    from .validation import read_checkpoint, resolve_run
    state = read_checkpoint(resolve_run(current, data['run_id']))
    recovered = state['nCompleted'] >= context['pending_iteration']
    data['next_retry_at'] = None
    _save(store, data, 'recovered' if recovered else 'retry_failed',
          '故障候选已提交，原优化任务继续执行。' if recovered else '本次恢复尚未提交故障候选，正在重新检查错误证据。')


def inspect_recovery(workflow_id, *, settings=None):
    """Read state and eligibility only. A GET never waits, reserves or resumes."""
    current = settings if settings is not None else runtime.load_settings()
    store = WorkflowStore(current)
    try:
        workflow = store.load(workflow_id)
        context = inspect_failure(workflow_id, current)
        data = _load(store, workflow)
        view = {key: context.get(key) for key in (
            'workflow_id', 'run_id', 'completed_candidates', 'target_total_candidates',
            'remaining_candidates', 'pending_iteration', 'pending_phase', 'http_attempts', 'last_error')}
        view.update(status='available' if context.get('eligible') else
                    'completed' if workflow.status == 'completed' else
                    'needs_review' if workflow.status in ('failed', 'needs_review') else 'idle',
                    reason_zh=context.get('reason_zh', ''), can_recover=context.get('eligible', False),
                    attempt=0, max_attempts=POLICY['max_attempts'], total_attempts=0,
                    max_total_attempts=POLICY['max_total_attempts'], max_http_attempts=POLICY['max_http_attempts'],
                    next_retry_at=None, events=[])
        if data is not None:
            episode = data.get('episode')
            same_episode = episode and episode['context'].get('episode_id') == context.get('episode_id')
            view.update(status=data['status'], reason_zh=data['reason_zh'],
                        attempt=episode['attempts'] if same_episode else 0,
                        total_attempts=data['total_attempts'], next_retry_at=data.get('next_retry_at'),
                        events=data['events'], last_error=context.get('last_error') or data.get('last_error'))
            if (data['status'] == 'retrying' and episode and
                    (context.get('completed_candidates') or 0) >= episode['context']['pending_iteration']):
                view.update(status='recovered', reason_zh='故障候选已提交，优化任务正在继续。')
            if (workflow.status in ('failed', 'interrupted', 'needs_review')
                    and context.get('last_error') and view['status'] in ('recovered', 'completed')):
                view.update(status='available' if context.get('eligible') else 'needs_review',
                            reason_zh=context.get('reason_zh', ''), next_retry_at=None)
            if workflow.status == 'completed':
                view.update(status='completed', reason_zh='工作流已完成。', can_recover=False)
            elif context.get('eligible'):
                if data['status'] == 'needs_review':
                    view.update(status='available', reason_zh=context.get('reason_zh', ''))
                limit = _remaining_budget(data, context)
                if limit and data['status'] not in ('waiting', 'retrying'):
                    view.update(status='exhausted', reason_zh=limit, can_recover=False)
        if view['status'] in ('recovered', 'completed'):
            view['last_recovered_error'] = view.get('last_error')
            view['last_error'] = None
        return view
    except Exception as error:
        return {'workflow_id': workflow_id, 'status': 'needs_review', 'can_recover': False,
                'reason_zh': runtime.safe_text(error), 'events': []}


def _schedule(store, data, context, stop):
    if stop.is_set():
        data['next_retry_at'] = None
        _save(store, data, 'stopped', '已停止后续自动恢复，断点可供手动继续。')
        return False
    episode = data.get('episode')
    same = episode and episode['context']['episode_id'] == context['episode_id']
    if data['status'] == 'retrying':
        _save(store, data, 'needs_review', '上次自动恢复在请求期间中断，请核对回执后手动继续。')
        return False
    continuing_wait = same and data['status'] == 'waiting'
    if continuing_wait:
        if episode['trigger_signature'] != context['trigger_signature']:
            _save(store, data, 'needs_review', '等待期间故障证据已变化，请重新核对断点。')
            return False
        deadline = datetime.fromisoformat(data['next_retry_at']).timestamp()
    else:
        limit = _remaining_budget(data, context)
        if limit:
            _save(store, data, 'exhausted', limit)
            return False
        if not same:
            episode = {'context': dict(context), 'attempts': 0}
            data['episode'] = episode
        delay = POLICY['delays_seconds'][episode['attempts']]
        deadline = _now_epoch() + delay
        episode.update(attempts=episode['attempts'] + 1,
                       trigger_signature=context['trigger_signature'],
                       http_attempts_before=context['http_attempts'],
                       reserved_http=context['call_http_reserve'])
        data['total_attempts'] += 1
        data['total_wait_seconds'] += delay
        data['next_retry_at'] = _iso(deadline)
        data['last_error'] = context.get('last_error')
        causes = {'eligible_candidate_bounds': '候选尺寸越界',
                  'eligible_candidate_duplicate': '候选尺寸重复',
                  'eligible_http': 'HTTP服务错误'}
        cause = causes.get(context.get('category'), '候选提案格式或数值错误')
        _save(store, data, 'waiting', '检测到可恢复的%s，等待%d秒后重试原断点。' % (cause, delay))
    if not _wait_until(deadline, stop):
        data['next_retry_at'] = None
        _save(store, data, 'stopped', '已停止后续自动恢复，断点可供手动继续。')
        return False
    latest = inspect_failure(context['workflow_id'], store.settings)
    if (not latest.get('eligible') or latest.get('episode_id') != context['episode_id']
            or latest.get('trigger_signature') != context['trigger_signature']):
        _save(store, data, 'needs_review', '等待后断点或错误证据已变化，自动恢复已暂停。')
        return False
    # Persist the reservation before resuming. A crash in the request phase
    # remains visible across app restarts and cannot silently clear the budget.
    data['next_retry_at'] = None
    _save(store, data, 'retrying', '正在调用原工作流续算，并复核恢复后的提交结果。')
    return True


def _saved_execution_ready(workflow, settings):
    """An explicit resume may reuse accepted work through the original supervisor.

    Request-stage failures still go through evidence and recovery reservations.
    The supervisor retains its compatibility, budget and native-result checks.
    """
    if workflow.run_id is None:
        return False
    from .validation import read_checkpoint, resolve_run
    checkpoint = read_checkpoint(resolve_run(settings, workflow.run_id))
    if checkpoint['status'] not in ('complete', 'failed', 'interrupted'):
        return False
    pending = checkpoint.get('pending')
    if pending is None:
        return checkpoint.get('baseline') is not None
    return (pending.get('phase') in ('candidate_ready', 'solving', 'evaluated')
            and isinstance(pending.get('proposal'), dict))


def _execute(initial, workflow_id, kwargs, stop, holder):
    from . import supervisor
    current = kwargs.get('settings') or runtime.load_settings()
    store = WorkflowStore(current)
    result = None
    with ExitStack() as stack:
        def bind(created):
            holder['workflow_id'] = created
            stack.enter_context(store._file_lock(created, '.recovery.lock'))
            callback = original_callback
            if callback is not None:
                callback(created)
        if workflow_id is None:
            original_callback = kwargs.get('on_workflow_created')
            kwargs['on_workflow_created'] = bind
        else:
            stack.enter_context(store._file_lock(workflow_id, '.recovery.lock'))
            workflow = store.load(workflow_id)
            data = _load(store, workflow)
            if data is not None and data['status'] == 'retrying':
                from .validation import read_checkpoint, resolve_run
                state = read_checkpoint(resolve_run(current, workflow.run_id))
                if state['nCompleted'] >= data['episode']['context']['pending_iteration']:
                    _finish(store, data, current)
            context = inspect_failure(workflow_id, current)
            if (workflow.status in ('failed', 'interrupted', 'needs_review')
                    and not _saved_execution_ready(workflow, current)):
                snapshot = supervisor.get_workflow_status(workflow_id, settings=current)
                result = ToolResult(False, 'workflow', 'failed', snapshot.data, workflow.error)
        if result is None:
            result = initial(**kwargs)
        workflow_id = holder['workflow_id']
        while workflow_id is not None and not result.ok:
            context = inspect_failure(workflow_id, current)
            workflow = store.load(workflow_id)
            data = _load(store, workflow) or _new(workflow)
            if not context.get('eligible'):
                data['last_error'] = context.get('last_error') or result.error
                _save(store, data, 'needs_review', context.get('reason_zh', '故障需要人工检查。'))
                break
            if not _schedule(store, data, context, stop):
                break
            result = supervisor.resume_workflow(workflow_id, live=True, settings=current)
            _finish(store, data, current)
        if workflow_id is not None:
            if result.ok and result.status == 'completed':
                workflow = store.load(workflow_id)
                data = _load(store, workflow)
                if data is not None:
                    _save(store, data, 'completed', '原工作流已完成，自动恢复记录已保存。')
            result.data['recovery'] = inspect_recovery(workflow_id, settings=current)
        return result


def _dispatch(initial, workflow_id, *, auto_recover, recovery_stop, kwargs):
    if type(auto_recover) is not bool:
        raise TaskValidationError('RECOVERY_FLAG', 'auto_recover must be a boolean.')
    if not auto_recover or kwargs.get('live', False) is not True:
        return initial(**kwargs)
    stop = recovery_stop if recovery_stop is not None else threading.Event()
    holder = {'workflow_id': workflow_id}
    try:
        return _execute(initial, workflow_id, dict(kwargs), stop, holder)
    except (Exception, KeyboardInterrupt) as error:
        data = {'workflow_id': holder['workflow_id']}
        if holder['workflow_id'] is not None:
            from .supervisor import get_workflow_status
            saved = get_workflow_status(holder['workflow_id'], settings=kwargs.get('settings'))
            if saved.ok:
                data = saved.data
            data['recovery'] = inspect_recovery(holder['workflow_id'], settings=kwargs.get('settings'))
        return ToolResult(False, 'workflow', 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                          data, {'code': str(getattr(error, 'code', 'RECOVERY_ERROR')),
                                 'message': runtime.safe_text(error) or 'Recovery interrupted.'})


def run_workflow(*args, auto_recover=False, recovery_stop=None, **kwargs):
    from .supervisor import run_workflow as operation
    if args:
        kwargs['request'] = args[0]
        if len(args) != 1:
            raise TypeError('At most one request argument is supported.')
    return _dispatch(operation, None, auto_recover=auto_recover,
                     recovery_stop=recovery_stop, kwargs=kwargs)


def resume_workflow(workflow_id, *, auto_recover=False, recovery_stop=None, **kwargs):
    from .supervisor import resume_workflow as operation
    def initial(**options):
        return operation(workflow_id, **options)
    return _dispatch(initial, workflow_id, auto_recover=auto_recover,
                     recovery_stop=recovery_stop, kwargs=kwargs)
