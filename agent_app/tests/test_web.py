"""Real loopback HTTP boundaries with mocked workflow/analysis operations."""
from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_app import web
from agent_app.schemas import TaskValidationError, ToolResult
from agent_app.state import WorkflowStore


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='disk_web_test_')
        self.root = Path(self.temp.name)
        self.output = self.root / 'runs'
        self.output.mkdir()
        self.settings = SimpleNamespace(root=self.root, output_root=self.output)
        self.store = WorkflowStore(self.settings)
        self.release, self.entered = threading.Event(), threading.Event()
        self.release.set()
        self.failure = False
        self.patches = [patch.object(web, 'run_workflow', side_effect=self.fake_run),
                        patch.object(web, 'resume_workflow', side_effect=self.fake_resume),
                        patch.object(web, 'get_workflow_status', side_effect=self.fake_status),
                        patch.object(web, 'analyze_run', return_value=ToolResult(True, 'analyze', 'analyzed',
                                                                             {'summary_zh': '只读结果'})),
                        patch.object(web, 'inspect_run', return_value={
                            'schema_version': 'disk-agent.performance.v1', 'run_id': 'run_test',
                            'status': 'insufficient_data', 'reason': 'insufficient_measured_samples',
                            'measured_samples': 5, 'unique_samples': 5, 'minimum_samples': 24,
                            'validation': {'enabled': False}, 'role': 'shadow_only',
                            'predictions': [], 'records': [], 'record_summary': {
                                'pre_evaluation_prediction_records': 0, 'compared_records': 0,
                                'missing_pre_prediction_count': 4}, 'limitations': ['std不是95%区间']})]
        self.run, self.resume, self.status, self.analysis, self.predictor = [item.start() for item in self.patches]
        self.server = web.create_server(settings=self.settings, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.token = self.request('GET', '/api/config')[1]['csrf_token']
        self.assertEqual(self.request('GET', '/api/config')[1]['app_id'], 'disk-agent.workbench.v1')

    def tearDown(self):
        self.release.set()
        deadline = time.monotonic() + 3
        while self.server.active_job_id and time.monotonic() < deadline:
            time.sleep(.01)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_external_preview_and_listing_are_api_free(self):
        self.server.external.settings = SimpleNamespace(
            problem_file=Path(__file__).resolve().parents[2]/'api_bridge'/'problem.json')
        code, case, _ = self.request('GET', '/api/external/case')
        self.assertEqual(code, 200)
        parameters = {v['name']: v['reference'] for v in case['problem']['variables']}
        code, result, _ = self.request('POST', '/api/external/candidates', {'parameters': parameters, 'live': False})
        self.assertEqual(code, 200)
        self.assertEqual(result['internal_model_api_calls'], 0)
        self.assertFalse(result['solver_started'])
        self.assertEqual(self.request('GET', '/api/external/candidates')[1]['candidates'], [])
        self.run.assert_not_called()

    def test_external_write_retains_csrf_protection(self):
        code, _, _ = self.request('POST', '/api/external/candidates', {'live': True}, csrf=False)
        self.assertNotEqual(code, 200)

    def request(self, method, path, value=None, *, headers=None, raw=None, csrf=True):
        body = raw if raw is not None else json.dumps(value, ensure_ascii=False).encode('utf-8') if value is not None else None
        current = {}
        if method == 'POST':
            current['Content-Type'] = 'application/json'
            if csrf:
                current['X-CSRF-Token'] = self.token
        current.update(headers or {})
        connection = HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=current)
            response = connection.getresponse()
            data, response_headers = response.read(), dict(response.getheaders())
            result = json.loads(data) if 'application/json' in response_headers.get('Content-Type', '') else data.decode('utf-8')
            return response.status, result, response_headers
        finally:
            connection.close()

    def fake_run(self, request=None, *, settings=None, live=False, use_llm=False, context_run_id=None,
                 on_workflow_created=None):
        state = self.store.create(request)
        if on_workflow_created is not None:
            on_workflow_created(state.workflow_id)
        state.task = {'operation': 'optimize', 'total_candidate_budget': 4}
        state.target_total_candidates = 4
        self.store.record(state, 'validated')
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('test worker not released')
        if self.failure:
            state.error = {'code': 'FAKE_FAILED', 'message': '模拟失败'}
            self.store.record(state, 'failed')
            return ToolResult(False, 'workflow', 'failed', {'workflow_id': state.workflow_id}, state.error)
        return ToolResult(True, 'workflow', 'validated', {'workflow_id': state.workflow_id, 'state': state.to_dict()})

    def fake_resume(self, workflow_id, *, settings=None, live=False):
        state = self.store.load(workflow_id)
        return ToolResult(True, 'workflow', state.status, {'workflow_id': workflow_id, 'state': state.to_dict()})

    def fake_status(self, workflow_id, *, settings=None):
        state = self.store.load(workflow_id)
        return ToolResult(True, 'workflow', 'workflow_read', {'workflow_id': workflow_id, 'state': state.to_dict()})

    def wait_job(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, data, _ = self.request('GET', '/api/jobs/' + job_id)
            self.assertEqual(status, 200)
            if data['job']['status'] == 'completed':
                return data['job']
            time.sleep(.01)
        self.fail('background job did not complete')

    def save_run(self, name='run_test', done=3, target=4):
        folder = self.output / name
        folder.mkdir()
        (folder / 'checkpoint.json').write_text(json.dumps({'nCompleted': done, 'nLLM': target,
                'status': 'running', 'baseline': {}, 'history': [{'stress_mpa': 800}] * 40}), encoding='utf-8')
        return folder

    def test_startup_and_polling_have_no_execution_side_effects(self):
        for path in ('/', '/api/config', '/api/workflows', '/api/runs'):
            self.assertEqual(self.request('GET', path)[0], 200)
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.run.assert_not_called()
        self.resume.assert_not_called()
        self.analysis.assert_not_called()
        self.predictor.assert_not_called()
        self.assertFalse((self.root / 'agent_workflows').exists())

    def test_submit_defaults_to_preview_and_returns_background_result(self):
        status, result, _ = self.request('POST', '/api/workflows', {'request': '新建优化4个候选'})
        self.assertEqual(status, 202)
        job = self.wait_job(result['job_id'])
        self.assertTrue(job['ok'])
        self.assertTrue(job['workflow_id'].startswith('wf_'))
        self.assertEqual(self.run.call_count, 1)
        kwargs = dict(self.run.call_args.kwargs)
        self.assertTrue(callable(kwargs.pop('on_workflow_created')))
        self.assertEqual(kwargs, dict(settings=self.settings, request='新建优化4个候选',
                                     live=False, use_llm=False, context_run_id=None))
        self.assertEqual(self.request('GET', '/api/workflows/' + job['workflow_id'])[0], 200)

    def test_live_and_planning_flags_are_explicit_and_independent(self):
        status, result, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选',
                'live': True, 'use_llm': False, 'context_run_id': 'run_context'})
        self.assertEqual(status, 202)
        self.wait_job(result['job_id'])
        self.assertTrue(self.run.call_args.kwargs['live'])
        self.assertFalse(self.run.call_args.kwargs['use_llm'])
        self.assertEqual(self.run.call_args.kwargs['context_run_id'], 'run_context')

    def test_resume_uses_existing_workflow_and_never_infers_live_permission(self):
        state = self.store.create('优化4个候选')
        for payload, expected in (({}, False), ({'live': True}, True)):
            status, result, _ = self.request('POST', '/api/workflows/' + state.workflow_id + '/resume', payload)
            self.assertEqual(status, 202)
            job = self.wait_job(result['job_id'])
            self.assertEqual(job['workflow_id'], state.workflow_id)
            self.resume.assert_called_with(settings=self.settings, workflow_id=state.workflow_id, live=expected)
        self.run.assert_not_called()

    def test_busy_worker_rejects_second_submission_and_accepts_after_release(self):
        self.release.clear()
        status, first, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选'})
        self.assertEqual(status, 202)
        self.assertTrue(self.entered.wait(2))
        status, result, _ = self.request('POST', '/api/workflows', {'request': '优化8个候选', 'live': True})
        self.assertEqual((status, result['error']['code']), (409, 'WEB_BUSY'))
        self.assertEqual(self.run.call_count, 1)
        self.release.set()
        self.wait_job(first['job_id'])
        self.assertEqual(self.request('POST', '/api/workflows', {'request': '优化4个候选'})[0], 202)

    def test_running_job_exposes_created_workflow_before_operation_returns(self):
        self.release.clear()
        _, submitted, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选', 'live': True})
        self.assertTrue(self.entered.wait(2))
        _, current, _ = self.request('GET', '/api/jobs/' + submitted['job_id'])
        job = current['job']
        self.assertEqual(job['status'], 'running')
        self.assertIsNone(job['result'])
        self.assertTrue(job['workflow_id'].startswith('wf_'))
        self.assertEqual(self.request('GET', '/api/workflows/' + job['workflow_id'])[0], 200)
        self.release.set()
        self.assertEqual(self.wait_job(submitted['job_id'])['workflow_id'], job['workflow_id'])

    def test_failed_operation_remains_failed_in_terminal_job(self):
        self.failure = True
        _, first, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选'})
        job = self.wait_job(first['job_id'])
        self.assertEqual(job['status'], 'completed')
        self.assertFalse(job['ok'])
        self.assertFalse(job['result']['ok'])
        self.assertEqual(job['result']['status'], 'failed')

    def test_worker_exception_is_structured_and_releases_busy_slot(self):
        self.run.side_effect = RuntimeError('mock operation crashed')
        _, value, _ = self.request('POST', '/api/workflows', {'request': '优化4个候选'})
        job = self.wait_job(value['job_id'])
        self.assertFalse(job['ok'])
        self.assertEqual(job['result']['error']['code'], 'RuntimeError')
        self.assertIsNone(self.server.active_job_id)
        self.assertEqual(self.request('POST', '/api/workflows', {'request': '优化4个候选'})[0], 202)

    def test_csrf_and_cross_site_origin_host_are_rejected(self):
        payload = {'request': '优化4个候选', 'live': True}
        self.assertEqual(self.request('POST', '/api/workflows', payload, csrf=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/workflows', payload, headers={'X-CSRF-Token': 'wrong'})[0], 403)
        for origin in ('https://evil.example', 'null', f'http://127.0.0.1:{self.port}.evil.example'):
            with self.subTest(origin=origin):
                self.assertEqual(self.request('POST', '/api/workflows', payload, headers={'Origin': origin})[0], 403)
        self.assertEqual(self.request('GET', '/api/config', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/config', headers={'Origin': 'https://evil.example'})[0], 403)
        self.run.assert_not_called()

    def test_rejected_headers_with_delayed_post_body_still_return_403(self):
        payload = json.dumps({'request': '优化4个候选', 'live': True}).encode('utf-8')
        for origin, token, code in (('null', self.token, 'WEB_ORIGIN'),
                                    (f'http://127.0.0.1:{self.port}', 'wrong', 'WEB_CSRF')):
            with self.subTest(code=code):
                connection = HTTPConnection('127.0.0.1', self.port, timeout=2)
                try:
                    connection.putrequest('POST', '/api/workflows')
                    connection.putheader('Origin', origin)
                    connection.putheader('X-CSRF-Token', token)
                    connection.putheader('Content-Type', 'application/json')
                    connection.putheader('Content-Length', str(len(payload)))
                    connection.endheaders()
                    # Reproduce the real TCP race: the server rejects headers
                    # before the client's separately transmitted body arrives.
                    time.sleep(.02)
                    connection.send(payload)
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 403)
                    self.assertEqual(result['error']['code'], code)
                finally:
                    connection.close()
        self.run.assert_not_called()
        self.resume.assert_not_called()

    def test_unknown_fields_types_duplicate_keys_and_body_limit_are_rejected(self):
        invalid = [{'request': '优化4个候选', 'live': 'true'}, {'request': '优化4个候选', 'use_llm': 1},
                   {'request': '优化4个候选', 'context_run_id': '../run_escape'},
                   {'request': '优化4个候选', 'command': 'anything'}, {'request': 4},
                   {'request': ''}, {'request': 'x\0y'}, {'request': 'a' * 4001}, []]
        for value in invalid:
            with self.subTest(value=str(value)[:80]):
                self.assertEqual(self.request('POST', '/api/workflows', value)[0], 400)
        for raw in (b'{', b'{"request":"a","request":"b"}', b'{"request":"a","live":NaN}'):
            self.assertEqual(self.request('POST', '/api/workflows', raw=raw)[0], 400)
        self.assertEqual(self.request('POST', '/api/workflows', raw=b'x' * (web.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request('POST', '/api/workflows', {'request': 'a'},
                                      headers={'Content-Type': 'text/plain'})[0], 400)
        self.run.assert_not_called()

    def test_paths_unknown_jobs_missing_workflow_and_resume_fields_are_safe(self):
        for path in ('/api/jobs/job_unknown', '/api/jobs/job_' + '0' * 24,
                     '/api/workflows/wf_missing', '/api/workflows/wf_test/../../secret',
                     '/api/workflows/%2e%2e%2fsecret', '/api/runs/run_a%2f..%2fsecret/analysis', '/arbitrary.txt'):
            with self.subTest(path=path):
                self.assertIn(self.request('GET', path)[0], (400, 404))
        state = self.store.create()
        for payload in ({'live': 1}, {'unknown': True}, {'live': True, 'use_llm': True}):
            self.assertEqual(self.request('POST', '/api/workflows/' + state.workflow_id + '/resume', payload)[0], 400)
        self.resume.assert_not_called()

    def test_run_listing_is_shallow_cached_and_analysis_is_explicit_readonly(self):
        self.save_run()
        with patch.object(web.runtime, 'read_json', wraps=web.runtime.read_json) as read:
            status, result, _ = self.request('GET', '/api/runs')
            self.assertEqual(status, 200)
            self.assertEqual(result['runs'][0]['completed_candidates'], 3)
            self.assertNotIn('history', result['runs'][0])
            self.request('GET', '/api/runs')
            self.assertEqual(read.call_count, 1)
        self.analysis.assert_not_called()
        self.assertEqual(self.request('GET', '/api/runs/run_test/analysis')[0], 200)
        self.analysis.assert_called_once_with('run_test', settings=self.settings, verify_native=False)

    def test_predictions_endpoint_is_explicit_readonly_and_keeps_gp_status(self):
        folder = self.save_run(done=4, target=8)
        before = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        self.request('GET', '/api/runs')
        self.request('GET', '/api/workflows')
        self.predictor.assert_not_called()
        status, result, _ = self.request('GET', '/api/runs/run_test/predictions')
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['status'], 'insufficient_data')
        self.assertEqual((result['data']['measured_samples'], result['data']['minimum_samples']), (5, 24))
        self.assertEqual(result['data']['record_summary']['missing_pre_prediction_count'], 4)
        self.assertEqual(result['data']['records'], [])
        self.predictor.assert_called_once_with('run_test', settings=self.settings, include_records=True)
        after = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        self.assertEqual(after, before)
        self.run.assert_not_called()
        self.resume.assert_not_called()
        self.analysis.assert_not_called()

    def test_prediction_refresh_reads_new_checkpoint_and_new_audit_without_stale_cache(self):
        folder = self.save_run(done=4, target=8)
        audits = folder / 'prediction_audit'
        audits.mkdir()

        def inspect(run_id, *, settings, include_records):
            checkpoint = json.loads((folder / 'checkpoint.json').read_text(encoding='utf-8'))
            records = [json.loads(path.read_text(encoding='utf-8')) for path in sorted(audits.glob('iter_*.json'))]
            return {'run_id': run_id, 'measured_samples': checkpoint['nCompleted'] + 1,
                    'records': records, 'record_summary': {'pre_evaluation_prediction_records': len(records)}}

        self.predictor.side_effect = inspect
        _, initial, _ = self.request('GET', '/api/runs/run_test/predictions')
        self.assertEqual(initial['data']['measured_samples'], 5)
        self.assertEqual(initial['data']['records'], [])
        (audits / 'iter_005.json').write_text(json.dumps({'iteration': 5, 'status': 'pending',
                'predicted_stress_mpa': 680.0, 'predictive_std_mpa': 2.5}), encoding='utf-8')
        # A new audit may exist before its subsequent FEA commit.
        _, audited, _ = self.request('GET', '/api/runs/run_test/predictions')
        self.assertEqual(audited['data']['records'][0]['status'], 'pending')
        checkpoint = json.loads((folder / 'checkpoint.json').read_text(encoding='utf-8'))
        checkpoint['nCompleted'] = 5
        (folder / 'checkpoint.json').write_text(json.dumps(checkpoint), encoding='utf-8')
        _, committed, _ = self.request('GET', '/api/runs/run_test/predictions')
        self.assertEqual(committed['data']['measured_samples'], 6)
        self.assertEqual(self.predictor.call_count, 3)

    def test_predictions_path_and_origin_validation_precede_inspection(self):
        self.save_run()
        for path in ('/api/runs/run_missing/predictions', '/api/runs/run_a%2f..%2fsecret/predictions',
                     '/api/runs/..%5crun_test/predictions'):
            with self.subTest(path=path):
                self.assertIn(self.request('GET', path)[0], (400, 404))
        self.assertEqual(self.request('GET', '/api/runs/run_test/predictions',
                                      headers={'Origin': 'https://evil.example'})[0], 403)
        self.predictor.assert_not_called()

    def test_workflow_progress_uses_committed_run_and_frozen_workflow_target(self):
        self.save_run()
        state = self.store.create('优化8个候选')
        state.run_id, state.target_total_candidates = 'run_test', 8
        self.store.save(state)
        _, result, _ = self.request('GET', '/api/workflows')
        progress = result['workflows'][0]['progress']
        self.assertEqual(progress['completed_candidates'], 3)
        self.assertEqual(progress['target_total_candidates'], 8)
        self.assertEqual(progress['ratio'], 3 / 8)
        self.assertNotIn('state', result['workflows'][0])
        self.status.assert_not_called()

    def test_report_is_plain_text_and_ui_uses_safe_dom_text(self):
        injection = '<script>window.pwned=1</script><img src=x onerror=alert(1)>'
        state = self.store.create(injection)
        (self.store.directory(state.workflow_id) / 'report.md').write_text(injection, encoding='utf-8')
        status, body, headers = self.request('GET', '/api/workflows/' + state.workflow_id + '/report')
        self.assertEqual((status, body), (200, injection))
        self.assertTrue(headers['Content-Type'].startswith('text/plain'))
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        _, page, headers = self.request('GET', '/')
        self.assertNotIn(injection, page)
        self.assertIn('.textContent=', page)
        self.assertNotIn('innerHTML', page)
        self.assertNotIn('document.write', page)
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_bind_validation_and_occupied_port_error_are_clear(self):
        for port in (True, -1, 65536, '8765'):
            with self.subTest(port=port), self.assertRaises(TaskValidationError):
                web.create_server(settings=self.settings, port=port)
        with self.assertRaises(TaskValidationError) as caught:
            web.create_server(settings=self.settings, port=self.port)
        self.assertEqual(caught.exception.code, 'WEB_BIND')


if __name__ == '__main__':
    unittest.main()
