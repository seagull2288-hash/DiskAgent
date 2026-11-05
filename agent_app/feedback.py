"""Pure, bounded feedback from committed measurements; never executes a tool.

The digest detects accidental changes, not a malicious author able to recompute
it.  The caller must independently match the run and history signature before
using persisted feedback for another request.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import re

from .schemas import OBJECTIVE, TaskValidationError


FEEDBACK_SCHEMA = 'disk-agent.batch-feedback.v1'
MAX_FEEDBACK_CHARS = 14000
MAX_PROMPT_CHARS = 6500
_ID = re.compile(r'(run|wf)_[A-Za-z0-9][A-Za-z0-9_-]{0,180}')
_SIGNATURE = re.compile(r'[0-9a-f]{64}')
_FEEDBACK_ID = re.compile(r'fdbk_[0-9a-f]{24}')
_RULES = frozenset(('nine variable bounds', 'four decimal numerical quantization',
                   'normalized-space duplicate exclusion'))
_UNKNOWN_CONSTRAINTS = frozenset(('allowable stress', 'maximum displacement',
    'mass bound', 'area bound', 'constant outer radius', 'manufacturing rules',
    'life', 'fatigue life', 'creep life'))
_MEASURED = ('iteration', 'stress_mpa', 'displacement_mm', 'approximate_area_m2')
_CHANGES = ('stress_reduction_from_baseline_percent',
            'displacement_change_from_baseline_percent',
            'approximate_area_change_from_baseline_percent')
_TOP = frozenset(('schema_version', 'feedback_id', 'workflow_id', 'generation',
    'run_id', 'objective', 'source_history_signature', 'source_completed_candidates',
    'source_window', 'baseline', 'best', 'current', 'recent_measured',
    'implemented_rules', 'unknown_engineering_constraints', 'engineering_qualification',
    'causal_attribution', 'hotspot_attribution', 'budget', 'program_advice', 'evidence'))


def _error(message, code='FEEDBACK_VALIDATION'):
    raise TaskValidationError(code, message)


def _json(value, active=None, depth=0):
    """Reject coercion, nonfinite numbers, cycles, and non-JSON input anywhere."""
    if depth > 64:
        _error('Feedback JSON exceeds the maximum nesting depth.')
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _error('Feedback input must contain only finite numbers.')
        return value
    if type(value) not in (dict, list):
        _error('Feedback input must contain native JSON values only.')
    active = set() if active is None else active
    if id(value) in active:
        _error('Feedback JSON contains a circular reference.')
    active.add(id(value))
    try:
        if type(value) is list:
            return [_json(item, active, depth + 1) for item in value]
        if any(type(key) is not str for key in value):
            _error('Feedback JSON keys must be strings.')
        return {key: _json(item, active, depth + 1) for key, item in value.items()}
    finally:
        active.remove(id(value))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _keys(value, allowed, name):
    if type(value) is not dict or set(value) != set(allowed):
        _error(name + ' must contain exactly its supported fields.')


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        _error(name + ' must be an integer >= ' + str(minimum) + ', not a boolean.')
    return value


def _number(value, name, *, positive=False, nonnegative=False):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        _error(name + ' must be a finite number, not a boolean.')
    if positive and value <= 0 or nonnegative and value < 0:
        _error(name + ' has an invalid sign.')
    return value


def _identifier(value, prefix):
    if type(value) is not str or not _ID.fullmatch(value) or not value.startswith(prefix + '_'):
        _error(prefix + ' identifier must be a directory name, not a path.')
    return value


def _close(value, expected, name):
    if expected is None:
        if value is not None:
            _error(name + ' must be null when the reference is zero or absent.')
    elif not math.isclose(_number(value, name), expected, rel_tol=1e-9, abs_tol=1e-9):
        _error(name + ' is inconsistent with committed measurements.')


def _percent(delta, base):
    return None if base == 0 else delta / base * 100.0


def _point(point, name, *, best=False):
    if point is None:
        return None
    if type(point) is not dict or any(key not in point for key in _MEASURED):
        _error(name + ' must contain the committed measurement fields.')
    result = {key: point[key] for key in _MEASURED}
    _integer(result['iteration'], name + '.iteration')
    _number(result['stress_mpa'], name + '.stress_mpa', positive=True)
    _number(result['displacement_mm'], name + '.displacement_mm', nonnegative=True)
    _number(result['approximate_area_m2'], name + '.approximate_area_m2', positive=True)
    if best:
        if any(key not in point for key in _CHANGES):
            _error('best must include all three measured baseline change rates.')
        result.update({key: point[key] for key in _CHANGES})
    return result


def _check_changes(best, baseline):
    if best is None:
        return
    _close(best[_CHANGES[0]], _percent(baseline['stress_mpa'] - best['stress_mpa'],
           baseline['stress_mpa']), _CHANGES[0])
    _close(best[_CHANGES[1]], _percent(best['displacement_mm'] - baseline['displacement_mm'],
           baseline['displacement_mm']), _CHANGES[1])
    _close(best[_CHANGES[2]], _percent(best['approximate_area_m2'] - baseline['approximate_area_m2'],
           baseline['approximate_area_m2']), _CHANGES[2])


def _evidence(value, run_id, record_count):
    if type(value) is not dict:
        _error('Analysis evidence is required.')
    if value.get('basis') != 'committed_controller_checkpoint':
        _error('Only committed checkpoint evidence can produce batch feedback.')
    path = value.get('checkpoint_file')
    if type(path) is not str or not path or len(path) > 2000 or any(ord(c) < 32 for c in path):
        _error('checkpoint_file must be a bounded, absolute checkpoint path.')
    parsed = PureWindowsPath(path) if '\\' in path or re.match(r'^[A-Za-z]:', path) else PurePosixPath(path)
    if not parsed.is_absolute() or parsed.name != 'checkpoint.json' or parsed.parent.name != run_id or '..' in parsed.parts:
        _error('checkpoint_file must identify this run checkpoint.')
    flag = value.get('native_records_reverified')
    if type(flag) is not bool or value.get('physical_accuracy_certified') is not False:
        _error('Native verification flags must be explicit booleans; physical accuracy is not certified.')
    status = value.get('native_verification_status')
    checked = _integer(value.get('native_records_checked'), 'native_records_checked')
    if flag:
        if not record_count or status != 'passed' or checked != record_count:
            _error('Native verification evidence does not cover committed records.')
    elif checked != 0 or status not in ('not_reverified', 'no_committed_records') or (record_count and status == 'no_committed_records'):
        _error('Native verification status and flag disagree.')
    return {'basis': 'committed_controller_checkpoint', 'checkpoint_file': path,
            'native_records_reverified': flag, 'native_records_checked': checked,
            'native_verification_status': status, 'physical_accuracy_certified': False}


def _constraint_summary(value):
    if type(value) is not dict or value.get('engineering_qualification') != 'not_assessed':
        _error('Engineering qualification must remain not_assessed.')
    if value.get('all_implemented_rules_passed') is not None and type(value.get('all_implemented_rules_passed')) is not bool:
        _error('all_implemented_rules_passed must be a boolean or null.')
    if value.get('all_implemented_rules_passed') is False:
        _error('A failed implemented rule prevents feedback use.')
    rules = value.get('implemented_rules')
    if type(rules) is not list or len(rules) > len(_RULES):
        _error('implemented_rules must be a bounded rule list.')
    result, seen = [], set()
    for item in rules:
        _keys(item, ('rule', 'status'), 'implemented rule')
        if type(item['rule']) is not str or item['rule'] not in _RULES or item['rule'] in seen:
            _error('Unsupported or duplicate implemented rule.')
        if item['status'] not in ('passed', 'unknown', 'not_evaluated'):
            _error('Failed or unsupported implemented rule status.')
        seen.add(item['rule'])
        result.append(dict(item))
    if value.get('all_implemented_rules_passed') is True and any(x['status'] != 'passed' for x in result):
        _error('Unknown rules must not be presented as passed.')
    constraints = value.get('engineering_constraints')
    if type(constraints) is not list or len(constraints) > len(_UNKNOWN_CONSTRAINTS):
        _error('engineering_constraints must be a bounded list.')
    unknown, seen = [], set()
    for item in constraints:
        if (type(item) is not dict or type(item.get('name')) is not str or
                item.get('name') not in _UNKNOWN_CONSTRAINTS or item.get('status') != 'unknown'):
            _error('Engineering constraints must be known labels explicitly marked unknown.')
        if item['name'] in seen:
            _error('Duplicate engineering constraint.')
        seen.add(item['name'])
        unknown.append({'name': item['name'], 'status': 'unknown'})
    for key, label in (('allowable_stress_status', 'allowable stress'),
                       ('mass_status', 'mass bound'), ('life_status', 'life')):
        if value.get(key) != 'unknown':
            _error(key + ' must remain unknown.')
        if label not in seen:
            unknown.append({'name': label, 'status': 'unknown'})
            seen.add(label)
    return result, unknown


def _advice(window, best):
    if best is None:
        return '尚无已提交实测结果，继续探索前须先取得并核对基准。工程约束未评估，不推断热点、梯度或参数因果。'
    if window['historical_best_improvement_mpa'] > 0:
        return ('本批已改善历史最优，下一批可围绕实测最优方案继续探索；仍须遵守边界、精度和去重规则。'
                '应力降低不代表强度或寿命合格；不推断热点、梯度或单参数机理。')
    return ('本批未改善历史最优，仅建议在现有预算和搜索规则内继续探索；不能据此判断局部最优或参数因果。'
            '工程约束未评估，不推断热点、梯度或强度、寿命可行性。')


def _digest(value):
    content = {key: item for key, item in value.items() if key != 'feedback_id'}
    return 'fdbk_' + hashlib.sha256(_canonical(content).encode('utf-8')).hexdigest()[:24]


def build_feedback(analysis: dict, *, workflow_id: str, generation: int,
                   history_signature: str, target_total_candidates: int) -> dict:
    """Build a bounded factual record from successful ``analyze_run.data``."""
    analysis = _json(analysis)
    if type(analysis) is not dict or 'schema_version' in analysis:
        _error('Expected analyze_run.data, without an envelope or schema alias.')
    _identifier(workflow_id, 'wf')
    _integer(generation, 'generation', 1)
    if type(history_signature) is not str or not _SIGNATURE.fullmatch(history_signature):
        _error('history_signature must be the lowercase SHA256 history signature.')
    target = _integer(target_total_candidates, 'target_total_candidates', 1)
    run_id = _identifier(analysis.get('run_id'), 'run')
    objective = analysis.get('feedback')
    if type(objective) is not dict or objective.get('objective') != {
            'name': 'maximum_von_mises_stress', 'unit': 'MPa', 'direction': 'minimize'}:
        _error('Analysis objective must explicitly minimize maximum von Mises stress in MPa.')
    done = _integer(analysis.get('completed_candidates'), 'completed_candidates')
    if target < done or _integer(analysis.get('target_total_candidates'), 'analysis target', 1) < done:
        _error('Completed candidates exceed the frozen target or recorded target.')
    baseline = _point(analysis.get('baseline'), 'baseline')
    best = _point(analysis.get('best'), 'best', best=True)
    current = _point(analysis.get('current'), 'current')
    if any(x is None for x in (baseline, best, current)):
        if done or any(x is not None for x in (baseline, best, current)):
            _error('Committed candidates require baseline, best and current measurements.')
    elif baseline['iteration'] != 0 or current['iteration'] != done or best['iteration'] > done:
        _error('Measurement iteration metadata does not match the committed count.')
    candidates = analysis.get('candidates')
    if type(candidates) is not list or len(candidates) != done:
        _error('candidates must match completed_candidates.')
    measured = [_point(item, 'candidate') for item in candidates]
    if any(item is None or item['iteration'] != i for i, item in enumerate(measured, 1)):
        _error('Candidate iterations must be contiguous committed observations.')
    history = ([baseline] if baseline else []) + measured
    if history:
        if current != history[-1] or {key: best[key] for key in _MEASURED} != min(history, key=lambda x: x['stress_mpa']):
            _error('Best/current metadata disagrees with committed measurements.')
        _check_changes(best, baseline)
    window = analysis.get('window')
    if type(window) is not dict:
        _error('Analysis window is required.')
    since = _integer(window.get('since_candidate'), 'since_candidate')
    if since > done:
        _error('Feedback window is beyond the completed history.')
    expected_iterations = list(range(since + 1, done + 1))
    if (type(window.get('candidate_iterations')) is not list or
            any(type(i) is not int for i in window['candidate_iterations']) or
            window['candidate_iterations'] != expected_iterations or
            _integer(window.get('completed_candidates_in_window'), 'window count') != len(expected_iterations) or
            type(window.get('no_new_candidates')) is not bool or window['no_new_candidates'] != (not expected_iterations)):
        _error('Window iteration metadata is inconsistent.')
    before = min(history[:since + 1], key=lambda x: x['stress_mpa']) if history else None
    gain = before['stress_mpa'] - best['stress_mpa'] if before else None
    gain_percent = _percent(gain, before['stress_mpa']) if before else None
    _close(window.get('historical_best_improvement_mpa'), gain, 'window best improvement')
    _close(window.get('historical_best_improvement_percent'), gain_percent, 'window improvement percent')
    _close(window.get('best_before_stress_mpa'), before['stress_mpa'] if before else None, 'window best before')
    _close(window.get('best_after_stress_mpa'), best['stress_mpa'] if best else None, 'window best after')
    before_iteration = window.get('best_before_iteration')
    if ((before is not None and type(before_iteration) is not int) or
            before_iteration != (before['iteration'] if before else None)):
        _error('Window best-before iteration is inconsistent.')
    rules, unknown = _constraint_summary(analysis.get('constraint_evaluation'))
    evidence = _evidence(analysis.get('evidence'), run_id, len(history))
    recent = []
    prior_best = baseline['stress_mpa'] if baseline else None
    for raw, item in zip(candidates, measured):
        improved = item['stress_mpa'] < prior_best
        if type(raw.get('improved_historical_best')) is not bool or raw['improved_historical_best'] != improved:
            _error('Historical-best improvement flags disagree with measured stress.')
        if item['iteration'] > since:
            recent.append({'iteration': item['iteration'], 'stress_mpa': item['stress_mpa'],
                           'improved_historical_best': improved})
        prior_best = min(prior_best, item['stress_mpa'])
    source_window = {'since_candidate': since,
        'completed_candidates_in_window': len(expected_iterations),
        'candidate_iterations': expected_iterations,
        'historical_best_improvement_mpa': gain,
        'historical_best_improvement_percent': gain_percent}
    result = {'schema_version': FEEDBACK_SCHEMA, 'feedback_id': '',
        'workflow_id': workflow_id, 'generation': generation, 'run_id': run_id,
        'objective': OBJECTIVE, 'source_history_signature': history_signature,
        'source_completed_candidates': done, 'source_window': source_window,
        'baseline': baseline, 'best': best, 'current': current,
        'recent_measured': recent[-3:], 'implemented_rules': rules,
        'unknown_engineering_constraints': unknown,
        'engineering_qualification': 'not_assessed', 'causal_attribution': 'not_established',
        'hotspot_attribution': 'not_available',
        'budget': {'target_total_candidates': target, 'completed_candidates': done,
                   'remaining_candidates': target - done},
        'program_advice': _advice(source_window, best), 'evidence': evidence}
    result['feedback_id'] = _digest(result)
    return validate_feedback(result)


def validate_feedback(feedback: dict) -> dict:
    """Return an independent validated copy; reject changed or unknown fields."""
    result = _json(feedback)
    _keys(result, _TOP, 'feedback')
    if result['schema_version'] != FEEDBACK_SCHEMA:
        _error('Unsupported feedback schema version; aliases are not accepted.', 'FEEDBACK_SCHEMA')
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > MAX_FEEDBACK_CHARS:
        _error('Full feedback exceeds 14000 serialized characters.', 'FEEDBACK_SIZE')
    if type(result['feedback_id']) is not str or not _FEEDBACK_ID.fullmatch(result['feedback_id']) or result['feedback_id'] != _digest(result):
        _error('Feedback ID does not match canonical content.', 'FEEDBACK_INTEGRITY')
    _identifier(result['workflow_id'], 'wf')
    _identifier(result['run_id'], 'run')
    _integer(result['generation'], 'generation', 1)
    if result['objective'] != OBJECTIVE or type(result['source_history_signature']) is not str or not _SIGNATURE.fullmatch(result['source_history_signature']):
        _error('Feedback objective or history signature is unsupported.')
    done = _integer(result['source_completed_candidates'], 'source_completed_candidates')
    budget = result['budget']
    _keys(budget, ('target_total_candidates', 'completed_candidates', 'remaining_candidates'), 'budget')
    target = _integer(budget['target_total_candidates'], 'target_total_candidates', 1)
    if _integer(budget['completed_candidates'], 'budget completed') != done or _integer(budget['remaining_candidates'], 'remaining') != target - done:
        _error('Frozen budget does not match source completed candidates.')
    for name in ('baseline', 'best', 'current'):
        point = result[name]
        if point is not None:
            _keys(point, _MEASURED + (_CHANGES if name == 'best' else ()), name)
            _point(point, name, best=(name == 'best'))
    baseline, best, current = (result[name] for name in ('baseline', 'best', 'current'))
    if any(x is None for x in (baseline, best, current)):
        if done or any(x is not None for x in (baseline, best, current)):
            _error('Incomplete measurement summary.')
    else:
        if baseline['iteration'] != 0 or current['iteration'] != done or best['iteration'] > done or best['stress_mpa'] > min(baseline['stress_mpa'], current['stress_mpa']):
            _error('Measurement summary metadata is inconsistent.')
        if best['iteration'] == 0 and any(best[key] != baseline[key] for key in _MEASURED):
            _error('Best baseline summary is inconsistent.')
        if best['iteration'] == done and any(best[key] != current[key] for key in _MEASURED):
            _error('Best current summary is inconsistent.')
        _check_changes(best, baseline)
    window = result['source_window']
    _keys(window, ('since_candidate', 'completed_candidates_in_window', 'candidate_iterations',
        'historical_best_improvement_mpa', 'historical_best_improvement_percent'), 'source_window')
    since = _integer(window['since_candidate'], 'since_candidate')
    iterations = window['candidate_iterations']
    count = _integer(window['completed_candidates_in_window'], 'window count')
    if (since > done or type(iterations) is not list or count != done - since or
            len(iterations) != count or
            any(type(value) is not int or value != i for i, value in enumerate(iterations, since + 1))):
        _error('Source window must match the committed candidate range.')
    gain = window['historical_best_improvement_mpa']
    if best is None:
        _close(gain, None, 'window improvement')
        _close(window['historical_best_improvement_percent'], None, 'window improvement percent')
    else:
        _number(gain, 'window improvement', nonnegative=True)
        _close(window['historical_best_improvement_percent'], _percent(gain, best['stress_mpa'] + gain), 'window improvement percent')
        if (not iterations and gain != 0) or gain > baseline['stress_mpa'] - best['stress_mpa'] + 1e-9:
            _error('Window improvement is inconsistent with baseline or empty window.')
    recent = result['recent_measured']
    if type(recent) is not list or len(recent) != min(3, len(iterations)):
        _error('Recent measurements must contain the last at most three window candidates.')
    for item, iteration in zip(recent, iterations[-3:]):
        _keys(item, ('iteration', 'stress_mpa', 'improved_historical_best'), 'recent measurement')
        if _integer(item['iteration'], 'recent iteration') != iteration or type(item['improved_historical_best']) is not bool:
            _error('Recent iteration/improvement metadata is invalid.')
        _number(item['stress_mpa'], 'recent stress', positive=True)
        if best and item['stress_mpa'] < best['stress_mpa']:
            _error('Recent stress cannot improve beyond the recorded historical best.')
    if recent and recent[-1]['stress_mpa'] != current['stress_mpa']:
        _error('Most recent measured stress must equal current stress.')
    rules = result['implemented_rules']
    unknown = result['unknown_engineering_constraints']
    # Reuse exactly the same rule vocabulary; no unknown status becomes a pass.
    _constraint_summary({'implemented_rules': rules, 'all_implemented_rules_passed': None,
        'engineering_constraints': unknown, 'engineering_qualification': 'not_assessed',
        'allowable_stress_status': 'unknown', 'mass_status': 'unknown', 'life_status': 'unknown'})
    for item in unknown:
        _keys(item, ('name', 'status'), 'unknown engineering constraint')
    if any(name not in {item['name'] for item in unknown} for name in ('allowable stress', 'mass bound', 'life')):
        _error('Unknown strength, mass and life constraints must remain explicit.')
    if result['engineering_qualification'] != 'not_assessed' or result['causal_attribution'] != 'not_established' or result['hotspot_attribution'] != 'not_available':
        _error('Engineering qualification, causal and hotspot attribution cannot be asserted.')
    _keys(result['evidence'], ('basis', 'checkpoint_file', 'native_records_reverified',
        'native_records_checked', 'native_verification_status', 'physical_accuracy_certified'), 'evidence')
    _evidence(result['evidence'], result['run_id'], done + 1 if baseline else 0)
    if result['program_advice'] != _advice(window, best):
        _error('Program advice must be the bounded factual template.')
    return result


def feedback_prompt_context(feedback: dict) -> dict:
    """Return factual prompt context without absolute paths or verbose evidence."""
    value = validate_feedback(feedback)
    context = {key: value[key] for key in ('schema_version', 'feedback_id', 'workflow_id',
        'generation', 'run_id', 'objective', 'source_history_signature',
        'source_completed_candidates', 'source_window', 'baseline', 'best', 'current',
        'recent_measured', 'implemented_rules', 'unknown_engineering_constraints',
        'engineering_qualification', 'causal_attribution', 'hotspot_attribution',
        'budget', 'program_advice')}
    context['evidence'] = {key: value['evidence'][key] for key in (
        'basis', 'native_records_reverified', 'native_verification_status', 'physical_accuracy_certified')}
    if len(json.dumps(context, ensure_ascii=False, allow_nan=False)) > MAX_PROMPT_CHARS:
        _error('Feedback prompt context exceeds 6500 serialized characters.', 'FEEDBACK_SIZE')
    return context
