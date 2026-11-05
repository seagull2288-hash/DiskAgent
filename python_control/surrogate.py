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


def fit_validated(history, problem, cfg):
    report = {'enabled': False, 'measured_samples': len(history), 'predictions_are_measurements': False}
    if len(history) < cfg['surrogate_min_samples']:
        report['reason'] = 'insufficient_measured_samples'
        return None, report
    # Keep an evenly spaced chronological subset; do not train only on good points.
    indices = np.unique(np.linspace(0, len(history)-1, min(len(history), cfg['surrogate_max_samples']), dtype=int))
    x = normalize([history_x(history)[i] for i in indices], problem)
    y = np.array([history[i]['stress_mpa'] for i in indices], dtype=float)
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
        if cv_nrmse > cfg['surrogate_max_cv_nrmse'] or max(cv_rmse, holdout_rmse) > cfg['surrogate_max_rmse_mpa']:
            report['reason'] = 'validation_error_above_limit'
            return None, report
        gp = GaussianProcess(length).fit(x, y)
        report.update(enabled=True, reason='cross_validation_and_forward_holdout_passed')
        return gp, report
    except (np.linalg.LinAlgError, FloatingPointError, ValueError) as exc:
        report.update(reason='numerical_fit_failure', error=type(exc).__name__)
        return None, report


def select_pool(pool, history, problem, cfg, *, enabled=True):
    from .sampling import diverse_indices
    model, report = fit_validated(history, problem, cfg) if enabled else (None, {'enabled': False, 'reason': 'center_only_strategy'})
    z = normalize(pool, problem)
    if model is None:
        chosen = diverse_indices(pool, history, problem, cfg['batch_size'])
        return chosen, dict(report, selection='space_coverage_without_surrogate'), []
    mean, std = model.predict(z)
    lcb = mean-cfg['surrogate_uncertainty_weight']*std
    measured = normalize(history_x(history), problem)
    distance = np.sqrt(((z[:, None, :]-measured[None, :, :])**2).sum(axis=2).min(axis=1))
    # Always reserve a portion for exploration; predictions cannot veto all
    # uncertain designs. Exploitation is restricted to a neighborhood of data.
    trusted = [int(i) for i in np.argsort(lcb) if distance[i] <= cfg['surrogate_max_distance']]
    n_exploit = cfg['batch_size']-cfg['exploration_slots']
    chosen = trusted[:n_exploit]
    for i in diverse_indices(pool, history, problem, len(pool)):
        if i not in chosen:
            chosen.append(i)
        if len(chosen) == cfg['batch_size']:
            break
    predictions = [{'pool_index': i, 'predicted_stress_mpa': float(mean[i]), 'predictive_std_mpa': float(std[i]),
                    'lower_confidence_bound_mpa': float(lcb[i]), 'distance_to_measurements': float(distance[i])}
                   for i in range(len(pool))]
    return chosen, dict(report, selection='lower_confidence_bound_plus_reserved_exploration',
                         exploitation_selected=min(len(trusted), n_exploit)), predictions
