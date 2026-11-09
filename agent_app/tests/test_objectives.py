"""Objective tradeoffs, hard constraints and independently labeled GP targets."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from agent_app.schemas import TaskSpec, TaskValidationError
from python_control import objectives, runtime, search, surrogate, workbench


PROJECT = Path(__file__).resolve().parents[2]


def dual_policy():
    policy = objectives.default_policy()
    policy['objectives'].append({'metric': 'mass_kg', 'direction': 'minimize', 'weight': 1.0})
    return objectives.validate_policy(policy)


def mass_limit(value=1.0):
    return {'metric': 'mass_kg', 'operator': '<=', 'value': value, 'upper': None,
            'reference': 'baseline_ratio', 'source': '探索性基准约束'}


class ObjectiveEvaluationTests(unittest.TestCase):
    def test_tradeoffs_and_dominated_points_have_separate_recommendation_and_pareto_set(self):
        history = [{'stress_mpa': 1000, 'mass_kg': 10}, {'stress_mpa': 800, 'mass_kg': 11},
                   {'stress_mpa': 900, 'mass_kg': 8}, {'stress_mpa': 950, 'mass_kg': 9}]
        result = objectives.archive(history, dual_policy())
        self.assertEqual(result['best_iteration'], 2)
        self.assertEqual(result['pareto_iterations'], [1, 2])
        policy = dual_policy()
        policy['objectives'][0]['weight'] = 9
        self.assertEqual(objectives.archive(history, policy)['best_iteration'], 1)

    def test_hard_mass_limit_excludes_lowest_stress_and_missing_mass_is_unknown(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit()]
        result = objectives.archive([{'stress_mpa': 1000, 'mass_kg': 10},
                                     {'stress_mpa': 500, 'mass_kg': 12},
                                     {'stress_mpa': 900, 'mass_kg': 9},
                                     {'stress_mpa': 400}], policy)
        self.assertEqual(result['best_iteration'], 2)
        self.assertEqual(result['evaluations'][1]['status'], 'infeasible')
        self.assertEqual(result['evaluations'][3]['status'], 'missing')
        self.assertNotIn(1, result['pareto_iterations'])
        self.assertNotIn(3, result['pareto_iterations'])

    def test_no_feasible_design_has_no_recommendation_but_retains_search_anchor(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit(0.5)]
        result = objectives.archive([{'stress_mpa': 1000, 'mass_kg': 10},
                                     {'stress_mpa': 900, 'mass_kg': 9}], policy)
        self.assertIsNone(result['best_iteration'])
        self.assertEqual(result['anchor_iteration'], 1)
        self.assertEqual(result['pareto_iterations'], [])

    def test_weight_scaling_does_not_change_ranking_or_allow_nonfinite_scores(self):
        history = [{'stress_mpa': 1000, 'mass_kg': 10}, {'stress_mpa': 900, 'mass_kg': 8}]
        policy = dual_policy()
        expected = objectives.archive(history, policy)['evaluations'][1]['score']
        for item in policy['objectives']:
            item['weight'] = 1e308
        self.assertAlmostEqual(objectives.archive(history, policy)['evaluations'][1]['score'], expected)

    def test_constraint_thresholds_remain_relative_to_original_baseline(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit(0.95)]
        result = objectives.archive([{'stress_mpa': 1000, 'mass_kg': 10},
                                     {'stress_mpa': 900, 'mass_kg': 9},
                                     {'stress_mpa': 850, 'mass_kg': 9.4}], policy)
        self.assertEqual(result['evaluations'][2]['status'], 'feasible')
        self.assertEqual(result['evaluations'][2]['constraint_checks'][0]['limit'], 9.5)

    def test_missing_limits_and_unsupported_metric_types_are_structured_task_errors(self):
        for value in (None, True, 0, float('inf')):
            policy = dual_policy()
            policy['constraints'] = [mass_limit(value)]
            with self.subTest(value=value), self.assertRaises(TaskValidationError):
                TaskSpec.from_dict({'operation': 'optimize', 'total_candidate_budget': 1,
                                    'optimization_policy': policy})
        policy = dual_policy()
        policy['objectives'][0]['metric'] = []
        with self.assertRaises(TaskValidationError):
            TaskSpec.from_dict({'operation': 'optimize', 'total_candidate_budget': 1,
                                'optimization_policy': policy})

    def test_policy_id_is_stable_and_task_records_configured_objectives(self):
        policy = dual_policy()
        task = TaskSpec.from_dict({'operation': 'optimize', 'total_candidate_budget': 1,
                                   'optimization_policy': policy})
        self.assertEqual(task.objective, 'configured_objectives')
        self.assertEqual(objectives.policy_id(policy), objectives.policy_id(deepcopy(policy)))
        changed = deepcopy(policy)
        changed['objectives'][0]['weight'] = 2
        self.assertNotEqual(objectives.policy_id(policy), objectives.policy_id(changed))

    def test_frequency_exclusion_band_and_maximization_use_explicit_modal_definition(self):
        policy = objectives.default_policy()
        policy['objectives'] = [{'metric': 'frequency_hz', 'direction': 'maximize', 'weight': 1}]
        with self.assertRaises(runtime.ControllerError):
            objectives.validate_policy(policy)
        policy['modal'] = {'analysis_name': 'Configured modal', 'mode_index': 1,
                           'family': 'axisymmetric_fixed_order'}
        policy['constraints'] = [{'metric': 'frequency_hz', 'operator': 'outside_band',
                                  'value': 100, 'upper': 120, 'reference': 'absolute', 'source': '试验频带'}]
        result = objectives.archive([{'frequency_hz': 90}, {'frequency_hz': 110}, {'frequency_hz': 130}], policy)
        self.assertEqual(result['best_iteration'], 2)
        self.assertEqual(result['evaluations'][1]['status'], 'infeasible')

    def test_signed_margins_use_physical_units_and_frozen_baseline_limits(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit(0.95), {
            'metric': 'stress_mpa', 'operator': '>=', 'value': 800, 'upper': None,
            'reference': 'absolute', 'source': '测试下限'}]
        result = objectives.archive([{'stress_mpa': 1000, 'mass_kg': 10},
                                     {'stress_mpa': 750, 'mass_kg': 9}], policy)
        checks = result['evaluations'][1]['constraint_checks']
        self.assertEqual((checks[0]['limit'], checks[0]['margin'], checks[0]['margin_unit']), (9.5, 0.5, 'kg'))
        self.assertEqual((checks[1]['margin'], checks[1]['status'], checks[1]['margin_unit']), (-50, 'failed', 'MPa'))
        self.assertEqual(result['evaluations'][0]['constraint_checks'][0]['margin'], -0.5)

    def test_frequency_band_margin_matches_both_boundaries_and_inside_outside_status(self):
        policy = objectives.default_policy()
        policy['modal'] = {'analysis_name': 'Modal', 'mode_index': 1, 'family': 'axisymmetric_fixed_order'}
        policy['constraints'] = [{'metric': 'frequency_hz', 'operator': 'outside_band',
                                  'value': 100, 'upper': 120, 'reference': 'absolute', 'source': '测试频带'}]
        for frequency, margin, status in [(90, 10, 'passed'), (100, 0, 'passed'),
                                          (110, -10, 'failed'), (120, 0, 'passed'), (130, 10, 'passed')]:
            with self.subTest(frequency=frequency):
                check = objectives.evaluate({'stress_mpa': 900, 'frequency_hz': frequency},
                                             {'stress_mpa': 1000}, policy)['constraint_checks'][0]
                self.assertEqual((check['margin'], check['status'], check['margin_unit']), (margin, status, 'Hz'))

    def test_missing_measurement_keeps_known_limit_and_unknown_margin(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit(0.95)]
        check = objectives.evaluate({'stress_mpa': 900}, {'stress_mpa': 1000, 'mass_kg': 10},
                                     policy)['constraint_checks'][0]
        self.assertEqual(check['limit'], 9.5)
        self.assertEqual(check['status'], 'missing')
        self.assertIsNone(check['margin'])
        check = objectives.evaluate({'stress_mpa': 900, 'mass_kg': 9}, {'stress_mpa': 1000},
                                     policy)['constraint_checks'][0]
        self.assertIsNone(check['limit'])
        self.assertIsNone(check['margin'])

    def test_named_selections_keep_infeasible_stress_minimum_separate_from_recommendation(self):
        history = [{'stress_mpa': 1000, 'mass_kg': 10}, {'stress_mpa': 800, 'mass_kg': 12},
                   {'stress_mpa': 900, 'mass_kg': 9}]
        before = deepcopy(history)
        policy = dual_policy()
        policy['constraints'] = [mass_limit()]
        result = objectives.selection_summary(history, objectives.archive(history, policy))
        self.assertEqual(result['minimum_stress']['iteration'], 1)
        self.assertEqual(result['minimum_stress']['evaluation']['status'], 'infeasible')
        self.assertEqual(result['recommended']['iteration'], 2)
        self.assertEqual([design['iteration'] for design in result['pareto']], [2])
        self.assertEqual(result['policy_id'], objectives.policy_id(policy))
        self.assertEqual(result['policy_source'], 'saved_optimization_policy')
        self.assertEqual(history, before)

    def test_empty_legacy_and_missing_metric_selections_have_explicit_meaning(self):
        empty = objectives.selection_summary([])
        self.assertIsNone(empty['minimum_stress'])
        self.assertIsNone(empty['recommended'])
        self.assertEqual(empty['pareto'], [])
        history = [{'stress_mpa': 1000}, {'stress_mpa': 900}]
        legacy = objectives.selection_summary(history)
        self.assertEqual(legacy['policy_source'], 'default_stress_objective')
        self.assertEqual(legacy['constraint_count'], 0)
        self.assertEqual(legacy['recommended']['iteration'], 1)
        missing = objectives.selection_summary(history, objectives.archive(history, dual_policy()))
        self.assertIsNone(missing['recommended'])
        self.assertEqual(missing['minimum_stress']['evaluation']['status'], 'missing')
        self.assertEqual(missing['pareto'], [])


class ObjectivePredictionTests(unittest.TestCase):
    def setUp(self):
        self.problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        self.policy = dual_policy()
        self.policy['constraints'] = [mass_limit()]
        self.problem['optimization_policy'] = self.policy
        self.cfg = dict(search.DEFAULT_CONFIG, surrogate_max_distance=100)
        self.center = [item['reference'] for item in self.problem['variables']]
        self.history = [{'parameters': dict(zip(runtime.bridge.NAMES, self.center)),
                         'stress_mpa': 1000, 'mass_kg': 10}]
        self.pool = [list(self.center), list(self.center)]
        self.pool[1][0] += 0.1

    def test_prediction_units_and_mass_feasibility_affect_selection(self):
        class Model:
            def __init__(self, values):
                self.values = values

            def predict(self, values):
                return np.array(self.values), np.zeros(2)

        def fit(history, problem, cfg, *, metric, maximum_rmse):
            if metric == 'mass_kg':
                self.assertEqual(maximum_rmse, 0.5)
            values = [12, 9] if metric == 'mass_kg' else [500, 900]
            return Model(values), {'enabled': True, 'reason': 'fixture_validated'}

        with patch.object(surrogate, 'fit_validated', side_effect=fit):
            order, report, predictions = surrogate.rank_pool(self.pool, self.history, self.problem, self.cfg)
        self.assertEqual(order[0], 1)
        self.assertTrue(report['enabled'])
        self.assertEqual(predictions[0]['metric_predictions']['mass_kg']['unit'], 'kg')
        self.assertEqual(predictions[0]['metric_predictions']['stress_mpa']['unit'], 'MPa')
        self.assertNotIn('predicted_stress_mpa', predictions[0])

    def test_one_unavailable_required_model_keeps_surrogate_selection_disabled(self):
        with patch.object(surrogate, 'fit_validated', return_value=(None, {
                'enabled': False, 'reason': 'insufficient_measured_samples'})):
            order, report, predictions = surrogate.rank_pool(self.pool, self.history, self.problem, self.cfg)
        self.assertFalse(report['enabled'])
        self.assertEqual((order, predictions), ([], []))


class NativeMassEvidenceTests(unittest.TestCase):
    def test_native_mass_mismatch_or_wrong_scope_rejects_record(self):
        problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        x = [item['reference'] for item in problem['variables']]
        native = {'success': True, 'solve_requested': True, 'stress_MPa': 800,
                  'displacement_mm': 0.2, 'mass_kg': 10,
                  'actual': {'result_file_after': {'fresh_for_this_solve': True}},
                  'metric_evidence': {'mass_kg': {'unit': 'kg', 'scope': 'full_axisymmetric_disk'}}}
        with tempfile.TemporaryDirectory(prefix='disk_mass_evidence_') as folder:
            path = Path(folder) / 'native.json'
            path.write_text(json.dumps(native), encoding='utf-8')
            detail = {'nativeReportFile': str(path), 'xActual': x, 'stressMPa': 800,
                      'displacementMM': 0.2, 'area': workbench.approximate_area(x), 'mass_kg': 10}
            workbench.verify_native(detail)
            with self.assertRaises(runtime.ControllerError):
                workbench.verify_native(dict(detail, mass_kg=9))
            native['metric_evidence']['mass_kg']['scope'] = 'sector'
            path.write_text(json.dumps(native), encoding='utf-8')
            with self.assertRaises(runtime.ControllerError):
                workbench.verify_native(detail)
