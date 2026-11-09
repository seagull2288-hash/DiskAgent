"""Read-only task/case/run validation. Never calls an API or starts a solver."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from python_control import controller
from python_control.runtime import Settings, bridge, load_settings, read_json

from .schemas import TaskSpec, TaskValidationError
from .record_trash import is_deleted


@dataclass(frozen=True)
class ValidatedTask:
    spec: TaskSpec
    settings: Settings
    problem: dict[str, Any]
    run_dir: Path | None
    completed_candidates: int
    target_total_candidates: int | None


def resolve_run(settings: Settings, run_id: str | None) -> Path:
    root = settings.output_root.resolve()
    if run_id is None:
        latest = read_json(root / 'latest_run.json')
        if not isinstance(latest, dict) or not isinstance(latest.get('runDir'), str):
            raise TaskValidationError('RUN_REFERENCE', 'Invalid latest_run.json.')
        candidate = Path(latest['runDir']).resolve()
        if candidate.parent == root and is_deleted(candidate):
            available = [folder for folder in root.iterdir()
                         if folder.name.startswith('run_') and folder.is_dir()
                         and (folder / 'checkpoint.json').is_file() and not is_deleted(folder)]
            if not available:
                raise TaskValidationError('RUN_NOT_FOUND', '当前没有可用的运行记录，请新建优化。')
            candidate = max(available, key=lambda folder: (folder / 'checkpoint.json').stat().st_mtime_ns).resolve()
    else:
        candidate = (root / run_id).resolve()
    # Direct children only, including after resolving symlinks/junctions.
    if candidate.parent != root or not candidate.name.startswith('run_'):
        raise TaskValidationError('RUN_PATH', 'Run must be a direct child of the Python output directory.')
    if not candidate.is_dir() or is_deleted(candidate):
        raise TaskValidationError('RUN_NOT_FOUND', 'Requested Python run does not exist.')
    return candidate


def read_checkpoint(run_dir: Path) -> dict[str, Any]:
    if is_deleted(run_dir):
        raise TaskValidationError('RUN_NOT_FOUND', '该运行已从列表删除。')
    state = read_json(run_dir / 'checkpoint.json')
    if not isinstance(state, dict) or state.get('schema') != controller.SCHEMA or state.get('controller') != 'python':
        raise TaskValidationError('CHECKPOINT_SCHEMA', 'A current Python checkpoint.json is required; MATLAB .mat is unsupported.')
    if not isinstance(state.get('runDir'), str) or Path(state['runDir']).resolve() != run_dir.resolve():
        raise TaskValidationError('RUN_REFERENCE', 'Checkpoint belongs to a different run directory.')
    done, target = state.get('nCompleted'), state.get('nLLM')
    if type(done) is not int or type(target) is not int or done < 0 or target < done or target < 1:
        raise TaskValidationError('CHECKPOINT_COUNTS', 'Invalid completed/target candidate counts.')
    history, records = state.get('history'), state.get('records')
    has_baseline = state.get('baseline') is not None
    if done and not has_baseline:
        raise TaskValidationError('CHECKPOINT_RESULT', 'Completed candidates require a committed baseline.')
    if not isinstance(history, list) or not isinstance(records, list) or len(records) != done or len(history) != done + int(has_baseline):
        raise TaskValidationError('CHECKPOINT_COUNTS', 'Committed history, records and counts disagree.')
    if not isinstance(state.get('status'), str):
        raise TaskValidationError('CHECKPOINT_STATUS', 'Checkpoint has no run status.')
    if has_baseline and not isinstance(state['baseline'], dict):
        raise TaskValidationError('CHECKPOINT_RESULT', 'Invalid baseline record.')
    return state


def validate_task(payload: dict[str, Any] | TaskSpec, *, settings: Settings | None = None) -> ValidatedTask:
    spec = TaskSpec.from_dict(payload)  # Reject malformed requests before environment access.
    current = settings if settings is not None else load_settings()
    if spec.optimization_policy is not None:
        current = current.for_policy(spec.optimization_policy)
    problem = bridge.load_problem(current.problem_file)
    if problem.get('workbench_parameter_ids') != ['P8', 'P9', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7'] or problem.get('decimals') != 4:
        raise TaskValidationError('CASE_MAPPING', 'The fixed case requires its existing parameter mapping and four decimals.')
    run_dir = None
    done = 0
    if spec.run_id is not None or spec.operation == 'status':
        run_dir = resolve_run(current, spec.run_id)
        checkpoint = read_checkpoint(run_dir)
        done = checkpoint['nCompleted']
        saved_policy = checkpoint.get('definition', {}).get('optimization_policy')
        if (spec.operation == 'optimize' and spec.optimization_policy is None
                and current.optimization_policy is None and saved_policy is not None):
            current = current.for_policy(saved_policy)
    target = None
    if spec.operation == 'optimize':
        target = (done + spec.additional_candidate_budget if spec.additional_candidate_budget is not None
                  else spec.total_candidate_budget)
        if target is None or target <= done:
            raise TaskValidationError('BUDGET_REACHED', 'Target total must exceed the number of completed candidates.')
        current = replace(current, candidate_count=target)
        if run_dir is not None:
            # Existing compatibility and native-record checks remain authoritative.
            # This call reads files and mutates only its in-memory state, never the saved checkpoint.
            restored = controller.restore_state(current, str(run_dir))
            if restored['nCompleted'] != done:
                raise TaskValidationError('RUN_CHANGED', 'Run changed during validation; read its status and retry.')
    return ValidatedTask(spec, current, problem, run_dir, done, target)
