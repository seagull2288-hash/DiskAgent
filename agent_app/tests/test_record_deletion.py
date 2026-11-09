"""Delete only temporary fixtures through authenticated loopback HTTP."""
import json
import unittest

from agent_app.record_trash import DELETION_FILE
from agent_app.tests import test_web as fixture
from agent_app.validation import read_checkpoint, resolve_run
from python_control import runtime


class RecordDeletionTests(unittest.TestCase):
    setUp = fixture.WebTests.setUp
    tearDown = fixture.WebTests.tearDown
    request = fixture.WebTests.request
    save_run = fixture.WebTests.save_run
    fake_run = fixture.WebTests.fake_run
    fake_resume = fixture.WebTests.fake_resume
    fake_status = fixture.WebTests.fake_status

    def completed_run(self, name='run_test'):
        folder = self.save_run(name)
        snapshot = runtime.read_json(folder / 'checkpoint.json')
        snapshot['status'] = 'complete'
        runtime.write_json(folder / 'checkpoint.json', snapshot)
        return folder

    def test_run_rename_persists_without_changing_identity_or_checkpoint(self):
        folder = self.completed_run()
        original = (folder / 'checkpoint.json').read_bytes()
        listed = self.request('GET', '/api/runs')[1]['runs'][0]
        self.assertEqual(listed['display_name'], '')
        status, result, _ = self.request('POST', '/api/runs/run_test/rename',
                                         {'display_name': '  质量优化对比实验  '})
        self.assertEqual(status, 200)
        self.assertEqual(result['run_id'], 'run_test')
        self.assertEqual(result['display_name'], '质量优化对比实验')
        self.assertTrue((folder / '.diskagent-name.json').is_file())
        for clear_cache in (False, True):
            if clear_cache:
                self.server.cache.clear()
            listed = self.request('GET', '/api/runs')[1]['runs'][0]
            self.assertEqual(listed['run_id'], 'run_test')
            self.assertEqual(listed['display_name'], '质量优化对比实验')
        self.assertEqual(self.request('POST', '/api/runs/run_test/rename',
                                     {'display_name': '  '})[0], 200)
        self.assertEqual(self.request('GET', '/api/runs')[1]['runs'][0]['display_name'], '')
        self.assertEqual((folder / 'checkpoint.json').read_bytes(), original)
        self.assertEqual(resolve_run(self.settings, 'run_test'), folder)
        self.run.assert_not_called()
        self.resume.assert_not_called()

    def test_run_rename_requires_csrf_exact_fields_and_valid_name(self):
        folder = self.completed_run()
        route = '/api/runs/run_test/rename'
        self.assertEqual(self.request('POST', route, {'display_name': '实验'}, csrf=False)[0], 403)
        for payload in ({}, {'display_name': '实验', 'extra': True},
                        {'display_name': None}, {'display_name': 1}, {'display_name': []},
                        {'display_name': '名字\n换行'}, {'display_name': '名字\x00'},
                        {'display_name': '名字\x85'}, {'display_name': '盘😀' * 40 + '盘'}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request('POST', route, payload)[0], 400)
        self.assertFalse((folder / '.diskagent-name.json').exists())
        longest = '盘😀' * 40
        self.assertEqual(self.request('POST', route, {'display_name': longest})[0], 200)
        self.assertEqual(self.request('GET', '/api/runs')[1]['runs'][0]['display_name'], longest)

    def test_run_name_survives_soft_delete_and_restore(self):
        folder = self.completed_run()
        self.assertEqual(self.request('POST', '/api/runs/run_test/rename',
                                     {'display_name': '保留实验名称'})[0], 200)
        original = (folder / '.diskagent-name.json').read_bytes()
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/runs/run_test/rename',
                                     {'display_name': '已删除的实验'})[0], 404)
        self.assertEqual((folder / '.diskagent-name.json').read_bytes(), original)
        (folder / DELETION_FILE).unlink()
        listed = self.request('GET', '/api/runs')[1]['runs'][0]
        self.assertEqual(listed['run_id'], 'run_test')
        self.assertEqual(listed['display_name'], '保留实验名称')

    def test_workflow_delete_removes_list_entry_and_preserves_evidence(self):
        state = self.store.create('待删除的临时测试工作流')
        folder = self.store.directory(state.workflow_id)
        report = folder / 'report.md'
        report.write_text('temporary evidence', encoding='utf-8')
        original = (folder / 'state.json').read_bytes()
        self.assertEqual(len(self.request('GET', '/api/workflows')[1]['workflows']), 1)
        status, result, _ = self.request('POST', '/api/workflows/' + state.workflow_id + '/delete', {})
        self.assertEqual(status, 200)
        self.assertEqual(result['deletion_mode'], 'recoverable_soft_delete')
        self.assertTrue((folder / DELETION_FILE).is_file())
        self.assertEqual((folder / 'state.json').read_bytes(), original)
        self.assertEqual(report.read_text(encoding='utf-8'), 'temporary evidence')
        self.assertEqual(self.request('GET', '/api/workflows')[1]['workflows'], [])
        self.assertEqual(self.request('GET', '/api/workflows/' + state.workflow_id)[0], 404)
        self.run.assert_not_called()
        self.resume.assert_not_called()
        self.predictor.assert_not_called()

    def test_run_delete_is_idempotent_and_preserves_checkpoint(self):
        folder = self.completed_run()
        original = (folder / 'checkpoint.json').read_bytes()
        self.assertEqual(len(self.request('GET', '/api/runs')[1]['runs']), 1)
        status, result, _ = self.request('POST', '/api/runs/run_test/delete', {})
        self.assertEqual(status, 200)
        self.assertTrue(result['deleted'])
        marker = (folder / DELETION_FILE).read_bytes()
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 200)
        self.assertEqual((folder / DELETION_FILE).read_bytes(), marker)
        self.assertEqual((folder / 'checkpoint.json').read_bytes(), original)
        self.assertEqual(self.request('GET', '/api/runs')[1]['runs'], [])
        self.assertEqual(self.request('GET', '/api/runs/run_test/analysis')[0], 404)
        self.assertEqual(self.request('GET', '/api/runs/run_test/optimization-trace')[0], 404)
        with self.assertRaisesRegex(Exception, '已从列表删除'):
            read_checkpoint(folder)

    def test_removing_soft_delete_marker_restores_original_records(self):
        folder = self.completed_run()
        state = self.store.create('可恢复的临时测试工作流')
        workflow = self.store.directory(state.workflow_id)
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/workflows/' + state.workflow_id + '/delete', {})[0], 200)
        (folder / DELETION_FILE).unlink()
        (workflow / DELETION_FILE).unlink()
        self.assertEqual(len(self.request('GET', '/api/runs')[1]['runs']), 1)
        self.assertEqual(len(self.request('GET', '/api/workflows')[1]['workflows']), 1)
        self.assertEqual(self.store.load(state.workflow_id).to_dict(), state.to_dict())

    def test_linked_run_requires_workflow_deletion_first(self):
        folder = self.completed_run()
        state = self.store.create('引用此运行的工作流')
        state.run_id = folder.name
        state.task = {'operation': 'status', 'run_id': folder.name}
        self.store.save(state)
        status, result, _ = self.request('POST', '/api/runs/run_test/delete', {})
        self.assertEqual(status, 409)
        self.assertEqual(result['error']['code'], 'RECORD_REFERENCED')
        self.assertFalse((folder / DELETION_FILE).exists())
        self.assertEqual(self.request('POST', '/api/workflows/' + state.workflow_id + '/delete', {})[0], 200)
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 200)

    def test_active_worker_and_external_engine_lock_block_deletion(self):
        folder = self.completed_run()
        self.server.worker_lock.acquire()
        try:
            self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 409)
        finally:
            self.server.worker_lock.release()
        with runtime.project_lock(self.root):
            self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 409)
        self.assertFalse((folder / DELETION_FILE).exists())
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 200)

    def test_workflow_execution_lock_blocks_deletion(self):
        state = self.store.create('临时锁定工作流')
        with self.store.lock(state.workflow_id):
            self.assertEqual(self.request('POST', '/api/workflows/' + state.workflow_id + '/delete', {})[0], 409)
        self.assertFalse((self.store.directory(state.workflow_id) / DELETION_FILE).exists())

    def test_running_checkpoint_cannot_be_deleted(self):
        folder = self.save_run()
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {})[0], 409)
        self.assertFalse((folder / DELETION_FILE).exists())

    def test_deletion_requires_csrf_empty_payload_and_safe_identifier(self):
        folder = self.completed_run()
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {}, csrf=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {'confirmed': True})[0], 400)
        self.assertEqual(self.request('POST', '/api/runs/run_test/delete', {},
                                      headers={'Origin': 'https://example.invalid'})[0], 403)
        for path in ('/api/runs/run_missing/delete', '/api/runs/run_a%2f..%2fsecret/delete',
                     '/api/workflows/wf_a%5c..%5csecret/delete'):
            self.assertIn(self.request('POST', path, {})[0], (400, 404))
        self.assertFalse((folder / DELETION_FILE).exists())

    def test_latest_resolution_skips_deleted_run_and_rejects_deleted_explicit_id(self):
        older = self.completed_run('run_older')
        latest = self.completed_run('run_latest')
        pointer = self.output / 'latest_run.json'
        pointer.write_text(json.dumps({'runDir': str(latest)}), encoding='utf-8')
        original = pointer.read_bytes()
        self.assertEqual(self.request('POST', '/api/runs/run_latest/delete', {})[0], 200)
        self.assertEqual(resolve_run(self.settings, None), older)
        self.assertEqual(pointer.read_bytes(), original)
        with self.assertRaises(Exception):
            resolve_run(self.settings, latest.name)
        self.assertEqual(self.request('POST', '/api/runs/run_older/delete', {})[0], 200)
        with self.assertRaisesRegex(Exception, '当前没有可用'):
            resolve_run(self.settings, None)


if __name__ == '__main__':
    unittest.main()
