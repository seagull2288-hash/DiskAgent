"""Persistent, bounded orchestration over the existing synchronous disk tools.

The engine checkpoint owns FEA progress. Workflow state owns routing and its frozen
budget. No new optimizer, autonomous budget extension or reflection API is added.
"""
from __future__ import annotations

from pathlib import Path
import re

from python_control import runtime
from python_control.runtime import load_settings
from python_control.search import load_search_config
from .planner import plan_task
from .result_analysis import analyze_run, render_report
from .schemas import TaskSpec, TaskValidationError, ToolResult
from .state import WorkflowStore
from .tools import execute_task, get_run_status
from .validation import read_checkpoint, resolve_run, validate_task
from .feedback_search import default_feedback_policy,validate_policy,update_decision_outcomes,render_decision_report


def _response(store, state, *, status=None, ok=None, preview=None):
    if ok is None:
        ok = state.status not in ('failed','interrupted','needs_review','clarification_needed')
    folder = store.directory(state.workflow_id)
    data = {key:getattr(state,key) for key in (
        'workflow_id','run_id','task','start_completed_candidates','target_total_candidates',
        'batch_size','batches','pending_batch','analysis','result','feedback_policy','decision_trace')}
    data.update(workflow_status=state.status,workflow_dir=str(folder),state_file=str(folder/'state.json'),
                report_file=str(_artifact(folder,'report.md')) if _artifact(folder,'report.md').is_file() else None,
                state=state.to_dict(),live_execution_started=bool(
                    any(b.get('execution_started') for b in state.batches) or
                    (state.pending_batch or {}).get('execution_started')))
    if preview is not None:
        data['preview'] = preview
    return ToolResult(ok,'workflow',status or state.status,data,
                      state.error if not ok else None,list(state.warnings))


def _bare_error(exc):
    code = 'INTERRUPTED' if isinstance(exc,KeyboardInterrupt) else str(getattr(exc,'code',type(exc).__name__))
    return ToolResult(False,'workflow','interrupted' if code=='INTERRUPTED' else 'failed',
                      error={'code':code,'message':runtime.safe_text(exc) or 'Workflow interrupted.'})


def _failed(store,state,exc):
    result = _bare_error(exc)
    status = ('needs_review' if result.error['code'] in (
        'WORKFLOW_RUN_CHANGED','WORKFLOW_RUN_UNBOUND','WORKFLOW_RUN_INCOMPLETE',
        'WORKFLOW_COMPATIBILITY') else result.status)
    state.error = result.error
    try:
        store.record(state,status,error=result.error)
        return _response(store,state)
    except Exception as save_error:
        result.data.update(workflow_id=state.workflow_id,run_id=state.run_id,
                           state_write_error=runtime.safe_text(save_error))
        return result


def _tool_failed(store,state,result):
    state.result = result.to_dict()
    state.error = result.error or {'code':'TOOL_FAILED','message':'A workflow tool did not succeed.'}
    store.record(state,'interrupted' if result.status=='interrupted' else 'failed',error=state.error)
    return _response(store,state)


def _check_definition(state,current):
    if (state.engine_definition != current.definition() or
            state.engine_fingerprint != runtime.fingerprint(current)):
        raise TaskValidationError('WORKFLOW_COMPATIBILITY',
                                  'Engineering settings/files changed since workflow validation; start a reviewed new workflow.')
    if state.feedback_policy is not None:
        validate_policy(state.feedback_policy)


def _check_budget(state):
    spec = TaskSpec.from_dict(state.task)
    expected = (state.start_completed_candidates+spec.additional_candidate_budget
                if spec.additional_candidate_budget is not None else spec.total_candidate_budget)
    if (state.target_total_candidates!=expected or expected is None or
            expected<=state.start_completed_candidates or
            (spec.run_id is None and state.start_completed_candidates!=0)):
        raise TaskValidationError('WORKFLOW_BUDGET','Saved workflow budget disagrees with its original TaskSpec.')
    completed = state.start_completed_candidates
    for index,batch in enumerate(state.batches,1):
        target = batch.get('target_total_candidates')
        if (batch.get('index')!=index or batch.get('start_completed_candidates')!=completed or
                type(target) is not int or not completed<target<=expected or
                batch.get('completed_candidates')!=target or batch.get('run_id')!=state.run_id):
            raise TaskValidationError('WORKFLOW_BUDGET','Saved workflow batches are not a contiguous bounded sequence.')
        completed = target
    if state.pending_batch is not None and state.pending_batch.get('start_completed_candidates')!=completed:
        raise TaskValidationError('WORKFLOW_BUDGET','Pending batch does not continue the committed workflow sequence.')


def _artifact(folder,relative):
    path = folder/relative
    if not path.resolve().is_relative_to(folder.resolve()):
        raise TaskValidationError('WORKFLOW_PATH','Workflow report/analysis path escapes its directory.')
    return path


def _last_completed(state):
    return (state.batches[-1]['completed_candidates'] if state.batches else
            state.start_completed_candidates)


def _progress(state,current):
    """Reconcile only the bound run; never adopt the global latest pointer."""
    if state.run_id is None:
        if state.pending_batch is not None:
            raise TaskValidationError('WORKFLOW_RUN_UNBOUND',
                'The prior call did not commit a run binding. Review the interrupted call before starting another run.')
        return state.start_completed_candidates,None
    snapshot = read_checkpoint(resolve_run(current,state.run_id))
    done = snapshot['nCompleted']
    pending = state.pending_batch
    if pending is None:
        if done != _last_completed(state):
            raise TaskValidationError('WORKFLOW_RUN_CHANGED','The bound run advanced outside the recorded workflow batch.')
    else:
        start,target = pending['start_completed_candidates'],pending['target_total_candidates']
        if (type(start) is not int or type(target) is not int or
                not 0<=start<target<=state.target_total_candidates or not start<=done<=target or
                (done>start and snapshot['nLLM']!=target)):
            raise TaskValidationError('WORKFLOW_RUN_CHANGED','Run progress/target disagrees with the pending batch.')
    if done>state.target_total_candidates:
        raise TaskValidationError('WORKFLOW_RUN_CHANGED','Run exceeds the frozen workflow budget.')
    return done,snapshot


def _next_target(done,target,config):
    size,initial = config['batch_size'],config['initial_lhs']
    if config['strategy']=='direct':
        return min(target,done+size)
    if done<initial:
        return min(target,initial,done+size)
    return min(target,initial+((done-initial)//size+1)*size)


def _bind_run(store,state,current,run_id):
    TaskSpec.from_dict({'operation':'status','run_id':run_id})
    if state.run_id is not None and state.run_id!=run_id:
        raise TaskValidationError('WORKFLOW_RUN_CHANGED','A workflow cannot switch to another optimizer run.')
    # The controller commits its checkpoint before reaching this callback.
    read_checkpoint(resolve_run(current,run_id))
    state.run_id = run_id
    if state.pending_batch is not None:
        state.pending_batch['run_id'] = run_id
    store.save(state)


def _write_analysis(store,state,result,*,batch_index=None):
    folder = store.directory(state.workflow_id)
    state.analysis = result.data
    state.decision_trace = update_decision_outcomes(state.decision_trace,result.data)
    for decision in state.decision_trace:
        if re.fullmatch(r'dec_[a-f0-9]{24}',decision['decision_id']) is None:
            raise TaskValidationError('WORKFLOW_DECISION','Invalid saved decision identifier.')
        runtime.write_json(_artifact(folder,'decisions/'+decision['decision_id']+'.json'),decision)
    for warning in result.warnings:
        if warning not in state.warnings:
            state.warnings.append(warning)
    runtime.write_json(_artifact(folder,'analysis.json'),result.to_dict())
    if batch_index is not None:
        runtime.write_json(_artifact(folder,'analysis/batch_%03d.json'%batch_index),result.to_dict())
    try:
        runtime.atomic_text(_artifact(folder,'report.md'),render_report(result.data)+render_decision_report(state.decision_trace))
    except Exception as exc:
        message = 'Results/analysis are saved; Markdown report failed: '+runtime.safe_text(exc)
        if message not in state.warnings:
            state.warnings.append(message)


def _analyze_pending(store,state,current):
    pending = state.pending_batch
    snapshot = read_checkpoint(resolve_run(current,state.run_id))
    if (snapshot['nCompleted']!=pending['target_total_candidates'] or snapshot.get('pending') is not None or
            snapshot['status'] not in ('complete','interrupted')):
        raise TaskValidationError('WORKFLOW_RUN_INCOMPLETE','Pending batch is not a completed controller checkpoint.')
    if snapshot['status']=='interrupted':
        warning = 'The engine stopped after committing this full batch; committed results are reverified before continuation.'
        if warning not in state.warnings:
            state.warnings.append(warning)
    store.record(state,'analyzing',batch=pending['index'],run_id=state.run_id)
    result = analyze_run(state.run_id,settings=current,
                         since_candidate=pending['start_completed_candidates'],verify_native=True)
    if not result.ok:
        return _tool_failed(store,state,result)
    _write_analysis(store,state,result,batch_index=pending['index'])
    if result.data['constraint_evaluation']['all_implemented_rules_passed'] is False:
        raise TaskValidationError('WORKFLOW_RULES','Committed candidates violate an implemented search rule; review evidence.')
    finished = dict(pending,completed_candidates=snapshot['nCompleted'],status='completed',
                    finished_at=runtime.local_time(),analysis_file=str(
                        store.directory(state.workflow_id)/'analysis'/('batch_%03d.json'%pending['index'])))
    state.batches.append(finished)
    state.pending_batch = None
    state.error = None
    store.record(state,'validated',completed_candidates=snapshot['nCompleted'],batch=finished['index'])
    return None


def _record_decision(store,state,trace):
    if (trace.get('workflow_id')!=state.workflow_id or trace.get('run_id')!=state.run_id or
            re.fullmatch(r'dec_[a-f0-9]{24}',trace.get('decision_id','')) is None):
        raise TaskValidationError('WORKFLOW_DECISION','Search decision is not bound to this workflow and run.')
    entries=list(state.decision_trace)
    for i,old in enumerate(entries):
        if old['generation']==trace['generation'] and old['decision_id']!=trace['decision_id']:
            raise TaskValidationError('WORKFLOW_DECISION','A committed batch decision cannot be replaced.')
        if old['decision_id']==trace['decision_id']:
            immutable=('plan_signature','feedback_id','center','rationale','feedback_source_completed_candidates')
            if any(old.get(key)!=trace.get(key) for key in immutable):
                raise TaskValidationError('WORKFLOW_DECISION','Cached decision changed its committed evidence.')
            trace=dict(trace,outcome=old['outcome'])
            entries[i]=trace
            break
    else:
        entries.append(trace)
    state.decision_trace=sorted(entries,key=lambda item:item['generation'])
    store.save(state)


def _partial_analysis(store,state,current):
    if not state.run_id or not state.decision_trace:
        return
    try:
        result=analyze_run(state.run_id,settings=current,
                           since_candidate=state.start_completed_candidates,verify_native=True)
        if result.ok:
            _write_analysis(store,state,result)
        else:
            state.warnings.append('Partial decision outcomes could not be verified: '+result.error['message'])
    except Exception as exc:
        state.warnings.append('Partial decision report could not be saved: '+runtime.safe_text(exc))


def _run_optimization(store,state,current,*,live):
    if state.text_plan_only:
        return _response(store,state,status='planned',ok=True,
                         preview={'execution_suppressed_by_request':True})
    _check_definition(state,current)
    _check_budget(state)
    done,snapshot = _progress(state,current)
    # The target was frozen when the parent task was first validated. All child
    # tasks use absolute totals, so restart never re-adds an additional budget.
    preview = {'run_id':state.run_id,'completed_candidates':done,
               'target_total_candidates':state.target_total_candidates,
               'remaining_candidates':state.target_total_candidates-done,
               'baseline_additional':int(snapshot is None or snapshot['baseline'] is None),
               'live_execution_started':False}
    if not live:
        if done<state.target_total_candidates:
            validate_task({'operation':'optimize','run_id':state.run_id,
                           'total_candidate_budget':state.target_total_candidates},settings=current)
        return _response(store,state,status='validated',ok=True,preview=preview)
    store.record(state,'checking')
    check = execute_task({'operation':'check'},settings=current)
    if not check.ok:
        return _tool_failed(store,state,check)
    config = load_search_config(current.root)
    if config['batch_size']!=state.batch_size:
        raise TaskValidationError('WORKFLOW_COMPATIBILITY','Search batch size differs from the validated workflow.')
    while True:
        done,snapshot = _progress(state,current)
        if state.pending_batch is not None and done==state.pending_batch['target_total_candidates']:
            failed = _analyze_pending(store,state,current)
            if failed is not None:
                return failed
            continue
        if done==state.target_total_candidates:
            break
        if state.pending_batch is None:
            state.pending_batch = {
                'index':len(state.batches)+1,'start_completed_candidates':done,
                'target_total_candidates':_next_target(done,state.target_total_candidates,config),
                'run_id':state.run_id,'status':'scheduled','execution_started':False,
                'started_at':runtime.local_time()}
            store.record(state,'running',batch=state.pending_batch['index'])
        pending = state.pending_batch
        task = {'operation':'optimize','total_candidate_budget':pending['target_total_candidates']}
        if state.run_id is not None:
            task['run_id'] = state.run_id
        # Callback persists the actual new run before its first baseline evaluator.
        pending['execution_started'] = True
        pending['status'] = 'running'
        store.record(state,'running',batch=pending['index'])
        feedback_options={}
        if state.feedback_policy is not None and state.feedback_policy['enabled']:
            feedback_options={'feedback_context':{'workflow_id':state.workflow_id,'policy':state.feedback_policy,
                                                  'target_total_candidates':state.target_total_candidates},
                              'on_search_decision':lambda trace:_record_decision(store,state,trace)}
        result = execute_task(task,live=True,settings=current,expected_completed_candidates=done,
                              on_run_bound=lambda run_id:_bind_run(store,state,current,run_id),**feedback_options)
        state.result = result.to_dict()
        if result.data.get('run_id') is not None:
            _bind_run(store,state,current,result.data['run_id'])
        if not result.ok:
            _partial_analysis(store,state,current)
            return _tool_failed(store,state,result)
        if state.run_id is None:
            raise TaskValidationError('WORKFLOW_RUN_UNBOUND','Executor returned without a persistent run binding.')
        failed = _analyze_pending(store,state,current)
        if failed is not None:
            return failed
    # Final report covers the entire requested window, including all its batches.
    store.record(state,'analyzing',scope='workflow')
    analysis = analyze_run(state.run_id,settings=current,
                           since_candidate=state.start_completed_candidates,verify_native=True)
    if not analysis.ok:
        return _tool_failed(store,state,analysis)
    _write_analysis(store,state,analysis)
    state.error = None
    store.record(state,'completed',completed_candidates=done)
    return _response(store,state)


def run_workflow(request=None,*,task=None,live=False,use_llm=False,context_run_id=None,settings=None,
                 on_workflow_created=None):
    """Create and plan one persisted workflow; optimize is preview-only by default."""
    store = state = None
    try:
        if type(live) is not bool or type(use_llm) is not bool:
            raise TaskValidationError('WORKFLOW_FLAG','live and use_llm must be booleans.')
        if on_workflow_created is not None and not callable(on_workflow_created):
            raise TaskValidationError('WORKFLOW_OBSERVER','on_workflow_created must be callable.')
        if (request is None)==(task is None):
            raise TaskValidationError('WORKFLOW_INPUT','Supply exactly one natural-language request or TaskSpec.')
        if task is not None and (use_llm or context_run_id is not None):
            raise TaskValidationError('WORKFLOW_INPUT','LLM planning/context apply only to natural-language requests.')
        current = settings if settings is not None else load_settings()
        store = WorkflowStore(current)
        state = store.create(request_text=request)
        if on_workflow_created is not None:
            on_workflow_created(state.workflow_id)
        with store.lock(state.workflow_id):
            store.record(state,'planning')
            if request is not None:
                plan = plan_task(request,settings=current,use_llm=use_llm,context_run_id=context_run_id)
                state.planning = plan.to_dict()
                if not plan.ok:
                    state.error = plan.error
                    store.record(state,'clarification_needed' if plan.status=='clarification_needed' else 'failed')
                    return _response(store,state)
                spec = TaskSpec.from_dict(plan.data['task'])
                state.text_plan_only = plan.data['plan_only']
            else:
                spec = TaskSpec.from_dict(task)
            state.task = spec.to_dict()
            store.record(state,'planned')
            if state.text_plan_only:
                return _response(store,state)
            store.record(state,'validating')
            validated = validate_task(spec,settings=current)
            state.run_id = validated.run_dir.name if validated.run_dir else None
            state.start_completed_candidates = validated.completed_candidates
            state.target_total_candidates = validated.target_total_candidates
            search_config=load_search_config(current.root)
            state.batch_size = search_config['batch_size']
            if spec.operation=='optimize':
                state.feedback_policy=default_feedback_policy(enabled=search_config['strategy']!='direct')
            state.engine_definition = current.definition()
            state.engine_fingerprint = runtime.fingerprint(current)
            store.record(state,'validated')
            return _dispatch(store,state,current,live=live)
    except (Exception,KeyboardInterrupt) as exc:
        return _failed(store,state,exc) if state is not None else _bare_error(exc)


def _dispatch(store,state,current,*,live):
    spec = TaskSpec.from_dict(state.task)
    if spec.operation=='optimize':
        return _run_optimization(store,state,current,live=live)
    payload = spec.to_dict()
    if state.run_id is not None:
        payload['run_id'] = state.run_id
    result = execute_task(payload,settings=current)
    state.result = result.to_dict()
    if not result.ok:
        return _tool_failed(store,state,result)
    if spec.operation=='status':
        analysis = analyze_run(state.run_id,settings=current)
        if not analysis.ok:
            return _tool_failed(store,state,analysis)
        _write_analysis(store,state,analysis)
    state.error = None
    store.record(state,'completed')
    return _response(store,state)


def resume_workflow(workflow_id,*,live=False,settings=None):
    """Resume a frozen workflow, explicitly reauthorizing real execution each time."""
    store = state = None
    try:
        if type(live) is not bool:
            raise TaskValidationError('WORKFLOW_FLAG','live must be a boolean.')
        current = settings if settings is not None else load_settings()
        store = WorkflowStore(current)
        with store.lock(workflow_id):
            state = store.load(workflow_id)
            if state.status=='completed' or state.text_plan_only:
                return _response(store,state)
            if state.task is None:
                return _response(store,state,ok=False)
            if state.engine_definition is None:
                raise TaskValidationError('WORKFLOW_INCOMPLETE','Initial task validation did not commit; submit a corrected request.')
            return _dispatch(store,state,current,live=live)
    except (Exception,KeyboardInterrupt) as exc:
        return _failed(store,state,exc) if state is not None else _bare_error(exc)


def get_workflow_status(workflow_id,*,settings=None):
    """Read workflow state and, when bound, its current engine snapshot."""
    try:
        current = settings if settings is not None else load_settings()
        store = WorkflowStore(current)
        state = store.load(workflow_id)
        result = _response(store,state,status='workflow_read',ok=True)
        if state.run_id is not None:
            snapshot = get_run_status(state.run_id,settings=current)
            result.data['run_snapshot'] = snapshot.to_dict()
        return result
    except Exception as exc:
        return _bare_error(exc)
