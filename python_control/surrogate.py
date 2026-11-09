"""Small-data NumPy Gaussian process. Predictions never become measured history."""
from __future__ import annotations

import numpy as np

from .sampling import history_x, normalize


class GaussianProcess:
    def __init__(self, length_scale):
        self.length_scale = length_scale

    def kernel(self, a, b):
        d = ((a[:, None, :]-b[None, :, :])**2).sum(axis=2)
        return np.exp(-.5*d/self.length_scale**2)

    def fit(self, x, y):
        self.x = np.asarray(x, dtype=float)
        self.mean = float(np.mean(y))
        self.scale = max(float(np.std(y)), 1e-6)
        self.chol = np.linalg.cholesky(self.kernel(self.x, self.x)+np.eye(len(x))*1e-6)
        self.alpha = np.linalg.solve(self.chol.T, np.linalg.solve(self.chol, (y-self.mean)/self.scale))
        return self

    def predict(self, x):
        k = self.kernel(np.asarray(x), self.x)
        mean = self.mean+self.scale*(k@self.alpha)
        v = np.linalg.solve(self.chol, k.T)
        std = self.scale*np.sqrt(np.maximum(1-(v*v).sum(axis=0), 1e-10))
        return mean, std


def fit_validated(history, problem, cfg, *, metric='stress_mpa', maximum_rmse=None):
    report = {'enabled': False, 'measured_samples': len(history), 'predictions_are_measurements': False}
    if len(history) < cfg['surrogate_min_samples']:
        report['reason'] = 'insufficient_measured_samples'
        return None, report
    # Keep an evenly spaced chronological subset; do not train only on good points.
    indices = np.unique(np.linspace(0, len(history)-1, min(len(history), cfg['surrogate_max_samples']), dtype=int))
    x = normalize([history_x(history)[i] for i in indices], problem)
    if any(history[i].get(metric) is None for i in indices):
        report['reason'] = 'missing_metric_measurements'
        return None, report
    y = np.array([history[i][metric] for i in indices], dtype=float)
    report['training_history_indices_0based'] = indices.tolist()
    if not np.all(np.isfinite(y)) or np.std(y) < 1e-6:
        report['reason'] = 'nonfinite_or_uninformative_objectives'
        return None, report
    # Hyperparameter selection uses only the earlier 80%; latest 20% is a separate
    # forward validation set, never used to choose length scale.
    cut = max(cfg['surrogate_folds']+2, int(len(y)*.8))
    order = np.random.default_rng(cfg['seed']).permutation(cut)
    folds = np.array_split(order, cfg['surrogate_folds'])
    scores = []
    try:
        for length in cfg['surrogate_length_scales']:
            prediction = np.empty(cut)
            for valid in folds:
                train = np.setdiff1d(np.arange(cut), valid)
                gp = GaussianProcess(length).fit(x[train], y[train])
                prediction[valid] = gp.predict(x[valid])[0]
            rmse = float(np.sqrt(np.mean((prediction-y[:cut])**2)))
            scores.append((rmse, length))
        cv_rmse, length = min(scores)
        holdout = GaussianProcess(length).fit(x[:cut], y[:cut]).predict(x[cut:])[0]
        holdout_rmse = float(np.sqrt(np.mean((holdout-y[cut:])**2)))
        cv_nrmse = cv_rmse/max(float(np.std(y[:cut])), 1e-6)
        report.update(cv_rmse_mpa=cv_rmse, cv_nrmse=cv_nrmse, forward_holdout_rmse_mpa=holdout_rmse,
                      length_scale=length, fit_samples=cut, forward_holdout_samples=len(y)-cut)
        limit = cfg['surrogate_max_rmse_mpa'] if maximum_rmse is None else maximum_rmse
        if metric != 'stress_mpa':
            report.update(target_metric=metric, maximum_rmse=limit,
                          cv_rmse=cv_rmse, forward_holdout_rmse=holdout_rmse)
            report.pop('cv_rmse_mpa', None)
            report.pop('forward_holdout_rmse_mpa', None)
        if cv_nrmse > cfg['surrogate_max_cv_nrmse'] or max(cv_rmse, holdout_rmse) > limit:
            report['reason'] = 'validation_error_above_limit'
            return None, report
        gp = GaussianProcess(length).fit(x, y)
        report.update(enabled=True, reason='cross_validation_and_forward_holdout_passed')
        return gp, report
    except (np.linalg.LinAlgError, FloatingPointError, ValueError) as exc:
        report.update(reason='numerical_fit_failure', error=type(exc).__name__)
        return None, report


def _fit_selection_model(history, problem, cfg, **kwargs):
    if cfg.get('selection_policy') == 'hybrid_local_v3':
        from .local_surrogate import fit_local_validated
        return fit_local_validated(history, problem, cfg, **kwargs)
    return fit_validated(history, problem, cfg, **kwargs)


def _support_mask(model, z):
    if hasattr(model, 'support_mask'):
        support = np.asarray(model.support_mask(z), dtype=bool)
        if support.shape != (len(z),):
            from .runtime import ControllerError
            raise ControllerError('Invalid local GP support mask.')
        return support
    return np.ones(len(z), dtype=bool)


def rank_pool(pool, history, problem, cfg, *, enabled=True):
    """Return trusted acquisition order and explicitly labeled prediction units."""
    if problem.get('optimization_policy') is not None:
        return rank_policy_pool(pool, history, problem, cfg, enabled=enabled)
    model, report = _fit_selection_model(history, problem, cfg) if enabled else (None, {'enabled': False, 'reason': 'center_only_strategy'})
    if model is None:
        return [], report, []
    z = normalize(pool, problem)
    mean, std = model.predict(z)
    if (np.shape(mean) != (len(pool),) or np.shape(std) != (len(pool),)
            or not np.all(np.isfinite(mean)) or not np.all(np.isfinite(std)) or np.any(std < 0)):
        from .runtime import ControllerError
        raise ControllerError('Invalid GP predictions.')
    lcb = mean-cfg['surrogate_uncertainty_weight']*std
    measured = normalize(history_x(history), problem)
    distance = np.sqrt(((z[:, None, :]-measured[None, :, :])**2).sum(axis=2).min(axis=1))
    # Always reserve a portion for exploration; predictions cannot veto all
    # uncertain designs. Exploitation is restricted to a neighborhood of data.
    support = _support_mask(model, z)
    trusted = [int(i) for i in np.argsort(lcb) if support[i] and distance[i] <= cfg['surrogate_max_distance']]
    predictions = [{'pool_index': i, 'predicted_stress_mpa': float(mean[i]), 'predictive_std_mpa': float(std[i]),
                    'lower_confidence_bound_mpa': float(lcb[i]), 'distance_to_measurements': float(distance[i]),
                    'within_validated_region': bool(support[i])}
                   for i in range(len(pool))]
    return trusted, report, predictions


def rank_policy_pool(pool, history, problem, cfg, *, enabled=True):
    from .objectives import METRICS, evaluate, rank_key, validate_policy
    policy = validate_policy(problem['optimization_policy'])
    required = sorted({item['metric'] for item in policy['objectives'] + policy['constraints']})
    reports, models = {}, {}
    for metric in required:
        baseline = history[0].get(metric)
        limit = cfg['surrogate_max_rmse_mpa'] if metric == 'stress_mpa' else (baseline or 0) * 0.05
        model, report = _fit_selection_model(history, problem, cfg, metric=metric, maximum_rmse=limit) if enabled else (
            None, {'enabled': False, 'reason': 'center_only_strategy'})
        reports[metric] = dict(report, unit=METRICS[metric]['unit'], maximum_rmse=limit)
        if model is not None:
            models[metric] = model
    ready = len(models) == len(required)
    report = {'enabled': ready, 'measured_samples': len(history), 'metric_models': reports,
              'predictions_are_measurements': False,
              'reason': 'all_metric_models_validated' if ready else next(
                  item['reason'] for item in reports.values() if not item['enabled'])}
    if not ready:
        return [], report, []
    z = normalize(pool, problem)
    support = np.logical_and.reduce([_support_mask(model, z) for model in models.values()])
    model_values = {metric: model.predict(z) for metric, model in models.items()}
    measured = normalize(history_x(history), problem)
    distance = np.sqrt(((z[:, None, :] - measured[None, :, :])**2).sum(axis=2).min(axis=1))
    predictions, ranks = [], []
    for index in range(len(pool)):
        means, optimistic, metric_predictions = {}, {}, {}
        for metric, (mean, std) in model_values.items():
            mu, sigma = float(mean[index]), float(std[index])
            if not np.isfinite(mu) or not np.isfinite(sigma) or sigma < 0:
                from .runtime import ControllerError
                raise ControllerError('Invalid multi-metric GP predictions.')
            means[metric] = max(1e-12, mu)
            objective = next((item for item in policy['objectives'] if item['metric'] == metric), None)
            shift = cfg['surrogate_uncertainty_weight'] * sigma
            optimistic[metric] = max(1e-12, mu + shift if objective and objective['direction'] == 'maximize' else mu - shift)
            metric_predictions[metric] = {'mean': mu, 'std': sigma, 'unit': METRICS[metric]['unit']}
        result = evaluate(means, history[0], policy)
        objective_policy = dict(policy, constraints=[])
        result['score'] = evaluate(optimistic, history[0], objective_policy)['score']
        ranks.append(rank_key(result))
        predictions.append({'pool_index': index, 'metric_predictions': metric_predictions,
                            'predicted_constraint_status': result['status'], 'acquisition_score': result['score'],
                            'distance_to_measurements': float(distance[index]),
                            'within_validated_region': bool(support[index]), 'predictions_are_measurements': False})
    order = sorted(range(len(pool)), key=lambda index: (ranks[index], index))
    trusted = [index for index in order if support[index] and distance[index] <= cfg['surrogate_max_distance']]
    return trusted, report, predictions


def select_pool(pool, history, problem, cfg, *, enabled=True):
    from .sampling import diverse_indices
    trusted, report, predictions = rank_pool(pool, history, problem, cfg, enabled=enabled)
    n_exploit = cfg['batch_size'] - cfg['exploration_slots']
    chosen = trusted[:n_exploit]
    for index in diverse_indices(pool, history, problem, len(pool)):
        if index not in chosen:
            chosen.append(index)
        if len(chosen) == cfg['batch_size']:
            break
    selection = 'lower_confidence_bound_plus_reserved_exploration' if report['enabled'] else 'space_coverage_without_surrogate'
    return chosen, dict(report, selection=selection, exploitation_selected=min(len(trusted), n_exploit)), predictions


def select_hybrid_pool(grouped, history, problem, cfg, *, enabled=True):
    """Rank within explicit groups; neither GP nor maximin may erase quotas.

    A new LLM center is protected, remaining utilization slots come only from
    the incumbent box, and exploration slots come only from the global group.
    The same groups are respected when validation fails. Predictions remain
    diagnostics, never observations.
    """
    from .runtime import ControllerError
    from .sampling import diverse_indices

    pool, origins = grouped['pool'], grouped['pool_origins']
    local, global_ = grouped['local_indices'], grouped['global_indices']
    protected = grouped['protected_center_index']
    all_indices = list(local) + list(global_) + ([] if protected is None else [protected])
    if (len(pool) != len(origins) or len(all_indices) != len(pool)
            or any(type(i) is not int or not 0 <= i < len(pool) for i in all_indices)
            or len(set(all_indices)) != len(pool)
            or any(origins[i] != 'best_local' for i in local)
            or any(origins[i] != 'global_uniform' for i in global_)
            or protected is not None and origins[protected] != 'llm_center'):
        raise ControllerError('Malformed hybrid candidate groups.')
    directional = cfg.get('selection_policy') == 'hybrid_local_v3'
    n_global = grouped['selection_quota']['global_uniform'] if directional else cfg['exploration_slots']
    if type(n_global) is not int or not 0 <= n_global <= cfg['exploration_slots']:
        raise ControllerError('Invalid scheduled global exploration quota.')
    n_local = cfg['batch_size'] - n_global - int(protected is not None)
    if n_local < 0 or len(local) < n_local or len(global_) < n_global:
        raise ControllerError('Candidate groups cannot satisfy explicit hybrid quotas.')

    trusted, report, predictions = rank_pool(pool, history, problem, cfg, enabled=enabled)
    chosen = [] if protected is None else [protected]
    directional_chosen = []
    if directional:
        fallback = grouped.get('fallback_local_indices')
        if (not isinstance(fallback, list) or len(fallback) != len(local)
                or len(set(fallback)) != len(local) or set(fallback) != set(local)):
            raise ControllerError('Directional fallback must order every local candidate exactly once.')
        # Keep one measured poll direction even while GP ranking is active.
        directional_chosen = fallback[:min(1, n_local)]
        chosen.extend(directional_chosen)
    ranked_local = [index for index in trusted if index in local and index not in chosen][:n_local-len(directional_chosen)]
    chosen.extend(ranked_local)

    # Diversity is restricted to the local box; it can no longer prefer a
    # far-away global point over the guaranteed incumbent-neighborhood slots.
    local_remaining = [i for i in local if i not in chosen]
    missing = n_local - len(ranked_local) - len(directional_chosen)
    if missing:
        if directional:
            additional = [index for index in fallback if index not in chosen][:missing]
            directional_chosen.extend(additional)
            chosen.extend(additional)
        else:
            references = history + [{'parameters': dict(zip(problem['input_order'], pool[i]))}
                                    for i in chosen]
            order = diverse_indices([pool[i] for i in local_remaining], references, problem, missing)
            chosen.extend(local_remaining[i] for i in order)
    if n_global:
        references = history + [{'parameters': dict(zip(problem['input_order'], pool[i]))}
                                for i in chosen]
        order = diverse_indices([pool[i] for i in global_], references, problem, n_global)
        chosen.extend(global_[i] for i in order)
    if len(chosen) != cfg['batch_size'] or len(set(chosen)) != len(chosen):
        raise ControllerError('Hybrid selector violated its committed batch size.')
    if not report['enabled']:
        selection_reason = report['reason']
    elif n_local == len(directional_chosen):
        selection_reason = 'no_local_ranking_slots'
    elif not ranked_local:
        selection_reason = 'no_trusted_local_candidates'
    else:
        selection_reason = 'surrogate_ranked_trusted_local_candidates'
    selection = 'hybrid_local_quotas_with_surrogate' if ranked_local else 'hybrid_local_quotas_without_surrogate'
    if directional:
        selection = 'directional_local_with_surrogate' if ranked_local else 'directional_local_without_surrogate'
    return chosen, dict(report,
        selection=selection,
        selection_reason=selection_reason,
        surrogate_used=bool(ranked_local), exploitation_selected=len(ranked_local),
        protected_center_selected=protected is not None,
        directional_selected=len(directional_chosen),
        actual_origin_counts={name: sum(origins[i] == name for i in chosen)
                              for name in ('llm_center', 'best_local', 'global_uniform')}), predictions
