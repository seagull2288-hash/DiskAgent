"""Recover terminal results from verified artifacts without replaying computation."""
from __future__ import annotations

from pathlib import Path

from python_control import runtime


def recover_job(job, settings):
    if job.get('status') != 'detached':
        return job
    try:
        if job.get('operation') == 'evaluate_design' and job.get('evaluation_root'):
            folder = Path(job['evaluation_root'])
            expected = Path(settings.root) / 'agent_jobs' / 'evaluations'
            if folder.resolve().parent != expected.resolve() or folder.resolve() != folder:
                raise ValueError('Evaluation artifacts are outside their registered directory.')
            request = runtime.read_json(folder / 'request.json')
            candidates = list(folder.glob('eval_*'))
            if len(candidates) != 1:
                return job
            from python_control.workbench import collect_result, verify_native
            from python_control.controller import observation, require_metrics
            from python_control.objectives import validate_policy
            policy = request.get('optimization_policy')
            if policy is not None:
                policy = validate_policy(policy)
            problem = runtime.bridge.load_problem(settings.problem_file)
            x = runtime.bridge.ordered_values(request['parameters'], problem)
            detail = collect_result(candidates[0], x, problem)
            verify_native(detail)
            require_metrics(detail, policy)
            data = dict(request, observation=observation(detail), native_verified=True,
                        native_report_file=detail['nativeReportFile'], evaluation_directory=detail['runDir'],
                        metric_evidence=detail.get('metric_evidence', {}),
                        engineering_feasibility='not_assessed_without_reference_and_constraint_evaluation')
            from .schemas import ToolResult
            result = ToolResult(True, 'evaluate_design', 'completed', data).to_dict()
            runtime.write_json(folder / 'result.json', data)
        elif job.get('workflow_id'):
            from .supervisor import get_workflow_status
            result = get_workflow_status(job['workflow_id'], settings=settings).to_dict()
            terminal = result.get('data', {}).get('workflow_status')
            if terminal not in ('completed', 'failed', 'interrupted', 'needs_review', 'clarification_needed'):
                return job
            if terminal != 'completed':
                result['ok'] = False
                result['error'] = result.get('data', {}).get('state', {}).get('error') or {
                    'code': 'RECOVERED_WORKFLOW_FAILED', 'message': '原工作流以异常状态结束。'}
        else:
            return job
        return dict(job, status='completed', ok=result['ok'], result=result,
                    recovered_at=runtime.local_time(), recovery_source='verified_saved_artifacts')
    except Exception as exc:
        return dict(job, recovery_error=runtime.safe_text(exc))
