"""Recovery HTTP controls with isolated workflow storage and waiting workers."""
import threading
import unittest
from unittest.mock import patch

from agent_app import recovery, supervisor, web
from agent_app.schemas import ToolResult
from agent_app.tests import test_web as web_fixture


_READ_STATUS_WITH_RECOVERY = web.get_workflow_status


class WebRecoveryTests(unittest.TestCase):
    # Borrow only fixture helpers, so unittest discovers this module's new cases.
    request = web_fixture.WebTests.request
    wait_job = web_fixture.WebTests.wait_job
    fake_status = web_fixture.WebTests.fake_status

    def setUp(self):
        self.recovery_entered = threading.Event()
        self.received_stop = None
        self.hold_recovery = False
        web_fixture.WebTests.setUp(self)

    def tearDown(self):
        if self.received_stop is not None:
            self.received_stop.set()
        web_fixture.WebTests.tearDown(self)

    def fake_run(self, request=None, *, settings=None, live=False, use_llm=False,
                 context_run_id=None, on_workflow_created=None, auto_recover=False,
                 recovery_stop=None):
        state = self.store.create(request)
        if on_workflow_created is not None:
            on_workflow_created(state.workflow_id)
        state.task = {'operation': 'optimize', 'total_candidate_budget': 4}
        state.target_total_candidates = 4
        self.store.record(state, 'validated')
        return self.waiting_result(state, recovery_stop)

    def fake_resume(self, workflow_id, *, settings=None, live=False,
                    auto_recover=False, recovery_stop=None):
        return self.waiting_result(self.store.load(workflow_id), recovery_stop)

    def waiting_result(self, state, stop):
        self.received_stop = stop
        self.recovery_entered.set()
        if self.hold_recovery and stop is not None:
            if not stop.wait(5):
                raise RuntimeError('Fixture recovery worker did not receive its stop event.')
            return ToolResult(False, 'workflow', 'interrupted',
                              {'workflow_id': state.workflow_id},
                              {'code': 'FIXTURE_STOPPED', 'message': 'Fixture recovery stopped.'})
        return ToolResult(True, 'workflow', 'validated',
                          {'workflow_id': state.workflow_id, 'state': state.to_dict()})

    def start_waiting(self):
        self.hold_recovery = True
        status, submitted, _ = self.request('POST', '/api/workflows', {
            'request': '优化4个候选', 'live': True, 'auto_recover': True})
        self.assertEqual(status, 202)
        self.assertTrue(self.recovery_entered.wait(2))
        return submitted['job_id'], self.received_stop

    def stop_and_wait(self, job_id):
        status, response, _ = self.request('POST', '/api/jobs/' + job_id + '/stop-recovery', {})
        self.assertEqual(status, 200)
        self.assertTrue(response['stop_requested'])
        job = self.wait_job(job_id)
        self.assertEqual(job['status'], 'completed')
        self.assertTrue(job['recovery_stop_requested'])
        self.assertIsNone(self.server.active_job_id)
        self.assertNotIn(job_id, self.server.recovery_stops)
        return job

    def test_live_auto_recovery_event_keeps_worker_busy_until_stopped_and_releases_slot(self):
        job_id, stop = self.start_waiting()
        self.assertIsInstance(stop, threading.Event)
        self.assertFalse(stop.is_set())
        self.assertIs(self.run.call_args.kwargs['recovery_stop'], stop)
        self.assertIs(self.server.recovery_stops[job_id], stop)
        _, response, _ = self.request('GET', '/api/jobs/' + job_id)
        self.assertEqual(response['job']['status'], 'running')
        self.assertTrue(response['job']['recovery_enabled'])
        self.assertEqual(self.server.active_job_id, job_id)
        status, response, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选'})
        self.assertEqual((status, response['error']['code']), (409, 'WEB_BUSY'))
        self.assertEqual(self.run.call_count, 1)
        job = self.stop_and_wait(job_id)
        self.assertTrue(stop.is_set())
        status, response, _ = self.request('POST', '/api/jobs/' + job_id + '/stop-recovery', {})
        self.assertEqual((status, response['error']['code']), (400, 'RECOVERY_NOT_ACTIVE'))

        # The same event contract applies to explicit recovery of an existing workflow.
        self.recovery_entered.clear()
        status, submitted, _ = self.request('POST', '/api/workflows/' + job['workflow_id'] + '/resume',
                                             {'live': True, 'auto_recover': True})
        self.assertEqual(status, 202)
        self.assertTrue(self.recovery_entered.wait(2))
        self.assertIsInstance(self.received_stop, threading.Event)
        self.assertIsNot(self.received_stop, stop)
        self.assertIs(self.resume.call_args.kwargs['recovery_stop'], self.received_stop)
        self.stop_and_wait(submitted['job_id'])

    def test_stop_endpoint_checks_csrf_origin_body_and_unknown_job_before_setting_event(self):
        job_id, stop = self.start_waiting()
        endpoint = '/api/jobs/' + job_id + '/stop-recovery'
        self.assertEqual(self.request('POST', endpoint, {}, csrf=False)[0], 403)
        self.assertFalse(stop.is_set())
        self.assertEqual(self.request('POST', endpoint, {}, headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertFalse(stop.is_set())
        self.assertEqual(self.request('POST', endpoint, {'live': True})[0], 400)
        self.assertFalse(stop.is_set())
        self.assertEqual(self.request('POST', endpoint, [])[0], 400)
        self.assertFalse(stop.is_set())
        unknown = 'job_' + '0' * 24
        self.assertEqual(self.request('GET', '/api/jobs/' + unknown)[0], 404)
        status, response, _ = self.request('POST', '/api/jobs/' + unknown + '/stop-recovery', {})
        self.assertEqual((status, response['error']['code']), (400, 'RECOVERY_NOT_ACTIVE'))
        self.assertFalse(stop.is_set())
        self.assertEqual(self.server.active_job_id, job_id)
        self.assertIs(self.server.recovery_stops[job_id], stop)
        self.stop_and_wait(job_id)

    def test_workflow_recovery_snapshot_calls_inspector_readonly_without_starting_work(self):
        state = self.store.create('优化100个候选')
        path = self.store.directory(state.workflow_id) / 'state.json'
        before = path.read_bytes()
        snapshot = {'status': 'available', 'can_recover': True, 'workflow_id': state.workflow_id,
                    'completed_candidates': 44, 'target_total_candidates': 100, 'remaining_candidates': 56}
        base = self.fake_status(state.workflow_id, settings=self.settings)
        with (patch.object(web, 'get_workflow_status', wraps=_READ_STATUS_WITH_RECOVERY),
              patch.object(supervisor, 'get_workflow_status', return_value=base) as status_reader,
              patch.object(recovery, 'inspect_recovery', return_value=snapshot) as inspector):
            for _ in range(2):
                status, response, _ = self.request('GET', '/api/workflows/' + state.workflow_id)
                self.assertEqual(status, 200)
                self.assertEqual(response['data']['recovery'], snapshot)
            self.assertEqual(inspector.call_count, 2)
            inspector.assert_called_with(state.workflow_id, settings=self.settings)
            self.assertEqual(status_reader.call_count, 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.server.jobs, {})
        self.assertEqual(self.server.recovery_stops, {})
        self.run.assert_not_called()
        self.resume.assert_not_called()
        self.analysis.assert_not_called()

    def test_invalid_recovery_flags_are_rejected_before_worker_creation(self):
        state = self.store.create()
        for value in (None, 0, 1, 'true', [], {}):
            with self.subTest(value=value):
                self.assertEqual(self.request('POST', '/api/workflows', {
                    'request': '优化4个候选', 'live': True, 'auto_recover': value})[0], 400)
                self.assertEqual(self.request('POST', '/api/workflows/' + state.workflow_id + '/resume',
                                             {'live': True, 'auto_recover': value})[0], 400)
        self.assertEqual(self.server.jobs, {})
        self.run.assert_not_called()
        self.resume.assert_not_called()

    def test_missing_flag_and_preview_do_not_allocate_automatic_recovery_event(self):
        for payload in ({'request': '优化4个候选', 'live': True},
                        {'request': '优化4个候选', 'live': False, 'auto_recover': True},
                        {'request': '优化4个候选', 'live': True, 'auto_recover': False}):
            with self.subTest(payload=payload):
                status, response, _ = self.request('POST', '/api/workflows', payload)
                self.assertEqual(status, 202)
                job = self.wait_job(response['job_id'])
                self.assertFalse(job['recovery_enabled'])
                self.assertIsNone(self.received_stop)
                self.assertNotIn('recovery_stop', self.run.call_args.kwargs)
                if 'auto_recover' not in payload:
                    self.assertNotIn('auto_recover', self.run.call_args.kwargs)
        status, response, _ = self.request('POST', '/api/workflows/' + job['workflow_id'] + '/resume',
                                         {'live': True})
        self.assertEqual(status, 202)
        self.assertFalse(self.wait_job(response['job_id'])['recovery_enabled'])
        self.assertNotIn('auto_recover', self.resume.call_args.kwargs)
        self.assertNotIn('recovery_stop', self.resume.call_args.kwargs)
        self.assertIsNone(self.received_stop)


if __name__ == '__main__':
    unittest.main()
