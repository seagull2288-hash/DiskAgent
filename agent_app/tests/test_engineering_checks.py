"""Exercise registered geometry rules and optional native diagnostics offline."""
import ast
from enum import Enum
from copy import deepcopy
import json
import math
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from python_control import engineering_checks as checks, runtime


PROJECT = Path(__file__).resolve().parents[2]


class EngineeringChecksTests(unittest.TestCase):
    def setUp(self):
        self.problem = runtime.bridge.load_problem(PROJECT / 'api_bridge/problem.json')
        self.x = [variable['reference'] for variable in self.problem['variables']]

    def test_registered_station_geometry_keeps_unconfigured_requirements_unknown(self):
        report = checks.require_geometry(self.x, self.problem)
        self.assertTrue(report['ok'])
        self.assertEqual(report['station_radii_mm'][0], 83.0)
        self.assertAlmostEqual(report['outer_radius_mm'], 83 + sum(self.x[:5]))
        self.assertAlmostEqual(report['minimum_station_thickness_mm'], 2 * min(self.x[5:]))
        statuses = {item['id']: item['status'] for item in report['checks']}
        self.assertEqual(statuses['positive_spans'], 'passed')
        self.assertEqual(statuses['manufacturing_thickness'], 'unknown')
        self.assertEqual(statuses['cad_curve_validity'], 'unknown')
        self.assertFalse(report['full_geometry_certified'])

    def test_nonpositive_combined_geometry_and_nonfinite_dimensions_are_blocked(self):
        for index, number in ((0, 0), (2, -1), (7, 0), (5, -2), (3, math.inf)):
            x = list(self.x)
            x[index] = number
            with self.subTest(index=index, value=number):
                self.assertFalse(checks.inspect_geometry(x, self.problem)['ok'])
                with self.assertRaises(runtime.ControllerError):
                    checks.require_geometry(x, self.problem)

    def test_other_parameterizations_and_extreme_inputs_remain_explicit(self):
        other = deepcopy(self.problem)
        other['workbench_parameter_ids'] = list(reversed(other['workbench_parameter_ids']))
        self.assertEqual(checks.inspect_geometry(self.x, other)['status'], 'unknown')
        report = checks.inspect_geometry([1e308] * 9, self.problem)
        self.assertFalse(report['ok'])
        json.dumps(report, allow_nan=False)

    def test_native_quality_records_metrics_without_inventing_acceptance_limits(self):
        detail = {'stressMPa': 700}
        before = deepcopy(detail)
        metric = {'status': 'available', 'minimum': 0.1, 'maximum': 1.0,
                  'mean': 0.75, 'standard_deviation': 0.12}
        native = {'actual': {'mesh_quality_after': {'status': 'available',
                  'selector_restored': True, 'metrics': {'element_quality': metric}}}}
        report = checks.result_diagnostics(detail, native=native,
                    baseline={'stressMPa': 740}, previous={'stressMPa': 710})
        self.assertEqual(report['mesh_quality']['status'], 'partial')
        self.assertEqual(report['mesh_quality']['acceptance_status'], 'unknown')
        self.assertEqual(report['mesh_quality']['metrics']['element_quality']['minimum'], 0.1)
        self.assertEqual(len(report['stress_changes']), 2)
        self.assertEqual(report['warnings'], [])
        self.assertEqual(detail, before)

    def test_unavailable_quality_and_stress_change_are_diagnostics_only(self):
        detail = {'stressMPa': 500}
        report = checks.result_diagnostics(detail, native={}, baseline={'stressMPa': 740},
                                           previous={'stress_mpa': 700})
        self.assertTrue(report['ok'])
        self.assertEqual(report['acceptance_effect'], 'diagnostic_only')
        self.assertEqual(report['status'], 'warning')
        self.assertEqual(report['mesh_quality']['status'], 'unavailable')
        self.assertEqual(report['stress_change_warning_percent'], 25)
        self.assertEqual(len(report['warnings']), 2)
        self.assertEqual(detail['stressMPa'], 500)
        for invalid in (None, [], {'actual': []}, {'actual': {'mesh_quality_after': []}}):
            if invalid is None:
                invalid = {}
            self.assertTrue(checks.result_diagnostics(detail, native=invalid)['ok'])
        extreme = checks.result_diagnostics({'stressMPa': 1e308}, native={},
                                            baseline={'stressMPa': 1e-308})
        json.dumps(extreme, allow_nan=False)
        self.assertIsNone(extreme['stress_changes'][0]['relative_change_percent'])

    def mesh_capture(self, mesh):
        text = (PROJECT / 'mechanical_template.py.txt').read_text(encoding='utf-8')
        tree = ast.parse(text)
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in ('finite', 'mesh_quality_snapshot')]
        namespace = {'math': math, 'unicode': str, 'Model': SimpleNamespace(Mesh=mesh),
                     'ExtAPI': SimpleNamespace(DataModel=SimpleNamespace(
                         MeshDataByName=lambda name: SimpleNamespace(NodeCount=12, ElementCount=4)))}
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'native_mesh_fixture', 'exec'), namespace)
        enums = ModuleType('Ansys.Mechanical.DataModel.Enums')
        class MetricChoice(Enum):
            ElementQuality = 1
            JacobianRatio = 2

            def __int__(self):
                return self.value

        enums.MeshMetricType = MetricChoice
        with patch.dict('sys.modules', {'Ansys.Mechanical.DataModel.Enums': enums}):
            return namespace['mesh_quality_snapshot']()

    def test_native_mesh_capture_restores_statistics_selector(self):
        mesh = SimpleNamespace(MeshMetric='original', Minimum=0.2, Maximum=0.9,
                               Average=0.7, StandardDeviation=0.1)
        report = self.mesh_capture(mesh)
        self.assertEqual(report['status'], 'available')
        self.assertTrue(report['selector_restored'])
        self.assertEqual(mesh.MeshMetric, 'original')
        self.assertEqual(report['metrics']['jacobian_ratio']['minimum'], 0.2)
        self.assertIsNone(report['acceptance_thresholds'])

    def test_native_mesh_capture_handles_unsupported_statistics(self):
        mesh = SimpleNamespace(MeshMetric='original')
        report = self.mesh_capture(mesh)
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(mesh.MeshMetric, 'original')
        self.assertTrue(report['selector_restored'])
        self.assertEqual(report['metrics']['element_quality']['status'], 'unavailable')

    def test_v202_statistics_selector_accepts_integers_and_rejects_enum_objects(self):
        class IntegerSelector:
            Minimum, Maximum, Average, StandardDeviation = 0.2, 0.9, 0.7, 0.1

            def __init__(self):
                self.choice = 0

            @property
            def MeshMetric(self):
                return self.choice

            @MeshMetric.setter
            def MeshMetric(self, value):
                if type(value) is not int:
                    raise TypeError('expected int')
                self.choice = value

        mesh = IntegerSelector()
        report = self.mesh_capture(mesh)
        self.assertEqual(report['status'], 'available')
        self.assertEqual(mesh.MeshMetric, 0)

    def test_zero_initialized_mesh_statistics_are_reported_as_unavailable(self):
        mesh = SimpleNamespace(MeshMetric=0, Minimum=0, Maximum=0, Average=0, StandardDeviation=0)
        report = self.mesh_capture(mesh)
        self.assertEqual(report['status'], 'unavailable')
        self.assertIn('unpopulated', report['metrics']['element_quality']['reason'])
