"""Train-only local Matérn GP validation and read-only historical comparisons.

Every temporal fold defines its neighborhood from its training prefix. The
last block is used only for acceptance, never for kernel, support or uncertainty
calibration choices. Predictions remain separate from measured history.
"""
from __future__ import annotations

import math

try:
    import numpy as np
except ImportError:  # Numerical diagnostics should remain importable offline.
    np = None


MODEL_VERSION = 'numpy-matern52-ard-local.v1'
MINIMUM_LOCAL_SAMPLES = 24
MINIMUM_ROLLING_TRAINING_SAMPLES = 12
MINIMUM_VALIDATION_SAMPLES = 8
MINIMUM_FORWARD_INTERVAL_COVERAGE = .80
MINIMUM_FORWARD_RANK_CORRELATION = .30
OPTIMIZER_EVALUATIONS = 61
METRIC_UNITS = {'stress_mpa': 'MPa', 'mass_kg': 'kg', 'frequency_hz': 'Hz'}


class MaternARDGaussianProcess:
    """Unit-amplitude Matérn 5/2 with separate lengths and learned noise.

    A bounded deterministic coordinate search maximizes training likelihood.
    Noise is an observation standard deviation in standardized target units.
    Predictive standard deviations include that learned observation noise.
    """

    def __init__(self, *, evaluations=OPTIMIZER_EVALUATIONS):
        self.evaluations = evaluations

    @staticmethod
    def _kernel(a, b, lengths):
        distance = np.sqrt(np.maximum(
            (((a[:, None, :] - b[None, :, :]) / lengths) ** 2).sum(axis=2), 0.0))
        scaled = math.sqrt(5.0) * distance
        return (1.0 + scaled + scaled ** 2 / 3.0) * np.exp(-scaled)

    def fit(self, x, y):
        self.x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        self.mean = float(np.mean(y))
        self.scale = max(float(np.std(y)), 1e-6)
        target = (y - self.mean) / self.scale
        dimensions = self.x.shape[1]
        lower = np.log(np.r_[np.full(dimensions, .02), .001])
        upper = np.log(np.r_[np.full(dimensions, 5.0), .5])
        parameters = np.log(np.r_[np.full(dimensions, .35), .03])
        eye = np.eye(len(y))

        def likelihood(candidate):
            lengths, noise = np.exp(candidate[:-1]), float(np.exp(candidate[-1]))
            covariance = self._kernel(self.x, self.x, lengths) + eye * (noise ** 2 + 1e-10)
            try:
                chol = np.linalg.cholesky(covariance)
                whitened = np.linalg.solve(chol, target)
                return float(.5 * whitened @ whitened + np.log(np.diag(chol)).sum())
            except np.linalg.LinAlgError:
                return math.inf

        score, evaluations = likelihood(parameters), 1
        # Coordinate directions and budgets are fixed before seeing any holdout.
        for step in (math.log(4.0), math.log(2.0), math.log(2.0) / 2.0):
            for axis in range(dimensions + 1):
                candidates = []
                for direction in (-1.0, 1.0):
                    if evaluations >= self.evaluations:
                        break
                    candidate = parameters.copy()
                    candidate[axis] = np.clip(candidate[axis] + direction * step, lower[axis], upper[axis])
                    candidates.append((likelihood(candidate), candidate))
                    evaluations += 1
                for candidate_score, candidate in candidates:
                    if candidate_score < score:
                        score, parameters = candidate_score, candidate
        self.length_scales = np.exp(parameters[:-1])
        self.noise_std = float(np.exp(parameters[-1]))
        self.optimization_evaluations = evaluations
        self.training_negative_log_likelihood = score
        covariance = self._kernel(self.x, self.x, self.length_scales) + eye * (self.noise_std ** 2 + 1e-10)
        self.chol = np.linalg.cholesky(covariance)
        self.alpha = np.linalg.solve(self.chol.T, np.linalg.solve(self.chol, target))
        return self

    def predict(self, x):
        values = np.asarray(x, dtype=float)
        covariance = self._kernel(values, self.x, self.length_scales)
        mean = self.mean + self.scale * (covariance @ self.alpha)
        whitened = np.linalg.solve(self.chol, covariance.T)
        variance = np.maximum(1.0 + self.noise_std ** 2 - (whitened ** 2).sum(axis=0), 1e-10)
        return mean, self.scale * np.sqrt(variance)


class CalibratedLocalModel:
    """A frozen validated box, an explicit support mask and OOF-calibrated std."""

    def __init__(self, model, support, calibration):
        self.model = model
        self.support = support
        self.calibration = calibration

    def support_mask(self, z_normalized):
        z = np.asarray(z_normalized, dtype=float)
        inside = _inside_box(z, self.support)
        distance = _nearest_distance(z, self.model.x)
        return inside & (distance <= self.support['maximum_nearest_distance'])

    def predict(self, z_normalized):
        mean, std = self.model.predict(z_normalized)
        return mean, _calibrated_std(std, self.calibration)


def _base_report(history, metric):
    return {'enabled': False, 'reason': 'unavailable', 'model_version': MODEL_VERSION,
            'measured_samples': len(history), 'target_metric': metric,
            'unit': METRIC_UNITS.get(metric), 'predictions_are_measurements': False,
            'minimum_local_training_samples': MINIMUM_LOCAL_SAMPLES,
            'minimum_rolling_training_samples': MINIMUM_ROLLING_TRAINING_SAMPLES,
            'minimum_validation_samples': MINIMUM_VALIDATION_SAMPLES,
            'minimum_forward_interval_coverage': MINIMUM_FORWARD_INTERVAL_COVERAGE,
            'minimum_forward_rank_correlation': MINIMUM_FORWARD_RANK_CORRELATION,
            'validation_design': 'nested_expanding_prefix_then_untuned_forward_holdout',
            'backend': 'numpy', 'kernel': 'matern_5_2_ard_with_learned_noise',
            'independent_future_confirmation': False}


def _prepare(history, problem, cfg, metric):
    if metric not in METRIC_UNITS:
        raise ValueError('Unsupported target metric.')
    names = problem['input_order']
    if [v['name'] for v in problem['variables']] != names:
        raise ValueError('Variable bounds must follow input_order.')
    bounds = np.array([[v['lower'], v['upper']] for v in problem['variables']], dtype=float)
    if not np.all(np.isfinite(bounds)) or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError('Invalid variable bounds.')
    if any(row.get(metric) is None for row in history):
        raise LookupError('missing_metric_measurements')
    values = [[row['parameters'][name] for name in names] for row in history]
    if any(type(value) not in (int, float) for row in values for value in row):
        raise ValueError('Coordinates must be finite numeric measurements.')
    if any(type(row[metric]) not in (int, float) for row in history):
        raise ValueError('Metric measurements must be finite numbers.')
    x = (np.array(values, dtype=float) - bounds[:, 0]) / (bounds[:, 1] - bounds[:, 0])
    y = np.array([row[metric] for row in history], dtype=float)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)) or np.any(y <= 0):
        raise ValueError('Measurements must be finite and target values positive.')
    if np.any(x < -1e-10) or np.any(x > 1.0 + 1e-10):
        raise ValueError('Measured coordinates fall outside problem bounds.')
    maximum = int(cfg.get('surrogate_max_samples', 160))
    if maximum < MINIMUM_LOCAL_SAMPLES + MINIMUM_VALIDATION_SAMPLES:
        raise ValueError('Sample cap cannot support independent validation.')
    indices = np.unique(np.linspace(0, len(history) - 1, min(len(history), maximum), dtype=int))
    return x, y, indices


def _maximum_error(history, metric, cfg, maximum_rmse):
    if maximum_rmse is not None:
        limit = maximum_rmse
    elif metric == 'stress_mpa':
        limit = cfg.get('surrogate_max_rmse_mpa', 15.0)
    else:
        # Match the existing policy surrogate's baseline-relative metric limit.
        limit = history[0].get(metric, 0) * .05
    if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
        raise ValueError('A positive finite RMSE limit in target units is required.')
    return min(float(limit), 15.0) if metric == 'stress_mpa' else float(limit)


def _support(history, x, indices, stop, problem, cfg):
    """Freeze the anchor from the chronological prefix, including all policy metrics."""
    prefix = history[:int(indices[stop - 1]) + 1]
    if problem.get('optimization_policy') is not None:
        from .objectives import archive
        anchor_index = archive(prefix, problem['optimization_policy'])['anchor_iteration']
    else:
        anchor_index = min(range(len(prefix)), key=lambda i: prefix[i]['stress_mpa'])
    names = problem['input_order']
    bounds = np.array([[v['lower'], v['upper']] for v in problem['variables']], dtype=float)
    anchor = (np.array([prefix[anchor_index]['parameters'][name] for name in names]) - bounds[:, 0]) / (bounds[:, 1] - bounds[:, 0])
    radius = float(cfg.get('sigma_start', .08))
    minimum_radius = float(cfg.get('sigma_min', .02))
    distance = float(cfg.get('surrogate_max_distance', .6))
    if not 0 < minimum_radius <= radius <= 1 or not math.isfinite(distance) or distance <= 0:
        raise ValueError('Invalid local neighborhood settings.')
    return {'geometry': 'linf_box', 'anchor_normalized': anchor.tolist(),
            'anchor_training_history_index_0based': int(anchor_index),
            'radius_fraction': radius, 'radius_source': 'preconfigured_sigma_start',
            'lower_normalized': np.maximum(anchor - radius, 0.0).tolist(),
            'upper_normalized': np.minimum(anchor + radius, 1.0).tolist(),
            'maximum_nearest_distance': min(distance, radius * math.sqrt(len(names))),
            'definition_uses_validation_targets': False}


def _inside_box(x, support):
    return np.all((x >= np.array(support['lower_normalized']) - 1e-12)
                  & (x <= np.array(support['upper_normalized']) + 1e-12), axis=1)


def _nearest_distance(x, measured):
    return np.sqrt(((x[:, None, :] - measured[None, :, :]) ** 2).sum(axis=2).min(axis=1))


def _rolling_splits(stop, minimum, folds):
    start = max(minimum, stop // 2)
    if stop <= start:
        return []
    edges = np.unique(np.linspace(start, stop, min(folds, stop - start) + 1, dtype=int))
    return [(int(a), int(b)) for a, b in zip(edges[:-1], edges[1:])]


def _fit_backend(kind, x, y, cfg):
    if kind != 'rbf_global':
        return MaternARDGaussianProcess().fit(x, y)
    from .surrogate import GaussianProcess
    # Reproduce the current RBF's shuffled training-only length selection.
    folds = np.array_split(np.random.default_rng(cfg.get('seed', 20261029)).permutation(len(y)),
                           min(int(cfg.get('surrogate_folds', 4)), len(y) - 1))
    choices = []
    for length in cfg.get('surrogate_length_scales', [.15, .35, .7]):
        predictions = np.empty(len(y))
        for valid in folds:
            train = np.setdiff1d(np.arange(len(y)), valid)
            predictions[valid] = GaussianProcess(length).fit(x[train], y[train]).predict(x[valid])[0]
        choices.append((float(np.mean((predictions - y) ** 2)), length))
    return GaussianProcess(min(choices)[1]).fit(x, y)


def _calibration(errors, std):
    # The empirical 95th residual quantile and RMSE act as absolute floors.
    # This is conservative OOF calibration, not a certified coverage guarantee.
    absolute = np.abs(errors)
    rmse = float(np.sqrt(np.mean(errors ** 2)))
    quantile = float(np.quantile(absolute, .95, method='higher'))
    ratio = absolute / np.maximum(1.96 * std, 1e-12)
    multiplier = max(1.0, float(np.quantile(ratio, .95, method='higher')))
    floor = max(rmse, quantile / 1.96, 1e-9)
    calibrated = np.maximum(std * multiplier, floor)
    return {'method': 'rolling_oof_residual_inflation_and_floor', 'samples': len(errors),
            'std_multiplier': multiplier, 'std_floor': floor, 'nominal_coverage': .95,
            'empirical_oof_coverage': float(np.mean(absolute <= 1.96 * calibrated)),
            'calibration_uses_forward_holdout': False, 'coverage_is_certified': False,
            'std_floor_unit': 'target_metric_unit', 'raw_oof_rmse': rmse}


def _calibrated_std(std, calibration):
    return np.maximum(np.asarray(std) * calibration['std_multiplier'], calibration['std_floor'])


def _errors(actual, predicted):
    residual = actual - predicted
    return {'samples': len(actual), 'rmse': float(np.sqrt(np.mean(residual ** 2))),
            'mae': float(np.mean(np.abs(residual))), 'signed_bias': float(np.mean(residual)),
            'maximum_absolute_error': float(np.max(np.abs(residual))),
            'squared_error_sum': float(residual @ residual)}


def _rank_correlation(actual, predicted):
    """Spearman correlation with average ranks for equal measured values."""
    def ranks(values):
        _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
        ends = np.cumsum(counts)
        average = (ends - counts + ends - 1) / 2.0
        return average[inverse]

    a, b = ranks(actual), ranks(predicted)
    if np.std(a) == 0 or np.std(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _stratified(actual, predicted, distances, origins):
    by_origin, by_distance = {}, {}
    for origin in sorted(set(origins)):
        mask = np.array([item == origin for item in origins])
        by_origin[origin] = _errors(actual[mask], predicted[mask])
    for label, lower, upper in (('0_to_0.1', -1.0, .1), ('0.1_to_0.3', .1, .3),
                                ('0.3_to_0.6', .3, .6), ('above_0.6', .6, math.inf)):
        mask = (distances > lower) & (distances <= upper)
        if np.any(mask):
            by_distance[label] = _errors(actual[mask], predicted[mask])
    return by_origin, by_distance


def _duplicate_spread(x, y):
    groups = {}
    for coordinates, target in zip(x, y):
        groups.setdefault(tuple(coordinates), []).append(float(target))
    duplicated = [values for values in groups.values() if len(values) > 1]
    return {'exact_duplicate_groups': len(duplicated),
            'exact_duplicate_observations': sum(map(len, duplicated)),
            'maximum_target_range': max((max(v) - min(v) for v in duplicated), default=0.0),
            'interpretation': 'repeat_spread_only_not_estimated_solver_noise'}


def _validate(history, problem, cfg, metric, maximum_rmse, kind, origins=None, *, refit=True):
    report = _base_report(history, metric)
    report.update(kernel='rbf_isotropic' if kind == 'rbf_global' else 'matern_5_2_ard_with_learned_noise',
                  model_scope='local' if kind == 'matern_ard_local' else 'global')
    if np is None:
        report['reason'] = 'numpy_unavailable'
        return None, report
    if len(history) < MINIMUM_LOCAL_SAMPLES + MINIMUM_VALIDATION_SAMPLES:
        report['reason'] = 'insufficient_measured_samples_for_forward_validation'
        return None, report
    try:
        all_x, all_y, indices = _prepare(history, problem, cfg, metric)
        x, y = all_x[indices], all_y[indices]
        limit = _maximum_error(history, metric, cfg, maximum_rmse)
        configured_nrmse = float(cfg.get('surrogate_max_cv_nrmse', .5))
        if not math.isfinite(configured_nrmse) or configured_nrmse <= 0:
            raise ValueError('NRMSE limit must be positive and finite.')
        nrmse_limit = min(configured_nrmse, .5)
        minimum = max(MINIMUM_LOCAL_SAMPLES, int(cfg.get('surrogate_min_samples', 24)))
        cut = len(y) - max(MINIMUM_VALIDATION_SAMPLES, math.ceil(len(y) * .2))
        report.update(maximum_rmse=limit, maximum_cv_nrmse=nrmse_limit,
                      prefix_history_indices_0based=indices[:cut].tolist(),
                      forward_history_indices_0based=indices[cut:].tolist(),
                      training_prefix_samples=cut, forward_block_samples=len(y) - cut,
                      exact_duplicate_spread=_duplicate_spread(all_x, all_y))
        tolerance = float(problem.get('min_normalized_distance', 0.0))
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError('Duplicate tolerance must be finite and nonnegative.')
        duplicate_pairs = sum(int(np.count_nonzero(np.sum((all_x[i + 1:] - row) ** 2, axis=1)
                                                   <= tolerance ** 2))
                              for i, row in enumerate(all_x[:-1]))
        report['duplicate_design_pairs_within_tolerance'] = duplicate_pairs
        if duplicate_pairs:
            report['reason'] = 'duplicate_measured_designs'
            return None, report
        if cut < minimum:
            report['reason'] = 'insufficient_training_prefix_samples'
            return None, report
        local = kind == 'matern_ard_local'
        support = _support(history, x, indices, cut, problem, cfg) if local else None
        train_mask = _inside_box(x[:cut], support) if local else np.ones(cut, dtype=bool)
        train = np.flatnonzero(train_mask)
        report.update(fit_samples=len(train), local_training_samples=len(train),
                      unique_training_designs=len(np.unique(x[train], axis=0)),
                      training_history_indices_0based=indices[train].tolist())
        if support is not None:
            report['support'] = support
        if report['unique_training_designs'] < minimum:
            report['reason'] = 'insufficient_local_training_samples'
            return None, report
        if float(np.std(y[train])) < 1e-6:
            report['reason'] = 'uninformative_training_targets'
            return None, report
        valid = np.arange(cut, len(y))
        if local:
            valid = valid[_inside_box(x[valid], support)]
            if len(valid):
                valid = valid[_nearest_distance(x[valid], x[train]) <= support['maximum_nearest_distance']]
        report.update(forward_holdout_samples=len(valid),
                      excluded_forward_samples=len(y) - cut - len(valid),
                      evaluated_forward_history_indices_0based=indices[valid].tolist())
        if not len(valid):
            report['reason'] = 'insufficient_local_forward_validation_samples' if local else 'insufficient_forward_validation_samples'
            return None, report
        residuals, deviations, folds = [], [], []
        rolling_minimum = max(MINIMUM_ROLLING_TRAINING_SAMPLES, int(cfg.get('surrogate_folds', 4)) + 4)
        for fold_cut, fold_end in _rolling_splits(cut, rolling_minimum, int(cfg.get('surrogate_folds', 4))):
            fold_train, fold_valid = np.arange(fold_cut), np.arange(fold_cut, fold_end)
            if local:
                fold_support = _support(history, x, indices, fold_cut, problem, cfg)
                fold_train = fold_train[_inside_box(x[fold_train], fold_support)]
                fold_valid = fold_valid[_inside_box(x[fold_valid], fold_support)]
                if len(fold_train) >= rolling_minimum and len(fold_valid):
                    fold_valid = fold_valid[_nearest_distance(x[fold_valid], x[fold_train]) <= fold_support['maximum_nearest_distance']]
            entry = {'training_prefix_stop': fold_cut, 'validation_stop': fold_end,
                     'fit_samples': len(fold_train), 'validation_samples': len(fold_valid),
                     'unique_training_designs': len(np.unique(x[fold_train], axis=0))}
            if entry['unique_training_designs'] < rolling_minimum or not len(fold_valid):
                entry['status'] = 'insufficient_local_support'
                folds.append(entry)
                continue
            model = _fit_backend(kind, x[fold_train], y[fold_train], cfg)
            mean, std = model.predict(x[fold_valid])
            _check_predictions(mean, std, len(fold_valid))
            residuals.extend((y[fold_valid] - mean).tolist())
            deviations.extend(std.tolist())
            entry.update(status='evaluated', rmse=_errors(y[fold_valid], mean)['rmse'],
                         evaluated_history_indices_0based=indices[fold_valid].tolist())
            if local:
                entry['support'] = fold_support
            folds.append(entry)
        report.update(rolling_folds=folds, cv_samples=len(residuals))
        residuals, deviations = np.asarray(residuals), np.asarray(deviations)
        calibration = _calibration(residuals, deviations) if len(residuals) else None
        if calibration is not None:
            calibration['std_floor_unit'] = METRIC_UNITS[metric]
        report['uncertainty_calibration'] = calibration or {
            'available': False, 'samples': 0, 'reason': 'no_rolling_oof_residuals',
            'calibration_uses_forward_holdout': False, 'coverage_is_certified': False}
        cv_rmse = float(np.sqrt(np.mean(residuals ** 2))) if len(residuals) else None
        training_std = float(np.std(y[train]))
        cv_nrmse = cv_rmse / max(training_std, 1e-6) if cv_rmse is not None else None
        model = _fit_backend(kind, x[train], y[train], cfg)
        mean, std = model.predict(x[valid])
        _check_predictions(mean, std, len(valid))
        errors = _errors(y[valid], mean)
        rank_correlation = _rank_correlation(y[valid], mean)
        top_count = max(1, math.ceil(len(valid) / 4))
        objectives = (problem.get('optimization_policy') or {}).get('objectives', [])
        objective = next((item for item in objectives if item['metric'] == metric), None)
        direction = objective['direction'] if objective else ('maximize' if metric == 'frequency_hz' else 'minimize')
        top_indices = np.argsort(-mean if direction == 'maximize' else mean, kind='stable')[:top_count]
        regret = (np.max(y[valid]) - np.max(y[valid][top_indices]) if direction == 'maximize' else
                  np.min(y[valid][top_indices]) - np.min(y[valid]))
        ranking = {'spearman_correlation': rank_correlation, 'samples': len(valid),
                   'direction': direction, 'top_quarter_candidates': top_count,
                   'top_quarter_regret': float(regret),
                   'regret_unit': METRIC_UNITS[metric]}
        coverage = None
        if calibration is not None:
            calibrated_std = _calibrated_std(std, calibration)
            coverage = float(np.mean(np.abs(y[valid] - mean) <= 1.96 * calibrated_std))
        report.update(cv_rmse=cv_rmse, cv_nrmse=cv_nrmse,
                      cv_normalization_training_std=training_std,
                      forward_holdout_rmse=errors['rmse'], forward_errors=errors,
                      forward_ranking=ranking,
                      forward_calibrated_interval_coverage=coverage,
                      calibrated_interval_is_certified=False,
                      hyperparameters=_hyperparameters(model))
        # Global and local headline scores have different deployment scopes.
        # Also report every backend on the same prefix-defined local region.
        common_support = support or _support(history, x, indices, cut, problem, cfg)
        common_train = x[:cut][_inside_box(x[:cut], common_support)]
        common = _inside_box(x[valid], common_support)
        if len(common_train):
            common &= _nearest_distance(x[valid], common_train) <= common_support['maximum_nearest_distance']
        else:
            common[:] = False
        report['forward_common_local_support'] = (_errors(y[valid][common], mean[common])
                                                 if np.any(common) else {'samples': 0})
        if metric == 'stress_mpa':
            report.update(cv_rmse_mpa=cv_rmse, forward_holdout_rmse_mpa=errors['rmse'])
        if origins is not None:
            if len(origins) != len(history) or any(not isinstance(item, str) for item in origins):
                raise ValueError('origins must align exactly with measured history.')
            distances = _nearest_distance(x[valid], x[train])
            by_origin, by_distance = _stratified(y[valid], mean, distances, [origins[i] for i in indices[valid]])
            report.update(forward_by_origin=by_origin, forward_by_distance=by_distance)
        if len(residuals) < MINIMUM_VALIDATION_SAMPLES:
            report['reason'] = 'insufficient_rolling_validation_samples'
            return None, report
        if len(valid) < MINIMUM_VALIDATION_SAMPLES:
            report['reason'] = 'insufficient_local_forward_validation_samples' if local else 'insufficient_forward_validation_samples'
            return None, report
        if cv_nrmse > nrmse_limit or max(cv_rmse, errors['rmse']) > limit:
            report['reason'] = 'validation_error_above_limit'
            return None, report
        if coverage < MINIMUM_FORWARD_INTERVAL_COVERAGE:
            report['reason'] = 'forward_uncertainty_coverage_below_limit'
            return None, report
        if rank_correlation is None or rank_correlation < MINIMUM_FORWARD_RANK_CORRELATION:
            report['reason'] = 'forward_ranking_below_limit'
            return None, report
        if refit:
            final = np.flatnonzero(_inside_box(x, support)) if local else np.arange(len(x))
            model = _fit_backend(kind, x[final], y[final], cfg)
            report.update(local_training_samples=len(final),
                          unique_training_designs=len(np.unique(x[final], axis=0)),
                          training_history_indices_0based=indices[final].tolist(),
                          final_training_history_indices_0based=indices[final].tolist(),
                          final_hyperparameters=_hyperparameters(model))
        report.update(enabled=True, reason='rolling_cv_and_forward_holdout_passed')
        return (CalibratedLocalModel(model, support, calibration) if local else model), report
    except LookupError as exc:
        report.update(reason='missing_metric_measurements' if str(exc) == 'missing_metric_measurements'
                      else 'invalid_or_incomplete_numerical_input', error_type=type(exc).__name__)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, RuntimeError) as exc:
        report.update(reason='invalid_or_incomplete_numerical_input', error_type=type(exc).__name__)
    except (ArithmeticError, np.linalg.LinAlgError) as exc:
        report.update(reason='numerical_fit_failure', error_type=type(exc).__name__)
    return None, report


def _check_predictions(mean, std, count):
    if (np.shape(mean) != (count,) or np.shape(std) != (count,)
            or not np.all(np.isfinite(mean)) or not np.all(np.isfinite(std)) or np.any(std < 0)):
        raise ValueError('Model returned invalid predictive arrays.')


def _hyperparameters(model):
    if isinstance(model, MaternARDGaussianProcess):
        return {'length_scales': model.length_scales.tolist(),
                'noise_std_standardized': model.noise_std,
                'likelihood_evaluations': model.optimization_evaluations,
                'optimization_basis': 'training_likelihood_only'}
    return {'length_scale': model.length_scale, 'optimization_basis': 'training_only_shuffled_cv'}


def fit_local_validated(history, problem, cfg, *, metric='stress_mpa', maximum_rmse=None):
    """Return a trusted local model only after rolling and forward gates pass.

    The returned model accepts normalized coordinate arrays. Callers must also
    apply ``model.support_mask`` before using predictions for candidate ranking.
    Metric limits retain 15 MPa and NRMSE .5 caps for stress, and use explicitly
    supplied or baseline-relative limits in kg/Hz for other policy metrics.
    """
    return _validate(history, problem, cfg, metric, maximum_rmse, 'matern_ard_local')


def compare_models(history, problem, cfg, origins=None):
    """Compare three fixed designs on matching chronological blocks, read-only.

    Historical comparison does not certify an independent future experiment.
    Local forward scores explicitly include only support-eligible observations.
    No comparison winner changes production settings or historical run files.
    """
    report = {'schema_version': 'disk.surrogate-comparison.v1', 'read_only': True,
              'predictions_are_measurements': False, 'independent_future_confirmation': False,
              'selection_of_winner_changes_production': False,
              'validation_design': 'common_expanding_prefix_and_final_forward_block',
              'scope_note': 'global models evaluate all forward points; local model evaluates its frozen support only',
              'metric_models': {}}
    if origins is None:
        origins = [('baseline' if i == 0 else str(row.get('origin', 'unknown')))
                   for i, row in enumerate(history)]
    required = {'stress_mpa'}
    policy = problem.get('optimization_policy')
    if policy is not None:
        required = {item['metric'] for item in policy['objectives'] + policy['constraints']}
    for metric in sorted(required):
        report['metric_models'][metric] = {}
        for kind in ('rbf_global', 'matern_ard_global', 'matern_ard_local'):
            _, result = _validate(history, problem, cfg, metric, None, kind, origins, refit=False)
            report['metric_models'][metric][kind] = result
    # Preserve the production RBF's original shuffled-CV gate separately. Its
    # historical numbers differ from the common rolling comparison above.
    report['original_rbf_gate'] = {}
    for metric in sorted(required):
        original = {'enabled': False, 'reason': 'numpy_unavailable'}
        if np is not None:
            try:
                from .surrogate import fit_validated
                limit = _maximum_error(history, metric, cfg, None)
                _, original = fit_validated(history, problem, cfg, metric=metric, maximum_rmse=limit)
                original = dict(original, target_metric=metric, unit=METRIC_UNITS[metric],
                                validation_design='original_shuffled_cv_then_final_forward_holdout')
            except Exception as exc:
                original = {'enabled': False, 'reason': 'original_rbf_gate_unavailable',
                            'error_type': type(exc).__name__, 'target_metric': metric,
                            'unit': METRIC_UNITS.get(metric)}
        report['original_rbf_gate'][metric] = original
    return report
