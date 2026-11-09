"""Readiness checks use disposable storage and mocked external boundaries."""
from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_app import preflight
from agent_app.schemas import TaskValidationError
from python_control import objectives, runtime


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='disk_preflight_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = runtime.Settings(self.root, 1, self.root / 'project_v2/2dmodel.wbpj',
            self.root / 'NO_SOLVER.exe', self.root / 'runs', self.root / 'progress.log',
            self.root / 'api.json', self.root / 'problem.json', 'FIXTURE_API_KEY', False)
        self.secret = 'fixture-secret-never-log'
        blocks = [patch.object(preflight.entry, 'check_environment', return_value={}),
                  patch.object(preflight, 'project_unlocked'),
                  patch.object(runtime, 'saved_key', return_value=self.secret),
                  patch.object(preflight.shutil, 'disk_usage', return_value=SimpleNamespace(free=20 * 1024 ** 3)),
                  patch.object(preflight, '_license_endpoints', return_value=[]),
                  patch.object(preflight.socket, 'create_connection', side_effect=AssertionError('No license probe configured.'))]
        self.environment, self.project_lock, self.key, self.disk, self.endpoints, self.connect = [block.start() for block in blocks]
        for block in blocks:
            self.addCleanup(block.stop)

    def checks(self, report):
        return {item['id']: item for item in report['checks']}

    def test_ready_report_cleans_write_probe_and_keeps_unknown_license_explicit(self):
        report = preflight.require_ready(self.settings)
        self.assertTrue(report['ok'])
        self.assertEqual(report['api_requests_made'], 0)
        self.assertEqual(report['finite_element_solves_made'], 0)
        self.assertEqual(self.checks(report)['output_write']['status'], 'passed')
        self.assertEqual(self.checks(report)['license_entitlement']['status'], 'unknown')
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertNotIn(self.secret, json.dumps(report))
        self.connect.assert_not_called()

    def test_missing_files_or_key_block_before_any_solver_request(self):
        self.environment.side_effect = runtime.ControllerError('Required file is missing: fixture.wbpj')
        self.key.return_value = ''
        report = preflight.inspect_preflight(self.settings)
        self.assertFalse(report['ok'])
        self.assertEqual(self.checks(report)['environment']['status'], 'failed')
        self.assertEqual(self.checks(report)['api_key']['status'], 'failed')
        with self.assertRaisesRegex(TaskValidationError, 'API'):
            preflight.require_ready(self.settings)
        self.connect.assert_not_called()

    def test_low_disk_space_warns_and_full_disk_blocks(self):
        self.disk.return_value = SimpleNamespace(free=1024 ** 3)
        report = preflight.require_ready(self.settings)
        self.assertEqual(self.checks(report)['disk_space']['status'], 'warning')
        self.assertEqual(self.checks(report)['disk_space']['warning_below_bytes'], 5 * 1024 ** 3)
        self.disk.return_value = SimpleNamespace(free=0)
        with self.assertRaises(TaskValidationError):
            preflight.require_ready(self.settings)

    def test_write_denied_and_project_lock_are_confirmed_failures(self):
        self.project_lock.side_effect = runtime.ControllerError('Workbench project is locked.')
        with patch.object(preflight, '_probe_write', side_effect=PermissionError('fixture read-only output')):
            report = preflight.inspect_preflight(self.settings)
        checks = self.checks(report)
        self.assertEqual(checks['output_write']['status'], 'failed')
        self.assertEqual(checks['project_lock']['status'], 'failed')
        self.assertEqual(checks['disk_space']['status'], 'passed')
        self.assertEqual(len(checks), len(report['checks']))

    def test_license_tcp_reachability_does_not_certify_feature_authorization(self):
        self.endpoints.return_value = [('fixture-license', 1055)]
        self.connect.side_effect = None
        self.connect.return_value = nullcontext()
        report = preflight.inspect_preflight(self.settings)
        self.assertEqual(self.checks(report)['license_service']['status'], 'passed')
        self.assertEqual(self.checks(report)['license_entitlement']['status'], 'unknown')
        self.connect.assert_called_once_with(('fixture-license', 1055), timeout=0.5)
        self.connect.side_effect = TimeoutError('fixture unavailable')
        report = preflight.require_ready(self.settings)
        self.assertEqual(self.checks(report)['license_service']['status'], 'warning')

    def test_modal_policy_requires_the_verified_matching_project(self):
        policy = objectives.default_policy()
        policy['modal'] = {'analysis_name': 'Configured modal', 'mode_index': 1,
                           'family': 'axisymmetric_fixed_order'}
        selected = replace(self.settings, optimization_policy=policy)
        report = preflight.inspect_preflight(selected)
        self.assertEqual(self.checks(report)['analysis_policy']['status'], 'failed')
        modal = self.root / 'project_modal/2dmodel.wbpj'
        modal.parent.mkdir()
        modal.write_text('fixture only', encoding='utf-8')
        (modal.parent / 'engineering_profile.json').write_text(json.dumps({
            'status': 'native_verified', 'maximum_modes': 6,
            'modal_analysis_name': 'Configured modal'}), encoding='utf-8')
        report = preflight.inspect_preflight(selected)
        self.assertEqual(self.checks(report)['analysis_policy']['status'], 'failed')
        report = preflight.inspect_preflight(replace(selected, project_file=modal))
        self.assertEqual(self.checks(report)['analysis_policy']['status'], 'passed')

    def test_errors_redact_the_exact_configured_secret(self):
        self.environment.side_effect = runtime.ControllerError('Fixture ' + self.secret)
        report = preflight.inspect_preflight(self.settings)
        self.assertNotIn(self.secret, json.dumps(report))
        self.assertIn('[REDACTED]', self.checks(report)['environment']['message'])


class LicenseConfigurationTests(unittest.TestCase):
    def test_only_explicit_server_endpoints_are_probed(self):
        with tempfile.TemporaryDirectory(prefix='disk_license_') as temporary:
            settings = SimpleNamespace(workbench_executable=Path(temporary) / 'NO_SOLVER.exe')
            with patch.dict('os.environ', {'ANSYSLMD_LICENSE_FILE': '1055@fixture-server;invalid.lic',
                                          'ANSYSLI_SERVERS': '2325@localhost'}, clear=True):
                self.assertEqual(preflight._license_endpoints(settings),
                                 [('fixture-server', 1055), ('localhost', 2325)])


if __name__ == '__main__':
    unittest.main()
