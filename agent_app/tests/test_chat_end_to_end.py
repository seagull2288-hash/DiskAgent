"""Actual browser HTTP -> isolated LLM/MCP worker -> app -> native boundary.

Only the model provider and native solver are replaced, in a temporary project.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import ExitStack
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

from agent_app.tests import test_mcp_integration as fixture
from agent_app.tests import test_contract
from agent_app.tests.test_chat_agent import tool_call
from agent_app import web
from agent_app.mcp_bridge import WorkbenchClient


MCP_PYTHON = test_contract.PROJECT / '.venv-mcp' / 'Scripts' / 'python.exe'


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        last = body['messages'][-1]
        if last['role'] == 'tool':
            message = {'role': 'assistant', 'content': '已根据工具结果完成读取或预览。'}
        elif '根据以下工具返回的工程任务记录' in last['content']:
            message = {'role': 'assistant', 'content': '工程任务完成，已校验最大等效应力650 MPa。'}
        elif '优化' in last['content']:
            message = tool_call('disk_submit_optimization', {'task': {'operation': 'optimize', 'total_candidate_budget': 3}})
        elif '读取模型' in last['content']:
            message = tool_call('disk_read_case', {})
        else:
            message = tool_call('disk_validate_design', {'parameters': self.server.parameters})
        result = {'id': 'fixture-response', 'model': 'fixture-tools',
                  'choices': [{'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop', 'message': message}],
                  'usage': {'prompt_tokens': 30, 'completion_tokens': 10, 'total_tokens': 40}}
        encoded = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class FullChatMCPTests(test_contract.FixtureCase):
    fake_native = fixture.MCPBridgeTests.fake_native
    fake_workflow = fixture.MCPBridgeTests.fake_workflow

    def setUp(self):
        if not MCP_PYTHON.is_file():
            self.skipTest('The optional MCP environment is required for this full protocol test.')
        super().setUp()
        self.patches = ExitStack()
        self.calls = 0
        self.release, self.entered = threading.Event(), threading.Event()
        self.release.set()
        self.native = self.patches.enter_context(patch('python_control.workbench.evaluate', side_effect=self.fake_native))
        self.verified = self.patches.enter_context(patch('python_control.workbench.verify_native'))
        self.workflow = self.patches.enter_context(patch.object(web, 'run_workflow', side_effect=self.fake_workflow))
        self.server = web.create_server(self.settings, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.client = WorkbenchClient('http://127.0.0.1:' + str(self.server.server_address[1]), timeout=3)
        self.parameters = {v['name']: v['reference'] for v in self.problem['variables']}
        self.provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        self.provider.requests = []
        self.provider.parameters = dict(self.parameters, X1=round(self.parameters['X1'] + .2, 4))
        self.provider_thread = threading.Thread(target=self.provider.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.provider_thread.start()
        self.patches.enter_context(patch.dict('os.environ', {self.settings.key_environment_variable: 'fixture-chat-key'}))
        config = {'provider': 'openai_chat', 'base_url': 'http://127.0.0.1:' + str(self.provider.server_address[1]),
                  'model': 'fixture-tools', 'api_key_env': self.settings.key_environment_variable,
                  'timeout_seconds': 5, 'max_http_attempts': 1}
        self.settings.api_config.write_text(json.dumps(config), encoding='utf-8')
        self.chat = self.server.conversations()
        self.chat.python = MCP_PYTHON
        self.session_id = self.client._request('/api/chat/sessions', {})['session']['session_id']

    def tearDown(self):
        deadline = time.monotonic() + 10
        while self.chat._read(self.session_id)['status'] == 'working' and time.monotonic() < deadline:
            time.sleep(.03)
        self.provider.shutdown()
        self.provider.server_close()
        self.provider_thread.join(3)
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.patches.close()
        super().tearDown()

    def send(self, request):
        return self.client._request('/api/chat/sessions/' + self.session_id + '/messages', request)

    def state(self):
        response = self.client._request('/api/chat/sessions/' + self.session_id)
        self.assertTrue(response.get('ok'), response)
        return response['session']

    def wait_chat(self, predicate=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.state()
            if state['status'] == 'failed':
                self.fail(state['error'])
            if state['status'] != 'working' and (predicate is None or predicate(state)):
                return state
            time.sleep(.03)
        self.fail('Full chat pipeline timed out.')

    def test_read_preview_execute_deduplicate_and_automatically_summarize_native_result(self):
        self.send({'message': '读取模型', 'request_id': 'read_model'})
        state = self.wait_chat()
        self.assertTrue(any(event['type'] == 'connected' for event in state['events']))
        self.assertEqual(self.calls, 0)
        self.send({'message': '预览一次指定设计评价', 'request_id': 'preview_design'})
        state = self.wait_chat()
        preview = state['pending_preview']
        self.assertEqual(preview['arguments']['parameters'], self.provider.parameters)
        self.assertEqual(preview['budget']['design_evaluation_tasks'], 1)
        self.assertEqual(self.calls, 0)
        self.send({'action': 'execute', 'preview_id': preview['preview_id'], 'request_id': 'execute_once'})
        state = self.wait_chat(lambda state: any('650 MPa' in message['text'] for message in state['messages']))
        self.assertEqual(self.calls, 1)
        self.assertEqual(state['jobs'][0]['status'], 'completed')
        self.assertEqual(state['costs']['model_requests'], 5)
        self.assertEqual(state['costs']['total_tokens'], 200)
        self.assertTrue(any(event.get('name') == 'disk_get_job' for event in state['events']))
        self.send({'action': 'execute', 'preview_id': preview['preview_id'], 'request_id': 'execute_again'})
        self.assertEqual(self.calls, 1)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(all('tools' in request and 'response_format' not in request for request in self.provider.requests))

    def test_budgeted_optimization_preview_uses_existing_task_contract_without_solving(self):
        self.send({'message': '预览新建优化3个候选', 'request_id': 'preview_optimization'})
        state = self.wait_chat()
        preview = state['pending_preview']
        self.assertEqual(preview['tool'], 'disk_submit_optimization')
        self.assertEqual(preview['arguments']['task']['total_candidate_budget'], 3)
        self.assertEqual(preview['budget'], {'additional_candidates': 3, 'baseline_evaluations': 1})
        self.assertEqual(self.calls, 0)
        self.workflow.assert_not_called()
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(state['costs']['model_requests'], 0)

    def test_small_token_budget_stops_before_a_provider_request(self):
        self.send({'message': '读取模型', 'request_id': 'limited_read', 'token_budget': 1000})
        state = self.wait_chat()
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(state['costs']['model_requests'], 0)
        self.assertIn('token预算不足', state['messages'][-1]['text'])
        self.assertEqual(self.calls, 0)

    def test_environment_command_uses_actual_mcp_with_zero_provider_requests(self):
        self.send({'message': '检查当前轮盘优化环境', 'request_id': 'rule_preflight'})
        state = self.wait_chat()
        self.assertEqual(self.provider.requests, [])
        self.assertEqual(state['costs']['model_requests'], 0)
        self.assertEqual(self.calls, 0)
        self.assertTrue(any(event.get('name') == 'disk_check_environment' for event in state['events']))
        self.assertIn('体检完成', state['messages'][-1]['text'])
