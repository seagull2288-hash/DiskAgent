"""Real local HTTP conversation routes with a bounded replacement worker."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from agent_app import web
from agent_app.tests.test_contract import FixtureCase
from agent_app.tests import test_web as web_fixture


class ChatWebTests(FixtureCase):
    request = web_fixture.WebTests.request

    def setUp(self):
        super().setUp()
        self.server = web.create_server(self.settings, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.token = self.request('GET', '/api/config')[1]['csrf_token']
        self.service = self.server.conversations()
        self.service.python = Path(sys.executable)
        worker_patch = patch.object(self.service, '_work', side_effect=self.fake_work)
        self.worker = worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        super().tearDown()

    def fake_work(self, session_id, request_id, request, folder):
        self.service._event(session_id, request_id, {'type': 'connected', 'transport': 'mcp_stdio'})
        self.service._event(session_id, request_id, {'type': 'answer', 'text': '已读取工程模型。'})
        self.service._finish(session_id, request_id)

    def test_session_message_and_reload_are_persistent_and_independent_of_engine_worker(self):
        status, response, _ = self.request('POST', '/api/chat/sessions', {})
        self.assertEqual(status, 201)
        session_id = response['session']['session_id']
        path = '/api/chat/sessions/' + session_id
        self.server.worker_lock.acquire()
        try:
            status, response, _ = self.request('POST', path + '/messages', {'message': '读取模型', 'request_id': 'message_one'})
            self.assertEqual(status, 202, response)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                status, response, _ = self.request('GET', path)
                if response['session']['status'] != 'working':
                    break
                time.sleep(.01)
            self.assertEqual(response['session']['messages'][-1]['text'], '已读取工程模型。')
            self.assertIsNone(self.server.active_job_id)
            self.assertEqual(list(self.output.iterdir()), [])
            self.assertTrue((self.root / 'agent_chats' / session_id / 'state.json').is_file())
        finally:
            self.server.worker_lock.release()

    def test_unknown_fields_and_unpreviewed_execution_fail_before_worker(self):
        session_id = self.service.create()['session']['session_id']
        path = '/api/chat/sessions/' + session_id + '/messages'
        for value in ({'message': '读取模型', 'request_id': 'one', 'script': 'ignored'},
                      {'action': 'execute', 'request_id': 'two', 'preview_id': 'missing'}):
            status, response, _ = self.request('POST', path, value)
            self.assertEqual(status, 400, response)
        self.worker.assert_not_called()

    def test_new_chat_asset_and_panel_are_served(self):
        _, html, _ = self.request('GET', '/')
        self.assertIn('id="chat-panel"', html)
        status, script, headers = self.request('GET', '/chat.js')
        self.assertEqual(status, 200)
        self.assertIn('text/javascript', headers['Content-Type'])
        self.assertIn('preview_id: session.pending_preview.preview_id', script)
        self.worker.assert_not_called()
