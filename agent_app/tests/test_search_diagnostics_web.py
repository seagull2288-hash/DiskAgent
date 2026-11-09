"""Loopback routing for explicitly requested offline diagnostics."""
import unittest
from unittest.mock import patch

from agent_app import search_diagnostics
from agent_app.schemas import ToolResult
from agent_app.tests import test_web as fixture


class SearchDiagnosticsWebTests(unittest.TestCase):
    setUp = fixture.WebTests.setUp
    tearDown = fixture.WebTests.tearDown
    request = fixture.WebTests.request
    fake_run = fixture.WebTests.fake_run
    fake_resume = fixture.WebTests.fake_resume
    fake_status = fixture.WebTests.fake_status
    save_run = fixture.WebTests.save_run

    def test_diagnostics_get_returns_readonly_report_without_creating_job(self):
        run = self.save_run()
        checkpoint = run / 'checkpoint.json'
        before = checkpoint.read_bytes()
        data = {'run_id': run.name, 'models': {'metric_models': {}},
                'evidence': {'api_requests': 0, 'fea_evaluations': 0}}
        with patch.object(search_diagnostics, 'inspect_search_diagnostics',
                          return_value=ToolResult(True, 'search_diagnostics', 'analyzed', data)) as inspect:
            for path in ('/', '/api/config', '/api/runs'):
                self.assertEqual(self.request('GET', path)[0], 200)
            inspect.assert_not_called()
            status, response, _ = self.request('GET', '/api/runs/' + run.name + '/search-diagnostics')
            self.assertEqual(status, 200)
            self.assertTrue(response['ok'])
            self.assertEqual(response['data'], data)
            inspect.assert_called_once_with(run.name, settings=self.settings)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(self.server.jobs, {})
        self.run.assert_not_called()
        self.resume.assert_not_called()
        self.analysis.assert_not_called()
        self.predictor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
