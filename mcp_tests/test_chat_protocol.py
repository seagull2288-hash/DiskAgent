"""Browser agent through the actual MCP SDK/stdio bridge with fake LLM/FEA."""
import asyncio
import unittest

from agent_app.mcp_client import MCPClient
from agent_app.chat_worker import run_turn
from agent_app.tests.test_chat_agent import Model, tool_call
import test_stdio as fixture


class ChatProtocolTests(unittest.TestCase):
    setUp = fixture.StdioTests.setUp
    tearDown = fixture.StdioTests.tearDown

    def test_agent_read_tool_round_trip_uses_actual_stdio_and_http(self):
        async def check():
            events = []
            async with MCPClient('http://127.0.0.1:' + str(self.server.server_address[1])) as client:
                model = Model([tool_call('disk_read_case', {}),
                               {'role': 'assistant', 'content': '已读取当前二维轮盘。'}])
                await run_turn({'message': '读取模型'}, client, model, events.append)
            self.assertEqual(self.server.paths, ['/api/mcp/case'])
            self.assertEqual(len(model.calls), 2)
            self.assertTrue(any(event['type'] == 'tool_result' and event['result']['ok'] for event in events))
        asyncio.run(check())

    def test_frozen_execution_goes_through_actual_mcp_and_returns_original_job(self):
        async def check():
            events = []
            async with MCPClient('http://127.0.0.1:' + str(self.server.server_address[1])) as client:
                request = {'action': 'execute', 'execution_request_id': 'chat_protocol_once',
                           'approved_preview': {'tool': 'disk_evaluate_design', 'preview_id': 'preview_one',
                             'arguments': {'parameters': {'X1': 20}, 'optimization_policy': None}}}
                await run_turn(request, client, None, events.append)
            self.assertEqual(self.server.posts[-1]['request_id'], 'chat_protocol_once')
            self.assertTrue(self.server.posts[-1]['live'])
            self.assertTrue(any(event['type'] == 'job_submitted' for event in events))
        asyncio.run(check())
