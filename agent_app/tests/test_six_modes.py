"""Six ordered frequencies, selected-order agreement and legacy measurements."""
import json
from pathlib import Path
import tempfile
from textwrap import dedent
from types import SimpleNamespace
import unittest

from python_control import controller, native_metrics, runtime, workbench


class SixModeTests(unittest.TestCase):
    def collect(self, bad_order=None):
        class Result:
            ObjectState = 'Solved'
            Mode = 0
            deleted = False

            @property
            def ReportedFrequency(self):
                value = -1 if self.Mode == bad_order else self.Mode * 100.
                return SimpleNamespace(ConvertUnit=lambda _: SimpleNamespace(Value=value))

            def Delete(self):
                self.deleted = True

        results = []
        def add():
            result = Result()
            results.append(result)
            return result
        solution = SimpleNamespace(AddTotalDeformation=add, ClearGeneratedData=lambda: None,
            Solve=lambda _: None, EvaluateAllResults=lambda: None, WorkingDir='fixture', Status='Done')
        modal = SimpleNamespace(Name='Modal', Solution=solution, AnalysisSettings=SimpleNamespace(MaximumModesToFind=6),
            InitialConditions=[SimpleNamespace(PreStressEnvironmentModalIC=SimpleNamespace(ObjectId=22))])
        namespace = {'report': {}, 'progress': lambda _: None, 'finite': lambda value, _: float(value),
            'Model': SimpleNamespace(Analyses=[modal]), 'analysis': SimpleNamespace(ObjectId=22), 'unicode': str,
            'SolutionStatusType': SimpleNamespace(Done='Done'), 'ObjectState': SimpleNamespace(Solved='Solved'),
            'DateTime': SimpleNamespace(UtcNow=1), 'Path': SimpleNamespace(Combine=lambda *args: '/'.join(args)),
            'File': SimpleNamespace(Exists=lambda _: True), 'FileInfo': lambda _: SimpleNamespace(LastWriteTimeUtc=2),
            'file_snapshot': lambda path: {'path': path, 'exists': True}}
        policy = {'collect_mass': False, 'modal': {'analysis_name': 'Modal', 'mode_index': 3,
            'family': 'axisymmetric_fixed_order', 'prestress_required': True}}
        code = dedent(native_metrics.COLLECT).replace('__METRIC_POLICY__', repr(policy))
        if bad_order:
            with self.assertRaises(RuntimeError):
                exec(code, namespace)
        else:
            exec(code, namespace)
        self.assertTrue(all(result.deleted for result in results))
        return namespace['report']

    def test_collector_reads_all_six_and_selected_scalar_from_same_results(self):
        result = self.collect()
        self.assertEqual(result['modal_frequencies_hz'], [100, 200, 300, 400, 500, 600])
        self.assertEqual(result['frequency_hz'], 300)
        self.assertEqual(result['metric_evidence']['modal_frequencies_hz']['mode_indices'], [1, 2, 3, 4, 5, 6])
        self.assertTrue(result['metric_evidence']['modal_frequencies_hz']['prestress_verified'])
        self.collect(bad_order=4)

    def test_native_validation_rejects_changed_vector_and_preserves_legacy_scalar(self):
        project = Path(__file__).resolve().parents[2]
        problem = runtime.bridge.load_problem(project / 'api_bridge/problem.json')
        x = [variable['reference'] for variable in problem['variables']]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'native.json'
            native = {'success': True, 'solve_requested': True, 'stress_MPa': 700, 'displacement_mm': .2,
                'actual': {'result_file_after': {'fresh_for_this_solve': True}}, 'frequency_hz': 300,
                'metric_evidence': {'frequency_hz': {'unit': 'Hz', 'mode_index': 3, 'fresh_for_this_solve': True}}}
            detail = {'xActual': x, 'stressMPa': 700, 'displacementMM': .2, 'area': workbench.approximate_area(x),
                      'nativeReportFile': str(path), 'frequency_hz': 300}
            path.write_text(json.dumps(native), encoding='utf-8')
            workbench.verify_native(detail)
            self.assertNotIn('modal_frequencies_hz', controller.observation(detail))
            native['modal_frequencies_hz'] = [100, 200, 300, 400, 500, 600]
            native['metric_evidence']['modal_frequencies_hz'] = {'unit': 'Hz', 'mode_indices': list(range(1, 7)),
                                                               'fresh_for_this_solve': True}
            path.write_text(json.dumps(native), encoding='utf-8')
            detail['modal_frequencies_hz'] = list(native['modal_frequencies_hz'])
            workbench.verify_native(detail)
            self.assertEqual(controller.observation(detail)['modal_frequencies_hz'], native['modal_frequencies_hz'])
            detail['modal_frequencies_hz'][2] = 301
            with self.assertRaises(runtime.ControllerError):
                workbench.verify_native(detail)

    def test_prediction_input_accepts_six_measured_frequencies(self):
        from agent_app.performance_predictor import predict_snapshot
        from python_control.search import DEFAULT_CONFIG
        project = Path(__file__).resolve().parents[2]
        problem = runtime.bridge.load_problem(project / 'api_bridge/problem.json')
        point = {'parameters': {v['name']: v['reference'] for v in problem['variables']},
                 'stress_mpa': 700, 'displacement_mm': .2, 'area_m2': .01,
                 'frequency_hz': 300, 'modal_frequencies_hz': [100, 200, 300, 400, 500, 600]}
        result = predict_snapshot([point], problem, dict(DEFAULT_CONFIG))
        self.assertFalse(result['validation']['enabled'])
