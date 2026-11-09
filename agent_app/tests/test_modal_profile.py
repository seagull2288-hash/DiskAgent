"""Keep modal project routing explicit, frozen and separate from static runs."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_app.validation import validate_task
from python_control import journal, native_metrics, runtime
from python_control.objectives import default_policy


PROJECT = Path(__file__).resolve().parents[2]


class ModalProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='disk_modal_profile_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.modal = self.root / 'project_modal/2dmodel.wbpj'
        self.modal.parent.mkdir()
        self.modal.write_text('fixture only, never launched', encoding='utf-8')
        self.profile = self.modal.parent / 'engineering_profile.json'
        self.profile.write_text(json.dumps({'status': 'native_verified', 'maximum_modes': 6,
            'modal_analysis_name': 'DiskAgent Prestressed Axisymmetric'}), encoding='utf-8')
        self.settings = runtime.Settings(root=self.root, candidate_count=1,
            project_file=self.root / 'project_v2/2dmodel.wbpj',
            workbench_executable=self.root / 'NO_REAL_SOLVER.exe', output_root=self.root / 'runs',
            progress_log=self.root / 'fixture.log', api_config=self.root / 'unused.json',
            problem_file=PROJECT / 'api_bridge/problem.json',
            key_environment_variable='UNUSED_FIXTURE_KEY', plot_on_complete=False)
        self.policy = default_policy()
        self.policy['collect_mass'] = True
        self.policy['modal'] = {'analysis_name': 'DiskAgent Prestressed Axisymmetric',
                                'mode_index': 1, 'family': 'axisymmetric_fixed_order',
                                'prestress_required': True}

    def test_frequency_task_binds_verified_modal_project_during_preview(self):
        result = validate_task({'operation': 'optimize', 'total_candidate_budget': 1,
                                'optimization_policy': self.policy}, settings=self.settings)
        self.assertEqual(result.settings.project_file, self.modal)
        self.assertTrue(result.settings.definition()['optimization_policy']['modal']['prestress_required'])
        self.assertEqual(self.settings.project_file, self.root / 'project_v2/2dmodel.wbpj')

    def test_stress_and_mass_only_task_keeps_original_static_project(self):
        policy = default_policy()
        policy['collect_mass'] = True
        self.assertEqual(self.settings.for_policy(policy).project_file, self.settings.project_file)

    def test_unknown_analysis_or_unavailable_mode_fails_before_execution(self):
        self.policy['modal']['analysis_name'] = 'Other analysis'
        with self.assertRaises(runtime.ControllerError):
            self.settings.for_policy(self.policy)
        self.policy['modal']['analysis_name'] = 'DiskAgent Prestressed Axisymmetric'
        self.policy['modal']['mode_index'] = 7
        with self.assertRaises(runtime.ControllerError):
            self.settings.for_policy(self.policy)

    def test_generated_modal_journal_uses_modal_project_and_checks_prestress_source(self):
        problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        x = [item['reference'] for item in problem['variables']]
        path = self.root / 'evaluate.wbjn'
        modal_project = PROJECT / 'project_modal/2dmodel.wbpj'
        journal.create(PROJECT, x, path, self.root / 'result.csv', self.root / 'complete.ok',
                       project_file=modal_project)
        native_metrics.extend(path, self.policy)
        native = (self.root / 'SolveAndVerify.py').read_text(encoding='ascii')
        self.assertIn(str(modal_project).replace('\\', '/'), native)
        self.assertIn('prestress_required', native)
        self.assertIn('PreStressEnvironmentModalIC', native)
        compile(native, str(self.root / 'SolveAndVerify.py'), 'exec')
