"""Real HTTP routes, task previews and archive re-evaluation without real FEA."""
from copy import deepcopy
import threading
import time
import unittest

from agent_app import supervisor, web
from agent_app.tests import test_objective_workflow as workflow_fixture
from agent_app.tests import test_web as web_fixture


class ObjectiveWebTests(unittest.TestCase):
    def setUp(self):
        workflow_fixture.ObjectiveWorkflowTests.setUp(self)
        self.server = web.create_server(settings=self.settings, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.02}, daemon=True)
        self.thread.start()
        self.token = self.request('GET', '/api/config')[1]['csrf_token']

    def tearDown(self):
        deadline = time.monotonic() + 3
        while self.server.active_job_id and time.monotonic() < deadline:
            time.sleep(0.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    fake_proposal = workflow_fixture.ObjectiveWorkflowTests.fake_proposal
    transport = workflow_fixture.ObjectiveWorkflowTests.transport
    state = workflow_fixture.ObjectiveWorkflowTests.state
    run_dir = workflow_fixture.ObjectiveWorkflowTests.run_dir
    start = workflow_fixture.ObjectiveWorkflowTests.start
    request = web_fixture.WebTests.request
    wait_job = web_fixture.WebTests.wait_job

    def fake_evaluate(self, *args, **kwargs):
        detail = workflow_fixture.ObjectiveWorkflowTests.fake_evaluate(self, *args, **kwargs)
        if args[1].optimization_policy is None:
            detail.pop('mass_kg')
        return detail

    def test_new_panel_and_javascript_asset_are_served(self):
        status, html, _ = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('id="objective-policy"', html)
        self.assertIn('data-tab="objectives"', html)
        self.assertIn('<script src="/objectives.js"></script>', html)
        status, javascript, headers = self.request('GET', '/objectives.js')
        self.assertEqual(status, 200)
        self.assertIn('readOptimizationPolicy', javascript)
        self.assertIn('text/javascript', headers['Content-Type'])
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))

    def test_browser_preview_saves_exact_policy_and_remains_offline(self):
        status, submitted, _ = self.request('POST', '/api/workflows', {
            'request': '新建优化6个候选', 'live': False, 'use_llm': False,
            'optimization_policy': self.policy})
        self.assertEqual(status, 202)
        job = self.wait_job(submitted['job_id'])
        self.assertTrue(job['ok'], job)
        self.assertEqual(job['result']['data']['task']['optimization_policy'], self.policy)
        self.assertEqual(job['result']['data']['task']['objective'], 'configured_objectives')
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))

    def test_re_evaluation_changes_recommendation_without_rewriting_run_or_computing(self):
        result = self.start(live=True)
        self.assertTrue(result.ok, result.to_dict())
        folder = self.run_dir(result)
        before = {path.relative_to(folder): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        counts = len(self.requests), self.evaluations
        policy = deepcopy(self.policy)
        policy['objectives'][0]['weight'] = 9
        status, response, _ = self.request('POST', '/api/runs/' + folder.name + '/objectives', {
            'optimization_policy': policy})
        self.assertEqual(status, 200, response)
        self.assertEqual(response['data']['best_iteration'], 2)
        selections = response['data']['design_selection']
        self.assertEqual(selections['minimum_stress']['iteration'], 1)
        self.assertEqual(selections['recommended']['iteration'], 2)
        self.assertEqual(selections['policy_id'], response['data']['policy_id'])
        after = {path.relative_to(folder): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((len(self.requests), self.evaluations), counts)

    def test_old_stress_only_history_displays_missing_mass_and_no_dual_recommendation(self):
        result = supervisor.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 1},
                                         live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        folder = self.run_dir(result)
        status, response, _ = self.request('POST', '/api/runs/' + folder.name + '/objectives', {
            'optimization_policy': self.policy})
        self.assertEqual(status, 200, response)
        self.assertIsNone(response['data']['best_iteration'])
        self.assertEqual(response['data']['feasible_count'], 0)
        self.assertTrue(all(item['status'] == 'missing' for item in response['data']['evaluations']))
        self.assertTrue(all('mass_kg' not in item for item in response['data']['observations']))
        self.assertIsNone(response['data']['design_selection']['recommended'])
        self.assertEqual(response['data']['design_selection']['minimum_stress']['evaluation']['status'], 'missing')

    def test_enabled_constraint_without_limit_is_rejected_before_job_submission(self):
        policy = deepcopy(self.policy)
        policy['constraints'][0]['value'] = None
        status, response, _ = self.request('POST', '/api/workflows', {
            'request': '新建优化1个候选', 'live': False, 'optimization_policy': policy})
        self.assertEqual(status, 400)
        self.assertEqual(response['error']['code'], 'OPTIMIZATION_POLICY')
        self.assertIsNone(self.server.active_job_id)
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))
