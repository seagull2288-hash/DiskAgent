"""Real loopback MCP bridge, queue and persistence; native solving is replaced at its boundary."""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import threading
import time
from unittest.mock import patch

from agent_app import web
from agent_app.mcp_bridge import BridgeError, WorkbenchClient, compact_report
from agent_app.mcp_jobs import JobStore
from agent_app.schemas import ToolResult
from agent_app.state import WorkflowStore
from agent_app.tests.test_contract import FixtureCase
from python_control import runtime


class MCPBridgeTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.patches = ExitStack()
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        self.entered = threading.Event()
        self.native = self.patches.enter_context(patch('python_control.workbench.evaluate', side_effect=self.fake_native))
        self.verified = self.patches.enter_context(patch('python_control.workbench.verify_native'))
        self.workflow = self.patches.enter_context(patch.object(web, 'run_workflow', side_effect=self.fake_workflow))
        self.server = web.create_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.client = WorkbenchClient('http://127.0.0.1:' + str(self.server.server_address[1]), timeout=3)
        self.parameters = {v['name']: v['reference'] for v in self.problem['variables']}

    def tearDown(self):
        self.release.set()
        deadline = time.monotonic() + 3
        while self.server.active_job_id and time.monotonic() < deadline:
            time.sleep(.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.patches.close()
        super().tearDown()

    def fake_native(self, x, settings, problem, output_root, baseline=None):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('Fake native timed out')
        folder = Path(output_root) / 'eval_mock'
        folder.mkdir()
        report = folder / 'mechanical_verified.json'
        runtime.write_json(report, {'success': True, 'test_boundary': True})
        return {'runDir': str(folder), 'nativeReportFile': str(report), 'xActual': x,
                'stressMPa': 650., 'displacementMM': .4, 'area': .02}

    def fake_workflow(self, *, task, live, settings, use_llm, auto_recover, on_workflow_created):
        self.assertTrue(live)
        self.assertFalse(use_llm)
        state = WorkflowStore(settings).create('MCP test')
        on_workflow_created(state.workflow_id)
        return ToolResult(True, 'workflow', 'completed', {'workflow_id': state.workflow_id, 'task': task})

    def wait(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.client.job(job_id)
            if result['job']['status'] == 'completed':
                return result['job']
            time.sleep(.01)
        self.fail('Background job timed out')

    def action(self, **changes):
        return self.client.action('evaluate-design', dict(parameters=self.parameters, **changes))

    def test_discovery_and_preview_do_not_execute_or_create_jobs(self):
        self.assertEqual(self.client.config()['mcp_bridge']['version'], 'disk-agent.mcp.v1')
        case = self.client.read_case()
        self.assertTrue(case['ok'], case)
        result = self.action()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(result['data']['llm_requests'], 0)
        self.assertFalse((self.root / 'agent_jobs').exists())
        self.native.assert_not_called()
        self.workflow.assert_not_called()

    def test_invalid_dimensions_and_live_flags_never_launch(self):
        for params in ({}, dict(self.parameters, X1=True), dict(self.parameters, X1=1e10),
                       dict(self.parameters, script='execute()')):
            result = self.client.action('evaluate-design', {'parameters': params, 'live': True, 'request_id': 'invalid'})
            self.assertFalse(result['ok'], result)
        for value in (None, 1, 'true'):
            self.assertFalse(self.action(live=value)['ok'])
        self.assertFalse(self.action(live=True)['ok'])
        self.native.assert_not_called()

    def test_live_single_design_is_verified_serial_and_persisted(self):
        self.release.clear()
        submitted = self.action(live=True, request_id='design-one')
        self.assertTrue(submitted['ok'], submitted)
        self.assertTrue(self.entered.wait(2))
        # Web and MCP use the exact same busy gate.
        busy = self.action(live=True, request_id='design-two')
        self.assertEqual(busy['error']['code'], 'WEB_BUSY')
        replay = self.action(live=True, request_id='design-one')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['job_id'], submitted['job_id'])
        self.release.set()
        job = self.wait(submitted['job_id'])
        self.assertTrue(job['ok'], job)
        self.assertEqual(job['result']['data']['observation']['stress_mpa'], 650)
        self.assertFalse(job['result']['data']['affects_optimizer_history'])
        self.verified.assert_called_once()
        self.assertEqual(self.calls, 1)
        self.assertEqual(list(self.output.iterdir()), [])
        self.server.jobs.clear()  # Simulate reading persisted result after app restart.
        self.assertEqual(self.client.job(submitted['job_id'])['job']['result'], job['result'])
        again = self.action(live=True, request_id='design-one')
        self.assertEqual(again['job_id'], submitted['job_id'])
        self.assertEqual(self.calls, 1)

    def test_changed_idempotency_arguments_conflict(self):
        job = self.action(live=True, request_id='one')
        self.wait(job['job_id'])
        params = dict(self.parameters, X1=self.parameters['X1'] + .01)
        result = self.client.action('evaluate-design', dict(parameters=params, live=True, request_id='one'))
        self.assertEqual(result['error']['code'], 'REQUEST_CONFLICT')
        self.assertEqual(self.calls, 1)

    def test_native_failure_releases_busy_gate_and_is_not_retried(self):
        self.native.side_effect = RuntimeError('fake native error')
        job = self.action(live=True, request_id='failure')
        result = self.wait(job['job_id'])
        self.assertFalse(result['ok'])
        self.assertIn('fake native error', result['result']['error']['message'])
        replay = self.action(live=True, request_id='failure')
        self.assertEqual(replay['job_id'], job['job_id'])
        self.assertIsNone(self.server.active_job_id)
        self.assertEqual(self.native.call_count, 1)

    def test_engine_project_lock_also_blocks_native_evaluation(self):
        with runtime.project_lock(self.root):
            job = self.action(live=True, request_id='locked')
            result = self.wait(job['job_id'])
        self.assertFalse(result['ok'])
        self.native.assert_not_called()

    def test_task_budget_and_policy_use_existing_workflow_and_replay(self):
        task = {'operation': 'optimize', 'total_candidate_budget': 3}
        preview = self.client.action('submit-optimization', {'task': task})
        self.assertTrue(preview['ok'], preview)
        self.workflow.assert_not_called()
        submitted = self.client.action('submit-optimization', dict(task=task, live=True, request_id='budget3'))
        job = self.wait(submitted['job_id'])
        self.assertTrue(job['ok'], job)
        self.assertEqual(job['result']['data']['task']['total_candidate_budget'], 3)
        self.assertIsNotNone(job['workflow_id'])
        self.workflow.assert_called_once()
        replay = self.client.action('submit-optimization', dict(task=task, live=True, request_id='budget3'))
        self.assertEqual(replay['job_id'], submitted['job_id'])
        self.workflow.assert_called_once()

    def test_malformed_task_and_extra_script_are_rejected(self):
        for task in ({'operation': 'check'}, {'operation': 'optimize'},
                     {'operation': 'optimize', 'total_candidate_budget': True}):
            self.assertFalse(self.client.action('submit-optimization', {'task': task})['ok'])
        result = self.client.action('evaluate-design', dict(parameters=self.parameters, script='anything'))
        self.assertEqual(result['error']['code'], 'MCP_FIELDS')
        self.workflow.assert_not_called()
        self.native.assert_not_called()

    def test_stale_queued_record_is_detached_never_automatically_replayed(self):
        store = self.server.job_store
        job_id = 'job_' + 'a' * 24
        store.save({'job_id': job_id, 'status': 'running', 'workflow_id': None})
        record = self.client.job(job_id)['job']
        self.assertEqual(record['status'], 'detached')
        self.assertEqual(record['recorded_status'], 'running')
        self.native.assert_not_called()

    def test_ids_and_loopback_origin_cannot_escape(self):
        for url in ('https://127.0.0.1:8766', 'http://example.com:8766', 'http://localhost:8766',
                    'http://127.0.0.1:8766/private', 'http://u:p@127.0.0.1:8766'):
            with self.assertRaises(BridgeError):
                WorkbenchClient(url)
        for method in (self.client.job, self.client.run, self.client.workflow):
            with self.assertRaises(BridgeError):
                method('../api/config')

    def test_compact_report_keeps_recommendation_and_constraints(self):
        result = compact_report({'ok': True, 'data': {'samples': [{}] * 100,
            'design_selection': {'recommended': {'iteration': 3}},
            'optimization': {'observations': [{}] * 100, 'evaluations': [{}] * 100,
                             'policy': {'constraints': ['mass']}}}})
        self.assertEqual(result['data']['samples_count'], 100)
        self.assertEqual(result['data']['optimization']['policy']['constraints'], ['mass'])
        self.assertEqual(result['data']['design_selection']['recommended']['iteration'], 3)
