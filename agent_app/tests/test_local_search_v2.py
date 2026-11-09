"""Pure grouped-pool and completed-history adaptation tests; no API or FEA."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

import numpy as np

from python_control import local_search, runtime
from python_control.sampling import normalize


PROJECT = Path(__file__).resolve().parents[2]


class LocalSearchV2Tests(unittest.TestCase):
    def setUp(self):
        self.problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        self.config = json.loads((PROJECT / 'python_control/search_config.json').read_text(encoding='utf-8-sig'))
        self.config.update(selection_policy='hybrid_local_v2', batch_size=4,
                           initial_lhs=0, pool_size=32, exploration_slots=1,
                           sigma_start=.08, sigma_min=.02, global_fraction=.2)

    def history(self, stresses):
        result = []
        for i, stress in enumerate(stresses):
            parameters = {v['name']: v['reference'] for v in self.problem['variables']}
            parameters['X1'] = round(parameters['X1'] + .001 * i, 4)
            result.append({'parameters': parameters, 'stress_mpa': stress,
                           'displacement_mm': .2, 'area_m2': .01})
        return result

    def center(self, history, *, fresh=False):
        x = [history[0]['parameters'][name] for name in runtime.bridge.NAMES]
        if fresh:
            x[0] = round(x[0] + 1, 4)
        return x

    def plan(self, history=None, *, fresh=False, config=None, generation=0):
        history = history if history is not None else self.history([1000.0])
        return local_search.build_local_plan(self.center(history, fresh=fresh),
            self.problem, history, config or self.config, generation)

    def assert_unique_and_bounded(self, plan, history):
        occupied = [[row['parameters'][n] for n in runtime.bridge.NAMES] for row in history]
        for x in plan['pool']:
            self.assertEqual(runtime.bridge.ordered_values(dict(zip(runtime.bridge.NAMES, x)), self.problem), x)
            self.assertTrue(all(runtime.bridge.normalized_distance2(x, old, self.problem)
                                > self.problem['min_normalized_distance'] ** 2 for old in occupied))
            occupied.append(x)
        anchor = normalize(plan['local_state']['anchor_x'], self.problem)
        for i in plan['local_indices']:
            self.assertLessEqual(float(np.max(np.abs(normalize(plan['pool'][i], self.problem) - anchor))),
                                 plan['local_state']['radius_fraction'] + 1e-12)

    def test_new_center_is_protected_and_groups_have_explicit_quotas(self):
        history = self.history([1000.0])
        plan = self.plan(history, fresh=True)
        self.assertEqual(plan['protected_center_index'], 0)
        self.assertEqual(plan['pool'][0], self.center(history, fresh=True))
        self.assertEqual(plan['pool_origins'][0], 'llm_center')
        self.assertEqual(plan['selection_quota'], {'llm_center': 1, 'best_local': 2, 'global_uniform': 1})
        self.assertEqual(len(plan['pool']), 32)
        self.assertTrue(all(plan['pool_origins'][i] == 'best_local' for i in plan['local_indices']))
        self.assertTrue(all(plan['pool_origins'][i] == 'global_uniform' for i in plan['global_indices']))
        self.assertEqual(set(plan['local_indices']) | set(plan['global_indices']) | {0}, set(range(32)))
        self.assert_unique_and_bounded(plan, history)

    def test_duplicate_center_is_replaced_by_third_incumbent_local_slot(self):
        history = self.history([1000.0])
        plan = self.plan(history)
        self.assertIsNone(plan['protected_center_index'])
        self.assertNotIn('llm_center', plan['pool_origins'])
        self.assertEqual(plan['selection_quota'], {'llm_center': 0, 'best_local': 3, 'global_uniform': 1})
        self.assertGreaterEqual(len(plan['local_indices']), 3)
        self.assert_unique_and_bounded(plan, history)

    def test_center_that_quantizes_to_history_is_treated_as_duplicate(self):
        history = self.history([1000.0])
        center = self.center(history)
        center[0] += .00001
        plan = local_search.build_local_plan(center, self.problem, history, self.config, 0)
        self.assertIsNone(plan['protected_center_index'])
        self.assertEqual(plan['selection_quota']['best_local'], 3)

    def test_smallest_pool_supports_both_center_cases(self):
        config = dict(self.config, pool_size=4)
        for fresh, expected_local in ((True, 2), (False, 3)):
            with self.subTest(fresh=fresh):
                plan = self.plan(fresh=fresh, config=config)
                self.assertEqual(len(plan['pool']), 4)
                self.assertEqual(len(plan['local_indices']), expected_local)
                self.assertEqual(len(plan['global_indices']), 1)

    def test_zero_global_fraction_still_reserves_the_global_slot(self):
        plan = self.plan(fresh=True, config=dict(self.config, global_fraction=0))
        self.assertEqual(len(plan['global_indices']), 1)
        self.assertEqual(plan['selection_quota']['global_uniform'], 1)

    def test_all_global_pool_target_cannot_erase_local_slots(self):
        plan = self.plan(fresh=True, config=dict(self.config, global_fraction=1))
        self.assertEqual(len(plan['local_indices']), 2)
        self.assertEqual(plan['selection_quota']['best_local'], 2)
        self.assertEqual(len(plan['pool']), 32)

    def test_pool_is_reproducible_and_does_not_mutate_inputs(self):
        history = self.history([1000.0])
        center = self.center(history, fresh=True)
        inputs = deepcopy((center, self.problem, history, self.config))
        first = local_search.build_local_plan(center, self.problem, history, self.config, 0)
        second = local_search.build_local_plan(center, self.problem, history, self.config, 0)
        self.assertEqual(first, second)
        self.assertEqual((center, self.problem, history, self.config), inputs)

    def test_local_radius_changes_do_not_consume_the_global_rng_stream(self):
        first = self.plan(config=self.config)
        second = self.plan(config=dict(self.config, sigma_start=.04))
        self.assertEqual([first['pool'][i] for i in first['global_indices']],
                         [second['pool'][i] for i in second['global_indices']])
        self.assertNotEqual([first['pool'][i] for i in first['local_indices']],
                            [second['pool'][i] for i in second['local_indices']])

    def test_boundary_anchor_reflection_remains_inside_local_box(self):
        history = self.history([1000.0])
        history[0]['parameters'] = {v['name']: v['lower'] for v in self.problem['variables']}
        plan = self.plan(history)
        self.assert_unique_and_bounded(plan, history)
        self.assertTrue(all(all(value >= v['lower'] for value, v in zip(x, self.problem['variables']))
                            for x in plan['pool']))

    def test_radius_contracts_after_two_no_improvement_batches_and_has_floor(self):
        history = self.history([1000.0] + [1100.0] * 16)
        state = local_search.derive_local_state(history, self.config)
        self.assertEqual([e['radius_after'] for e in state['adaptation_evidence']], [.08, .04, .04, .02])
        self.assertEqual(state['radius_fraction'], .02)
        self.assertEqual(state['completed_batches'], 4)
        self.assertEqual(state['no_improvement_streak'], 4)
        self.assertEqual(state['adaptation_action'], 'shrink')
        later = local_search.derive_local_state(self.history([1000.0] + [1100.0] * 24), self.config)
        self.assertEqual(later['radius_fraction'], .02)
        self.assertEqual(later['adaptation_action'], 'hold')

    def test_successes_reexpand_contracted_radius_and_respect_upper_bound(self):
        stresses = [1000.0] + [1100.0] * 16
        for best in (990., 980., 970., 960., 950., 940., 930., 920.):
            stresses += [best] * 4
        state = local_search.derive_local_state(self.history(stresses), self.config)
        radii = [e['radius_after'] for e in state['adaptation_evidence']]
        self.assertEqual(radii[:4], [.08, .04, .04, .02])
        self.assertAlmostEqual(radii[5], .03)
        self.assertAlmostEqual(radii[7], .045)
        self.assertAlmostEqual(radii[9], .0675)
        self.assertEqual(radii[11], .08)
        self.assertEqual(state['success_streak'], 8)
        self.assertEqual(state['adaptation_action'], 'expand')

    def test_partial_batch_cannot_change_radius_anchor_or_adaptation_evidence(self):
        complete = self.history([1000.0] + [1100.0] * 4)
        partial = self.history([1000.0] + [1100.0] * 4 + [100.0, 90.0, 80.0])
        before = local_search.derive_local_state(complete, self.config)
        after = local_search.derive_local_state(partial, self.config)
        for key in ('radius_fraction', 'anchor_x', 'anchor_iteration', 'completed_batches',
                    'success_streak', 'no_improvement_streak', 'adaptation_evidence', 'basis_history_signature'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after['ignored_partial_candidates'], 3)

    def test_tiny_numeric_gains_do_not_count_as_success(self):
        state = local_search.derive_local_state(
            self.history([1000.0] + [999.9999] * 4 + [999.9998] * 4), self.config)
        self.assertEqual(state['radius_fraction'], .04)
        self.assertEqual([e['result'] for e in state['adaptation_evidence']], ['no_improvement', 'no_improvement'])

    def test_initial_design_is_not_counted_as_an_adaptive_search_batch(self):
        config = dict(self.config, initial_lhs=4)
        state = local_search.derive_local_state(self.history([1000.0, 900., 800., 700., 600.]), config)
        self.assertEqual(state['completed_batches'], 0)
        self.assertEqual(state['radius_fraction'], .08)
        self.assertEqual(state['anchor_iteration'], 4)
        plan = self.plan(self.history([1000.0, 900., 800., 700., 600.]), config=config, generation=0)
        self.assertEqual(plan['local_state']['anchor_iteration'], 4)

    def test_partial_or_future_history_and_wrong_generation_are_rejected(self):
        for count, generation in ((1, 0), (4, 0), (0, 1), (8, 1)):
            with self.subTest(completed=count, generation=generation):
                with self.assertRaises(runtime.ControllerError):
                    self.plan(self.history([1000.0] + [1100.0] * count), generation=generation)

    def test_invalid_center_is_not_reflected_or_silently_replaced(self):
        history = self.history([1000.0])
        for bad in (True, float('nan'), self.problem['variables'][0]['upper'] + 1):
            with self.subTest(value=bad):
                center = self.center(history)
                center[0] = bad
                with self.assertRaises((runtime.ControllerError, runtime.bridge.BridgeError)):
                    local_search.build_local_plan(center, self.problem, history, self.config, 0)

    def test_invalid_or_noncanonical_measured_history_is_rejected(self):
        for field in ('stress_bool', 'stress_nan', 'missing_parameter', 'unquantized'):
            with self.subTest(field=field):
                history = self.history([1000.0])
                if field == 'stress_bool': history[0]['stress_mpa'] = True
                if field == 'stress_nan': history[0]['stress_mpa'] = float('nan')
                if field == 'missing_parameter': del history[0]['parameters']['Y4']
                if field == 'unquantized': history[0]['parameters']['X1'] += .000001
                with self.assertRaises((runtime.ControllerError, runtime.bridge.BridgeError)):
                    local_search.build_local_plan(self.center(self.history([1000.0])),
                        self.problem, history, self.config, 0)

    def test_quantization_exhaustion_fails_closed_without_group_substitution(self):
        problem = deepcopy(self.problem)
        for variable in problem['variables']:
            variable['lower'] = variable['reference'] - 1e-8
            variable['upper'] = variable['reference'] + 1e-8
        history = self.history([1000.0])
        with self.assertRaises(runtime.ControllerError):
            local_search.build_local_plan(self.center(history), problem, history,
                                          dict(self.config, pool_size=4), 0)

    def test_duplicate_tolerance_exhaustion_fails_closed(self):
        problem = deepcopy(self.problem)
        problem['min_normalized_distance'] = 100.0
        history = self.history([1000.0])
        with self.assertRaises(runtime.ControllerError):
            local_search.build_local_plan(self.center(history), problem, history,
                                          dict(self.config, pool_size=4), 0)

    def test_invalid_policy_configuration_is_rejected(self):
        for changes in ({'pool_size': True}, {'seed': -1}, {'global_fraction': 1.1},
                        {'sigma_min': 0}, {'sigma_min': .1}, {'pool_size': 3},
                        {'exploration_slots': 0}, {'selection_policy': 'another'}):
            with self.subTest(changes=changes):
                with self.assertRaises(runtime.ControllerError):
                    self.plan(config=dict(self.config, **changes))


if __name__ == '__main__':
    unittest.main()
