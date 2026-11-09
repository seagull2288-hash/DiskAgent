"""Deterministic coordinate and sparse-pattern pools for hybrid local v3.

The caller supplies the frozen committed history of one batch. This module
generates designs and quotas, while the existing selector owns model ranking.
"""
from __future__ import annotations

import math

import numpy as np

from . import local_search
from .runtime import ControllerError, bridge
from .sampling import history_x


POLICY = 'hybrid_local_v3'
STEP_SCALES = (0.25, 0.5, 1.0)
_PATTERN_STREAM = 13017
_GLOBAL_STREAM = 13037


def _fail(message):
    raise ControllerError('Directional search: ' + message)


def _integer(value, name, minimum):
    if type(value) is not int or value < minimum:
        _fail(name + ' must be an integer >= ' + str(minimum) + '.')
    return value


def _finite(value, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        _fail(name + ' must be finite and numeric, not a boolean.')
    return float(value)


def _config(config):
    if not isinstance(config, dict) or config.get('selection_policy') != POLICY:
        _fail('selection_policy must explicitly be ' + POLICY + '.')
    result = {}
    for name, minimum in (('seed', 0), ('initial_lhs', 0), ('batch_size', 2),
                          ('pool_size', 2), ('exploration_slots', 0)):
        result[name] = _integer(config.get(name), name, minimum)
    for name in ('sigma_start', 'sigma_min', 'global_fraction'):
        result[name] = _finite(config.get(name), name)
    if not result['pool_size'] >= result['batch_size'] > result['exploration_slots']:
        _fail('pool_size >= batch_size > exploration_slots is required.')
    if not 0 < result['sigma_min'] <= result['sigma_start'] <= 1:
        _fail('0 < sigma_min <= sigma_start <= 1 is required.')
    if not 0 <= result['global_fraction'] <= 1:
        _fail('global_fraction must lie in [0, 1].')
    return result


def _exploration_schedule(completed_batches, no_improvement_streak, maximum):
    batch = completed_batches + 1
    if maximum == 0:
        reason = 'local_refinement'
    elif batch <= 8:
        reason = 'early8'
    elif no_improvement_streak > 0 and no_improvement_streak % 3 == 0:
        reason = 'stagnation_pulse'
    elif (batch - 8) % 3 == 0:
        reason = 'periodic'
    else:
        reason = 'local_refinement'
    count = int(reason != 'local_refinement')
    return {'batch_number': batch, 'schedule_reason': reason, 'count': count,
            'configured_maximum': maximum,
            'no_improvement_streak': no_improvement_streak,
            'early_batches': 8, 'late_period_batches': 3}


def derive_directional_state(history, config, policy=None):
    """Replay the existing radius rule and add the v3 direction/global schedule.

    Partial batches retain the previous complete batch's anchor and radius.
    The inherited improvement tolerance controls algorithm state only.
    """
    cfg = _config(config)
    # Reuse the established radius replay. Its v2 config contract requires one
    # exploration slot, whereas v3 may schedule zero slots for the next batch.
    radius_config = dict(config, selection_policy=local_search.POLICY,
                         exploration_slots=max(1, cfg['exploration_slots']))
    state = local_search.derive_local_state(history, radius_config, policy=policy)
    schedule = _exploration_schedule(state['completed_batches'],
                                     state['no_improvement_streak'],
                                     cfg['exploration_slots'])
    rotation = state['completed_batches'] * 2 % len(bridge.NAMES)
    state.update(policy=POLICY, direction_rotation=rotation,
        direction_parameter_order=bridge.NAMES[rotation:] + bridge.NAMES[:rotation],
        coordinate_step_scales=list(STEP_SCALES),
        global_schedule=schedule, global_exploration_slots=schedule['count'],
        numeric_tolerance_role='algorithm_state_only',
        engineering_significance_threshold=None)
    return state


def _canonical_trial(anchor, axes, signs, step, radius, problem, kind):
    raw = list(anchor)
    clipped = False
    for axis, sign in zip(axes, signs):
        variable = problem['variables'][axis]
        requested = anchor[axis] + sign * step * radius * (variable['upper'] - variable['lower'])
        feasible = min(variable['upper'], max(variable['lower'], requested))
        clipped = clipped or feasible != requested
        raw[axis] = feasible
    x = bridge.ordered_values(dict(zip(bridge.NAMES, raw)), problem)
    changed = [i for i, (before, after) in enumerate(zip(anchor, x)) if before != after]
    if not changed:
        return None
    delta = [(after - before) / (variable['upper'] - variable['lower'])
             for before, after, variable in zip(anchor, x, problem['variables'])]
    if max(map(abs, delta)) > radius + 1e-12:
        return None
    # Rounding at a boundary may collapse a component to zero, but a requested
    # sign can never turn into an opposite-side trial.
    if any(delta[axis] * sign < 0 for axis, sign in zip(axes, signs)):
        _fail('Canonical trial reversed its requested feasible direction.')
    return x, {'direction_kind': kind,
        'changed_parameters': [bridge.NAMES[i] for i in changed],
        'step_scale': step, 'normalized_delta': delta,
        'clipped_to_bounds': clipped}


def _coordinate_trials(anchor, order, step, radius, problem, generation):
    by_axis = []
    for axis in order:
        preferred = 1 if (generation + axis) % 2 == 0 else -1
        trials = []
        for sign in (preferred, -preferred):
            trial = _canonical_trial(anchor, [axis], [sign], step, radius,
                                     problem, 'coordinate')
            if trial is not None:
                trials.append(trial)
        by_axis.append(trials)
    # The first pass covers distinct axes. A bound-facing zero move is skipped
    # so that the feasible direction of that axis can still take its turn.
    for sign_pass in (0, 1):
        for trials in by_axis:
            if len(trials) > sign_pass:
                yield trials[sign_pass]


def _scale_counts(count):
    if count < 3:
        return [count, 0, 0]
    inner = (count + 1) // 2
    middle = (count - inner) // 2
    return [inner, middle, count - inner - middle]


def build_directional_plan(center, problem, history, config, generation):
    """Build a bounded, quantized pool with an ordered local fallback.

    Coordinate trials preserve eight parameters. Sparse patterns preserve at
    least seven and supplement scales whose coordinate trials are exhausted.
    Boundary handling truncates in the requested direction or skips a zero
    move. No directional trial uses reflection.
    """
    cfg = _config(config)
    local_search._problem(problem)
    local_search._history(history)
    generation = _integer(generation, 'generation', 0)
    expected = cfg['initial_lhs'] + generation * cfg['batch_size']
    if len(history) - 1 != expected:
        _fail('history must be the complete frozen basis of this generation.')
    occupied = history_x(history)
    threshold = problem['min_normalized_distance'] ** 2
    for i, x in enumerate(occupied):
        canonical = bridge.ordered_values(dict(zip(bridge.NAMES, x)), problem)
        if canonical != x:
            _fail('Committed parameters must already have four-decimal precision.')
        if any(bridge.normalized_distance2(x, old, problem) <= threshold for old in occupied[:i]):
            _fail('Committed history contains duplicate designs.')
    if not isinstance(center, (list, tuple)) or len(center) != len(bridge.NAMES):
        _fail('center must contain exactly nine numeric values.')
    for value in center:
        _finite(value, 'center coordinate')
    canonical_center = bridge.ordered_values(dict(zip(bridge.NAMES, center)), problem)
    duplicate = any(bridge.normalized_distance2(canonical_center, x, problem) <= threshold
                    for x in occupied)
    protected = None if duplicate else 0
    protected_count = int(protected is not None)
    state = derive_directional_state(history, config, policy=problem.get('optimization_policy'))
    global_slots = state['global_exploration_slots']
    local_slots = cfg['batch_size'] - global_slots - protected_count
    if local_slots < 0:
        _fail('Protected center and global slots exceed this batch.')
    max_global = cfg['pool_size'] - protected_count - local_slots
    global_count = (min(max_global, max(global_slots,
                    int(round(cfg['pool_size'] * cfg['global_fraction']))))
                    if global_slots else 0)
    local_count = cfg['pool_size'] - protected_count - global_count
    if local_count < local_slots or global_count < global_slots:
        _fail('Candidate groups cannot satisfy the directional batch quotas.')
    pool = [list(canonical_center)] if protected is not None else []
    origins = ['llm_center'] if protected is not None else []
    local_indices, global_indices, details = [], [], {}
    anchor = state['anchor_x']
    radius = state['radius_fraction']
    rotation = state['direction_rotation']
    order = list(range(rotation, len(bridge.NAMES))) + list(range(rotation))
    pattern_rng = np.random.default_rng(np.random.SeedSequence([
        cfg['seed'], generation, _PATTERN_STREAM]))
    global_rng = np.random.default_rng(np.random.SeedSequence([
        cfg['seed'], generation, _GLOBAL_STREAM]))

    def accept(trial, origin, destination):
        if trial is None:
            return False
        x, detail = trial
        if any(bridge.normalized_distance2(x, old, problem) <= threshold
               for old in occupied + pool):
            return False
        index = len(pool)
        pool.append(x)
        origins.append(origin)
        destination.append(index)
        if detail is not None:
            details[str(index)] = dict(detail, pool_index=index)
        return True

    for step, count in zip(STEP_SCALES, _scale_counts(local_count)):
        start = len(local_indices)
        for trial in _coordinate_trials(anchor, order, step, radius, problem, generation):
            if len(local_indices) - start == count:
                break
            accept(trial, 'best_local', local_indices)
        # When a bound or past measurements exhaust coordinate trials, use
        # deterministic two-axis patterns at the same scale to fill the pool.
        for _ in range(max(1, count) * 200):
            if len(local_indices) - start == count:
                break
            axes = pattern_rng.choice(len(bridge.NAMES), size=2, replace=False).tolist()
            signs = pattern_rng.choice([-1, 1], size=2).tolist()
            trial = _canonical_trial(anchor, axes, signs, step, radius, problem, 'pattern')
            accept(trial, 'best_local', local_indices)
        if len(local_indices) - start != count:
            _fail('Insufficient distinct directional trials at step scale ' + str(step) + '.')
    for _ in range(max(1, global_count) * 200):
        if len(global_indices) == global_count:
            break
        z = global_rng.random(len(bridge.NAMES))
        raw = [variable['lower'] + float(value) * (variable['upper'] - variable['lower'])
               for value, variable in zip(z, problem['variables'])]
        x = bridge.ordered_values(dict(zip(bridge.NAMES, raw)), problem)
        accept((x, None), 'global_uniform', global_indices)
    if len(global_indices) != global_count or len(pool) != cfg['pool_size']:
        _fail('Insufficient distinct global trials for the configured pool.')
    return {'pool': pool, 'pool_origins': origins, 'local_state': state,
        'protected_center_index': protected, 'local_indices': local_indices,
        'global_indices': global_indices, 'fallback_local_indices': list(local_indices),
        'local_candidate_details': details,
        'center_status': 'duplicate_replaced_by_best_local' if duplicate else 'protected_unmeasured',
        'selection_quota': {'llm_center': protected_count, 'best_local': local_slots,
                            'global_uniform': global_slots},
        'pool_group_sizes': {'llm_center': protected_count, 'best_local': local_count,
                            'global_uniform': global_count},
        'global_schedule': dict(state['global_schedule']),
        'rng_streams': {'seed': cfg['seed'], 'generation': generation,
                        'sparse_pattern': _PATTERN_STREAM, 'global_uniform': _GLOBAL_STREAM}}
