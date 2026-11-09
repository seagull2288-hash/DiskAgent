"""Synchronous tools over the existing optimizer. Live execution is explicit."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Any

import run_python_optimization as entry
from python_control import controller
from python_control import runtime
from python_control.plots import plot_history
from python_control.workbench import evaluate as evaluate_native

from .schemas import CASE_ID, TaskSpec, ToolResult, TaskValidationError
from .validation import read_checkpoint, validate_task

_LIVE_LOCK = threading.Lock()


def _error(operation: str, exc: BaseException, *, data=None, warnings=None) -> ToolResult:
    code = getattr(exc, 'code', 'INTERRUPTED' if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
    message = runtime.safe_text(str(exc)) or ('Execution interrupted.' if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
    return ToolResult(False, operation, 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                      data or {}, {'code': str(code), 'message': message}, warnings or [])


def _case_data(validated) -> dict[str, Any]:
    p, settings = validated.problem, validated.settings
    context = p.get('engineering_context', {})
    return {
        'case_id': CASE_ID, 'objective': p['objective'],
        'variables': p['variables'], 'input_order': p['input_order'],
        'workbench_parameter_ids': p['workbench_parameter_ids'], 'decimals': p['decimals'],
        'implemented_constraints': p.get('constraints_currently_implemented', []),
        'not_implemented_constraints': context.get('not_implemented_constraints', []),
        'available_feedback': context.get('available_feedback', []),
        'feedback_limit': context.get('feedback_limit'),
        'declared_physics_profile': context.get('freshly_verified_current_run', {}),
        'physics_reverified_by_this_read': False,
        'definition_file': str(settings.problem_file),
        'project_file': str(settings.project_file), 'solver_execution': 'serial',
    }


def read_case(*, settings=None) -> ToolResult:
    try:
        return ToolResult(True, 'read_case', 'case_read',
                          _case_data(validate_task({'operation': 'check'}, settings=settings)))
    except Exception as exc:
        return _error('read_case', exc)


def _snapshot_data(run_dir: Path) -> dict[str, Any]:
    state = read_checkpoint(run_dir)
    problem = runtime.bridge.load_problem(run_dir / 'problem.json')
    history = state['history']
    best = None
    minimum_mass = None
    if history:
        for i, point in enumerate(history):
            if not isinstance(point, dict):
                raise TaskValidationError('CHECKPOINT_RESULT', 'Invalid committed observation.')
            expected_x = runtime.bridge.ordered_values(point.get('parameters'), problem)
            stress = runtime.bridge.finite_number(point.get('stress_mpa'), 'stress_mpa')
            displacement = runtime.bridge.finite_number(point.get('displacement_mm'), 'displacement_mm')
            if stress <= 0 or displacement < 0:
                raise TaskValidationError('CHECKPOINT_RESULT', 'Invalid stress/deformation in committed history.')
            detail = state['baseline'] if i == 0 else state['records'][i-1].get('detail')
            if not isinstance(detail, dict) or controller.observation(detail) != point:
                raise TaskValidationError('CHECKPOINT_RESULT', 'Committed observations and evaluation records disagree.')
            if detail.get('xActual') != expected_x:
                raise TaskValidationError('CHECKPOINT_RESULT', 'Committed geometry is not the canonical nine-parameter design.')
            if runtime.bridge.finite_number(point.get('area_m2'), 'area_m2') <= 0:
                raise TaskValidationError('CHECKPOINT_RESULT', 'Committed section area must be positive.')
            if point.get('mass_kg') is not None:
                mass = runtime.bridge.finite_number(point['mass_kg'], 'mass_kg')
                if mass <= 0:
                    raise TaskValidationError('CHECKPOINT_RESULT', 'Committed disk mass must be positive.')
                if minimum_mass is None or mass < minimum_mass['mass_kg']:
                    minimum_mass = {'iteration': i, 'mass_kg': mass}
        index = min(range(len(history)), key=lambda i: history[i]['stress_mpa'])
        point = history[index]
        detail = state['baseline'] if index == 0 else state['records'][index-1]['detail']
        best = {'iteration': index, 'parameters': point['parameters'],
                'stress_mpa': point['stress_mpa'], 'displacement_mm': point['displacement_mm'],
                'reduction_percent': (history[0]['stress_mpa']-point['stress_mpa']) / history[0]['stress_mpa']*100,
                'native_report_file': detail.get('nativeReportFile'),
                'evaluation_directory': detail.get('runDir')}
    pending = state.get('pending')
    result = {
        'case_id': CASE_ID, 'run_id': run_dir.name, 'run_dir': str(run_dir),
        'run_status': state['status'], 'completed_candidates': state['nCompleted'],
        'target_total_candidates': state['nLLM'],
        'remaining_candidates': state['nLLM'] - state['nCompleted'],
        'baseline_evaluations_completed': int(state['baseline'] is not None),
        'baseline_stress_mpa': history[0]['stress_mpa'] if history else None,
        'best': best,
        'minimum_mass': minimum_mass,
        'pending': {key: pending.get(key) for key in ('iteration', 'phase')} if isinstance(pending, dict) else None,
        'result_basis': 'committed_controller_checkpoint',
        'native_records_reverified_by_adapter': False,
        'sources': {'checkpoint': str(run_dir/'checkpoint.json'), 'history_csv': str(run_dir/'history.csv')},
        'last_error': {key: runtime.safe_text(state['lastError'].get(key, '')) for key in ('type', 'message', 'time')}
                      if isinstance(state.get('lastError'), dict) else None,
    }
    if state.get('optimization_policy') is not None:
        from python_control.objectives import archive
        result['optimization'] = archive(history, state['optimization_policy'])
        result['optimization']['observations'] = [dict(point, iteration=i) for i, point in enumerate(history)]
    result['design_selection'] = controller.selection_summary(history, result.get('optimization'))
    return result


def get_run_status(run_id=None, *, settings=None) -> ToolResult:
    payload = {'operation': 'status'}
    if run_id is not None:
        payload['run_id'] = run_id
    return execute_task(payload, settings=settings)


def run_optimization(payload, *, live=False, settings=None) -> ToolResult:
    try:
        task = TaskSpec.from_dict(payload)
        if task.operation != 'optimize':
            raise TaskValidationError('OPERATION', 'run_optimization requires an optimize task.')
    except Exception as exc:
        return _error('optimize', exc)
    return execute_task(task, live=live, settings=settings)


@contextmanager
def _serial_live():
    if not _LIVE_LOCK.acquire(blocking=False):
        raise TaskValidationError('BUSY', 'Another live adapter call is running in this process.')
    try:
        yield
    finally:
        _LIVE_LOCK.release()


def execute_task(payload, *, live=False, settings=None, on_run_bound=None,
                 expected_completed_candidates=None,feedback_context=None,
                 on_search_decision=None) -> ToolResult:
    """Validate and execute one task; optimize defaults to a read-only plan.

    check/status never call the LLM or solver. Live optimization retains the
    original preflight -> credential context -> locked controller -> plot order.
    """
    operation = 'unknown'
    owned_run = None
    warnings: list[str] = []
    try:
        if type(live) is not bool:
            raise TaskValidationError('LIVE_FLAG', 'live must be a boolean.')
        if on_run_bound is not None and not callable(on_run_bound):
            raise TaskValidationError('RUN_OBSERVER', 'on_run_bound must be callable.')
        if on_search_decision is not None and not callable(on_search_decision):
            raise TaskValidationError('DECISION_OBSERVER','on_search_decision must be callable.')
        if expected_completed_candidates is not None and (type(expected_completed_candidates) is not int or
                                                          expected_completed_candidates < 0):
            raise TaskValidationError('EXPECTED_COUNT', 'Expected completed count must be a nonnegative integer.')
        spec = TaskSpec.from_dict(payload)
        operation = spec.operation
        validated = validate_task(spec, settings=settings)
        if expected_completed_candidates is not None and validated.completed_candidates != expected_completed_candidates:
            raise TaskValidationError('RUN_CHANGED', 'Run changed since the workflow scheduled this batch.')
        current = validated.settings
        if operation == 'status':
            return ToolResult(True, operation, 'status_read', _snapshot_data(validated.run_dir))
        if operation == 'check':
            report = entry.check_environment(current)
            data = _case_data(validated)
            data['environment'] = {key: report[key] for key in (
                'python_version', 'api_key_configured', 'api_requests_made', 'finite_element_solves_made',
                'search_strategy', 'initial_lhs', 'batch_size', 'pool_size', 'surrogate_min_samples')}
            return ToolResult(True, operation, 'checked', data)
        plan = {'case_id': CASE_ID, 'run_id': validated.run_dir.name if validated.run_dir else None,
                'mode': 'resume' if validated.run_dir else 'new',
                'completed_candidates': validated.completed_candidates,
                'target_total_candidates': validated.target_total_candidates,
                'additional_candidates_to_complete': validated.target_total_candidates-validated.completed_candidates,
                'baseline_additional': 0 if validated.run_dir and read_checkpoint(validated.run_dir)['baseline'] is not None else 1,
                'live_execution_started': False,
                'environment_preflight_performed': False,
                'task': spec.to_dict()}
        if not live:
            return ToolResult(True, operation, 'validated', plan)
        feedback_proposer = None
        if feedback_context is not None:
            from .feedback_search import FeedbackSearchProposer
            from python_control.search import load_search_config
            if (not isinstance(feedback_context,dict) or
                    type(feedback_context.get('target_total_candidates')) is not int or
                    validated.target_total_candidates>feedback_context['target_total_candidates']):
                raise TaskValidationError('FEEDBACK_BUDGET','Child target exceeds its frozen feedback workflow budget.')
            feedback_proposer = FeedbackSearchProposer(load_search_config(current.root),settings=current,
                context=feedback_context,on_decision=on_search_decision)
            feedback_proposer.validate_binding(validated.run_dir.name if validated.run_dir else None)
            if validated.run_dir is None and on_run_bound is None:
                raise TaskValidationError('FEEDBACK_OBSERVER','New feedback workflows require a durable run binding callback.')
        # Environment mutation for API credentials is process-global, so live calls
        # are serial even before the existing inter-process engine lock is entered.
        with _serial_live():
            entry.check_environment(current)
            owned_run = validated.run_dir
            observed_run = None

            def notify_run_bound(folder):
                nonlocal observed_run
                if observed_run == folder:
                    return
                if on_run_bound is not None:
                    on_run_bound(folder.name)
                observed_run = folder

            if owned_run is not None:
                notify_run_bound(owned_run)
            initial_resume = read_checkpoint(owned_run) if owned_run is not None else None
            if initial_resume is not None and initial_resume['nCompleted'] != validated.completed_candidates:
                raise TaskValidationError('RUN_CHANGED', 'Run changed after task validation; read its status and retry.')
            resume_start_checked = initial_resume is None

            def guard_resume_start():
                nonlocal resume_start_checked
                if resume_start_checked:
                    return
                latest = read_checkpoint(owned_run)
                extra = latest['nCompleted'] - validated.completed_candidates
                original_pending = initial_resume.get('pending')
                # The engine may commit an already-evaluated pending result before
                # calling a proposer/evaluator. That single saved result is valid
                # continuation work; unrelated progress invalidates the old budget.
                pending_committed = (extra == 1 and isinstance(original_pending, dict)
                                     and original_pending.get('detail') is not None
                                     and latest['records'][-1].get('detail') == original_pending['detail'])
                if extra != 0 and not pending_committed:
                    raise TaskValidationError('RUN_CHANGED', 'Run advanced before execution; additional budget must be recalculated.')
                resume_start_checked = True

            def tracked_evaluator(x, evaluation_settings, problem, output_root, baseline=None, *, evaluation_dir=None):
                nonlocal owned_run
                guard_resume_start()
                candidate = Path(output_root).resolve().parent
                if candidate.parent != current.output_root.resolve():
                    raise TaskValidationError('RUN_PATH', 'Evaluator output is outside the Python run root.')
                if owned_run is not None and candidate != owned_run:
                    raise TaskValidationError('RUN_REFERENCE', 'Evaluator switched to a different run.')
                owned_run = candidate
                # Persist the workflow/run binding before expensive baseline work.
                notify_run_bound(candidate)
                if feedback_proposer is not None and baseline is not None:
                    saved=read_checkpoint(candidate)
                    proposal=(saved.get('pending') or {}).get('proposal')
                    if not isinstance(proposal,dict):
                        raise TaskValidationError('FEEDBACK_CANDIDATE','Evaluator has no committed candidate to audit.')
                    feedback_proposer.ensure_candidate(proposal,problem,saved['history'],candidate)
                if baseline is not None and search_config['strategy'] != 'direct':
                    # Shadow predictions are durable before candidate FEA. They
                    # never affect the original proposal, budget or observations.
                    from .prediction_audit import record_before_solve
                    record_before_solve(candidate, x, problem, settings=evaluation_settings)
                options = {'evaluation_dir': evaluation_dir} if evaluation_dir is not None else {}
                return evaluate_native(x, evaluation_settings, problem, output_root, baseline, **options)

            tracked_evaluator.supports_durable_evaluation = (
                getattr(evaluate_native, 'supports_durable_evaluation', False) is True)

            api = runtime.bridge.load_config(current.api_config)
            from python_control.search import load_search_config
            search_config = load_search_config(current.root)
            run_options = {'evaluator': tracked_evaluator}
            if search_config['strategy'] == 'direct':
                from .direct_proposals import DirectProposer
                direct_propose = DirectProposer(search_config)
                run_options['proposal_fn'] = direct_propose
            if validated.run_dir is not None or feedback_proposer is not None:
                # Guard before a provider request too, not only before an FEA call.
                from python_control.search import SearchProposer, load_search_config
                search_config = load_search_config(current.root)
                proposer = (feedback_proposer if feedback_proposer is not None else
                            direct_propose if search_config['strategy'] == 'direct' else SearchProposer(search_config))

                def tracked_proposer(*args, **kwargs):
                    guard_resume_start()
                    return proposer(*args, **kwargs)

                if hasattr(proposer, 'config'):
                    tracked_proposer.config = proposer.config
                run_options['proposal_fn'] = tracked_proposer
            with runtime.api_key(current, api['api_key_env']):
                returned = Path(controller.run(current, str(validated.run_dir) if validated.run_dir else None,
                                               **run_options)).resolve()
            guard_resume_start()  # Also catch a stale request that caused no new calls.
            if returned.parent != current.output_root.resolve() or (owned_run is not None and returned != owned_run):
                raise TaskValidationError('RUN_REFERENCE', 'Controller returned an unexpected run directory.')
            owned_run = returned
            notify_run_bound(returned)
            data = _snapshot_data(returned)
            data['live_execution_started'] = True
            data['plots'] = []
            if current.plot_on_complete:
                try:
                    data['plots'] = [str(p) for p in plot_history(returned)]
                except Exception as exc:
                    warnings.append('Optimization results are saved; plot generation failed: ' + runtime.safe_text(exc))
            if data['run_status'] != 'complete':
                raise TaskValidationError('RUN_INCOMPLETE', 'Controller returned without a complete checkpoint.')
            return ToolResult(True, operation, 'completed', data, warnings=warnings)
    except (Exception, KeyboardInterrupt) as exc:
        # Never attribute a global latest_run.json to this call after a failure.
        data = {}
        if owned_run is not None:
            data = {'run_id': owned_run.name, 'run_dir': str(owned_run)}
            try:
                data.update(_snapshot_data(owned_run))
            except Exception:
                pass
        return _error(operation, exc, data=data, warnings=warnings)
