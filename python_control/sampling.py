"""Deterministic normalized-space sampling with reflection, rounding and deduplication."""
from __future__ import annotations

import hashlib
import json
import numpy as np

from .runtime import ControllerError, bridge


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def bounds(problem):
    return np.array([[v['lower'], v['upper']] for v in problem['variables']], dtype=float).T


def normalize(xs, problem):
    low, high = bounds(problem)
    return (np.asarray(xs, dtype=float)-low)/(high-low)


def history_x(history):
    return [[row['parameters'][name] for name in bridge.NAMES] for row in history]


def unique_design(z, problem, occupied):
    low, high = bounds(problem)
    # Reflection avoids the point masses caused by clipping near variable bounds.
    z = 1-np.abs(np.mod(np.asarray(z), 2)-1)
    raw = low + z*(high-low)
    x = bridge.ordered_values(dict(zip(bridge.NAMES, map(float, raw))), problem)
    if any(bridge.normalized_distance2(x, old, problem) <= problem['min_normalized_distance']**2 for old in occupied):
        return None
    return x


def latin_hypercube(count, problem, seed):
    rng = np.random.default_rng(seed)
    grid = np.column_stack([(rng.permutation(count)+rng.random(count))/count for _ in bridge.NAMES])
    result = []
    for z in grid:
        x = unique_design(z, problem, result)
        if x is None:
            raise ControllerError('LHS quantization produced a duplicate; change its seed or size.')
        result.append(x)
    return result


def candidate_pool(center, problem, history, config, generation):
    rng = np.random.default_rng(np.random.SeedSequence([config['seed'], generation, 917]))
    sigma = max(config['sigma_min'], config['sigma_start']*config['sigma_decay']**generation)
    occupied = history_x(history)
    result = []
    z_center = normalize(center, problem)
    for trial in range(config['pool_size']*200):
        if trial == 0:
            z = z_center
        elif rng.random() < config['global_fraction']:
            z = rng.random(9)
        else:
            z = z_center + rng.normal(0, sigma, 9)
        x = unique_design(z, problem, occupied+result)
        if x is not None:
            result.append(x)
        if len(result) == config['pool_size']:
            return result, sigma
    raise ControllerError('Could not generate enough distinct candidates; no solver was launched.')


def diverse_indices(pool, history, problem, count):
    z = normalize(pool, problem)
    measured = normalize(history_x(history), problem)
    chosen = []
    for _ in range(count):
        ref = np.vstack([measured, z[chosen]]) if chosen else measured
        distances = ((z[:, None, :]-ref[None, :, :])**2).sum(axis=2).min(axis=1)
        distances[chosen] = -1
        chosen.append(int(np.argmax(distances)))
    return chosen
