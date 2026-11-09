"""Offline local GP regression tests. No API, solver or run mutations."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from python_control import local_surrogate as gp
from python_control import runtime
from python_control.objectives import default_policy
from python_control.search import DEFAULT_CONFIG


PROJECT = Path(__file__).resolve().parents[2]


class LocalSurrogateTests(unittest.TestCase):
    def setUp(self):
        self.problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        self.cfg = dict(DEFAULT_CONFIG, strategy='center_surrogate', selection_policy='hybrid_local_v3')
        self.z = np.random.default_rng(20261107).uniform(.49, .51, (80, 9))
        bounds = np.array([[v['lower'], v['upper']] for v in self.problem['variables']])
        x = bounds[:, 0] + self.z * (bounds[:, 1] - bounds[:, 0])
        self.history = []
        for normalized, coordinates in zip(self.z, x):
            value = float(10.0 * (normalized[0] - .5) + 2.0 * (normalized[1] - .5))
            self.history.append({'parameters': dict(zip(self.problem['input_order'], coordinates.tolist())),
                                 'stress_mpa': 100.0 + value, 'mass_kg': 2.0 + value * .01,
                                 'frequency_hz': 1000.0 - value * 10.0,
                                 'displacement_mm': .2, 'area_m2': .01})

    def test_matern_has_independent_train_only_lengths_and_observation_noise(self):
        x = np.random.default_rng(4).uniform(0, 1, (35, 9))
        y = 100.0 + x[:, 0]
        model = gp.MaternARDGaussianProcess().fit(x, y)
        mean, std = model.predict(x[:4])
        self.assertEqual(model.length_scales.shape, (9,))
        self.assertLess(model.length_scales[0], max(model.length_scales[1:]))
        self.assertGreater(model.noise_std, 0)
        self.assertLessEqual(model.optimization_evaluations, gp.OPTIMIZER_EVALUATIONS)
        self.assertTrue(np.all(np.isfinite(mean)))
        self.assertTrue(np.all(std > 0))

    def test_smooth_local_history_passes_and_support_mask_is_explicit(self):
        snapshot = deepcopy(self.history)
        model, report = gp.fit_local_validated(self.history, self.problem, self.cfg)
        self.assertTrue(report['enabled'], report)
        self.assertIsNotNone(model)
        self.assertEqual(self.history, snapshot)
        self.assertEqual(report['unit'], 'MPa')
        self.assertGreaterEqual(report['cv_samples'], 8)
        self.assertGreaterEqual(report['forward_holdout_samples'], 8)
        self.assertEqual(len(report['training_history_indices_0based']), 80)
        self.assertLess(report['support']['anchor_training_history_index_0based'], 64)
        self.assertTrue(model.support_mask(self.z[:1])[0])
        self.assertFalse(model.support_mask(np.ones((1, 9)))[0])
        self.assertEqual(model.support_mask(self.z[:3]).dtype, np.dtype(bool))
        _, std = model.predict(self.z[:3])
        self.assertTrue(np.all(std >= report['uncertainty_calibration']['std_floor']))
        self.assertFalse(report['uncertainty_calibration']['coverage_is_certified'])

    def test_changed_forward_labels_cannot_change_training_choices_or_calibration(self):
        _, before = gp.fit_local_validated(self.history, self.problem, self.cfg)
        changed = deepcopy(self.history)
        for point in changed[64:]:
            point['stress_mpa'] += 1000
        model, after = gp.fit_local_validated(changed, self.problem, self.cfg)
        self.assertIsNone(model)
        self.assertFalse(after['enabled'])
        self.assertEqual(after['reason'], 'validation_error_above_limit')
        self.assertEqual(before['support'], after['support'])
        self.assertEqual(before['hyperparameters'], after['hyperparameters'])
        self.assertEqual(before['uncertainty_calibration'], after['uncertainty_calibration'])
        self.assertEqual(before['cv_rmse'], after['cv_rmse'])

    def test_training_and_validation_sample_minimums_cannot_be_disabled(self):
        cfg = dict(self.cfg, surrogate_min_samples=1, surrogate_max_cv_nrmse=99,
                   surrogate_max_rmse_mpa=999)
        model, report = gp.fit_local_validated(self.history[:31], self.problem, cfg)
        self.assertIsNone(model)
        self.assertFalse(report['enabled'])
        self.assertEqual(report['minimum_local_training_samples'], 24)
        _, report = gp.fit_local_validated(self.history, self.problem, cfg, maximum_rmse=500)
        self.assertEqual(report['maximum_rmse'], 15)
        self.assertEqual(report['maximum_cv_nrmse'], .5)

    def test_forward_undercoverage_fails_even_when_absolute_error_is_small(self):
        history = deepcopy(self.history)
        for point in history[64:]:
            point['stress_mpa'] += 1.0
        model, report = gp.fit_local_validated(history, self.problem, self.cfg)
        self.assertIsNone(model)
        self.assertLess(report['forward_holdout_rmse'], 15.0)
        self.assertLess(report['cv_nrmse'], .5)
        self.assertEqual(report['reason'], 'forward_uncertainty_coverage_below_limit')
        self.assertLess(report['forward_calibrated_interval_coverage'], .8)

    def test_insufficient_rolling_support_retains_untrusted_forward_diagnostics(self):
        history = deepcopy(self.history)
        # Early designs occupy another box. A recent anchor has enough final
        # training neighbors, while earlier rolling prefixes still lack 24.
        for point in history[:33]:
            point['stress_mpa'] -= 10
            for name, variable in zip(self.problem['input_order'], self.problem['variables']):
                point['parameters'][name] += .3 * (variable['upper'] - variable['lower'])
        history[63]['stress_mpa'] = 80.0
        model, report = gp.fit_local_validated(history, self.problem, self.cfg)
        self.assertIsNone(model)
        self.assertEqual(report['reason'], 'insufficient_rolling_validation_samples')
        self.assertIsNotNone(report['forward_holdout_rmse'])
        self.assertLess(report['cv_samples'], 8)
        self.assertFalse(report['uncertainty_calibration']['coverage_is_certified'])

    def test_all_policy_metrics_have_correct_units_and_training_policy_anchor(self):
        policy = default_policy()
        policy['objectives'] = [{'metric': 'mass_kg', 'direction': 'minimize', 'weight': 1.0},
                                {'metric': 'frequency_hz', 'direction': 'maximize', 'weight': 1.0}]
        policy['collect_mass'] = True
        policy['modal'] = {'analysis_name': 'Modal', 'mode_index': 1, 'family': 'axisymmetric_fixed_order'}
        self.problem['optimization_policy'] = policy
        for metric, unit in (('mass_kg', 'kg'), ('frequency_hz', 'Hz')):
            with self.subTest(metric=metric):
                model, report = gp.fit_local_validated(self.history, self.problem, self.cfg, metric=metric)
                self.assertTrue(report['enabled'], report)
                self.assertIsNotNone(model)
                self.assertEqual(report['unit'], unit)
                self.assertAlmostEqual(report['maximum_rmse'], self.history[0][metric] * .05)
                self.assertNotIn('cv_rmse_mpa', report)
                self.assertEqual(report['uncertainty_calibration']['std_floor_unit'], unit)

    def test_comparison_is_read_only_same_time_blocks_and_source_stratified(self):
        snapshot = deepcopy(self.history)
        origins = ['best_local' if i % 4 else 'global_uniform' for i in range(80)]
        report = gp.compare_models(self.history, self.problem, self.cfg, origins)
        self.assertEqual(self.history, snapshot)
        self.assertTrue(report['read_only'])
        models = report['metric_models']['stress_mpa']
        self.assertEqual(set(models), {'rbf_global', 'matern_ard_global', 'matern_ard_local'})
        blocks = [r['forward_history_indices_0based'] for r in models.values()]
        self.assertTrue(all(block == blocks[0] for block in blocks))
        for result in models.values():
            self.assertEqual(set(result['forward_by_origin']), {'best_local', 'global_uniform'})
            self.assertFalse(result['independent_future_confirmation'])
            self.assertEqual(result['forward_common_local_support']['samples'], 16)
        original = report['original_rbf_gate']['stress_mpa']
        self.assertEqual(original['validation_design'], 'original_shuffled_cv_then_final_forward_holdout')
        self.assertIn('cv_rmse_mpa', original)
        json.dumps(report, allow_nan=False)

    def test_duplicate_spread_is_reported_without_claiming_solver_noise(self):
        history = deepcopy(self.history)
        history[-1]['parameters'] = dict(history[-2]['parameters'])
        history[-1]['stress_mpa'] = history[-2]['stress_mpa'] + .01
        model, report = gp.fit_local_validated(history, self.problem, self.cfg)
        self.assertIsNone(model)
        self.assertFalse(report['enabled'])
        self.assertEqual(report['reason'], 'duplicate_measured_designs')
        spread = report['exact_duplicate_spread']
        self.assertEqual(spread['exact_duplicate_groups'], 1)
        self.assertEqual(spread['exact_duplicate_observations'], 2)
        self.assertAlmostEqual(spread['maximum_target_range'], .01)
        self.assertEqual(spread['interpretation'], 'repeat_spread_only_not_estimated_solver_noise')

    def test_missing_dependency_metric_and_invalid_values_fail_closed(self):
        with patch.object(gp, 'np', None):
            model, report = gp.fit_local_validated(self.history, self.problem, self.cfg)
            self.assertIsNone(model)
            self.assertEqual(report['reason'], 'numpy_unavailable')
        history = deepcopy(self.history)
        del history[-1]['mass_kg']
        _, report = gp.fit_local_validated(history, self.problem, self.cfg, metric='mass_kg')
        self.assertEqual(report['reason'], 'missing_metric_measurements')
        history[0]['stress_mpa'] = float('nan')
        _, report = gp.fit_local_validated(history, self.problem, self.cfg)
        self.assertFalse(report['enabled'])
        _, report = gp.fit_local_validated(self.history, self.problem,
                                         dict(self.cfg, surrogate_max_cv_nrmse=float('nan')))
        self.assertFalse(report['enabled'])

    def test_small_absolute_error_does_not_bypass_reversed_forward_ranking(self):
        fit = gp._fit_backend

        class Reversed:
            length_scale = .35

            def predict(self, z):
                target = 10.0 * (z[:, 0] - .5) + 2.0 * (z[:, 1] - .5)
                return 100.0 - target, np.ones(len(z))

        def fit_model(kind, x, y, cfg):
            return Reversed() if len(y) == 64 else fit(kind, x, y, cfg)

        with patch.object(gp, '_fit_backend', side_effect=fit_model):
            model, report = gp.fit_local_validated(self.history, self.problem, self.cfg)
        self.assertIsNone(model)
        self.assertLess(report['forward_holdout_rmse'], 15)
        self.assertGreaterEqual(report['forward_calibrated_interval_coverage'], .8)
        self.assertEqual(report['reason'], 'forward_ranking_below_limit')
        self.assertAlmostEqual(report['forward_ranking']['spearman_correlation'], -1)


if __name__ == '__main__':
    unittest.main()
