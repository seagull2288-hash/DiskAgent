"""Reproducible grouped pools for the opt-in hybrid local search policy.

This module does not rank candidates, call a model or start a solver. It keeps
the proposed center, incumbent-neighborhood candidates and global candidates in
separate groups, so downstream ranking cannot silently erase the local quota.
"""
from __future__ import annotations

import math
import numpy as np

from .runtime import ControllerError, bridge
from .sampling import history_x, normalize, signature, unique_design


POLICY = 'hybrid_local_v2'
_LOCAL_STREAM = 12011
_GLOBAL_STREAM = 12037


def _fail(message):
    raise ControllerError('Local search: ' + message)


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(name + ' must be a finite number, not a boolean.')
    return float(value)


def _integer(value, name, minimum):
    if type(value) is not int or value < minimum:
        _fail(name + ' must be an integer >= ' + str(minimum) + '.')
    return value


def _config(config):
    if not isinstance(config, dict):
        _fail('config must be a dictionary.')
    if config.get('selection_policy', POLICY) != POLICY:
        _fail('unsupported selection policy.')
    result = {}
    for name, minimum in (('seed', 0), ('initial_lhs', 0), ('batch_size', 2),
                          ('pool_size', 2), ('exploration_slots', 1)):
        if name not in config:
            _fail('missing configuration field ' + name + '.')
        result[name] = _integer(config[name], name, minimum)
    for name in ('sigma_min', 'sigma_start', 'global_fraction'):
        if name not in config:
            _fail('missing configuration field ' + name + '.')
        result[name] = _number(config[name], name)
    if not (result['pool_size'] >= result['batch_size'] > result['exploration_slots']):
        _fail('pool_size >= batch_size > exploration_slots is required.')
    if not (0 < result['sigma_min'] <= result['sigma_start'] <= 1):
        _fail('0 < sigma_min <= sigma_start <= 1 is required.')
    if not 0 <= result['global_fraction'] <= 1:
        _fail('global_fraction must lie in [0, 1].')
    return result


def _history(history):
    if not isinstance(history, list) or not history:
        _fail('a committed baseline history is required.')
    for i, point in enumerate(history):
        if (not isinstance(point, dict) or not isinstance(point.get('parameters'), dict) or
                set(point['parameters']) != set(bridge.NAMES)):
            _fail('history requires the nine named parameters at observation ' + str(i) + '.')
        for name in bridge.NAMES:
            _number(point['parameters'][name], 'history parameter ' + name)
        if _number(point.get('stress_mpa'), 'measured stress') <= 0:
            _fail('committed measured stresses must be positive.')
    return history


def _problem(problem):
    if (not isinstance(problem, dict) or problem.get('input_order') != bridge.NAMES or
            type(problem.get('decimals')) is not int or problem['decimals'] != 4):
        _fail('the canonical nine-variable, four-decimal problem is required.')
    variables = problem.get('variables')
    if (not isinstance(variables, list) or len(variables) != 9 or
            any(not isinstance(v, dict) for v in variables) or
            [v.get('name') for v in variables] != bridge.NAMES):
        _fail('problem variables must follow the canonical input order.')
    for variable in variables:
        low = _number(variable.get('lower'), 'lower bound')
        high = _number(variable.get('upper'), 'upper bound')
        reference = _number(variable.get('reference'), 'reference')
        if not low < high or not low <= reference <= high:
            _fail('invalid variable bounds/reference.')
    if _number(problem.get('min_normalized_distance'), 'duplicate tolerance') < 0:
        _fail('duplicate tolerance must be nonnegative.')
    return problem


def derive_local_state(history, config, *, policy=None):
    """Replay radius changes from complete, committed batches only.

    A batch succeeds only if its historical best improves by more than
    max(1e-9 MPa, abs(best_before)*1e-6). This is a numeric search tolerance,
    not a strength/qualification threshold. Two successive successes expand
    radius by 1.5; two successive no-improvement batches contract it by 0.5.
    Failed solver attempts do not appear in committed history and are not
    inferred here. Partial batches cannot change the radius or its anchor.
    """
    cfg = _config(config)
    _history(history)
    done = len(history) - 1
    initial, size = cfg['initial_lhs'], cfg['batch_size']
    batches = max(0, (done - initial) // size)
    eligible = initial + batches * size if done >= initial else 0
    radius = cfg['sigma_start']
    successes = failures = 0
    evidence = []
    for batch in range(batches):
        start, end = initial + batch * size, initial + (batch + 1) * size
        before = min(p['stress_mpa'] for p in history[:start + 1])
        after = min(p['stress_mpa'] for p in history[:end + 1])
        gain, tolerance = before - after, max(1e-9, abs(before) * 1e-6)
        improved = gain > tolerance
        if policy is not None:
            from .objectives import archive, rank_key
            evaluated = archive(history[:end + 1], policy)['evaluations']
            improved = min(map(rank_key, evaluated)) < min(map(rank_key, evaluated[:start + 1]))
        prior = radius
        if improved:
            successes, failures = successes + 1, 0
            event = 'success'
            if successes % 2 == 0:
                radius = min(cfg['sigma_start'], radius * 1.5)
        else:
            successes, failures = 0, failures + 1
            event = 'no_improvement'
            if failures % 2 == 0:
                radius = max(cfg['sigma_min'], radius * 0.5)
        evidence.append({
            'batch': batch + 1, 'start_completed_candidates': start,
            'end_completed_candidates': end, 'best_before_mpa': before,
            'best_after_mpa': after, 'improvement_mpa': gain,
            'numeric_improvement_tolerance_mpa': tolerance, 'result': event,
            'radius_before': prior, 'radius_after': radius,
            'success_streak': successes, 'no_improvement_streak': failures,
        })
    prefix = history[:eligible + 1]
    best_index = min(range(len(prefix)), key=lambda i: prefix[i]['stress_mpa'])
    if policy is not None:
        from .objectives import archive
        best_index = archive(prefix, policy)['anchor_iteration']
    action = ('initial' if not evidence else
              'shrink' if evidence[-1]['radius_after'] < evidence[-1]['radius_before'] else
              'expand' if evidence[-1]['radius_after'] > evidence[-1]['radius_before'] else 'hold')
    return {
        'policy': POLICY, 'metric': 'normalized_linf', 'radius_fraction': radius,
        'anchor_x': history_x([prefix[best_index]])[0],
        'anchor_iteration': best_index, 'anchor_stress_mpa': prefix[best_index]['stress_mpa'],
        'completed_batches': batches, 'success_streak': successes,
        'no_improvement_streak': failures,
        'adaptation_action': action,
        'streaks': {'success': successes, 'no_improvement': failures},
        'basis_completed_candidates': eligible,
        'ignored_partial_candidates': done - eligible,
        'initial_lhs': initial, 'basis_history_signature': signature(prefix),
        'adaptation_evidence': evidence,
        'solver_failure_policy': 'not_inferred_from_committed_history',
    }


def build_local_plan(center, problem, history, config, generation):
    """Return an immutable-ready grouped pool; final quota ranking is external.

    ``generation`` is zero-based, matching sampling.candidate_pool. History
    must be exactly the frozen basis of that new batch. An admissible unmeasured
    quantized center is protected; a duplicate center consumes no pool position.
    Local points lie in a normalized L-infinity box around the measured best.
    """
    cfg = _config(config)
    _problem(problem)
    _history(history)
    generation = _integer(generation, 'generation', 0)
    expected = cfg['initial_lhs'] + generation * cfg['batch_size']
    if len(history) - 1 != expected:
        _fail('history must be the complete frozen basis of this generation; partial/future history is unsupported.')
    occupied = history_x(history)
    for x in occupied:
        if bridge.ordered_values(dict(zip(bridge.NAMES, x)), problem) != x:
            _fail('committed geometry must already be canonical at four decimals.')
    if not isinstance(center, (list, tuple)) or len(center) != 9:
        _fail('center must contain exactly nine numeric values.')
    for value in center:
        _number(value, 'center coordinate')
    # ordered_values rejects out-of-bound input before quantization; only newly
    # sampled normalized points use the existing reflection rule.
    canonical_center = bridge.ordered_values(dict(zip(bridge.NAMES, center)), problem)
    duplicate = any(bridge.normalized_distance2(canonical_center, x, problem)
                    <= problem['min_normalized_distance'] ** 2 for x in occupied)
    protected = None if duplicate else 0
    protected_count = int(protected is not None)
    local_slots = cfg['batch_size'] - cfg['exploration_slots'] - protected_count
    max_global = cfg['pool_size'] - protected_count - local_slots
    global_count = max(cfg['exploration_slots'], int(round(cfg['pool_size'] * cfg['global_fraction'])))
    global_count = min(max_global, global_count)
    local_count = cfg['pool_size'] - protected_count - global_count
    if local_count < local_slots or global_count < cfg['exploration_slots']:
        _fail('candidate groups cannot satisfy their explicit batch quotas.')

    state = derive_local_state(history, config, policy=problem.get('optimization_policy'))
    radius = state['radius_fraction']
    anchor = normalize(state['anchor_x'], problem)
    pool = [list(canonical_center)] if protected is not None else []
    origins = ['llm_center'] if protected is not None else []
    local_indices, global_indices = [], []
    local_rng = np.random.default_rng(np.random.SeedSequence([cfg['seed'], generation, _LOCAL_STREAM]))
    global_rng = np.random.default_rng(np.random.SeedSequence([cfg['seed'], generation, _GLOBAL_STREAM]))

    for name, count, rng, indices in (
        ('best_local', local_count, local_rng, local_indices),
        ('global_uniform', global_count, global_rng, global_indices),
    ):
        for _ in range(max(1, count) * 200):
            if len(indices) == count:
                break
            z = anchor + rng.uniform(-radius, radius, 9) if name == 'best_local' else rng.random(9)
            x = unique_design(z, problem, occupied + pool)
            if x is None:
                continue
            if name == 'best_local' and np.max(np.abs(normalize(x, problem) - anchor)) > radius + 1e-12:
                # Quantization may move a point just beyond a narrow box.
                continue
            indices.append(len(pool))
            pool.append(x)
            origins.append(name)
        if len(indices) != count:
            _fail('insufficient distinct ' + name + ' candidates after bounded sampling; no quota substitution allowed.')
    if len(pool) != cfg['pool_size'] or len(origins) != len(pool):
        _fail('candidate pool size does not match its configured contract.')
    return {
        'pool': pool, 'pool_origins': origins, 'local_state': state,
        'protected_center_index': protected,
        'local_indices': local_indices, 'global_indices': global_indices,
        'center_status': 'duplicate_replaced_by_best_local' if duplicate else 'protected_unmeasured',
        'selection_quota': {'llm_center': protected_count, 'best_local': local_slots,
                            'global_uniform': cfg['exploration_slots']},
        'pool_group_sizes': {'llm_center': protected_count, 'best_local': local_count,
                             'global_uniform': global_count},
        'rng_streams': {'seed': cfg['seed'], 'generation': generation,
                        'best_local': _LOCAL_STREAM, 'global_uniform': _GLOBAL_STREAM},
    }
