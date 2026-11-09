"""Replay generated journals against a stale-model fixture without a solver."""
import json
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from python_control import journal, runtime, workbench


PROJECT = Path(__file__).resolve().parents[2]
MAPPING = ['P8', 'P9', 'P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7']


class WorkbenchGeometrySyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='disk_geometry_sync_')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        self.reference = [v['reference'] for v in self.problem['variables']]

    def replay(self, expected, refresh_works=True):
        macro = self.folder / 'evaluate.wbjn'
        result, complete = self.folder / 'result.csv', self.folder / 'complete.ok'
        journal.create(PROJECT, expected, macro, result, complete)
        source = macro.read_text(encoding='ascii')
        calls = []
        # The saved project DP already has the reference expressions, while its
        # Mechanical database retains the geometry of an earlier design.
        state = {'parameters': dict(zip(MAPPING, self.reference)),
                 'geometry': [18.1174, 48.4278, 53.8126, 23.6493, 4.0410,
                              44.0, 8.0, 6.0, 18.0]}
        state['model'] = list(state['geometry'])
        state['dirty_parameters'] = set()
        state['expressions'] = []

        class File:
            @staticmethod
            def WriteAllText(path, text):
                Path(path).write_text(text, encoding='utf-8')

            @staticmethod
            def ReadAllText(path):
                return Path(path).read_text(encoding='utf-8')

        class DesignPoint:
            def SetParameterExpression(self, Parameter, Expression):
                value = float(Expression)
                if state['parameters'][Parameter.name] != value:
                    state['dirty_parameters'].add(Parameter.name)
                state['parameters'][Parameter.name] = value
                state['expressions'].append((Parameter.name, value))
                calls.append('set:' + Parameter.name)

        class GeometryComponent:
            def Update(self, Force=False, **options):
                self_check.assertTrue(Force)
                self_check.assertEqual(options, {})
                for name in state['dirty_parameters']:
                    state['geometry'][MAPPING.index(name)] = state['parameters'][name]
                state['dirty_parameters'].clear()
                calls.append('force_geometry')

        class ModelComponent:
            def Refresh(self):
                calls.append('refresh_model')
                if refresh_works:
                    state['model'] = list(state['geometry'])

        class Model:
            def Edit(self, Interactive):
                self_check.assertFalse(Interactive)
                calls.append('edit_model')

            def SendCommand(self, Language, Command):
                self_check.assertEqual(Language, 'Python')
                calls.append('native_verification')
                success = state['model'] == expected
                report = {'success': success, 'error': None if success else 'stale geometry'}
                (self_check.folder / 'mechanical_verified.json').write_text(json.dumps(report), encoding='utf-8')

            def Exit(self):
                calls.append('exit_model')

        class System:
            def GetContainer(self, ComponentName):
                if ComponentName == 'Geometry':
                    return SimpleNamespace(GetGeometryProperties=lambda: SimpleNamespace(
                        GeometryImportSavePartFile=False,
                        GeometryFilePath=str(PROJECT / 'project_v2/2dmodel_files/dp0/SYS/DM/SYS.agdb')))
                return Model()

            def GetComponent(self, Name):
                return GeometryComponent() if Name == 'Geometry' else ModelComponent()

        class Parameters:
            @staticmethod
            def GetDesignPoint(Name):
                self_check.assertEqual(Name, '0')
                return DesignPoint()

            @staticmethod
            def GetParameter(Name):
                index = MAPPING.index(Name)
                return SimpleNamespace(name=Name, DisplayText='DS_' + self_check.problem['input_order'][index])

            @staticmethod
            def ExportAllDesignPointsData(FilePath):
                calls.append('export')
                Path(FilePath).write_text('fixture export', encoding='ascii')

        self_check = self
        system_module, io_module = ModuleType('System'), ModuleType('System.IO')
        io_module.File = File
        system_module.IO = io_module
        system_module.DateTime = SimpleNamespace(UtcNow=SimpleNamespace(
            ToString=lambda format: '2026-11-07T06:00:00.0000000Z'))
        def update_design_points(DesignPoints):
            self.assertEqual(len(DesignPoints), 1)
            calls.append('update_design_point')
        namespace = {'SetScriptVersion': lambda **kwargs: None, 'unicode': str,
                     'GetSystem': lambda **kwargs: System(), 'Parameters': Parameters,
                     'UpdateAllDesignPoints': update_design_points}
        with patch.dict('sys.modules', {'System': system_module, 'System.IO': io_module}):
            exec(compile(source, str(macro), 'exec'), namespace)
        return state, calls, source, complete

    def test_reference_equal_to_saved_dp_still_refreshes_stale_model(self):
        state, calls, source, complete = self.replay(self.reference)
        self.assertEqual(state['model'], self.reference)
        self.assertEqual(complete.read_text(encoding='ascii').strip(), 'WB2D_COMPLETE_V1')
        self.assertLess(calls.index('set:P7'), calls.index('force_geometry'))
        self.assertLess(calls.index('force_geometry'), calls.index('refresh_model'))
        self.assertLess(calls.index('refresh_model'), calls.index('update_design_point'))
        self.assertLess(calls.index('update_design_point'), calls.index('native_verification'))
        self.assertEqual(calls.count('update_design_point'), 1)
        self.assertNotIn('Reset(', source)

    def test_modified_candidate_is_applied_before_forced_geometry_update(self):
        candidate = list(self.reference)
        candidate[0] = round(candidate[0] + .1234, 4)
        state, calls, source, complete = self.replay(candidate)
        self.assertEqual(state['model'], candidate)
        self.assertTrue(complete.is_file())

    def test_bound_values_are_touched_inside_bounds_and_restored_before_update(self):
        candidate = [v['upper'] if i % 2 else v['lower']
                     for i, v in enumerate(self.problem['variables'])]
        state, calls, source, complete = self.replay(candidate)
        self.assertEqual(state['model'], candidate)
        self.assertEqual(len(state['expressions']), 18)
        for i, variable in enumerate(self.problem['variables']):
            first, final = state['expressions'][i * 2:i * 2 + 2]
            self.assertEqual(first[0], MAPPING[i])
            self.assertEqual(final, (MAPPING[i], candidate[i]))
            self.assertGreaterEqual(first[1], variable['lower'])
            self.assertLessEqual(first[1], variable['upper'])
            self.assertAlmostEqual(abs(first[1] - final[1]), .0001)
        self.assertEqual(calls.count('force_geometry'), 1)

    def test_native_mismatch_still_prevents_result_export_and_completion(self):
        with self.assertRaisesRegex(RuntimeError, 'Native verification failed: stale geometry'):
            self.replay(self.reference, refresh_works=False)
        self.assertFalse((self.folder / 'complete.ok').exists())
        self.assertFalse((self.folder / 'result.csv').exists())

    def test_nonzero_workbench_exit_surfaces_native_preflight_error(self):
        settings = SimpleNamespace(project_file=self.folder / 'project/model.wbpj',
            workbench_executable=self.folder / 'NO_SOLVER.exe', root=self.folder)
        def create(root, x, macro, result, complete):
            Path(macro).write_text('# fixture only', encoding='ascii')
            report = {'success': False, 'stage': 'preflight',
                      'error': 'maximum radius mm differs: actual=231.0481, expected=237.098'}
            (Path(macro).parent / 'mechanical_verified.json').write_text(json.dumps(report), encoding='utf-8')
        process = Mock()
        process.pid = 123456789
        process.wait.return_value = 2
        with patch.object(workbench, 'create_journal', side_effect=create), patch.object(
                workbench.subprocess, 'Popen', return_value=process), patch.object(
                workbench.sys, 'platform', 'win32'), patch.object(
                workbench.subprocess, 'CREATE_NEW_PROCESS_GROUP', 512, create=True):
            with self.assertRaisesRegex(runtime.ControllerError, 'preflight.*231.0481.*237.098.*mechanical_verified.json'):
                workbench.evaluate(self.reference, settings, self.problem, self.folder / 'evaluations')


if __name__ == '__main__':
    unittest.main()
