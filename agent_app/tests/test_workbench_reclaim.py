"""Native completion evidence is collected before considering a second launch."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from python_control import evaluation_lifecycle as lifecycle
from python_control import runtime, workbench


class WorkbenchReclaimTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='disk_reclaim_')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        project = self.root / 'project' / 'fixture.wbpj'
        project.parent.mkdir()
        project.write_text('fixture project', encoding='ascii')
        self.settings = SimpleNamespace(root=self.root, project_file=project,
            workbench_executable=self.root / 'NO_SOLVER.exe', optimization_policy=None)
        self.problem = runtime.bridge.load_problem(Path(__file__).resolve().parents[2] / 'api_bridge/problem.json')
        self.x = [variable['reference'] for variable in self.problem['variables']]

    def test_completed_solve_is_reverified_and_reclaimed_without_another_process(self):
        output = self.root / 'evaluations'
        slot = output / 'eval_fixture'
        request = {'x': self.x, 'project': str(self.settings.project_file),
                   'case_signature': lifecycle.evaluation_signature(self.settings, self.problem, None)}
        lifecycle.prepare_attempt(output, slot, request)
        (slot / 'complete.ok').write_text('WB2D_COMPLETE_V1', encoding='ascii')
        with patch.object(workbench, 'collect_result', return_value={'runDir': str(slot)}) as collect, \
             patch.object(workbench.subprocess, 'Popen') as launch:
            result = workbench.evaluate(self.x, self.settings, self.problem, output, evaluation_dir=slot)
        self.assertTrue(result['recoveredCompletedEvaluation'])
        collect.assert_called_once_with(slot, self.x, self.problem, None)
        launch.assert_not_called()

    def test_fresh_baseline_context_does_not_change_the_physical_evaluation_binding(self):
        fresh = deepcopy(self.problem)
        fresh['input_numeric_convention'] = 'verified after baseline'
        fresh['engineering_context']['freshly_verified_current_run'] = {'fixture': 'new observations'}
        self.assertEqual(lifecycle.evaluation_signature(self.settings, fresh, None),
                         lifecycle.evaluation_signature(self.settings, self.problem, None))
        fresh['variables'][0]['upper'] += 1
        self.assertNotEqual(lifecycle.evaluation_signature(self.settings, fresh, None),
                            lifecycle.evaluation_signature(self.settings, self.problem, None))
