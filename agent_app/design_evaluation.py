"""Bounded single-design capability over the existing verified native evaluator."""
from __future__ import annotations

from pathlib import Path
import uuid

from python_control import runtime
from .schemas import TaskValidationError, ToolResult
from .validation import validate_task
from .vendor.workbench_mcp import path_state, state_changed


def validate_design(parameters, *, optimization_policy=None, settings=None):
    task = {'operation': 'optimize', 'total_candidate_budget': 1}
    if optimization_policy is not None:
        task['optimization_policy'] = optimization_policy
    validated = validate_task(task, settings=settings)
    x = runtime.bridge.ordered_values(parameters, validated.problem)
    from python_control.engineering_checks import require_geometry
    geometry = require_geometry(x, validated.problem)
    data = {'case_id': validated.spec.case_id,
            'parameters': dict(zip(validated.problem['input_order'], x)),
            'project_file': str(validated.settings.project_file),
            'optimization_policy': validated.spec.optimization_policy,
            'workbench_evaluation_tasks': 1, 'llm_requests': 0,
            'baseline_evaluation_tasks': 0,
            'duplicate_exclusion': 'not_applicable_to_independent_design_evaluation',
            'constraints_checked': ['nine_variable_bounds', 'four_decimal_quantization'],
            'engineering_feasibility': 'not_assessed_by_parameter_validation',
            'affects_optimizer_history': False, 'geometry_precheck': geometry}
    return validated, x, data


def evaluate_design(parameters, *, optimization_policy=None, live=False, settings=None,
                    on_workflow_created=None, on_evaluation_created=None):
    from .tools import _error
    try:
        if type(live) is not bool:
            raise TaskValidationError('LIVE_FLAG', 'live must be a boolean.')
        validated, x, data = validate_design(parameters, optimization_policy=optimization_policy, settings=settings)
        if not live:
            return ToolResult(True, 'evaluate_design', 'validated', data)
        current = validated.settings
        from python_control.workbench import evaluate, verify_native
        from python_control.controller import observation, require_metrics
        with runtime.project_lock(current.root):
            folder = current.root / 'agent_jobs' / 'evaluations' / ('design_' + uuid.uuid4().hex)
            folder.mkdir(parents=True, exist_ok=False)
            runtime.write_json(folder / 'request.json', data)
            if on_evaluation_created is not None:
                on_evaluation_created(str(folder))
            # Reuse the same Journal, CSV mapping, freshness and physics checks as optimization.
            detail = evaluate(x, current, validated.problem, folder)
            evaluation_dir = Path(detail['runDir']).resolve()
            if evaluation_dir.parent != folder.resolve():
                raise TaskValidationError('EVALUATION_PATH', 'Native evaluation escaped its dedicated output folder.')
            report = Path(detail['nativeReportFile']).resolve()
            if report.parent != evaluation_dir:
                raise TaskValidationError('EVALUATION_PATH', 'Native report escaped its evaluation folder.')
            artifact = path_state(report)
            if not state_changed({'exists': False}, artifact):
                raise TaskValidationError('EVALUATION_ARTIFACT', 'A fresh nonempty native report is required.')
            verify_native(detail)
            require_metrics(detail, current.optimization_policy)
            data.update(observation=observation(detail), native_report_file=str(report),
                        evaluation_directory=str(evaluation_dir), native_verified=True,
                        metric_evidence=detail.get('metric_evidence', {}),
                        engineering_checks=detail.get('engineering_checks'),
                        geometry_precheck=detail.get('geometry_checks', data['geometry_precheck']),
                        artifact=artifact,
                        engineering_feasibility='not_assessed_without_reference_and_constraint_evaluation')
            runtime.write_json(folder / 'result.json', data)
        return ToolResult(True, 'evaluate_design', 'completed', data)
    except (Exception, KeyboardInterrupt) as exc:
        return _error('evaluate_design', exc)
