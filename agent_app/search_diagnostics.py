"""Read-only search and GP diagnostics over one verified historical snapshot."""
from __future__ import annotations

import numpy as np

from python_control import runtime
from python_control.sampling import signature
from python_control.search import validate_search_config
from .result_analysis import analyze_run
from .schemas import TaskSpec, TaskValidationError, ToolResult
from .validation import read_checkpoint, resolve_run


def _repeatability(history, names):
    groups = {}
    for index, point in enumerate(history):
        key = tuple(point['parameters'][name] for name in names)
        groups.setdefault(key, []).append((index, point['stress_mpa']))
    repeats = []
    for points in groups.values():
        if len(points) > 1:
            values = np.asarray([value for _, value in points], dtype=float)
            repeats.append({'iterations': [index for index, _ in points],
                            'stress_range_mpa': float(np.ptp(values)),
                            'stress_std_mpa': float(np.std(values, ddof=1))})
    return {'status': 'observed_repeats' if repeats else 'repeat_measurements_required',
            'exact_design_groups': repeats,
            'reason_zh': '相同尺寸的重复结果仅用于数值波动诊断。' if repeats else
                         '历史记录缺少同一尺寸的重复求解，尚无法据此设定工程停滞阈值。'}


def inspect_search_diagnostics(run_id, *, settings=None):
    """Fit CPU-only comparison models without changing source files or budgets."""
    TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
    current = settings if settings is not None else runtime.load_settings()
    root = resolve_run(current, run_id)
    state = read_checkpoint(root)
    basis = signature(state)
    verified = analyze_run(run_id, settings=current, verify_native=True)
    if not verified.ok:
        return verified
    problem = runtime.bridge.load_problem(root / 'problem.json')
    config = validate_search_config(runtime.read_json(root / 'search_config.json'))
    history = state['history']
    if not history:
        raise TaskValidationError('DIAGNOSTIC_DATA', 'A committed baseline is required for search diagnostics.')
    origins = ['baseline'] + [record.get('proposal', {}).get('selection_origin', 'unknown')
                              for record in state['records']]
    from python_control.local_surrogate import compare_models
    models = compare_models(history, problem, config, origins=origins)
    y = np.asarray([point['stress_mpa'] for point in history], dtype=float)
    best = int(np.argmin(y))
    origin_stats = {}
    for origin in sorted(set(origins[1:])):
        indices = [i for i in range(1, len(history)) if origins[i] == origin]
        origin_stats[origin] = {
            'candidates': len(indices),
            'best_updates': sum(bool(y[i] < np.min(y[:i])) for i in indices),
            'best_stress_mpa': float(np.min(y[indices]))}
    data = {'schema_version': 'disk-agent.search-diagnostics.v1', 'run_id': run_id,
            'data_signature': signature(history), 'saved_selection_policy': config.get('selection_policy', 'legacy_v1'),
            'trajectory': {'completed_candidates': len(history)-1, 'best_iteration': best,
                           'best_stress_mpa': float(y[best]), 'baseline_stress_mpa': float(y[0]),
                           'candidates_since_best': len(history)-1-best, 'origin_statistics': origin_stats},
            'models': models, 'repeatability': _repeatability(history, problem['input_order']),
            'evidence': {'native_records_reverified': True, 'retrospective_diagnostic': True,
                         'api_requests': 0, 'fea_evaluations': 0}}
    if signature(read_checkpoint(root)) != basis:
        raise TaskValidationError('DIAGNOSTIC_BUSY', 'Run changed during diagnostics; inspect a stable snapshot.')
    return ToolResult(True, 'search_diagnostics', 'analyzed', data)
