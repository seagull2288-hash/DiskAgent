"""Versioned objective policies and deterministic evaluation of measured results."""
from __future__ import annotations

import hashlib
import json
import math

from .runtime import ControllerError


SCHEMA = 'disk.optimization-policy.v1'
METRICS = {
    'stress_mpa': {'label': '最大等效应力', 'unit': 'MPa', 'direction': 'minimize'},
    'mass_kg': {'label': '整盘质量', 'unit': 'kg', 'direction': 'minimize'},
    'frequency_hz': {'label': '指定模态频率', 'unit': 'Hz', 'direction': 'maximize'},
}


def number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise ControllerError(label + ' requires a positive finite number.')
    return float(value)


def default_policy():
    return {'schema_version': SCHEMA,
            'objectives': [{'metric': 'stress_mpa', 'direction': 'minimize', 'weight': 1.0}],
            'constraints': [], 'collect_mass': False, 'modal': None}


def validate_policy(value):
    """Return a canonical JSON copy; missing constraint limits fail at preview."""
    if not isinstance(value, dict) or set(value) != set(default_policy()):
        raise ControllerError('Optimization policy requires schema_version, objectives, constraints, collect_mass and modal.')
    if value['schema_version'] != SCHEMA or type(value['collect_mass']) is not bool:
        raise ControllerError('Unsupported optimization policy version or mass collection flag.')
    objectives, constraints = value['objectives'], value['constraints']
    if not isinstance(objectives, list) or not 1 <= len(objectives) <= len(METRICS):
        raise ControllerError('Select between one and three objectives.')
    seen = set()
    for item in objectives:
        if not isinstance(item, dict) or set(item) != {'metric', 'direction', 'weight'}:
            raise ControllerError('Each objective requires metric, direction and weight.')
        metric = item['metric']
        if (not isinstance(metric, str) or metric not in METRICS or metric in seen
                or item['direction'] not in ('minimize', 'maximize')):
            raise ControllerError('Objective metrics must be supported and unique with an explicit direction.')
        number(item['weight'], 'Objective weight')
        seen.add(metric)
    if not isinstance(constraints, list) or len(constraints) > 12:
        raise ControllerError('Constraints must be a list of at most twelve entries.')
    for item in constraints:
        if not isinstance(item, dict) or set(item) != {'metric', 'operator', 'value', 'upper', 'reference', 'source'}:
            raise ControllerError('A constraint requires metric, operator, value, upper, reference and source.')
        if (not isinstance(item['metric'], str) or item['metric'] not in METRICS
                or item['operator'] not in ('<=', '>=', 'outside_band')):
            raise ControllerError('Unsupported constraint metric or operator.')
        if item['reference'] not in ('absolute', 'baseline_ratio'):
            raise ControllerError('Constraint reference must be absolute or baseline_ratio.')
        number(item['value'], 'Constraint limit')
        if item['operator'] == 'outside_band':
            if item['metric'] != 'frequency_hz' or number(item['upper'], 'Band upper limit') <= item['value']:
                raise ControllerError('A frequency exclusion band requires ordered positive limits.')
        elif item['upper'] is not None:
            raise ControllerError('Only an exclusion band accepts an upper limit.')
        if not isinstance(item['source'], str) or not item['source'].strip() or len(item['source']) > 300:
            raise ControllerError('Record the constraint source, including exploratory baseline settings.')
    required = {item['metric'] for item in objectives + constraints}
    modal = value['modal']
    if modal is not None:
        modal_fields = {'analysis_name', 'mode_index', 'family'}
        if not isinstance(modal, dict) or set(modal) not in (modal_fields, modal_fields | {'prestress_required'}):
            raise ControllerError('Modal collection requires analysis_name, mode_index and family.')
        if 'prestress_required' in modal and type(modal['prestress_required']) is not bool:
            raise ControllerError('prestress_required must be a boolean.')
        if (not isinstance(modal['analysis_name'], str) or not modal['analysis_name'].strip()
                or len(modal['analysis_name']) > 120 or any(c in modal['analysis_name'] for c in '\0\r\n')):
            raise ControllerError('Name the configured Mechanical modal analysis.')
        if type(modal['mode_index']) is not int or not 1 <= modal['mode_index'] <= 100:
            raise ControllerError('Modal order must be an integer between 1 and 100.')
        if modal['family'] != 'axisymmetric_fixed_order':
            raise ControllerError('This adapter supports explicitly selected axisymmetric fixed-order modes only.')
    if 'frequency_hz' in required and modal is None:
        raise ControllerError('Frequency objectives/constraints require a configured modal analysis.')
    result = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    result['collect_mass'] = value['collect_mass'] or 'mass_kg' in required
    return result


def policy_id(policy):
    encoded = json.dumps(validate_policy(policy), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return 'policy_' + hashlib.sha256(encoded.encode()).hexdigest()[:20]


def evaluate(point, baseline, policy):
    """Evaluate hard constraints first; all scaling is frozen to the baseline."""
    policy = validate_policy(policy)
    checks, violations = [], []
    missing = set()
    for item in policy['constraints']:
        metric, value = item['metric'], point.get(item['metric'])
        scale = baseline.get(metric) if item['reference'] == 'baseline_ratio' else 1.0
        lower = upper = None
        if scale is not None:
            scale = number(scale, 'Constraint scale')
            lower = number(number(item['value'], 'Limit') * scale, 'Scaled limit')
            upper = number(item['upper'] * scale, 'Scaled upper limit') if item['upper'] is not None else None
        if value is None or scale is None:
            missing.add(metric)
            checks.append(dict(item, status='missing', actual=value, limit=lower, upper_limit=upper,
                               violation=None, margin=None, margin_unit=METRICS[metric]['unit']))
            continue
        value = number(value, metric)
        if item['operator'] == '<=':
            margin = lower - value
            violation = max(0.0, value / lower - 1)
        elif item['operator'] == '>=':
            margin = value - lower
            violation = max(0.0, 1 - value / lower)
        else:
            margin = max(lower - value, value - upper)
            violation = min(value - lower, upper - value) / (upper - lower) if lower < value < upper else 0.0
        violations.append(violation)
        checks.append(dict(item, status='passed' if violation == 0 else 'failed', actual=value,
                           limit=lower, upper_limit=upper, violation=violation,
                           margin=margin, margin_unit=METRICS[metric]['unit']))
    score, vector = 0.0, []
    weight_scale = max(item['weight'] for item in policy['objectives'])
    weight_sum = sum(item['weight'] / weight_scale for item in policy['objectives'])
    for item in policy['objectives']:
        metric = item['metric']
        if point.get(metric) is None or baseline.get(metric) is None:
            missing.add(metric)
            continue
        ratio = number(point[metric], metric) / number(baseline[metric], 'Baseline ' + metric)
        directed = ratio if item['direction'] == 'minimize' else -ratio
        if not math.isfinite(directed):
            raise ControllerError('Normalized objective exceeds the finite numeric range.')
        score += (item['weight'] / weight_scale) / weight_sum * directed
        vector.append(directed)
    status = 'missing' if missing else 'infeasible' if any(violations) else 'feasible'
    return {'status': status, 'score': None if missing else score,
            'objective_vector': vector, 'constraint_checks': checks,
            'total_violation': sum(violations), 'missing_metrics': sorted(missing)}


def rank_key(result):
    return ({'feasible': 0, 'infeasible': 1, 'missing': 2}[result['status']],
            result['total_violation'], result['score'] if result['score'] is not None else math.inf)


def archive(history, policy):
    policy = validate_policy(policy)
    evaluations = [dict(evaluate(point, history[0], policy), iteration=i) for i, point in enumerate(history)]
    feasible = [item for item in evaluations if item['status'] == 'feasible']
    ordered = sorted(feasible, key=lambda item: (rank_key(item), item['iteration']))
    pareto = []
    for item in feasible:
        a = item['objective_vector']
        dominated = any(all(x <= y for x, y in zip(other['objective_vector'], a))
                        and any(x < y for x, y in zip(other['objective_vector'], a)) for other in feasible)
        if not dominated:
            pareto.append(item['iteration'])
    return {'policy': policy, 'policy_id': policy_id(policy), 'evaluations': evaluations,
            'best_iteration': ordered[0]['iteration'] if ordered else None,
            'anchor_iteration': min(evaluations, key=rank_key)['iteration'] if evaluations else None,
            'pareto_iterations': pareto, 'feasible_count': len(feasible),
            'ranking_basis': 'hard_constraints_then_weighted_baseline_normalized_objectives',
            'mode_tracking': 'fixed_order_only' if policy['modal'] else 'not_requested'}


def selection_summary(history, optimization=None):
    """Give every result surface the same three explicitly named selections."""
    evaluated = optimization if optimization is not None else archive(history, default_policy())

    def design(iteration):
        if iteration is None:
            return None
        return {'iteration': iteration, 'observation': history[iteration],
                'evaluation': evaluated['evaluations'][iteration]}

    minimum = min(range(len(history)), key=lambda i: history[i]['stress_mpa']) if history else None
    return {'policy_id': evaluated['policy_id'],
            'policy_source': 'saved_optimization_policy' if optimization is not None else 'default_stress_objective',
            'constraint_count': len(evaluated['policy']['constraints']),
            'ranking_basis': evaluated['ranking_basis'],
            'minimum_stress': design(minimum),
            'recommended': design(evaluated['best_iteration']),
            'pareto': [design(i) for i in evaluated['pareto_iterations']]}


