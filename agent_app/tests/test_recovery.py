"""Durable automatic recovery over real control flow and fake HTTP/FEA only."""
import io
import json
from pathlib import Path
import threading
import urllib.error
import unittest
from unittest.mock import patch

from agent_app import recovery, supervisor
from agent_app import recovery_evidence
from agent_app.state import WorkflowStore
from agent_app.tests import test_feedback_integration as feedback_fixture
from agent_app.tests import test_direct_optimization as direct_fixture
from python_control import controller, objectives, runtime


class RecoveryIntegrationTests(unittest.TestCase):
    fake_proposal = feedback_fixture.FeedbackIntegrationTests.fake_proposal
    fake_evaluate = feedback_fixture.FeedbackIntegrationTests.fake_evaluate
    state = feedback_fixture.FeedbackIntegrationTests.state
    run_dir = feedback_fixture.FeedbackIntegrationTests.run_dir

    def setUp(self):
        original_client = runtime.bridge.Client
        feedback_fixture.FeedbackIntegrationTests.setUp(self)
        self.actions = []
        self.default_action = 'ok'
        self.inner_delays = []
        self.outer_delays = []
        self.fake_now = 1800000000.0
        runtime.bridge.load_config.return_value.update(max_http_attempts=2,
            max_proposal_attempts=2, retry_delay_seconds=0,
            max_retry_delay_seconds=0)

        def client_factory(api, *, live):
            self.assertIs(live, True)
            return original_client(api, live=live, transport=self.transport,
                                   sleep=self.inner_delays.append)

        block = patch.object(runtime.bridge, 'Client', side_effect=client_factory)
        block.start()
        self.addCleanup(block.stop)
        for clock in (
            patch.object(recovery, '_now_epoch', side_effect=lambda: self.fake_now),
            patch.object(recovery, '_wait_until', side_effect=self.advance_until),
        ):
            clock.start()
            self.addCleanup(clock.stop)

    def advance_until(self, deadline, stop):
        if stop is not None and stop.is_set():
            return False
        self.outer_delays.append(max(0.0, deadline - self.fake_now))
        self.fake_now = max(self.fake_now, deadline)
        return True

    def transport(self, request, *, timeout):
        action = self.actions.pop(0) if self.actions else self.default_action
        if type(action) is int:
            self.requests.append(json.loads(request.data.decode('utf-8')))
            raise urllib.error.HTTPError(request.full_url, action, 'fixture HTTP failure',
                {'Retry-After': '0'}, io.BytesIO(b'fixture response body'))
        if action == 'network':
            self.requests.append(json.loads(request.data.decode('utf-8')))
            raise urllib.error.URLError('fixture unknown request delivery')
        if action == 'interrupt':
            self.requests.append(json.loads(request.data.decode('utf-8')))
            raise KeyboardInterrupt('Fixture interruption after durable HTTP started event.')
        if action == 'schema':
            previous = self.response_text
            self.response_text = '{"wrong_schema":true}'
            try:
                return feedback_fixture.FeedbackIntegrationTests.transport(self, request, timeout=timeout)
            finally:
                self.response_text = previous
        self.assertEqual(action, 'ok')
        return feedback_fixture.FeedbackIntegrationTests.transport(self, request, timeout=timeout)

    def start(self, total=4, *, auto=True, live=True, stop=None):
        return recovery.run_workflow(task={'operation': 'optimize',
            'total_candidate_budget': total}, live=live, auto_recover=auto,
            recovery_stop=stop, settings=self.settings)

    def plain_start(self, total=4):
        return supervisor.run_workflow(task={'operation': 'optimize',
            'total_candidate_budget': total}, live=True, settings=self.settings)

    def resume(self, workflow_id, *, auto=True, live=True, stop=None):
        return recovery.resume_workflow(workflow_id, live=live, auto_recover=auto,
            recovery_stop=stop, settings=self.settings)

    def sidecar(self, result):
        path = WorkflowStore(self.settings).directory(self.state(result).workflow_id) / 'recovery.json'
        return runtime.read_json(path)

    def http_started(self, result):
        count = 0
        for path in self.run_dir(result).glob('search/generation_*/center_api/request_*/proposal_*/transport.json'):
            events = runtime.read_json(path)
            count += sum(item.get('status') == 'started' for item in events)
        return count

    def assert_budget(self, result, *, target, done):
        state = self.state(result)
        checkpoint = runtime.read_json(self.run_dir(result) / 'checkpoint.json')
        self.assertEqual(state.target_total_candidates, target)
        self.assertEqual(checkpoint['nCompleted'], done)
        self.assertEqual(len(checkpoint['records']), done)
        self.assertEqual(len(checkpoint['history']), done + 1)
        self.assertIsNotNone(checkpoint['baseline'])

    def test_explicit_resume_rebuilds_failed_analysis_without_recomputing_results(self):
        with patch.object(supervisor, '_write_analysis', side_effect=OSError('Fixture report write failure.')):
            failed = self.plain_start(total=2)
        self.assertFalse(failed.ok, failed.to_dict())
        checkpoint = runtime.read_json(self.run_dir(failed) / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 2)
        before = len(self.requests), self.evaluations
        resumed = self.resume(self.state(failed).workflow_id)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)
        self.assertEqual(self.outer_delays, [])
        self.assert_budget(resumed, target=2, done=2)

    def test_explicit_resume_uses_accepted_candidate_after_solver_failure(self):
        self.fail_at = 1
        failed = self.plain_start(total=1)
        self.assertFalse(failed.ok, failed.to_dict())
        checkpoint = runtime.read_json(self.run_dir(failed) / 'checkpoint.json')
        self.assertEqual(checkpoint['pending']['phase'], 'solving')
        self.assertIsNotNone(checkpoint['pending']['proposal'])
        requests_before = len(self.requests)
        self.fail_at = None
        resumed = self.resume(self.state(failed).workflow_id)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(len(self.requests), requests_before)
        self.assertEqual(self.outer_delays, [])
        self.assert_budget(resumed, target=1, done=1)

    def test_completed_recovery_separates_current_error_from_historical_error(self):
        self.actions = [500, 500, 'ok']
        result = self.start()
        self.assertTrue(result.ok, result.to_dict())
        view = recovery.inspect_recovery(self.state(result).workflow_id, settings=self.settings)
        self.assertEqual(view['status'], 'completed')
        self.assertIsNone(view['last_error'])
        self.assertEqual(view['last_recovered_error']['code'], 'HTTP_ERROR')
        self.assertTrue(view['events'])

    def test_previous_recovery_does_not_hide_a_later_manual_solver_failure(self):
        self.default_action = 500
        first = self.plain_start(total=1)
        store = WorkflowStore(self.settings)
        workflow = store.load(self.state(first).workflow_id)
        saved = recovery._new(workflow)
        saved['last_error'] = workflow.error
        recovery._save(store, saved, 'recovered', '旧请求故障已恢复。')
        self.default_action = 'ok'
        self.fail_at = 1
        failed = supervisor.resume_workflow(workflow.workflow_id, live=True, settings=self.settings)
        self.assertFalse(failed.ok, failed.to_dict())
        current_error = store.load(workflow.workflow_id).error
        self.assertNotEqual(current_error['code'], 'HTTP_ERROR')
        view = recovery.inspect_recovery(workflow.workflow_id, settings=self.settings)
        self.assertEqual(view['status'], 'needs_review')
        self.assertEqual(view['last_error'], current_error)
        self.assertNotIn('last_recovered_error', view)

    def test_preview_and_inspection_do_not_execute_or_create_recovery_budget(self):
        result = self.start(auto=True, live=False)
        self.assertTrue(result.ok, result.to_dict())
        workflow_id = self.state(result).workflow_id
        folder = WorkflowStore(self.settings).directory(workflow_id)
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        recovery.inspect_recovery(workflow_id, settings=self.settings)
        after = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))
        self.assertEqual(before, after)
        self.assertFalse((folder / 'recovery.json').exists())

    def test_auto_false_preserves_original_failure_without_outer_retries(self):
        self.default_action = 500
        result = self.start(auto=False)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        self.assertEqual(self.http_started(result), 2)
        self.assert_budget(result, target=4, done=0)
        folder = WorkflowStore(self.settings).directory(self.state(result).workflow_id)
        self.assertFalse((folder / 'recovery.json').exists())

    def test_http500_recovers_once_without_repeating_baseline_or_extending_budget(self):
        self.actions = [500, 500, 'ok']
        result = self.start()
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (3, 5))
        self.assertEqual(self.http_started(result), 3)
        self.assert_budget(result, target=4, done=4)
        self.assertTrue(self.sidecar(result))
        self.assertEqual(self.outer_delays, [30.0])

    def test_http502_and_504_can_recover_with_the_same_budget(self):
        for code in (502, 504):
            self.actions = [code, code, 'ok']
            before = len(self.requests), self.evaluations
            result = self.start()
            with self.subTest(code=code):
                self.assertTrue(result.ok, result.to_dict())
                self.assertEqual((len(self.requests) - before[0], self.evaluations - before[1]), (3, 5))
                self.assert_budget(result, target=4, done=4)

    def test_persistent_http500_exhausts_two_automatic_resumes_per_incident(self):
        self.default_action = 500
        result = self.start()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (6, 1))
        self.assertEqual(self.http_started(result), 6)
        self.assert_budget(result, target=4, done=0)
        self.assertEqual(self.outer_delays, [30.0, 120.0])
        prior = len(self.requests)
        again = self.resume(self.state(result).workflow_id)
        self.assertFalse(again.ok, again.to_dict())
        self.assertEqual(len(self.requests), prior)

    def test_existing_four_http_attempts_do_not_double_count_reserved_budget(self):
        self.default_action = 500
        first = self.plain_start()
        second = supervisor.resume_workflow(self.state(first).workflow_id,
                                           live=True, settings=self.settings)
        self.assertFalse(second.ok)
        self.assertEqual(self.http_started(second), 4)
        result = self.resume(self.state(second).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.http_started(result), 8)
        self.assertEqual((len(self.requests), self.evaluations), (8, 1))
        self.assert_budget(result, target=4, done=0)

    def test_schema_retry_reserves_worst_case_and_stops_before_exceeding_http_cap(self):
        self.default_action = 500
        first = self.plain_start()
        second = supervisor.resume_workflow(self.state(first).workflow_id,
                                           live=True, settings=self.settings)
        self.assertEqual(self.http_started(second), 4)
        # First proposal consumes two HTTP attempts then returns invalid schema.
        # The second proposal consumes two more before its terminal HTTP 500.
        self.actions = [500, 'schema', 500, 500]
        result = self.resume(self.state(second).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.http_started(result), 8)
        self.assertEqual((len(self.requests), self.evaluations), (8, 1))
        self.assertEqual(self.actions, [])
        again = self.resume(self.state(result).workflow_id)
        self.assertFalse(again.ok)
        self.assertEqual(self.http_started(again), 8)

    def test_eight_existing_attempts_prevent_a_four_attempt_reservation(self):
        self.default_action = 500
        result = self.plain_start()
        for _ in range(3):
            result = supervisor.resume_workflow(self.state(result).workflow_id,
                                                live=True, settings=self.settings)
        self.assertEqual(self.http_started(result), 8)
        result = self.resume(self.state(result).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.http_started(result), 8)

    def test_progress_into_next_batch_allows_new_episode_without_resetting_workflow_count(self):
        self.actions = [500, 500, 'ok', 500, 500, 'ok']
        result = self.start(total=8)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (6, 9))
        self.assert_budget(result, target=8, done=8)
        self.assertTrue(self.sidecar(result))

    def test_workflow_global_six_resume_limit_survives_new_incidents(self):
        self.actions = ([500, 500, 'ok'] * 6) + [500, 500]
        self.default_action = 500
        result = self.start(total=28)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (20, 25))
        self.assert_budget(result, target=28, done=24)
        again = self.resume(self.state(result).workflow_id)
        self.assertFalse(again.ok)
        self.assertEqual(len(self.requests), 20)

    def test_nonwhitelisted_http_and_unknown_delivery_remain_manual(self):
        for code, expected_calls in ((401, 1), (403, 1), (429, 2), (503, 2)):
            self.default_action = code
            before = len(self.requests), self.evaluations
            result = self.start()
            with self.subTest(code=code):
                self.assertFalse(result.ok, result.to_dict())
                self.assertEqual((len(self.requests) - before[0], self.evaluations - before[1]),
                                 (expected_calls, 1))
        self.default_action = 'network'
        before = len(self.requests), self.evaluations
        result = self.start()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests) - before[0], self.evaluations - before[1]), (1, 1))

    def test_geometry_failure_has_no_automatic_recovery(self):
        self.fail_at = 1
        result = self.start()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        self.assert_budget(result, target=4, done=0)

    def test_engine_compatibility_failure_stops_before_another_http_request(self):
        self.default_action = 500
        first = self.plain_start()
        with patch.object(runtime, 'fingerprint', return_value={'modified': 'review required'}), \
                patch.object(controller, 'fingerprint', return_value={'modified': 'review required'}):
            result = self.resume(self.state(first).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))

    def test_native_evidence_failure_stops_before_wait_or_http_request(self):
        self.default_action = 500
        first = self.plain_start()
        with patch.object(controller, 'verify_native',
                          side_effect=RuntimeError('fixture native evidence is invalid')):
            result = self.resume(self.state(first).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        self.assertEqual(self.outer_delays, [])

    def test_pending_state_or_batch_basis_corruption_blocks_recovery(self):
        self.default_action = 500
        for change in ('phase', 'proposal', 'basis'):
            first = self.plain_start()
            run = self.run_dir(first)
            if change == 'basis':
                path = run / 'search/generation_001/basis.json'
                basis = runtime.read_json(path)
                basis['history_signature'] = '0' * 64
                runtime.write_json(path, basis)
            else:
                path = run / 'checkpoint.json'
                checkpoint = runtime.read_json(path)
                if change == 'phase':
                    checkpoint['pending']['phase'] = 'solving'
                else:
                    checkpoint['pending']['proposal'] = {'x': [0] * 9}
                runtime.write_json(path, checkpoint)
            before = len(self.requests), self.evaluations
            result = self.resume(self.state(first).workflow_id)
            with self.subTest(change=change):
                self.assertFalse(result.ok, result.to_dict())
                self.assertEqual((len(self.requests), self.evaluations), before)
                self.assertEqual(self.outer_delays, [])

    def test_process_interruption_preserves_wait_and_explicit_resume_waits_only_remainder(self):
        self.actions = [500, 500, 'ok']
        deadlines = []

        def interrupt_during_wait(deadline, event):
            self.assertFalse(event.is_set())
            deadlines.append(deadline)
            self.fake_now += 10.0
            raise KeyboardInterrupt('Fixture process interruption during persisted wait.')

        with patch.object(recovery, '_wait_until', side_effect=interrupt_during_wait):
            first = self.start()
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        saved = self.sidecar(first)
        self.assertIsNotNone(saved['next_retry_at'])
        self.assertEqual(len(deadlines), 1)
        before = len(self.requests), self.evaluations
        recovery.inspect_recovery(self.state(first).workflow_id, settings=self.settings)
        self.assertEqual((len(self.requests), self.evaluations), before)
        self.assertEqual(self.sidecar(first), saved)
        resumed = self.resume(self.state(first).workflow_id, stop=threading.Event())
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(self.outer_delays, [20.0])
        self.assertEqual((len(self.requests), self.evaluations), (3, 5))
        self.assert_budget(resumed, target=4, done=4)

    def test_stop_event_cancels_next_retry_and_keeps_the_reserved_attempt(self):
        self.default_action = 500
        stop = threading.Event()

        def stop_during_wait(deadline, event):
            self.assertIs(event, stop)
            event.set()
            return False

        with patch.object(recovery, '_wait_until', side_effect=stop_during_wait):
            result = self.start(stop=stop)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        saved = self.sidecar(result)
        self.assertEqual(saved['status'], 'stopped')
        self.assertIsNone(saved['next_retry_at'])
        self.assertEqual(saved['total_attempts'], 1)
        self.assertEqual(saved['episode']['attempts'], 1)

    def test_interrupted_inflight_retry_stays_manual_without_a_free_resume_request(self):
        self.actions = [500, 500, 'interrupt']
        result = self.start()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (3, 1))
        self.assertEqual(self.sidecar(result)['status'], 'retrying')
        before = len(self.requests), self.evaluations
        resumed = self.resume(self.state(result).workflow_id)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)
        self.assertEqual(self.sidecar(resumed)['status'], 'needs_review')

    def test_persisted_total_wait_limit_blocks_further_scheduling(self):
        self.default_action = 500
        stop = threading.Event()

        def stop_wait(deadline, event):
            event.set()
            return False

        with patch.object(recovery, '_wait_until', side_effect=stop_wait):
            first = self.start(stop=stop)
        folder = WorkflowStore(self.settings).directory(self.state(first).workflow_id)
        saved = self.sidecar(first)
        saved['total_wait_seconds'] = 600
        runtime.write_json(folder / 'recovery.json', saved)
        self.outer_delays.clear()
        result = self.resume(self.state(first).workflow_id)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        self.assertEqual(self.outer_delays, [])

    def test_changed_pending_progress_during_wait_stops_before_provider_call(self):
        self.default_action = 500

        def alter_pending(deadline, stop):
            workflow_id = next(WorkflowStore(self.settings)._root().glob('wf_*')).name
            workflow = WorkflowStore(self.settings).load(workflow_id)
            checkpoint_file = self.output / workflow.run_id / 'checkpoint.json'
            checkpoint = runtime.read_json(checkpoint_file)
            checkpoint['pending']['iteration'] += 1
            runtime.write_json(checkpoint_file, checkpoint)
            self.fake_now = deadline
            return True

        with patch.object(recovery, '_wait_until', side_effect=alter_pending):
            result = self.start()
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))

    def test_recovery_lock_prevents_a_second_resume_while_first_waits(self):
        self.default_action = 500
        entered = threading.Event()
        release = threading.Event()
        results = []

        def hold_wait(deadline, stop):
            entered.set()
            self.assertTrue(release.wait(3), 'Fixture recovery wait was not released.')
            return False

        with patch.object(recovery, '_wait_until', side_effect=hold_wait):
            worker = threading.Thread(target=lambda: results.append(self.start()), daemon=True)
            worker.start()
            try:
                self.assertTrue(entered.wait(3), 'Recovery did not enter the persisted wait.')
                workflow_id = next(WorkflowStore(self.settings)._root().glob('wf_*')).name
                duplicate = self.resume(workflow_id)
                self.assertFalse(duplicate.ok, duplicate.to_dict())
                self.assertEqual((len(self.requests), self.evaluations), (2, 1))
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)


class DirectBoundsRecoveryTests(unittest.TestCase):
    """Authenticate final direct-proposal rejections through the real bridge."""
    fake_proposal = direct_fixture.DirectOptimizationTests.fake_proposal
    fake_evaluate = direct_fixture.DirectOptimizationTests.fake_evaluate
    advance_until = RecoveryIntegrationTests.advance_until

    def setUp(self):
        direct_fixture.DirectOptimizationTests.setUp(self)
        self.actions = []
        self.default_action = 'ok'
        self.outer_delays = []
        self.fake_now = 1800000000.0
        for item in (
            patch.object(recovery, '_now_epoch', side_effect=lambda: self.fake_now),
            patch.object(recovery, '_wait_until', side_effect=self.advance_until),
        ):
            item.start()
            self.addCleanup(item.stop)

    def transport(self, request, *, timeout):
        self.assertEqual(request.full_url, 'http://127.0.0.1:9/v1/responses')
        context = json.loads(json.loads(request.data.decode('utf-8'))['input'])
        self.requests.append(context)
        parameters = {item['name']: item['reference'] for item in context['variables']}
        parameters['X1'] = round(parameters['X1'] + .01 * context['candidate_iteration'], 4)
        action = self.actions.pop(0) if self.actions else self.default_action
        if action == 'bounds':
            variable = next(item for item in context['variables'] if item['name'] == 'Y1')
            parameters['Y1'] = variable['lower'] - 2.0
        elif action == 'duplicate':
            parameters = dict(context['observations'][0]['parameters'])
        else:
            self.assertIn(action, ('ok', 'schema', 'invalid_json', 'candidate_json', 'parameters', 'invalid_number'))
        payload = {'parameters': parameters, 'rationale': 'Controlled direct proposal fixture.',
                   'candidate_type': 'exploit'}
        if action == 'schema':
            payload = {'wrong_schema': True}
        elif action == 'parameters':
            del parameters['Y4']
        elif action == 'invalid_number':
            parameters['X1'] = 'not-a-number'
        answer = json.dumps(payload)
        if action == 'invalid_json':
            answer = '{invalid-json'
        elif action == 'candidate_json':
            answer = '```json\n' + answer
        wire = {'id': 'bounds-fixture-' + str(len(self.requests)), 'model': 'direct-fixture',
                'status': 'completed', 'output': [{'type': 'message', 'content': [
                    {'type': 'output_text', 'text': answer}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}}

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *args):
                return json.dumps(wire).encode('utf-8')

        return Response()

    def failed_bounds(self, *, policy=None):
        self.actions = ['bounds', 'bounds']
        task = {'operation': 'optimize', 'total_candidate_budget': 2}
        if policy is not None:
            task['optimization_policy'] = policy
        result = supervisor.run_workflow(task=task, live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(result.error['code'], 'BOUNDS')
        return result

    def pending(self, result):
        run = self.output / result.data['run_id']
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        output = Path(checkpoint['pending']['api_output'])
        return run, checkpoint, output

    def failed_duplicate(self):
        self.actions = ['duplicate', 'duplicate']
        result = supervisor.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 2},
                                         live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(result.error['code'], 'DUPLICATE')
        return result

    def test_proposal_format_errors_resume_the_same_candidate_within_budget(self):
        for action, code, category in (
                ('schema', 'CANDIDATE_SCHEMA', 'eligible_candidate_schema'),
                ('invalid_json', 'INVALID_JSON', 'eligible_candidate_invalid_json'),
                ('candidate_json', 'CANDIDATE_JSON', 'eligible_candidate_json'),
                ('parameters', 'PARAMETERS', 'eligible_candidate_parameters'),
                ('invalid_number', 'INVALID_NUMBER', 'eligible_candidate_invalid_number')):
            with self.subTest(action=action):
                before = len(self.requests), self.evaluations
                self.actions = [action, action]
                failed = supervisor.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 1},
                                                 live=True, settings=self.settings)
                self.assertFalse(failed.ok, failed.to_dict())
                self.assertEqual(failed.error['code'], code)
                context = recovery_evidence.inspect_failure(failed.data['workflow_id'], self.settings)
                self.assertTrue(context['eligible'], context)
                self.assertEqual(context['category'], category)
                self.assertEqual(context['completed_candidates'], 0)
                resumed = recovery.resume_workflow(failed.data['workflow_id'], live=True, auto_recover=True,
                                                   settings=self.settings)
                self.assertTrue(resumed.ok, resumed.to_dict())
                self.assertEqual(resumed.data['workflow_id'], failed.data['workflow_id'])
                self.assertEqual(resumed.data['run_id'], failed.data['run_id'])
                run = self.output / failed.data['run_id']
                checkpoint = runtime.read_json(run / 'checkpoint.json')
                self.assertEqual((checkpoint['nCompleted'], checkpoint['nLLM']), (1, 1))
                self.assertEqual((len(self.requests) - before[0], self.evaluations - before[1]), (3, 2))

    def test_persistent_format_errors_exhaust_the_original_retry_limit(self):
        for action in ('schema', 'invalid_json'):
            with self.subTest(action=action):
                before = len(self.requests), self.evaluations
                self.default_action = action
                result = recovery.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 1},
                                              live=True, auto_recover=True, settings=self.settings)
                self.assertFalse(result.ok, result.to_dict())
                self.assertEqual((len(self.requests) - before[0], self.evaluations - before[1]), (6, 1))
                saved = runtime.read_json(WorkflowStore(self.settings).directory(result.data['workflow_id']) / 'recovery.json')
                self.assertEqual((saved['status'], saved['total_attempts']), ('exhausted', 2))
                requests = len(self.requests)
                resumed = recovery.resume_workflow(result.data['workflow_id'], live=True, auto_recover=True,
                                                   settings=self.settings)
                self.assertFalse(resumed.ok, resumed.to_dict())
                self.assertEqual(len(self.requests), requests)

    def test_forged_format_rejection_code_never_causes_a_retry(self):
        self.actions = ['schema', 'schema']
        failed = supervisor.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 1},
                                         live=True, settings=self.settings)
        _, _, output = self.pending(failed)
        path = output / 'proposal_02' / 'rejection.json'
        rejection = runtime.read_json(path)
        rejection['code'] = 'INVALID_JSON'
        runtime.write_json(path, rejection)
        before = len(self.requests), self.evaluations
        resumed = recovery.resume_workflow(failed.data['workflow_id'], live=True, auto_recover=True,
                                           settings=self.settings)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)
        self.assertEqual(self.outer_delays, [])

    def test_duplicate_recovery_reuses_baseline_and_frozen_workflow(self):
        failed = self.failed_duplicate()
        run, checkpoint, _ = self.pending(failed)
        before = {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()}
        context = recovery_evidence.inspect_failure(failed.data['workflow_id'], self.settings)
        self.assertTrue(context['eligible'], context)
        self.assertEqual(context['category'], 'eligible_candidate_duplicate')
        self.assertEqual(context['http_attempts'], 2)
        self.assertEqual(context['pending_iteration'], 1)
        self.assertIsNone(checkpoint['pending']['proposal'])
        self.assertEqual(before, {str(path): path.read_bytes() for path in run.rglob('*') if path.is_file()})
        resumed = recovery.resume_workflow(failed.data['workflow_id'], live=True, auto_recover=True,
                                           settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(resumed.data['workflow_id'], failed.data['workflow_id'])
        self.assertEqual(resumed.data['run_id'], failed.data['run_id'])
        committed = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual((len(self.requests), self.evaluations), (4, 3))
        self.assertEqual(committed['nCompleted'], 2)
        self.assertEqual(committed['nLLM'], 2)
        self.assertEqual(self.outer_delays, [30.0])
        saved = runtime.read_json(WorkflowStore(self.settings).directory(failed.data['workflow_id']) / 'recovery.json')
        self.assertEqual(saved['total_attempts'], 1)
        self.assertEqual(saved['episode']['context']['category'], 'eligible_candidate_duplicate')

    def test_persistent_duplicate_exhausts_the_existing_retry_limit(self):
        self.default_action = 'duplicate'
        result = recovery.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 2},
                                      live=True, auto_recover=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (6, 1))
        self.assertEqual(self.outer_delays, [30.0, 120.0])
        saved = runtime.read_json(WorkflowStore(self.settings).directory(result.data['workflow_id']) / 'recovery.json')
        self.assertEqual(saved['status'], 'exhausted')
        self.assertEqual(saved['total_attempts'], 2)
        again = recovery.resume_workflow(result.data['workflow_id'], live=True, auto_recover=True,
                                         settings=self.settings)
        self.assertFalse(again.ok, again.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (6, 1))

    def test_duplicate_evidence_must_reproduce_the_same_rejection(self):
        for changed in ('code', 'replay', 'candidate'):
            with self.subTest(changed=changed):
                failed = self.failed_duplicate()
                run, checkpoint, output = self.pending(failed)
                proposal = output / 'proposal_02'
                if changed == 'code':
                    path = proposal / 'rejection.json'
                    value = runtime.read_json(path)
                    value['code'] = 'BOUNDS'
                elif changed == 'replay':
                    answer = runtime.read_json(proposal / 'answer.json')
                    candidate = runtime.bridge.parse_json(answer['text'])
                    candidate['parameters']['X1'] += .01
                    answer['text'] = json.dumps(candidate)
                    rejection = runtime.read_json(proposal / 'rejection.json')
                    rejection['previous_response'] = answer['text']
                    runtime.write_json(proposal / 'answer.json', answer)
                    path, value = proposal / 'rejection.json', rejection
                else:
                    path, value = output / 'candidate.json', {'untrusted': True}
                runtime.write_json(path, value)
                before = len(self.requests), self.evaluations
                resumed = recovery.resume_workflow(failed.data['workflow_id'], live=True, auto_recover=True,
                                                   settings=self.settings)
                self.assertFalse(resumed.ok, resumed.to_dict())
                self.assertEqual((len(self.requests), self.evaluations), before)
                self.assertEqual(self.outer_delays, [])

    def test_bounds_eligibility_is_read_only_and_requires_canonical_rejection(self):
        result = self.failed_bounds()
        run, checkpoint, _ = self.pending(result)
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        context = recovery_evidence.inspect_failure(result.data['workflow_id'], self.settings)
        self.assertTrue(context['eligible'], context)
        self.assertEqual(context['category'], 'eligible_candidate_bounds')
        self.assertEqual(context['http_attempts'], 2)
        self.assertEqual(context['pending_iteration'], 1)
        self.assertIsNone(checkpoint['pending']['proposal'])
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_bounds_recovery_reuses_baseline_and_frozen_candidate_budget(self):
        self.actions = ['bounds', 'bounds', 'ok', 'ok']
        result = recovery.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 2},
                                      live=True, auto_recover=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        run = self.output / result.data['run_id']
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual((len(self.requests), self.evaluations), (4, 3))
        self.assertEqual(checkpoint['nCompleted'], 2)
        self.assertEqual(len(checkpoint['records']), 2)
        self.assertEqual(result.data['state']['target_total_candidates'], 2)
        self.assertEqual(self.outer_delays, [30.0])
        saved = runtime.read_json(WorkflowStore(self.settings).directory(result.data['workflow_id']) / 'recovery.json')
        self.assertEqual(saved['total_attempts'], 1)
        self.assertEqual(saved['episode']['context']['category'], 'eligible_candidate_bounds')
        self.assertEqual(saved['status'], 'completed')

    def test_previous_needs_review_reason_updates_when_bounds_becomes_recoverable(self):
        result = self.failed_bounds()
        store = WorkflowStore(self.settings)
        workflow = store.load(result.data['workflow_id'])
        saved = recovery._new(workflow)
        saved['last_error'] = {'code': 'HTTP_ERROR', 'message': 'Older service error.'}
        recovery._save(store, saved, 'needs_review', '旧版仅支持HTTP恢复。')
        view = recovery.inspect_recovery(workflow.workflow_id, settings=self.settings)
        self.assertTrue(view['can_recover'], view)
        self.assertEqual(view['status'], 'available')
        self.assertIn('越界', view['reason_zh'])
        self.assertEqual(view['last_error']['code'], 'BOUNDS')

    def test_previous_recovery_does_not_hide_a_new_recoverable_bounds_failure(self):
        result = self.failed_bounds()
        store = WorkflowStore(self.settings)
        workflow = store.load(result.data['workflow_id'])
        for previous_status in ('recovered', 'completed'):
            with self.subTest(previous_status=previous_status):
                saved = recovery._new(workflow)
                saved['last_error'] = {'code': 'HTTP_ERROR', 'message': 'Older service error.'}
                recovery._save(store, saved, previous_status, '旧请求故障已恢复。')
                view = recovery.inspect_recovery(workflow.workflow_id, settings=self.settings)
                self.assertTrue(view['can_recover'], view)
                self.assertEqual(view['status'], 'available')
                self.assertEqual(view['last_error']['code'], 'BOUNDS')
                self.assertNotIn('last_recovered_error', view)

    def test_saved_objective_policy_is_bound_before_compatibility_checks(self):
        result = self.failed_bounds(policy=objectives.default_policy())
        self.assertIsNone(self.settings.optimization_policy)
        context = recovery_evidence.inspect_failure(result.data['workflow_id'], self.settings)
        self.assertTrue(context['eligible'], context)
        resumed = recovery.resume_workflow(result.data['workflow_id'], live=True, auto_recover=True,
                                           settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (4, 3))

    def test_persistent_bounds_failure_exhausts_the_existing_retry_limit(self):
        self.default_action = 'bounds'
        result = recovery.run_workflow(task={'operation': 'optimize', 'total_candidate_budget': 2},
                                      live=True, auto_recover=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (6, 1))
        self.assertEqual(self.outer_delays, [30.0, 120.0])
        saved = runtime.read_json(WorkflowStore(self.settings).directory(result.data['workflow_id']) / 'recovery.json')
        self.assertEqual(saved['status'], 'exhausted')
        self.assertEqual(saved['total_attempts'], 2)
        prior_calls = len(self.requests)
        again = recovery.resume_workflow(result.data['workflow_id'], live=True, auto_recover=True,
                                         settings=self.settings)
        self.assertFalse(again.ok, again.to_dict())
        self.assertEqual(len(self.requests), prior_calls)

    def test_tampered_bounds_evidence_never_triggers_a_retry(self):
        for changed in ('answer', 'rejection', 'manifest', 'fea', 'transport', 'candidate'):
            with self.subTest(changed=changed):
                result = self.failed_bounds()
                _, _, output = self.pending(result)
                proposal = output / 'proposal_02'
                if changed == 'answer':
                    path = proposal / 'answer.json'
                    value = runtime.read_json(path)
                    value['text'] = '{}'
                elif changed == 'rejection':
                    path = proposal / 'rejection.json'
                    value = runtime.read_json(path)
                    value['code'] = 'INVALID_JSON'
                elif changed in ('manifest', 'fea'):
                    path = output / 'manifest.json'
                    value = runtime.read_json(path)
                    if changed == 'manifest':
                        value['error']['message'] = 'Unrelated saved error.'
                    else:
                        value['finite_element_calls'] = 1
                elif changed == 'transport':
                    path = proposal / 'transport.json'
                    value = runtime.read_json(path)
                    value[-1].update(status='http_error', http_status=500)
                else:
                    path, value = output / 'candidate.json', {'untrusted': True}
                runtime.write_json(path, value)
                before = len(self.requests), self.evaluations
                resumed = recovery.resume_workflow(result.data['workflow_id'], live=True, auto_recover=True,
                                                   settings=self.settings)
                self.assertFalse(resumed.ok, resumed.to_dict())
                self.assertEqual((len(self.requests), self.evaluations), before)
                self.assertEqual(self.outer_delays, [])

