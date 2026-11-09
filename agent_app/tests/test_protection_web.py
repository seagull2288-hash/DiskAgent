"""Local diagnostic and recovery endpoints never submit solver work."""
import unittest
from unittest.mock import patch

from agent_app.tests import test_web as fixture
from python_control import runtime


class ProtectionWebTests(unittest.TestCase):
    setUp = fixture.WebTests.setUp
    tearDown = fixture.WebTests.tearDown
    request = fixture.WebTests.request
    save_run = fixture.WebTests.save_run
    fake_run = fixture.WebTests.fake_run
    fake_resume = fixture.WebTests.fake_resume
    fake_status = fixture.WebTests.fake_status

    def test_preflight_is_authenticated_and_returns_checks_without_execution(self):
        report = {'ok': True, 'status': 'warning', 'checks': [], 'summary': 'fixture',
                  'api_requests_made': 0, 'finite_element_solves_made': 0}
        with patch('agent_app.preflight.inspect_preflight', return_value=report) as inspect:
            self.assertEqual(self.request('POST', '/api/preflight', {}, csrf=False)[0], 403)
            inspect.assert_not_called()
            self.assertEqual(self.request('POST', '/api/preflight', {})[:2], (200, report))
            self.assertEqual(self.request('POST', '/api/preflight', {'unexpected': True})[0], 400)
        self.run.assert_not_called()
        self.resume.assert_not_called()

    def test_monitor_and_legacy_backup_reads_preserve_the_checkpoint(self):
        folder = self.save_run()
        original = (folder / 'checkpoint.json').read_bytes()
        status, data, _ = self.request('GET', '/api/runs/run_test/protection')
        self.assertEqual(status, 200)
        self.assertEqual(data['monitor']['status'], 'unavailable')
        self.assertEqual(self.request('GET', '/api/runs/run_test/backups')[1]['backups'], [])
        self.assertEqual((folder / 'checkpoint.json').read_bytes(), original)
        self.run.assert_not_called()
        self.resume.assert_not_called()

    def test_pending_slot_monitor_is_read_from_the_bound_retry_attempt(self):
        folder = self.save_run()
        slot = folder / 'evaluations' / 'eval_identity'
        attempt = folder / 'evaluations' / 'eval_identity_retry_fixture'
        slot.mkdir(parents=True)
        attempt.mkdir()
        runtime.write_json(slot / 'evaluation_slot.json', {'current_attempt': str(attempt)})
        runtime.write_json(attempt / 'monitor.json', {'status': 'completed', 'stage_label': '核验结果'})
        saved = runtime.read_json(folder / 'checkpoint.json')
        saved['baseline'] = {'fixture': True}
        saved['pending'] = {'phase': 'solving', 'evaluation_dir': str(slot)}
        runtime.write_json(folder / 'checkpoint.json', saved)
        self.assertEqual(self.request('GET', '/api/runs/run_test/protection')[1]['monitor']['stage_label'], '核验结果')

    def test_restore_preview_and_write_both_enforce_workflow_references(self):
        self.save_run()
        workflow = self.store.create('fixture linked workflow')
        workflow.run_id = 'run_test'
        self.store.save(workflow)
        with patch('python_control.checkpoint_history.preview_restore',
                   return_value={'ok': True, 'can_restore': True}) as preview, \
             patch('python_control.checkpoint_history.restore_backup') as restore:
            response = self.request('POST', '/api/runs/run_test/backups/preview', {'backup_id': 'fixture'})
            self.assertEqual(response[0], 200)
            self.assertFalse(response[1]['can_restore'])
            self.assertIn('关联工作流', response[1]['reason_zh'])
            self.assertEqual(self.request('POST', '/api/runs/run_test/backups/restore',
                {'backup_id': 'fixture', 'expected_current_sha256': 'a' * 64})[0], 409)
            restore.assert_not_called()
            preview.assert_called_once()

    def test_restore_requires_preview_digest_and_an_idle_worker(self):
        self.save_run()
        path = '/api/runs/run_test/backups/restore'
        with patch('python_control.checkpoint_history.restore_backup', return_value={'ok': True, 'restored': True}) as restore:
            self.assertEqual(self.request('POST', path, {'backup_id': 'fixture'})[0], 400)
            self.assertEqual(self.request('POST', path, {'backup_id': 'fixture', 'expected_current_sha256': 'bad'})[0], 400)
            self.server.worker_lock.acquire()
            try:
                self.assertEqual(self.request('POST', path,
                    {'backup_id': 'fixture', 'expected_current_sha256': None})[0], 409)
            finally:
                self.server.worker_lock.release()
            restore.assert_not_called()
            self.assertEqual(self.request('POST', path,
                {'backup_id': 'fixture', 'expected_current_sha256': None})[0], 200)
            self.assertIsNone(restore.call_args.kwargs['expected_current_sha256'])

