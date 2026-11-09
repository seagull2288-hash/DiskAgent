"""V3 integration through real workflow/controller boundaries with fake HTTP/FEA."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import numpy as np

from agent_app import search_diagnostics, supervisor
from agent_app.tests import test_feedback_integration as fixture
from python_control import runtime, search, surrogate


class DirectionalWorkflowTests(unittest.TestCase):
    fake_proposal = fixture.FeedbackIntegrationTests.fake_proposal
    fake_evaluate = fixture.FeedbackIntegrationTests.fake_evaluate
    transport = fixture.FeedbackIntegrationTests.transport
    start = fixture.FeedbackIntegrationTests.start
    state = fixture.FeedbackIntegrationTests.state
    run_dir = fixture.FeedbackIntegrationTests.run_dir

    def setUp(self):
        fixture.FeedbackIntegrationTests.setUp(self)
        path = self.root / 'python_control/search_config.json'
        config = runtime.read_json(path)
        config.update(selection_policy='hybrid_local_v3', sigma_min=.002)
        runtime.write_json(path, config)
        self.config = config

    def test_v3_workflow_records_directional_moves_feedback_and_presolve_audit(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 7))
        state = self.state(result)
        self.assertEqual(state.target_total_candidates, 6)
        self.assertEqual(len(state.decision_trace), 2)
        for trace in state.decision_trace:
            summary = trace['selection_summary']
            self.assertEqual(summary['policy_version'], 'hybrid_local_v3')
            self.assertEqual(summary['local_method'], 'multiscale_coordinate_poll')
            self.assertGreater(summary['directional_selected'], 0)
        run = self.run_dir(result)
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 6)
        self.assertEqual(result.data['analysis']['search_selection_summary']['unknown_candidates'], 0)
        for index, record in enumerate(checkpoint['records'], 1):
            proposal = record['proposal']
            self.assertEqual(proposal['selection_policy'], 'hybrid_local_v3')
            self.assertTrue((run / 'prediction_audit' / ('iter_%03d.json' % index)).is_file())
            if proposal['selection_origin'] == 'best_local':
                self.assertIsInstance(proposal['local_move'], dict)
                self.assertTrue(proposal['local_move']['changed_parameters'])
        second_context = json.loads(self.requests[1]['input'])
        self.assertEqual(second_context['search_selection']['policy'], 'hybrid_local_v3')

    def test_partial_v3_batch_resumes_same_plan_and_budget(self):
        self.stop_after = 1
        first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        run = self.run_dir(first)
        plan = run / 'search/generation_001/plan.json'
        before = plan.read_bytes()
        self.stop_after = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(plan.read_bytes(), before)
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.state(resumed).target_total_candidates, 4)

    def test_signed_directional_move_cannot_be_replaced_in_candidate(self):
        result = self.start(total=4)
        self.assertTrue(result.ok, result.to_dict())
        checkpoint = runtime.read_json(self.run_dir(result) / 'checkpoint.json')
        original = next(record['proposal'] for record in checkpoint['records']
                        if record['proposal']['selection_origin'] == 'best_local')
        changed = deepcopy(original)
        changed['local_move'] = {'direction_kind': 'invented'}
        problem = runtime.bridge.load_problem(self.run_dir(result) / 'problem.json')
        basis = checkpoint['history'][:original['basis_history_length'] + original['planOffset']]
        with self.assertRaises(runtime.ControllerError):
            search.validate_generated(changed, problem, basis)

    def test_diagnostics_are_readonly_and_do_not_call_provider_or_evaluator(self):
        result = self.start(total=4)
        self.assertTrue(result.ok, result.to_dict())
        run = self.run_dir(result)
        paths = [run / 'checkpoint.json', run / 'history.csv', run / 'search_config.json']
        before = {path: path.read_bytes() for path in paths}
        counts = (len(self.requests), self.evaluations)
        with patch('python_control.local_surrogate.compare_models', return_value={'models': []}):
            report = search_diagnostics.inspect_search_diagnostics(run.name, settings=self.settings)
        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(report.data['trajectory']['completed_candidates'], 4)
        self.assertEqual(report.data['evidence']['api_requests'], 0)
        self.assertEqual(report.data['repeatability']['status'], 'repeat_measurements_required')
        self.assertEqual((len(self.requests), self.evaluations), counts)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_local_gp_cannot_rank_outside_its_support_and_keeps_one_poll(self):
        from python_control.directional_search import build_directional_plan
        problem = runtime.bridge.load_problem(self.settings.problem_file)
        center = [variable['reference'] for variable in problem['variables']]
        history = [{'parameters': dict(zip(problem['input_order'], center)), 'stress_mpa': 740.0}]
        grouped = build_directional_plan(center, problem, history, self.config, 0)

        class Model:
            def predict(self, z):
                return np.arange(len(z), dtype=float) + 600, np.ones(len(z))

            def support_mask(self, z):
                mask = np.ones(len(z), dtype=bool)
                mask[grouped['global_indices']] = False
                return mask

        with patch('python_control.local_surrogate.fit_local_validated', return_value=(Model(), {
                'enabled': True, 'reason': 'fixture_validated_local', 'model_scope': 'local'})):
            selected, report, predictions = surrogate.select_hybrid_pool(
                grouped, history, problem, self.config)
        self.assertIn(grouped['fallback_local_indices'][0], selected)
        self.assertTrue(report['surrogate_used'])
        self.assertEqual(report['directional_selected'], 1)
        self.assertTrue(all(not predictions[i]['within_validated_region'] for i in grouped['global_indices']))
        broken = deepcopy(grouped)
        broken['fallback_local_indices'] = [grouped['local_indices'][0]] * len(grouped['local_indices'])
        with self.assertRaises(runtime.ControllerError):
            surrogate.select_hybrid_pool(broken, history, problem, self.config, enabled=False)


if __name__ == '__main__':
    unittest.main()
