"""Run with .venv-mcp Python: actual MCP SDK client/server over a stdio subprocess.

A tiny loopback fake stands in for the app; no engineering code, API keys or FEA.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.paths.append(self.path)
        if self.path == '/api/config':
            return self.reply({'ok': True, 'app_id': 'disk-agent.workbench.v1', 'csrf_token': 'local-test-token',
                               'mcp_bridge': {'version': 'disk-agent.mcp.v1'}, 'active_job_id': None})
        if self.path == '/api/mcp/case':
            return self.reply({'ok': True, 'data': {'case_id': 'disk_2d_current', 'variables': [{'name': 'X1'}]}})
        return self.reply({'ok': True, 'job': {'status': 'completed', 'result': {'ok': True}}})

    def do_POST(self):
        self.server.paths.append(self.path)
        value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.posts.append(value)
        if self.headers.get('X-CSRF-Token') != 'local-test-token':
            return self.reply({'ok': False, 'error': {'code': 'WEB_CSRF'}})
        if value.get('live'):
            return self.reply({'ok': True, 'job_id': 'job_' + '1' * 24, 'status': 'queued'})
        return self.reply({'ok': True, 'status': 'validated', 'data': {'llm_requests': 0, 'workbench_started': False}})


class StdioTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.paths, self.server.posts = [], []
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    async def session(self, checks):
        params = StdioServerParameters(command=sys.executable, args=[
            '-X', 'utf8', '-B', str(ROOT / 'agent_app/mcp_server.py'), '--app-url',
            'http://127.0.0.1:' + str(self.server.server_address[1])])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                self.assertIn('DiskAgent', initialized.serverInfo.name)
                await checks(client)

    def test_initialize_discovery_read_call_and_error(self):
        async def check(client):
            catalog = await client.list_tools()
            self.assertEqual(len(catalog.tools), 14)
            tools = {tool.name: tool for tool in catalog.tools}
            self.assertTrue(tools['disk_read_case'].annotations.readOnlyHint)
            self.assertFalse(tools['disk_evaluate_design'].annotations.readOnlyHint)
            self.assertFalse(self.server.paths)  # Discovery itself never contacts app/solver.
            case = await client.call_tool('disk_read_case', {})
            self.assertFalse(case.isError)
            self.assertEqual(case.structuredContent['data']['case_id'], 'disk_2d_current')
            config = await client.call_tool('disk_connection_status', {})
            self.assertNotIn('csrf_token', config.structuredContent)
            invalid = await client.call_tool('disk_get_job', {'job_id': '../config'})
            self.assertTrue(invalid.isError)
            self.assertNotIn('/api/jobs/../config', self.server.paths)
        asyncio.run(self.session(check))

    def test_preview_and_async_submit_poll(self):
        async def check(client):
            preview = await client.call_tool('disk_evaluate_design', {'parameters': {'X1': 20}})
            self.assertFalse(preview.isError)
            self.assertFalse(self.server.posts[-1]['live'])
            self.assertEqual(preview.structuredContent['status'], 'validated')
            live = await client.call_tool('disk_submit_optimization', {
                'task': {'operation': 'optimize', 'total_candidate_budget': 1},
                'live': True, 'request_id': 'protocol-test-one'})
            self.assertFalse(live.isError)
            self.assertEqual(live.structuredContent['status'], 'queued')
            self.assertFalse(self.server.posts[-1]['auto_recover'])
            job = await client.call_tool('disk_get_job', {'job_id': live.structuredContent['job_id']})
            self.assertEqual(job.structuredContent['job']['status'], 'completed')
        asyncio.run(self.session(check))


if __name__ == '__main__':
    unittest.main()
