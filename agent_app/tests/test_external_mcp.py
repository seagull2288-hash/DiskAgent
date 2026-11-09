"""No real API/solver calls: bounds, execution gating, persistence and MCP tools."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_app.external import ExternalService
from agent_app.mcp_server import create_mcp


class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        problem = Path(__file__).resolve().parents[2] / 'api_bridge' / 'problem.json'
        self.settings = SimpleNamespace(root=root, output_root=root/'runs', problem_file=problem,
                                        project_file=root/'project.wbpj', workbench_executable=root/'solver.exe')
        self.settings.project_file.touch()
        self.settings.workbench_executable.touch()
        self.service = ExternalService(self.settings)
        self.parameters = {v['name']: v['reference'] for v in json.loads(problem.read_text(encoding='utf-8-sig'))['variables']}
        self.id = 'candidate_' + 'a'*32

    def tearDown(self):
        for thread in self.service.threads.values():
            thread.join(5)
        self.temp.cleanup()

    def test_preview_does_not_solve_or_call_model(self):
        with patch('python_control.workbench.evaluate') as solve, patch('api_bridge.llm_bridge.propose') as model:
            result = self.service.submit(self.parameters, self.id)
            self.assertFalse(result['solver_started'])
            self.assertFalse(self.service.root.exists())
            solve.assert_not_called()
            model.assert_not_called()

    def test_reject_bounds_paths_and_nonboolean(self):
        for parameters, candidate_id, live in [({**self.parameters, 'X1': 999}, self.id, True),
                                               (self.parameters, '../escape', True),
                                               (self.parameters, self.id, 'false')]:
            with self.assertRaises(Exception):
                self.service.submit(parameters, candidate_id, live=live)

    def test_one_solve_retry_and_shared_results(self):
        with patch('python_control.workbench.evaluate', return_value={'stressMPa': 123, 'test_double': True}) as solve:
            self.service.submit(self.parameters, self.id, live=True)
            self.service.threads[self.id].join(5)
            other = ExternalService(self.settings)
            result = other.submit(self.parameters, self.id, live=True)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['result']['stressMPa'], 123)
            self.assertEqual(len(other.list()['candidates']), 1)
            solve.assert_called_once()
            with self.assertRaises(Exception):
                other.submit({**self.parameters, 'X1': 21}, self.id, live=True)

    def test_failure_is_persisted_without_automatic_retry(self):
        with patch('python_control.workbench.evaluate', side_effect=RuntimeError('solver failed')) as solve:
            self.service.submit(self.parameters, self.id, live=True)
            self.service.threads[self.id].join(5)
            result = self.service.submit(self.parameters, self.id, live=True)
            self.assertEqual(result['status'], 'failed')
            self.assertFalse(result['ok'])
            solve.assert_called_once()

    def test_mcp_discovery_and_readonly_call(self):
        async def check():
            server = create_mcp(self.settings)
            names = {tool.name for tool in await server.list_tools()}
            self.assertEqual(names, {'get_case', 'preview_candidate', 'submit_candidate', 'get_candidate_status', 'list_candidates'})
            result = await server.call_tool('preview_candidate', {'parameters': self.parameters})
            self.assertIn('solver_started', str(result))
            self.assertFalse(self.service.root.exists())
        asyncio.run(check())
