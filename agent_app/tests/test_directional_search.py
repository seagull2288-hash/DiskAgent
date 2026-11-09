"""V3 pool/state contracts with temporary data and no external execution."""
from copy import deepcopy
from unittest.mock import patch

import numpy as np

from agent_app.tests.test_contract import FixtureCase
from python_control import directional_search as directional
from python_control import runtime, search
from python_control.sampling import normalize, signature


class DirectionalSearchTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.cfg = dict(search.DEFAULT_CONFIG, strategy='center_surrogate',
                        selection_policy=directional.POLICY, sigma_min=.002)
        self.reference = [v['reference'] for v in self.problem['variables']]
        block = patch.object(runtime.bridge.Client, 'generate',
                             side_effect=AssertionError('No provider is permitted.'))
        block.start()
        self.addCleanup(block.stop)

    def history(self, count=1, *, improving=False, anchor=None):
        baseline = self.reference if anchor is None else list(anchor)
        rows = []
        for i in range(count):
            x = list(baseline)
            if i:
                for axis, factor in ((0, .01), (1, .0134)):
                    bound = self.problem['variables'][axis]
                    sign = 1 if baseline[axis] + factor * (count - 1) <= bound['upper'] else -1
                    x[axis] = round(baseline[axis] + sign * factor * i, 4)
            rows.append({'parameters': dict(zip(self.problem['input_order'], x)),
                'stress_mpa': 1000.0 - i if improving else 1000.0 + i,
                'displacement_mm': .2, 'area_m2': .01})
        return rows

    def plan(self, *, generation=0, history=None, center=None, cfg=None):
        cfg = self.cfg if cfg is None else cfg
        history = self.history(cfg['initial_lhs'] + generation * cfg['batch_size'] + 1) if history is None else history
        center = self.reference if center is None else center
        return directional.build_directional_plan(center, self.problem, history,
                                                   cfg, generation)

    def assert_valid_pool(self, plan, history):
        pool = plan['pool']
        self.assertEqual(len(pool), self.cfg['pool_size'])
        self.assertEqual(len(plan['pool_origins']), len(pool))
        self.assertEqual(len({tuple(x) for x in pool}), len(pool))
        occupied = [[row['parameters'][name] for name in self.problem['input_order']]
                    for row in history]
        for i, x in enumerate(pool):
            self.assertEqual(runtime.bridge.ordered_values(dict(zip(self.problem['input_order'], x)), self.problem), x)
            self.assertTrue(all(runtime.bridge.normalized_distance2(x, old, self.problem)
                > self.problem['min_normalized_distance'] ** 2 for old in occupied + pool[:i]))

    def test_grouped_pool_is_deterministic_canonical_unique_and_does_not_mutate_inputs(self):
        history = self.history(25)
        center = list(self.reference)
        center[2] += .1234
        original = deepcopy((history, center, self.problem, self.cfg))
        first = self.plan(generation=6, history=history, center=center)
        second = self.plan(generation=6, history=history, center=center)
        self.assertEqual(first, second)
        self.assertEqual((history, center, self.problem, self.cfg), original)
        self.assert_valid_pool(first, history)
        self.assertEqual(first['pool_group_sizes'],
            {origin: first['pool_origins'].count(origin) for origin in
                ('llm_center', 'best_local', 'global_uniform')})
        self.assertEqual(sum(first['selection_quota'].values()), self.cfg['batch_size'])

    def test_inner_coordinate_fallback_preserves_eight_parameters_and_all_step_scales_exist(self):
        plan = self.plan()
        anchor = np.array(plan['local_state']['anchor_x'])
        details = plan['local_candidate_details']
        self.assertIs(type(details), dict)
        self.assertEqual(set(details), {str(i) for i in plan['local_indices']})
        self.assertEqual(plan['fallback_local_indices'], plan['local_indices'])
        scales = {detail['step_scale'] for detail in details.values()}
        self.assertEqual(scales, {.25, .5, 1.0})
        first_axes = []
        for i in plan['fallback_local_indices'][:3]:
            detail = details[str(i)]
            self.assertEqual(detail['direction_kind'], 'coordinate')
            self.assertEqual(detail['step_scale'], .25)
            self.assertEqual(len(detail['changed_parameters']), 1)
            self.assertEqual(np.count_nonzero(np.array(plan['pool'][i]) != anchor), 1)
            first_axes.extend(detail['changed_parameters'])
        self.assertEqual(len(set(first_axes)), 3)

    def test_rotation_covers_all_nine_dimensions_in_five_batches(self):
        covered = set()
        for generation in range(5):
            plan = self.plan(generation=generation)
            for i in plan['fallback_local_indices'][:2]:
                detail = plan['local_candidate_details'][str(i)]
                self.assertEqual(detail['direction_kind'], 'coordinate')
                covered.update(detail['changed_parameters'])
        self.assertEqual(covered, set(self.problem['input_order']))

    def test_directional_poll_improves_a_nine_dimensional_sphere(self):
        from python_control.surrogate import select_hybrid_pool
        cfg = dict(self.cfg, exploration_slots=0)
        low = np.array([v['lower'] for v in self.problem['variables']])
        width = np.array([v['upper'] - v['lower'] for v in self.problem['variables']])
        start = (low + .7 * width).tolist()

        def observation(x):
            z = normalize(x, self.problem)
            return {'parameters': dict(zip(self.problem['input_order'], x)),
                'stress_mpa': 100.0 + float(np.sum((z - .5) ** 2)),
                'displacement_mm': .2, 'area_m2': .01}

        history = [observation(start)]
        initial = history[0]['stress_mpa']
        covered = set()
        for generation in range(12):
            incumbent = min(history, key=lambda item: item['stress_mpa'])
            center = [incumbent['parameters'][name] for name in self.problem['input_order']]
            grouped = directional.build_directional_plan(center, self.problem, history, cfg, generation)
            chosen, _, _ = select_hybrid_pool(grouped, history, self.problem, cfg, enabled=False)
            for index in chosen:
                covered.update(grouped['local_candidate_details'][str(index)]['changed_parameters'])
                history.append(observation(grouped['pool'][index]))
        self.assertEqual(covered, set(self.problem['input_order']))
        self.assertLess(min(row['stress_mpa'] for row in history), initial - .05)

    def test_bounds_skip_zero_moves_and_truncate_without_reflecting_direction(self):
        anchor = list(self.reference)
        anchor[0] = self.problem['variables'][0]['upper']
        anchor[1] = self.problem['variables'][1]['lower'] + .01
        anchor[8] = self.problem['variables'][8]['lower']
        history = self.history(anchor=anchor)
        plan = self.plan(history=history, center=anchor)
        axis_rows = [(plan['pool'][int(index)], detail)
            for index, detail in plan['local_candidate_details'].items()
            if detail['direction_kind'] == 'coordinate']
        x1 = [(x, d) for x, d in axis_rows if d['changed_parameters'] == ['X1']]
        y4 = [(x, d) for x, d in axis_rows if d['changed_parameters'] == ['Y4']]
        self.assertTrue(x1)
        self.assertTrue(y4)
        self.assertTrue(all(x[0] < anchor[0] for x, _ in x1))
        self.assertTrue(all(x[8] > anchor[8] for x, _ in y4))
        negative_x2 = [(x, d) for x, d in axis_rows
                       if d['changed_parameters'] == ['X2'] and x[1] < anchor[1]]
        self.assertTrue(negative_x2)
        self.assertEqual(negative_x2[0][0][1], self.problem['variables'][1]['lower'])
        self.assertTrue(negative_x2[0][1]['clipped_to_bounds'])
        self.assert_valid_pool(plan, history)

    def test_sparse_patterns_fill_a_corner_pool_while_other_parameters_stay_fixed(self):
        anchor = [v['lower'] for v in self.problem['variables']]
        history = self.history(anchor=anchor)
        plan = self.plan(history=history, center=anchor)
        patterns = [detail for detail in plan['local_candidate_details'].values()
                    if detail['direction_kind'] == 'pattern']
        self.assertTrue(patterns)
        for detail in plan['local_candidate_details'].values():
            self.assertLessEqual(len(detail['changed_parameters']), 2)
            x = plan['pool'][detail['pool_index']]
            self.assertEqual(sum(a != b for a, b in zip(anchor, x)), len(detail['changed_parameters']))
        self.assert_valid_pool(plan, history)

    def test_fine_radius_reaches_configured_floor_and_contains_five_micron_inner_steps(self):
        history = self.history(49)
        plan = self.plan(generation=12, history=history)
        self.assertEqual(plan['local_state']['radius_fraction'], .002)
        self.assertEqual(plan['local_state']['policy'], directional.POLICY)
        anchor = plan['local_state']['anchor_x']
        detail = next(d for d in plan['local_candidate_details'].values()
                      if d['direction_kind'] == 'coordinate' and d['step_scale'] == .25)
        x = plan['pool'][detail['pool_index']]
        self.assertAlmostEqual(max(abs(a - b) for a, b in zip(x, anchor)), .005)
        self.assertEqual(plan['local_state']['numeric_tolerance_role'], 'algorithm_state_only')
        self.assertIsNone(plan['local_state']['engineering_significance_threshold'])

    def test_early_and_late_periodic_global_quotas(self):
        for generation in range(15):
            history = self.history(generation * 4 + 1, improving=True)
            state = directional.derive_directional_state(history, self.cfg)
            batch = generation + 1
            expected = 1 if batch <= 8 or (batch - 8) % 3 == 0 else 0
            self.assertEqual(state['global_exploration_slots'], expected)
            expected_reason = 'early8' if batch <= 8 else 'periodic' if expected else 'local_refinement'
            self.assertEqual(state['global_schedule']['schedule_reason'], expected_reason)

    def test_stagnation_pulses_do_not_turn_every_late_batch_into_global_exploration(self):
        counts = []
        for completed in range(9, 13):
            history = self.history(completed * 4 + 1)
            # Six initial improving batches, followed by an increasing plateau.
            for i, row in enumerate(history):
                row['stress_mpa'] = 1000.0 - min(i, 24)
            state = directional.derive_directional_state(history, self.cfg)
            counts.append(state['global_exploration_slots'])
            if completed in (9, 12):
                self.assertEqual(state['global_schedule']['schedule_reason'], 'stagnation_pulse')
        self.assertEqual(counts, [1, 1, 0, 1])

    def test_zero_exploration_cap_and_zero_scheduled_global_pool(self):
        cfg = dict(self.cfg, exploration_slots=0)
        plan = self.plan(cfg=cfg)
        self.assertEqual(plan['selection_quota'], {'llm_center': 0, 'best_local': 4, 'global_uniform': 0})
        self.assertEqual(plan['global_indices'], [])
        self.assertEqual(plan['pool_group_sizes']['global_uniform'], 0)
        self.assertEqual(plan['local_state']['global_schedule']['schedule_reason'], 'local_refinement')
        late = self.plan(generation=8, history=self.history(33, improving=True))
        self.assertEqual(late['selection_quota']['global_uniform'], 0)
        self.assertEqual(late['global_indices'], [])

    def test_center_is_protected_only_when_unmeasured_and_does_not_enter_local_fallback(self):
        center = list(self.reference)
        center[0] += .1234
        plan = self.plan(center=center)
        self.assertEqual(plan['protected_center_index'], 0)
        self.assertEqual(plan['pool'][0], center)
        self.assertEqual(plan['pool_origins'][0], 'llm_center')
        self.assertNotIn(0, plan['fallback_local_indices'])
        self.assertEqual(plan['selection_quota'], {'llm_center': 1, 'best_local': 2, 'global_uniform': 1})
        duplicate = self.plan()
        self.assertIsNone(duplicate['protected_center_index'])
        self.assertEqual(duplicate['selection_quota']['best_local'], 3)

    def test_local_details_match_actual_normalized_deltas_and_radius(self):
        plan = self.plan()
        anchor = normalize(plan['local_state']['anchor_x'], self.problem)
        radius = plan['local_state']['radius_fraction']
        for key, detail in plan['local_candidate_details'].items():
            index = int(key)
            self.assertEqual(detail['pool_index'], index)
            delta = normalize(plan['pool'][index], self.problem) - anchor
            np.testing.assert_allclose(delta, detail['normalized_delta'], atol=1e-14)
            self.assertLessEqual(float(np.max(abs(delta))), radius + 1e-12)

    def test_partial_or_future_history_cannot_build_a_new_batch(self):
        for count in (4, 6, 9):
            with self.subTest(count=count), self.assertRaises(runtime.ControllerError):
                self.plan(generation=1, history=self.history(count))
        history = self.history(7, improving=True)
        state = directional.derive_directional_state(history, self.cfg)
        self.assertEqual(state['basis_completed_candidates'], 4)
        self.assertEqual(state['ignored_partial_candidates'], 2)
        self.assertEqual(state['anchor_iteration'], 4)
        self.assertEqual(state['basis_history_signature'], signature(history[:5]))

    def test_initial_lhs_offset_is_respected(self):
        cfg = dict(self.cfg, initial_lhs=2)
        history = self.history(3, improving=True)
        plan = self.plan(cfg=cfg, history=history)
        self.assertEqual(plan['local_state']['completed_batches'], 0)
        self.assertEqual(plan['local_state']['anchor_iteration'], 2)
        self.assertEqual(plan['selection_quota']['global_uniform'], 1)

    def test_malformed_policy_types_geometry_and_duplicate_history_are_rejected(self):
        for changes in ({'selection_policy': 'hybrid_local_v2'}, {'exploration_slots': True},
            {'exploration_slots': 4}, {'batch_size': 1}, {'sigma_min': 0},
            {'sigma_min': float('nan')}, {'seed': True}):
            with self.subTest(changes=changes), self.assertRaises(runtime.ControllerError):
                self.plan(cfg=dict(self.cfg, **changes))
        with self.assertRaises(runtime.ControllerError):
            self.plan(generation=True)
        center = list(self.reference)
        center[0] = True
        with self.assertRaises(runtime.ControllerError):
            self.plan(center=center)
        history = self.history(5)
        history[-1] = deepcopy(history[0])
        with self.assertRaises(runtime.ControllerError):
            self.plan(generation=1, history=history)
        history = self.history(5)
        history[1]['parameters']['X1'] += .00001
        with self.assertRaises(runtime.ControllerError):
            self.plan(generation=1, history=history)

