"""Independent v2 policy contracts using local data and fake execution boundaries.

No provider, installed solver, historical run or production config is used.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from agent_app import tools
from agent_app.tests import test_engine_integration as engine_fixture
from agent_app.tests.test_contract import FixtureCase
from python_control import controller, runtime, search, surrogate
from python_control.sampling import normalize, signature


POLICY = 'hybrid_local_v2'
ORIGINS = {'llm_center', 'best_local', 'global_uniform'}
ROLES = {'llm_center': 'llm_proposal', 'best_local': 'local_refinement',
         'global_uniform': 'global_exploration'}


class SearchPolicyConfigurationTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.cfg = deepcopy(search.DEFAULT_CONFIG)
        self.cfg['strategy'] = 'center_surrogate'
        self.config_path = self.root / 'python_control/search_config.json'
        self.config_path.parent.mkdir()
        block = patch.object(runtime.bridge.Client, 'generate',
                             side_effect=AssertionError('No provider calls are permitted.'))
        block.start()
        self.addCleanup(block.stop)

    def load(self, value):
        runtime.write_json(self.config_path, value)
        return search.load_search_config(self.root)

    def test_legacy_configuration_shape_and_signature_are_not_migrated(self):
        self.assertNotIn('selection_policy', search.DEFAULT_CONFIG)
        legacy = deepcopy(self.cfg)
        old_signature = signature(legacy)
        loaded = self.load(legacy)
        self.assertEqual(loaded, legacy)
        self.assertEqual(signature(loaded), old_signature)
        self.assertNotIn('selection_policy', loaded)
        explicit = dict(legacy, selection_policy='legacy_v1')
        self.assertEqual(self.load(explicit), explicit)
        v2 = dict(legacy, selection_policy=POLICY)
        self.assertEqual(self.load(v2), v2)

    def test_unknown_policy_types_and_config_fields_are_rejected(self):
        for value in ('v2', 'hybrid_local_v4', '', None, True, 2, [], {}):
            with self.subTest(value=value), self.assertRaises((ValueError, runtime.ControllerError)):
                self.load(dict(self.cfg, selection_policy=value))
        with self.assertRaises((ValueError, runtime.ControllerError)):
            self.load(dict(self.cfg, hidden_algorithm_override=True))
        for values in ({'batch_size': 1, 'exploration_slots': 0}, {'exploration_slots': 0}):
            with self.subTest(values=values), self.assertRaises((ValueError, runtime.ControllerError)):
                self.load(dict(self.cfg, selection_policy=POLICY, **values))

    def history(self, count=25):
        rows = []
        for i in range(count):
            parameters = {v['name']: v['reference'] for v in self.problem['variables']}
            parameters['X1'] = round(15.0 + 10.0 * i / max(1, count - 1), 4)
            rows.append({'parameters': parameters, 'stress_mpa': 1000.0 - i,
                         'displacement_mm': .2, 'area_m2': .01})
        return rows

    def grouped(self, history=None):
        from python_control.local_search import build_local_plan
        history = self.history() if history is None else history
        center = [v['reference'] for v in self.problem['variables']]
        center[0] = 21.1111
        cfg = dict(self.cfg, selection_policy=POLICY)
        return build_local_plan(center, self.problem, history, cfg, 6), history, cfg

    def assert_quota(self, grouped, indices):
        self.assertEqual(len(indices), 4)
        self.assertEqual(len(set(indices)), 4)
        origins = [grouped['pool_origins'][i] for i in indices]
        self.assertEqual({name: origins.count(name) for name in ORIGINS},
                         grouped['selection_quota'])
        protected = grouped['protected_center_index']
        if protected is not None:
            self.assertIs(type(protected), int)
            self.assertIn(protected, indices)

    def test_gp_off_fallback_preserves_center_local_and_global_quotas(self):
        grouped, history, cfg = self.grouped()
        with patch.object(surrogate, 'fit_validated',
                          return_value=(None, {'enabled': False,
                            'measured_samples': len(history), 'reason': 'validation_error_above_limit',
                            'predictions_are_measurements': False})):
            indices, report, predictions = surrogate.select_hybrid_pool(
                grouped, history, self.problem, cfg, enabled=True)
        self.assert_quota(grouped, indices)
        self.assertFalse(report['enabled'])
        self.assertEqual(predictions, [])

    def test_gp_ranking_cannot_evict_protected_center_or_reserved_global(self):
        grouped, history, cfg = self.grouped()
        normalized = normalize(grouped['pool'], self.problem)
        local_indices = set(grouped['local_indices'])
        for prefer_local in (False, True):
            class AdversarialRanking:
                def predict(self, values):
                    # Favor an entire origin group: the quota must still govern.
                    means = []
                    for row in values:
                        matches = np.where(np.all(np.isclose(normalized, row,
                            atol=1e-12, rtol=0), axis=1))[0]
                        index = int(matches[0])
                        local = index in local_indices
                        means.append(100.0 if local == prefer_local else 5000.0)
                    return np.array(means), np.zeros(len(means))
            with self.subTest(prefer_local=prefer_local), patch.object(surrogate,
                    'fit_validated', return_value=(AdversarialRanking(), {
                        'enabled': True, 'measured_samples': len(history),
                        'predictions_are_measurements': False,
                        'reason': 'cross_validation_and_forward_holdout_passed'})):
                indices, report, predictions = surrogate.select_hybrid_pool(
                    grouped, history, self.problem, cfg, enabled=True)
            self.assert_quota(grouped, indices)
            self.assertTrue(report['enabled'])
            self.assertTrue(predictions)

    def test_local_plan_uses_supplied_snapshot_without_mutation(self):
        history = self.history()
        before = deepcopy(history)
        grouped, _, cfg = self.grouped(history)
        repeated, _, _ = self.grouped(history)
        self.assertEqual(grouped, repeated)
        self.assertEqual(history, before)
        self.assertEqual(grouped['local_state']['anchor_x'],
            [history[-1]['parameters'][name] for name in self.problem['input_order']])
        self.assertEqual(len(grouped['pool']), cfg['pool_size'])
        self.assertEqual(len(grouped['pool_origins']), len(grouped['pool']))
        self.assertTrue(set(grouped['pool_origins']) <= ORIGINS)

    def test_validated_gp_with_no_trusted_local_points_reports_unused(self):
        grouped, history, cfg = self.grouped()
        cfg['surrogate_max_distance'] = 1e-12
        class LocalModel:
            def predict(self, values):
                return np.full(len(values), 700.0), np.full(len(values), 2.0)
        with patch.object(surrogate, 'fit_validated', return_value=(LocalModel(), {
                'enabled': True, 'measured_samples': len(history),
                'predictions_are_measurements': False,
                'reason': 'cross_validation_and_forward_holdout_passed'})):
            indices, report, predictions = surrogate.select_hybrid_pool(
                grouped, history, self.problem, cfg, enabled=True)
        self.assert_quota(grouped, indices)
        self.assertTrue(report['enabled'])
        self.assertFalse(report['surrogate_used'])
        self.assertEqual(report['exploitation_selected'], 0)
        self.assertEqual(report['selection'], 'hybrid_local_quotas_without_surrogate')
        self.assertEqual(len(predictions), len(grouped['pool']))

    def test_duplicate_center_slot_moves_to_local_refinement(self):
        from python_control.local_search import build_local_plan
        history = self.history()
        center = [history[-1]['parameters'][name] for name in self.problem['input_order']]
        cfg = dict(self.cfg, selection_policy=POLICY)
        grouped = build_local_plan(center, self.problem, history, cfg, 6)
        with patch.object(surrogate, 'fit_validated',
                          side_effect=AssertionError('center_only must not fit')):
            indices, report, _ = surrogate.select_hybrid_pool(
                grouped, history, self.problem, cfg, enabled=False)
        self.assertIsNone(grouped['protected_center_index'])
        self.assertEqual(grouped['selection_quota'],
                         {'llm_center': 0, 'best_local': 3, 'global_uniform': 1})
        self.assert_quota(grouped, indices)
        self.assertFalse(report['protected_center_selected'])
        self.assertFalse(report['surrogate_used'])

    def test_builder_rejects_partial_or_future_history_for_a_frozen_generation(self):
        from python_control.local_search import build_local_plan
        cfg = dict(self.cfg, selection_policy=POLICY)
        center = [v['reference'] for v in self.problem['variables']]
        for count in (24, 26, 29):
            with self.subTest(count=count), self.assertRaises(runtime.ControllerError):
                build_local_plan(center, self.problem, self.history(count), cfg, 6)


class SearchPolicyControllerTests(unittest.TestCase):
    fake_proposal = engine_fixture.ControllerIntegrationTests.fake_proposal

    def setUp(self):
        engine_fixture.ControllerIntegrationTests.setUp(self)
        self.center_requests = []
        self.center_contexts = []
        self.stop_after = None
        cfg = deepcopy(search.DEFAULT_CONFIG)
        cfg.update(strategy='center_surrogate', selection_policy=POLICY,
                   initial_lhs=0, batch_size=4)
        runtime.write_json(self.root / 'python_control/search_config.json', cfg)
        self.cfg = cfg
        for block in (
            patch.object(runtime, 'fingerprint', return_value={'fixture': 'stable-test-only'}),
            patch.object(search, 'request_center', side_effect=self.fake_request_center),
            patch.object(runtime.bridge.Client, 'generate',
                         side_effect=AssertionError('No real provider is permitted.')),
        ):
            block.start()
            self.addCleanup(block.stop)

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        index = self.evaluations
        detail = engine_fixture.ControllerIntegrationTests.fake_evaluate(
            self, x, settings, problem, output_root, baseline)
        if self.stop_after == index:
            detail['stopRequested'] = True
        return detail

    def fake_request_center(self, api, problem, history, folder, generation,
                            sigma, client_factory=None, selection_context=None):
        self.center_requests.append(deepcopy(history))
        self.center_contexts.append(deepcopy(selection_context))
        parameters = {v['name']: v['reference'] for v in problem['variables']}
        parameters['X1'] = round(parameters['X1'] + .031 * generation, 4)
        center = search.parse_center(json.dumps({'center': parameters,
            'rationale': 'FIXTURE search center, not measured physics.',
            'candidate_type': 'exploit'}), problem)
        attempt = Path(folder) / 'proposal_01'
        attempt.mkdir(parents=True, exist_ok=False)
        center['apiAttemptDirectory'] = str(attempt)
        return center

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def start(self, total=4):
        return tools.execute_task({'operation': 'optimize',
            'total_candidate_budget': total}, live=True, settings=self.settings)

    def test_new_v2_run_preserves_budget_metadata_and_frozen_batch_snapshot(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.evaluations, 7)
        self.assertEqual([len(h) for h in self.center_requests], [1, 5])
        for context in self.center_contexts:
            self.assertEqual(context['policy'], POLICY)
            self.assertEqual(context['local_radius_metric'], 'normalized_Linf')
        run = Path(result.data['run_dir'])
        state = runtime.read_json(run / 'checkpoint.json')
        first = runtime.read_json(run / 'search/generation_001/plan.json')
        second = runtime.read_json(run / 'search/generation_002/plan.json')
        self.assertEqual(state['nCompleted'], 6)
        self.assertEqual(first['selection_policy'], POLICY)
        self.assertEqual(first['local_state']['anchor_x'], state['baseline']['xActual'])
        self.assertEqual(second['local_state']['anchor_x'], state['records'][3]['detail']['xActual'])
        self.assertEqual(first['history_signature'], signature(state['history'][:1]))
        self.assertEqual(second['history_signature'], signature(state['history'][:5]))
        for record in state['records']:
            proposal = record['proposal']
            self.assertEqual(proposal['selection_policy'], POLICY)
            self.assertIn(proposal['selection_origin'], ORIGINS)
            self.assertEqual(proposal['selection_role'], ROLES[proposal['selection_origin']])
            self.assertEqual(proposal['x'], record['detail']['xActual'])
            self.assertGreater(proposal['local_radius_fraction'], 0)

    def test_partial_batch_resume_reuses_plan_and_makes_no_new_center_request(self):
        self.stop_after = 1
        first = self.start()
        self.assertFalse(first.ok, first.to_dict())
        run = Path(first.data['run_dir'])
        plan = run / 'search/generation_001/plan.json'
        saved_hash = self.digest(plan)
        self.assertEqual(len(self.center_requests), 1)
        self.stop_after = None
        resumed = tools.execute_task({'operation': 'optimize', 'run_id': run.name,
            'additional_candidate_budget': 3}, live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(self.digest(plan), saved_hash)
        self.assertEqual(len(self.center_requests), 1)
        self.assertEqual(self.evaluations, 5)
        state = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual(state['nCompleted'], 4)
        self.assertEqual(state['nLLM'], 4)
        self.assertEqual({record['proposal']['basis_history_length'] for record in state['records']}, {1})

    def test_candidate_selection_metadata_tampering_is_rejected(self):
        result = self.start()
        self.assertTrue(result.ok, result.to_dict())
        run = Path(result.data['run_dir'])
        state = runtime.read_json(run / 'checkpoint.json')
        problem = runtime.bridge.load_problem(run / 'problem.json')
        original = state['records'][0]['proposal']
        alternate_origin = next(value for value in ORIGINS if value != original['selection_origin'])
        changes = [{'selection_origin': alternate_origin},
            {'selection_role': 'global_exploration' if original['selection_role'] != 'global_exploration' else 'local_refinement'},
            {'selection_policy': 'legacy_v1'},
            {'local_radius_fraction': original['local_radius_fraction'] + .001}]
        for change in changes:
            candidate = deepcopy(original)
            candidate.update(change)
            with self.subTest(change=change), self.assertRaises((ValueError, runtime.ControllerError)):
                search.validate_generated(candidate, problem, state['history'][:1])

    def test_legacy_run_still_has_legacy_plan_shape(self):
        legacy = deepcopy(search.DEFAULT_CONFIG)
        legacy.update(strategy='center_surrogate')
        runtime.write_json(self.root / 'python_control/search_config.json', legacy)
        result = self.start()
        self.assertTrue(result.ok, result.to_dict())
        run = Path(result.data['run_dir'])
        plan = runtime.read_json(run / 'search/generation_001/plan.json')
        self.assertNotIn('selection_policy', plan)
        self.assertNotIn('pool_origins', plan)
        self.assertNotIn('local_state', plan)
        self.assertEqual(plan['config_signature'], signature(legacy))
        self.assertEqual(self.center_contexts, [None])

