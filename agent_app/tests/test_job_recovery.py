"""Cross-server status and verified recovery never launch another native solve."""
from pathlib import Path
import threading
import time
from unittest.mock import patch

from agent_app import web
from agent_app.mcp_bridge import WorkbenchClient
from agent_app.mcp_jobs import JobStore
from agent_app.chat_agent import ChatService
from agent_app.tests.test_contract import FixtureCase
from python_control import runtime


class JobRecoveryTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.calls = 0
        self.release = threading.Event()
        self.release.set()
        native = patch('python_control.workbench.evaluate', side_effect=self.native)
        verifier = patch('python_control.workbench.verify_native')
        native.start()
        verifier.start()
        self.addCleanup(native.stop)
        self.addCleanup(verifier.stop)
        self.servers = []
        self.server, self.client = self.start_server()

    def start_server(self):
        server = web.create_server(self.settings, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server, WorkbenchClient('http://127.0.0.1:' + str(server.server_address[1]), timeout=3)

    def tearDown(self):
        self.release.set()
        for server, thread in self.servers:
            deadline = time.monotonic() + 3
            while server.active_job_id and time.monotonic() < deadline:
                time.sleep(.01)
            server.shutdown()
            server.server_close()
            thread.join(3)
        super().tearDown()

    def native(self, x, settings, problem, output_root, baseline=None):
        self.calls += 1
        self.release.wait(3)
        folder = Path(output_root) / 'eval_one'
        folder.mkdir()
        runtime.write_json(folder / 'mechanical_verified.json', {'success': True})
        self.detail = {'runDir': str(folder), 'nativeReportFile': str(folder / 'mechanical_verified.json'),
                       'xActual': x, 'stressMPa': 700, 'displacementMM': .2, 'area': .01}
        return self.detail

    def submit(self):
        return self.client.action('evaluate-design', {'live': True, 'request_id': 'recover_once',
            'parameters': {v['name']: v['reference'] for v in self.problem['variables']}})

    def wait(self, job_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.server.job(job_id)
            if job['status'] == 'completed':
                return job
            time.sleep(.01)
        self.fail('Evaluation did not finish.')

    def test_second_server_reads_live_owner_and_does_not_claim_interruption(self):
        self.release.clear()
        submitted = self.submit()
        other, client = self.start_server()
        remote = client.job(submitted['job_id'])['job']
        self.assertIn(remote['status'], ('queued', 'running'))
        self.assertEqual(remote['owner_url'], self.server.job_store.owner_url)
        self.release.set()
        self.wait(submitted['job_id'])
        self.assertEqual(self.calls, 1)

    def test_orphan_terminal_artifacts_recover_without_new_solve(self):
        job = self.wait(self.submit()['job_id'])
        old = dict(job, status='running', result=None, ok=None, owner_instance='gone', owner_url='http://127.0.0.1:1')
        runtime.write_json(self.server.job_store.path(job['job_id']), old)
        other, _ = self.start_server()
        with patch('python_control.workbench.collect_result', return_value=self.detail) as collector:
            recovered = other.job(job['job_id'])
        self.assertEqual(recovered['status'], 'completed')
        self.assertTrue(recovered['ok'])
        self.assertEqual(recovered['result']['data']['observation']['stress_mpa'], 700)
        self.assertEqual(recovered['recovery_source'], 'verified_saved_artifacts')
        collector.assert_called_once()
        self.assertEqual(self.calls, 1)

    def test_chat_finds_lost_submission_response_by_original_request_mapping(self):
        job = self.wait(self.submit()['job_id'])
        chat = ChatService(self.settings, 'http://127.0.0.1:1', self.server.job)
        session_id = chat.create()['session']['session_id']
        state = chat._read(session_id)
        state['pending_preview'] = {'preview_id': 'preview_lost', 'execution_request_id': 'recover_once'}
        state.update(status='working', owner_instance='old-server')
        chat._save(state)
        with patch.object(chat, '_start_watch'):
            recovered = chat.recover(session_id)['session']
        self.assertEqual(recovered['pending_preview']['job_id'], job['job_id'])
        self.assertEqual(recovered['jobs'][0]['status'], 'completed')
        self.assertEqual(self.calls, 1)
