"""Guarded local GP diagnostics and predictions, always in shadow mode.

Only the read adapter establishes run/native provenance. ``predict_snapshot``
is a pure numerical boundary: its caller supplies one committed run snapshot.
No prediction is a measurement or an engineering qualification.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import numpy as np

from python_control import controller, local_surrogate, runtime, surrogate
from python_control.sampling import normalize, signature
from python_control.search import DEFAULT_CONFIG, load_search_config

from .schemas import TaskSpec, TaskValidationError
from .validation import read_checkpoint, resolve_run


PERFORMANCE_SCHEMA = 'disk-agent.performance.v1'
MODEL_VERSION = 'numpy-rbf-gp.shadow.v1'
MINIMUM_SAMPLES = 24
_NAMES = ['X1', 'X2', 'X3', 'X4', 'X5', 'Y1', 'Y2', 'Y3', 'Y4']
_MAPPING = ['P8', 'P9', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']
_OBJECTIVE = {'name': 'maximum_von_mises_stress', 'unit': 'MPa', 'direction': 'minimize'}
_HISTORY_FIELDS = {'parameters', 'stress_mpa', 'displacement_mm', 'area_m2'}
_LIMITATIONS = [
    '仅预测最大等效应力；不预测位移、质量、强度合格性或寿命。',
    'GP标准差未经覆盖率校准，不是95%可靠区间。',
    '样本数门槛与交叉验证通过不保证九维空间预测充分可靠。',
    '仅作影子记录，不改变候选选择；预测不得进入实测history或充当Workbench结果。',
    '公式近似截面积不等于质量；没有预先预测记录的历史不事后回填预测。',
]
# File reads happen once while importing code, never inside the pure predictor.
_CODE_SIGNATURE = signature({name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    for name, path in (('predictor', __file__), ('surrogate', surrogate.__file__),
                       ('local_surrogate', local_surrogate.__file__))})


def _fail(message, code='PREDICTION_INPUT'):
    raise TaskValidationError(code, message)


def _finite(value, name, *, positive=False, nonnegative=False):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid or positive and value <= 0 or nonnegative and value < 0:
        _fail(name + ' requires a finite numeric value with a valid sign, not a boolean.')
    return value


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        _fail(name + ' must be an integer >= ' + str(minimum) + ', not a boolean.')
    return value


def _native_json(value, depth=0):
    if depth > 40:
        _fail('Input JSON nesting exceeds the supported depth.')
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        return _finite(value, 'JSON number')
    if type(value) is list:
        return [_native_json(v, depth + 1) for v in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: _native_json(v, depth + 1) for key, v in value.items()}
    _fail('Inputs must contain only native finite JSON values.')


def _problem(problem):
    p = _native_json(problem)
    if (type(p) is not dict or p.get('schema_version') != 'wb2d-llm-problem.v1' or
            p.get('input_order') != _NAMES or p.get('workbench_parameter_ids') != _MAPPING or
            type(p.get('decimals')) is not int or p['decimals'] != 4 or p.get('objective') != _OBJECTIVE):
        _fail('Prediction requires the canonical nine-parameter mapping, four decimals and stress objective.', 'PREDICTION_CASE')
    variables = p.get('variables')
    if type(variables) is not list or len(variables) != 9:
        _fail('Exactly nine variable definitions are required.', 'PREDICTION_CASE')
    for i, v in enumerate(variables):
        if (type(v) is not dict or v.get('name') != _NAMES[i] or
                v.get('workbench_id') != _MAPPING[i] or v.get('source_cad_unit') != 'mm'):
            _fail('Variable order, Workbench mapping and mm numeric convention must match.', 'PREDICTION_CASE')
        lo = _finite(v.get('lower'), 'lower bound')
        hi = _finite(v.get('upper'), 'upper bound')
        ref = _finite(v.get('reference'), 'reference')
        if not lo < hi or not lo <= ref <= hi or not math.isfinite(hi - lo):
            _fail('Invalid variable bounds or reference.', 'PREDICTION_CASE')
    _finite(p.get('min_normalized_distance'), 'duplicate tolerance', nonnegative=True)
    return p


def _config(cfg):
    c = _native_json(cfg)
    required = set(DEFAULT_CONFIG)
    if type(c) is not dict or set(c) not in (required, required | {'selection_policy'}):
        _fail('Prediction requires the complete existing search configuration.')
    if c.get('selection_policy', 'legacy_v1') not in ('legacy_v1', 'hybrid_local_v2', 'hybrid_local_v3'):
        _fail('Unsupported selection policy.')
    if c['strategy'] not in ('direct', 'center_only', 'center_surrogate'):
        _fail('Unsupported search strategy.')
    for key in ('seed', 'initial_lhs', 'exploration_slots'):
        _integer(c[key], key)
    for key in ('batch_size', 'pool_size', 'surrogate_min_samples', 'surrogate_max_samples', 'surrogate_folds'):
        _integer(c[key], key, 1)
    if (c['surrogate_min_samples'] < MINIMUM_SAMPLES or
            not c['surrogate_max_samples'] >= c['surrogate_min_samples'] >= c['surrogate_folds'] + 4 or
            c['surrogate_folds'] < 2 or
            not c['pool_size'] >= c['batch_size'] > c['exploration_slots']):
        _fail('Surrogate sample minimum must remain at least 24; sample/fold/batch settings must be valid.')
    for key in ('sigma_start', 'sigma_min', 'sigma_decay', 'global_fraction',
                'surrogate_max_cv_nrmse', 'surrogate_max_rmse_mpa',
                'surrogate_uncertainty_weight', 'surrogate_max_distance'):
        _finite(c[key], key, nonnegative=True)
    if (not 0 < c['sigma_min'] <= c['sigma_start'] <= 1 or not 0 < c['sigma_decay'] <= 1 or
            not 0 <= c['global_fraction'] <= 1 or
            any(c[k] <= 0 for k in ('surrogate_max_cv_nrmse', 'surrogate_max_rmse_mpa',
                                   'surrogate_uncertainty_weight', 'surrogate_max_distance'))):
        _fail('Invalid surrogate error/distance/sampling thresholds.')
    lengths = c['surrogate_length_scales']
    if type(lengths) is not list or not lengths:
        _fail('Surrogate length scales must be a nonempty numeric list.')
    for length in lengths:
        _finite(length, 'length scale', positive=True)
    return c


def _x(values, problem):
    if type(values) is not list or len(values) != 9:
        _fail('A design must be a nine-number list in the canonical order.')
    for value in values:
        _finite(value, 'design parameter')
    canonical = runtime.bridge.ordered_values(dict(zip(_NAMES, values)), problem)
    if canonical != values:
        _fail('Design values must already have four-decimal precision; no quantization or clipping is performed.')
    return list(values)


def _history(history, problem):
    h = _native_json(history)
    if type(h) is not list:
        _fail('History must be a list of committed observations from one run.')
    xs = []
    for point in h:
        if (type(point) is not dict or not _HISTORY_FIELDS.issubset(point)
                or set(point) - _HISTORY_FIELDS - {'mass_kg', 'frequency_hz', 'modal_frequencies_hz'}):
            _fail('Training observations must be canonical committed measurements, without prediction fields.')
        for metric in ('mass_kg', 'frequency_hz'):
            if metric in point:
                _finite(point[metric], metric, positive=True)
        if 'modal_frequencies_hz' in point:
            frequencies = point['modal_frequencies_hz']
            if type(frequencies) is not list or len(frequencies) != 6:
                _fail('A recorded modal vector must contain six measured frequencies.')
            for value in frequencies:
                _finite(value, 'measured modal frequency', positive=True)
        parameters = point['parameters']
        if type(parameters) is not dict or set(parameters) != set(_NAMES):
            _fail('Each observation requires exactly nine named parameters.')
        xs.append(_x([parameters[name] for name in _NAMES], problem))
        _finite(point['stress_mpa'], 'measured stress', positive=True)
        _finite(point['displacement_mm'], 'measured displacement', nonnegative=True)
        _finite(point['area_m2'], 'measured approximate section area', positive=True)
    unique = []
    threshold = problem['min_normalized_distance'] ** 2
    for x in xs:
        if all(runtime.bridge.normalized_distance2(x, old, problem) > threshold for old in unique):
            unique.append(x)
    if len(unique) != len(xs):
        _fail('Duplicate measured designs must be resolved before GP fitting; they cannot increase sample count.', 'PREDICTION_DUPLICATE')
    return h, xs


def _base(history, problem, cfg):
    return {'schema_version': PERFORMANCE_SCHEMA, 'status': 'insufficient_data',
        'reason': 'insufficient_measured_samples', 'measured_samples': len(history),
        'unique_samples': len(history), 'minimum_samples': cfg['surrogate_min_samples'],
        'validation': {'enabled': False, 'measured_samples': len(history),
                       'predictions_are_measurements': False},
        'model_version': MODEL_VERSION, 'model_scope': 'global', 'model_code_signature': _CODE_SIGNATURE,
        'data_signature': signature(history), 'problem_signature': signature(problem),
        'config_signature': signature(cfg), 'role': 'shadow_only',
        'predictions': [], 'limitations': list(_LIMITATIONS)}


def _local_support_mask(z, report, history, problem, cfg):
    """Reconstruct the saved local support from measured coordinates.

    The model's boolean mask is checked against this descriptor. The same
    reconstruction verifies immutable before-solve audits without refitting.
    """
    support = report.get('support')
    if type(support) is not dict or support.get('geometry') != 'linf_box':
        raise ValueError('A local model requires a reproducible support box.')
    anchor_index = support.get('anchor_training_history_index_0based')
    if type(anchor_index) is not int or not 0 <= anchor_index < len(history):
        raise ValueError('Invalid measured anchor index.')
    measured = normalize([[point['parameters'][name] for name in problem['input_order']]
                          for point in history], problem)
    anchor = measured[anchor_index]
    radius = support.get('radius_fraction')
    maximum = support.get('maximum_nearest_distance')
    if (type(radius) not in (int, float) or not math.isfinite(radius) or
            radius != cfg['sigma_start'] or type(maximum) not in (int, float) or
            not math.isfinite(maximum) or maximum != min(cfg['surrogate_max_distance'],
                                                        radius * math.sqrt(len(anchor)))):
        raise ValueError('Local support thresholds differ from the saved configuration.')
    lower, upper = np.maximum(anchor - radius, 0.0), np.minimum(anchor + radius, 1.0)
    for key, expected in (('anchor_normalized', anchor), ('lower_normalized', lower),
                          ('upper_normalized', upper)):
        values = support.get(key)
        if (type(values) is not list or len(values) != len(anchor) or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in values) or
                not np.allclose(values, expected, rtol=0, atol=1e-12)):
            raise ValueError('Local support coordinates do not match measured evidence.')
    indices = report.get('final_training_history_indices_0based')
    if (type(indices) is not list or len(indices) < cfg['surrogate_min_samples'] or
            any(type(index) is not int or not 0 <= index < len(history) for index in indices) or
            indices != sorted(set(indices))):
        raise ValueError('Local support requires the actual final training indices.')
    sampled = np.unique(np.linspace(0, len(history) - 1,
                                   min(len(history), cfg['surrogate_max_samples']), dtype=int))
    inside_measured = np.all((measured >= lower - 1e-12) & (measured <= upper + 1e-12), axis=1)
    if indices != sampled[inside_measured[sampled]].tolist():
        raise ValueError('Final local training indices do not match the saved support.')
    nearest = np.sqrt(((z[:, None, :] - measured[indices][None, :, :]) ** 2).sum(axis=2).min(axis=1))
    return np.all((z >= lower - 1e-12) & (z <= upper + 1e-12), axis=1) & (nearest <= maximum)


def predict_snapshot(history, problem, cfg, candidates=None) -> dict:
    """Validate one committed snapshot and fit/predict locally; never changes it.

    Malformed input raises ``TaskValidationError``. Numerical/validation failures
    return no prediction values, and callers must keep the original solver path.
    Candidate designs are new canonical vectors, never historical labels.
    """
    p, c = _problem(problem), _config(cfg)
    h, xs = _history(history, p)
    if candidates is None:
        candidates = []
    values = _native_json(candidates)
    if type(values) is not list:
        _fail('candidates must be a list of nine-number designs.')
    requested = []
    threshold = p['min_normalized_distance'] ** 2
    for value in values:
        x = _x(value, p)
        if any(runtime.bridge.normalized_distance2(x, old, p) <= threshold for old in xs + requested):
            _fail('Prediction candidates must be distinct from history and each other.', 'PREDICTION_DUPLICATE')
        requested.append(x)
    result = _base(h, p, c)
    local = c.get('selection_policy') == 'hybrid_local_v3'
    if local:
        result.update(model_version=local_surrogate.MODEL_VERSION, model_scope='local')
        result['limitations'][1] = '标准差经训练期滚动OOF残差膨胀及下限校准，覆盖率未经独立认证。'
    if not h:
        result.update(status='no_baseline', reason='no_committed_baseline')
        return result
    if len(h) < c['surrogate_min_samples']:
        result['validation']['reason'] = result['reason']
        return result
    try:
        with np.errstate(all='raise'):
            fit = local_surrogate.fit_local_validated if local else surrogate.fit_validated
            model, report = fit(h, p, c)
        report = _native_json(report)
        if type(report) is not dict or type(report.get('enabled')) is not bool:
            raise ValueError('Invalid GP validation report.')
        result['validation'] = report
        if model is None or report['enabled'] is not True:
            result.update(status='validation_failed', reason=report.get('reason', 'validation_unavailable'))
            return result
        if requested:
            z = normalize(requested, p)
            with np.errstate(all='raise'):
                mean, std = model.predict(z)
                distance = np.sqrt(((z[:, None, :] - normalize(xs, p)[None, :, :]) ** 2).sum(axis=2).min(axis=1))
            if len(mean) != len(requested) or len(std) != len(requested):
                raise ValueError('Model returned mismatched predictions.')
            if local:
                support_mask = np.asarray(model.support_mask(z))
                verified_mask = _local_support_mask(z, report, h, p, c)
                if (support_mask.dtype != np.dtype(bool) or support_mask.shape != (len(requested),) or
                        not np.array_equal(support_mask, verified_mask)):
                    raise ValueError('Model support mask disagrees with its saved training region.')
            predictions = []
            for i, x in enumerate(requested):
                mu, sigma, dist = float(mean[i]), float(std[i]), float(distance[i])
                _finite(mu, 'predicted stress', positive=True)
                _finite(sigma, 'predictive standard deviation', nonnegative=True)
                _finite(dist, 'distance to measurements', nonnegative=True)
                predictions.append({'x': x, 'predicted_stress_mpa': mu,
                    'predictive_std_mpa': sigma, 'distance_to_measurements': dist,
                    'within_training_neighborhood': dist <= c['surrogate_max_distance']})
                if local:
                    predictions[-1].update(within_validated_region=bool(support_mask[i]),
                                           eligible_for_ranking=bool(support_mask[i]))
            result['predictions'] = predictions
        result.update(status='ready', reason=report.get('reason', 'rolling_cv_and_forward_holdout_passed')
                      if local else 'cross_validation_and_forward_holdout_passed')
    except Exception as exc:
        result.update(status='validation_failed', reason='numerical_or_prediction_unavailable',
            predictions=[], validation={'enabled': False, 'measured_samples': len(h),
                'predictions_are_measurements': False, 'reason': 'numerical_or_prediction_unavailable',
                'error_type': type(exc).__name__})
    return result


def _case_basis(problem):
    return {key: problem[key] for key in ('schema_version', 'input_order',
        'workbench_parameter_ids', 'decimals', 'min_normalized_distance', 'objective')} | {
        'variables': [{key: item[key] for key in ('name', 'workbench_id', 'source_cad_unit',
            'lower', 'upper', 'reference')} for item in problem['variables']]}


def _inside(path, root):
    return Path(path).resolve().is_relative_to(root.resolve())


def _run_files(root):
    """Reject redirected inputs even when a missing config would use fallback."""
    files = {name: root / name for name in ('checkpoint.json', 'problem.json', 'search_config.json')}
    if any(path.resolve() != path for path in files.values()):
        _fail('Run checkpoint, problem and search configuration must not redirect through links.', 'PREDICTION_PATH')
    return files


def inspect_run(run_id, *, settings=None, include_records=True) -> dict:
    """Read native committed evidence and immutable pre-solve audit records.

    Historical runs without pre-solve records retain null prediction/error
    values. This function never saves or backfills a prediction record.
    """
    TaskSpec.from_dict({'operation': 'status', 'run_id': run_id})
    if type(include_records) is not bool:
        _fail('include_records must be a boolean.')
    root = None
    result = {'schema_version': PERFORMANCE_SCHEMA, 'status': 'unavailable',
        'reason': 'run_snapshot_unavailable', 'measured_samples': None,
        'unique_samples': None, 'minimum_samples': MINIMUM_SAMPLES, 'validation': {},
        'model_version': MODEL_VERSION, 'model_code_signature': _CODE_SIGNATURE,
        'data_signature': None, 'problem_signature': None, 'config_signature': None,
        'role': 'shadow_only', 'predictions': [], 'limitations': list(_LIMITATIONS),
        'run_id': run_id, 'source_history_signature': None,
        'source_completed_candidates': None, 'records': [], 'record_summary': {}, 'evidence': {}}
    try:
        current = settings if settings is not None else runtime.load_settings()
        root = resolve_run(current, run_id)
        files = _run_files(root)
        initial = files['checkpoint.json'].read_bytes()
        checkpoint_hash = hashlib.sha256(initial).hexdigest()
        state = read_checkpoint(root)
        if state.get('definition') != current.definition() or state.get('fingerprint') != runtime.fingerprint(current):
            _fail('Run engineering settings or original controller fingerprint differ from the current settings.', 'PREDICTION_COMPATIBILITY')
        problem = _problem(runtime.bridge.load_problem(files['problem.json']))
        current_problem = _problem(runtime.bridge.load_problem(current.problem_file))
        if _case_basis(problem) != _case_basis(current_problem):
            _fail('Run and current case mappings, bounds, units or objective differ.', 'PREDICTION_COMPATIBILITY')
        cfg = runtime.read_json(files['search_config.json']) if files['search_config.json'].exists() else load_search_config(current.root)
        cfg = _config(cfg)
        _history(state['history'], problem)
        details = ([state['baseline']] if state['baseline'] is not None else []) + [record.get('detail') for record in state['records']]
        for i, (point, detail) in enumerate(zip(state['history'], details)):
            if type(detail) is not dict or controller.observation(detail) != point:
                _fail('Committed observation and evaluation detail disagree.', 'PREDICTION_EVIDENCE')
            if i:
                proposal = state['records'][i - 1].get('proposal')
                expected_x = [point['parameters'][name] for name in _NAMES]
                if type(proposal) is not dict or proposal.get('x') != expected_x:
                    _fail('Committed proposal geometry and actual measured design disagree.', 'PREDICTION_EVIDENCE')
            for key in ('nativeReportFile', 'runDir'):
                if type(detail.get(key)) is not str or not _inside(detail[key], root):
                    _fail('Native evidence belongs outside the requested run.', 'PREDICTION_EVIDENCE')
            if Path(detail['nativeReportFile']).resolve().parent != Path(detail['runDir']).resolve():
                _fail('Native report does not belong to its evaluation directory.', 'PREDICTION_EVIDENCE')
            controller.verify_native(detail, state['baseline'] if i else None)
        result = predict_snapshot(state['history'], problem, cfg)
        result.update(run_id=root.name, source_history_signature=signature(state['history']),
            source_completed_candidates=state['nCompleted'], records=[], record_summary={},
            evidence={'basis': 'committed_controller_checkpoint',
                'checkpoint_file': str(root / 'checkpoint.json'), 'checkpoint_sha256': checkpoint_hash,
                'prediction_audit_directory': str(root / 'prediction_audit'),
                'native_records_reverified': bool(details), 'native_records_checked': len(details),
                'physical_accuracy_certified': False})
        if include_records:
            from .prediction_audit import read_prediction_records
            records = read_prediction_records(root, state, problem)
            result['records'] = records['records']
            result['record_summary'] = records['summary']
        _run_files(root)
        if hashlib.sha256(files['checkpoint.json'].read_bytes()).hexdigest() != checkpoint_hash:
            _fail('Checkpoint changed during inspection; retry a stable snapshot.', 'PREDICTION_BUSY')
        return result
    except Exception as exc:
        # Never expose partially fitted predictions or joined rows after a failed
        # provenance check. Original sampling and FEA continue independently.
        result.update(status='unavailable', reason=str(getattr(exc, 'code', 'run_snapshot_unavailable')),
            predictions=[], records=[], record_summary={},
            error={'code': str(getattr(exc, 'code', type(exc).__name__)), 'message': runtime.safe_text(exc)})
        if root is not None:
            result['run_id'] = root.name
        return result
