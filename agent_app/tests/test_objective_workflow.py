"""Offline full workflow with real HTTP serialization and fake physical results."""
from copy import deepcopy
import csv
import json
import unittest

from agent_app import supervisor, tools
from agent_app.state import WorkflowStore
from agent_app.tests import test_feedback_integration as feedback_fixture
from agent_app.tests.test_objectives import dual_policy, mass_limit
from python_control import objectives, runtime


class ObjectiveWorkflowTests(unittest.TestCase):
    def setUp(self):
        feedback_fixture.FeedbackIntegrationTests.setUp(self)
        self.policy = dual_policy()
        self.policy['constraints'] = [mass_limit()]

    fake_proposal = feedback_fixture.FeedbackIntegrationTests.fake_proposal
    transport = feedback_fixture.FeedbackIntegrationTests.transport
    state = feedback_fixture.FeedbackIntegrationTests.state
    run_dir = feedback_fixture.FeedbackIntegrationTests.run_dir

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        index = self.evaluations
        detail = feedback_fixture.FeedbackIntegrationTests.fake_evaluate(
            self, x, settings, problem, output_root, baseline)
        values = [(1000, 10), (800, 12), (900, 9), (950, 8), (980, 8.4), (970, 7), (990, 7.5)]
        detail['stressMPa'], detail['mass_kg'] = values[index]
        return detail

    def start(self, total=6, live=False):
        return supervisor.run_workflow(request='新建优化' + str(total) + '个候选',
                                       optimization_policy=self.policy, live=live, settings=self.settings)

    def test_preview_freezes_policy_without_provider_or_solver_calls(self):
        result = self.start()
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))
        state = self.state(result)
        self.assertEqual(state.task['objective'], 'configured_objectives')
        self.assertEqual(state.engine_definition['optimization_policy'], self.policy)
        self.assertEqual(result.data['optimization_policy_id'], objectives.policy_id(self.policy))
        self.policy['objectives'][0]['weight'] = 9
        self.assertEqual(self.state(result).task['optimization_policy']['objectives'][0]['weight'], 1)

    def test_saved_preview_drives_requests_selection_evaluation_and_report(self):
        preview = self.start()
        result = supervisor.resume_workflow(preview.data['workflow_id'], live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 7))
        state = self.state(result)
        evaluated = state.analysis['optimization']
        self.assertEqual(evaluated['best_iteration'], 5)
        self.assertEqual(evaluated['pareto_iterations'], [2, 3, 5])
        self.assertEqual(evaluated['evaluations'][1]['status'], 'infeasible')
        second = json.loads(self.requests[1]['input'])
        self.assertEqual(second['objective']['objectives'], self.policy['objectives'])
        self.assertEqual(second['optimization']['best_iteration'], 3)
        self.assertEqual(second['batch_feedback']['source_completed_candidates'], 4)
        root = self.run_dir(result)
        selected = runtime.read_json(root / 'selected_design.json')
        self.assertEqual(selected['iteration'], 5)
        self.assertEqual(selected['observation']['mass_kg'], 7)
        selections = state.analysis['design_selection']
        self.assertEqual(selections['minimum_stress']['iteration'], 1)
        self.assertEqual(selections['minimum_stress']['evaluation']['status'], 'infeasible')
        self.assertEqual(selections['recommended']['iteration'], 5)
        self.assertEqual(selections['recommended']['evaluation']['constraint_checks'][0]['margin'], 3)
        self.assertEqual(runtime.read_json(root / 'design_selection.json'), selections)
        self.assertEqual(runtime.read_json(root / 'status.json')['design_selection'], selections)
        self.assertEqual(tools.get_run_status(root.name, settings=self.settings).data['design_selection'], selections)
        minimum = runtime.read_json(root / 'best_design.json')
        self.assertEqual((minimum['iteration'], minimum['selection_basis']), (1, 'minimum_stress'))
        self.assertEqual(minimum['evaluation']['status'], 'infeasible')
        self.assertEqual(selected['evaluation']['status'], 'feasible')
        with (root / 'history.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[1]['SelectionRoles'], 'minimum_stress')
        self.assertEqual(rows[1]['ConfiguredStatus'], 'infeasible')
        self.assertEqual(rows[5]['SelectionRoles'], 'recommended|pareto')
        self.assertEqual(rows[5]['PolicyID'], evaluated['policy_id'])
        checkpoint = runtime.read_json(root / 'checkpoint.json')
        self.assertEqual(len(checkpoint['history']), 7)
        self.assertEqual(len(list((root / 'prediction_audit').glob('iter_*.json'))), 6)
        report = (WorkflowStore(self.settings).directory(state.workflow_id) / 'report.md').read_text(encoding='utf-8')
        self.assertIn(evaluated['policy_id'], report)
        self.assertIn('整盘质量 kg', report)
        self.assertIn('探索性基准约束', report)
        self.assertIn('最小应力方案', report)
        self.assertIn('当前配置推荐方案', report)
        self.assertIn('Pareto 方案', report)
        self.assertIn('裕量', report)
        self.assertIn('-2.000000 kg', report)
        self.assertIn('3.000000 kg', report)

    def test_no_feasible_design_exports_no_recommendation_and_explains_it(self):
        self.policy['constraints'] = [mass_limit(0.5)]
        result = self.start(total=1, live=True)
        self.assertTrue(result.ok, result.to_dict())
        root = self.run_dir(result)
        selections = self.state(result).analysis['design_selection']
        self.assertIsNone(selections['recommended'])
        self.assertEqual(selections['pareto'], [])
        exported = runtime.read_json(root / 'selected_design.json')
        self.assertEqual(exported['status'], 'no_feasible_design')
        self.assertIsNone(exported['evaluation'])
        self.assertIsNone(exported['observation'])
        self.assertEqual(selections['minimum_stress']['evaluation']['status'], 'infeasible')

    def test_new_weights_cannot_be_applied_to_an_existing_checkpoint(self):
        result = self.start(total=1, live=True)
        self.assertTrue(result.ok, result.to_dict())
        changed = deepcopy(self.policy)
        changed['objectives'][0]['weight'] = 9
        before = len(self.requests), self.evaluations
        attempted = tools.execute_task({'operation': 'optimize', 'run_id': self.state(result).run_id,
                                         'additional_candidate_budget': 1, 'optimization_policy': changed},
                                        live=True, settings=self.settings)
        self.assertFalse(attempted.ok)
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_mass_only_objective_reaches_prompt_and_selects_lightest_feasible_design(self):
        self.policy['objectives'] = [{'metric': 'mass_kg', 'direction': 'minimize', 'weight': 1.0}]
        result = self.start(total=4, live=True)
        self.assertTrue(result.ok, result.to_dict())
        evaluated = self.state(result).analysis['optimization']
        self.assertEqual(evaluated['best_iteration'], 3)
        context = json.loads(self.requests[0]['input'])
        self.assertEqual(context['objective']['objectives'], self.policy['objectives'])

    def test_stress_objective_can_collect_mass_without_using_it_for_ranking(self):
        self.policy = objectives.default_policy()
        self.policy['collect_mass'] = True
        result = self.start(total=2, live=True)
        self.assertTrue(result.ok, result.to_dict())
        evaluated = self.state(result).analysis['optimization']
        self.assertEqual(evaluated['best_iteration'], 1)
        self.assertEqual(evaluated['observations'][1]['mass_kg'], 12)

    def test_completed_policy_run_can_append_with_original_policy_and_no_new_baseline(self):
        first = self.start(total=1, live=True)
        self.assertTrue(first.ok, first.to_dict())
        result = supervisor.run_workflow(task={'operation': 'optimize', 'run_id': self.state(first).run_id,
                                               'additional_candidate_budget': 1},
                                         live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.evaluations, 3)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.state(result).task['optimization_policy'], self.policy)
