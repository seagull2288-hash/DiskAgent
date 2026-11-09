"""Immutable before-solve shadow predictions, joined to committed results read-only.

These records never influence the original sampler or enter measured history. A
missing historical prediction stays missing: inspection cannot create a record.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

from python_control import controller, runtime
from python_control.sampling import normalize
from python_control.search import load_search_config
from python_control.workbench import canonical_x

from .performance_predictor import _local_support_mask, predict_snapshot
from .schemas import TaskValidationError
from .validation import read_checkpoint, resolve_run

SCHEMA = 'disk-agent.prediction-record.v1'
_NAME = re.compile(r'iter_([0-9]{3,9})\.json')
_STATES = {'no_baseline', 'insufficient_data', 'validation_failed', 'ready'}


def _fail(message):
    raise TaskValidationError('PREDICTION_AUDIT', message)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _directory(run_dir):
    run_dir = Path(run_dir).resolve()
    folder = run_dir / 'prediction_audit'
    if folder.resolve() != folder:
        _fail('Prediction audit directory cannot redirect outside its run.')
    return folder


def _file(run_dir, iteration):
    path = _directory(run_dir) / ('iter_%03d.json' % iteration)
    if path.resolve() != path:
        _fail('Prediction record cannot redirect outside its audit directory.')
    return path


def _config(run_dir, settings):
    path = Path(run_dir) / 'search_config.json'
    if path.resolve() != path:
        _fail('Search configuration cannot redirect outside its run.')
    if path.is_file():
        # predict_snapshot validates the complete saved configuration before fit.
        return runtime.read_json(path)
    return load_search_config(settings.root)


def _load(path):
    if path.stat().st_size > 2_000_000:
        _fail('Prediction audit record exceeds its size limit.')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail('Duplicate prediction record JSON key.')
            value[key] = item
        return value

    try:
        return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda _: _fail('Nonfinite prediction record.'))
    except (UnicodeError, ValueError) as exc:
        _fail('Cannot read prediction record: ' + str(exc))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _identity(value):
    return 'pred_' + _digest({key: value[key] for key in (
        'run_id', 'iteration', 'candidate_x', 'basis_history_signature', 'proposal_signature')})[:24]


def _validate(value, run_dir, state, problem, config=None):
    fields = {'schema_version', 'record_id', 'run_id', 'iteration', 'candidate_x',
              'basis_completed_candidates', 'basis_history_length', 'basis_history_signature',
              'proposal_signature', 'problem_signature', 'config_signature', 'search_config',
              'created_at', 'prediction', 'record_signature'}
    if not isinstance(value, dict) or set(value) != fields or value['schema_version'] != SCHEMA:
        _fail('Unsupported prediction record schema or fields.')
    try:
        signed = {key: item for key, item in value.items() if key != 'record_signature'}
        if value['record_signature'] != _digest(signed) or value['record_id'] != _identity(value):
            _fail('Prediction record integrity check failed.')
    except (TypeError, ValueError):
        _fail('Prediction record is not finite JSON.')
    k = value['iteration']
    if type(k) is not int or k < 1 or value['run_id'] != Path(run_dir).name:
        _fail('Prediction record belongs to another run or candidate.')
    if (type(value['basis_completed_candidates']) is not int or
            value['basis_completed_candidates'] != k - 1 or
            type(value['basis_history_length']) is not int or value['basis_history_length'] != k):
        _fail('Prediction source count does not precede its candidate.')
    if len(state['history']) < k or value['basis_history_signature'] != _digest(state['history'][:k]):
        _fail('Prediction source history does not match the committed prefix.')
    if value['problem_signature'] != _digest(problem):
        _fail('Prediction belongs to a different engineering problem.')
    if value['config_signature'] != _digest(value['search_config']):
        _fail('Prediction search configuration signature is invalid.')
    if config is not None and value['config_signature'] != _digest(config):
        _fail('Prediction search configuration changed before evaluation.')
    if k <= state['nCompleted']:
        record = state['records'][k - 1]
        if record.get('iteration') != k:
            _fail('Committed candidate numbering is inconsistent.')
        proposal = record.get('proposal')
        measured_x = runtime.bridge.ordered_values(state['history'][k]['parameters'], problem)
        if (not isinstance(record.get('detail'), dict) or
                record['detail'].get('xActual') != measured_x or
                not isinstance(proposal, dict) or proposal.get('x') != measured_x):
            _fail('Committed geometry does not match the predicted candidate.')
    elif k == state['nCompleted'] + 1 and isinstance(state.get('pending'), dict):
        if state['pending'].get('iteration') != k:
            _fail('Pending candidate numbering is inconsistent.')
        proposal = state['pending'].get('proposal')
    else:
        _fail('Prediction has no matching committed or pending candidate.')
    if not isinstance(proposal, dict) or value['proposal_signature'] != _digest(proposal):
        _fail('Prediction proposal does not match its candidate.')
    x = value['candidate_x']
    if (not isinstance(x, list) or len(x) != 9 or
            any(not _number(item) for item in x) or
            x != canonical_x(x, problem) or x != proposal.get('x')):
        _fail('Prediction geometry does not match its candidate.')
    try:
        timestamp = datetime.fromisoformat(value['created_at'])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            _fail('Prediction timestamp requires a timezone.')
    except (TypeError, ValueError):
        _fail('Invalid prediction timestamp.')
    prediction = value['prediction']
    if (not isinstance(prediction, dict) or
            prediction.get('schema_version') != 'disk-agent.performance.v1' or
            prediction.get('role') != 'shadow_only' or prediction.get('status') not in _STATES or
            type(prediction.get('measured_samples')) is not int or prediction['measured_samples'] != k):
        _fail('Invalid shadow model provenance.')
    if (prediction.get('data_signature') != value['basis_history_signature'] or
            prediction.get('problem_signature') != value['problem_signature'] or
            prediction.get('config_signature') != value['config_signature']):
        _fail('Shadow model signatures do not match its recorded inputs.')
    if (type(prediction.get('unique_samples')) is not int or prediction['unique_samples'] != k or
            type(prediction.get('minimum_samples')) is not int or prediction['minimum_samples'] < 24 or
            prediction['minimum_samples'] != value['search_config'].get('surrogate_min_samples') or
            not isinstance(prediction.get('model_version'), str) or not prediction['model_version'] or
            len(prediction['model_version']) > 120 or
            not isinstance(prediction.get('model_code_signature'), str) or
            re.fullmatch(r'[0-9a-f]{64}', prediction['model_code_signature']) is None):
        _fail('Invalid model version or measured-data gate in prediction provenance.')
    results = prediction.get('predictions')
    if not isinstance(results, list):
        _fail('Invalid shadow prediction list.')
    if prediction['status'] == 'ready':
        if (k < prediction['minimum_samples'] or
                not isinstance(prediction.get('validation'), dict) or
                prediction['validation'].get('enabled') is not True):
            _fail('A ready prediction requires sufficient measured data and passed validation.')
        if len(results) != 1 or not isinstance(results[0], dict) or results[0].get('x') != x:
            _fail('Ready prediction must name exactly its evaluated candidate.')
        point = results[0]
        if (not _number(point.get('predicted_stress_mpa')) or
                not _number(point.get('predictive_std_mpa')) or point['predictive_std_mpa'] < 0 or
                not _number(point.get('distance_to_measurements')) or point['distance_to_measurements'] < 0):
            _fail('Prediction values must be finite, with nonnegative uncertainty and distance.')
        within = point.get('within_training_neighborhood')
        if (type(within) is not bool or
                within != (point['distance_to_measurements'] <= value['search_config']['surrogate_max_distance'])):
            _fail('Prediction neighborhood flag does not match the model distance.')
        if value['search_config'].get('selection_policy') == 'hybrid_local_v3':
            try:
                expected = bool(_local_support_mask(normalize([x], problem), prediction['validation'],
                                                    state['history'][:k], problem, value['search_config'])[0])
            except (ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
                _fail('Cannot verify the saved local prediction region: ' + str(exc))
            if (type(point.get('within_validated_region')) is not bool or
                    type(point.get('eligible_for_ranking')) is not bool or
                    point['within_validated_region'] != expected or point['eligible_for_ranking'] != expected):
                _fail('Local ranking eligibility disagrees with the saved measured support.')
    elif results:
        _fail('Unavailable models cannot supply numeric predictions.')
    return value


def record_before_solve(run_dir, x, problem, *, settings):
    """Persist or reuse one immutable record before entering candidate FEA.

    Model unavailability is a normal state; an invalid/missing audit write is an
    execution error. The latter stops the new FEA rather than inventing evidence.
    """
    run_dir = Path(run_dir).resolve()
    if resolve_run(settings, run_dir.name) != run_dir:
        _fail('Prediction source must be the evaluator-owned run.')
    state = read_checkpoint(run_dir)
    pending = state.get('pending')
    if (state['baseline'] is None or not isinstance(pending, dict) or
            pending.get('iteration') != state['nCompleted'] + 1 or pending.get('detail') is not None or
            not isinstance(pending.get('proposal'), dict)):
        _fail('Only an unevaluated pending candidate can receive a before-solve record.')
    if x != pending['proposal'].get('x') or x != canonical_x(x, problem):
        _fail('Evaluator geometry does not match its pending candidate.')
    saved_problem = runtime.bridge.load_problem(run_dir / 'problem.json')
    if _digest(problem) != _digest(saved_problem):
        _fail('Evaluator problem does not match its run.')
    config = _config(run_dir, settings)
    k = pending['iteration']
    path = _file(run_dir, k)
    if path.exists():
        return _validate(_load(path), run_dir, state, problem, config)
    checkpoint_signature = hashlib.sha256((run_dir / 'checkpoint.json').read_bytes()).hexdigest()
    prediction = predict_snapshot(state['history'], problem, config, candidates=[x])
    value = dict(schema_version=SCHEMA, run_id=run_dir.name, iteration=k,
                 candidate_x=list(x), basis_completed_candidates=state['nCompleted'],
                 basis_history_length=len(state['history']), basis_history_signature=_digest(state['history']),
                 proposal_signature=_digest(pending['proposal']), problem_signature=_digest(problem),
                 config_signature=_digest(config), search_config=config,
                 created_at=datetime.now().astimezone().isoformat(timespec='microseconds'),
                 prediction=prediction)
    value['record_id'] = _identity(value)
    value['record_signature'] = _digest(value)
    _validate(value, run_dir, state, problem, config)
    if hashlib.sha256((run_dir / 'checkpoint.json').read_bytes()).hexdigest() != checkpoint_signature:
        _fail('Run advanced while the before-solve prediction was being prepared.')
    folder = _directory(run_dir)
    folder.mkdir(exist_ok=True)
    if path.exists():
        # The controller's project lock normally prevents this; never overwrite.
        return _validate(_load(path), run_dir, state, problem, config)
    runtime.write_json(path, value)
    return _validate(_load(path), run_dir, state, problem, config)


def read_prediction_records(run_dir, state, problem):
    """Join immutable forecasts to committed measurements without any writes."""
    run_dir = Path(run_dir).resolve()
    folder = _directory(run_dir)
    saved = {}
    if folder.is_dir():
        for path in sorted(folder.glob('iter_*.json')):
            matched = _NAME.fullmatch(path.name)
            if matched is None or path.resolve() != path:
                _fail('Invalid prediction record filename or redirected path.')
            record = _validate(_load(path), run_dir, state, problem)
            k = record['iteration']
            if k != int(matched[1]) or k in saved:
                _fail('Prediction filename and candidate numbering disagree.')
            saved[k] = (record, path)
    records, errors = [], []
    pending = state.get('pending')
    last = state['nCompleted'] + int(isinstance(pending, dict) and isinstance(pending.get('proposal'), dict))
    for k in range(1, last + 1):
        actual = None
        if k <= state['nCompleted']:
            original = state['records'][k - 1]
            if controller.observation(original['detail']) != state['history'][k]:
                _fail('Committed measurement and candidate record disagree.')
            actual = state['history'][k]['stress_mpa']
            if not _number(actual) or actual <= 0:
                _fail('Committed actual stress must be finite and positive.')
            x = original['proposal']['x']
            if x != runtime.bridge.ordered_values(state['history'][k]['parameters'], problem):
                _fail('Committed proposal geometry and measurement disagree.')
        else:
            x = pending['proposal']['x']
        item = dict(iteration=k, candidate_x=x, created_at=None, record_id=None, record_file=None,
                    basis_completed_candidates=None, prediction_status=None,
                    status='missing_pre_prediction', predicted_stress_mpa=None,
                    predictive_std_mpa=None, actual_stress_mpa=actual, error_mpa=None,
                    absolute_error_mpa=None, relative_error_percent=None,
                    within_validated_region=None, eligible_for_ranking=None,
                    model_scope=None, validation=None, uncertainty_calibration=None)
        if k in saved:
            value, path = saved[k]
            prediction = value['prediction']
            item.update(created_at=value['created_at'], record_id=value['record_id'], record_file=str(path),
                        basis_completed_candidates=value['basis_completed_candidates'],
                        prediction_status=prediction['status'], reason=prediction.get('reason'),
                        model_version=prediction.get('model_version'),
                        model_code_signature=prediction.get('model_code_signature'),
                        model_scope=prediction.get('model_scope'), validation=prediction.get('validation'),
                        uncertainty_calibration=(prediction['validation'].get('uncertainty_calibration')
                            if isinstance(prediction.get('validation'), dict) else None),
                        data_signature=prediction.get('data_signature'),
                        basis_history_signature=value['basis_history_signature'])
            if prediction['status'] == 'ready':
                point = prediction['predictions'][0]
                item.update(predicted_stress_mpa=point['predicted_stress_mpa'],
                            predictive_std_mpa=point['predictive_std_mpa'],
                            distance_to_measurements=point['distance_to_measurements'],
                            within_training_neighborhood=point.get('within_training_neighborhood'),
                            within_validated_region=point.get('within_validated_region'),
                            eligible_for_ranking=point.get('eligible_for_ranking'))
                if actual is not None:
                    error = actual - point['predicted_stress_mpa']
                    item.update(status='compared', error_mpa=error, absolute_error_mpa=abs(error),
                                relative_error_percent=abs(error) / actual * 100)
                    errors.append(error)
                else:
                    item['status'] = 'awaiting_commit' if pending.get('detail') is not None else 'pending'
            else:
                item['status'] = 'not_ready'
        records.append(item)
    return dict(records=records, summary={
        'pre_evaluation_prediction_records': len(saved),
        'numeric_prediction_records': sum(item['predicted_stress_mpa'] is not None for item in records),
        'compared_records': len(errors),
        'missing_pre_prediction_count': sum(item['status'] == 'missing_pre_prediction' for item in records),
        'mean_absolute_error_mpa': sum(abs(e) for e in errors) / len(errors) if errors else None,
        'root_mean_squared_error_mpa': math.sqrt(sum(e * e for e in errors) / len(errors)) if errors else None,
        'max_absolute_error_mpa': max(map(abs, errors)) if errors else None,
        'note': ('仅比较求解前已保存的影子预测与控制器已提交有限元结果；缺失历史预测不回填。'
                 '误差为实测减预测；各记录保留当时校准信息，覆盖率未经独立认证。'),
    })
