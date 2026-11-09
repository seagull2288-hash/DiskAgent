"""Regress the real 2D zero-mass property and MAPDL tonne conversion cases."""
import math
import os
from pathlib import Path
import tempfile
from textwrap import dedent
from types import SimpleNamespace
import unittest

from python_control.native_metrics import (
    COLLECT, engineering_density, revolved_mass, solver_mass_evidence)


PROJECT = Path(__file__).resolve().parents[2]
MATERIAL = PROJECT / 'project_v2/2dmodel_files/dp0/SYS/ENGD/material.engd'


class AxisymmetricMassTests(unittest.TestCase):
    def test_annular_cylinder_matches_closed_form_volume(self):
        inner, outer, thickness, density = 0.1, 0.2, 0.02, 7850
        volume, mass = revolved_mass((outer - inner) * thickness, (inner + outer) / 2, density)
        expected_volume = math.pi * (outer ** 2 - inner ** 2) * thickness
        self.assertAlmostEqual(volume, expected_volume, places=12)
        self.assertAlmostEqual(mass, expected_volume * density, places=10)

    def test_density_is_read_from_assigned_material_with_explicit_si_units(self):
        self.assertEqual(engineering_density(str(MATERIAL), 'Structural Steel'), 7850)
        with self.assertRaises(ValueError):
            engineering_density(str(MATERIAL), 'Unassigned material')
        text = MATERIAL.read_text(encoding='utf-8').replace('<Name>kg</Name>', '<Name>lb</Name>')
        with tempfile.TemporaryDirectory(prefix='disk_density_') as folder:
            path = Path(folder) / 'material.engd'
            path.write_text(text, encoding='utf-8')
            with self.assertRaises(ValueError):
                engineering_density(str(path), 'Structural Steel')

    def test_solver_tonne_and_full_circle_are_required_for_crosscheck(self):
        text = (' MASS = TONNE (Mg)\n TOTAL MASS = 0.36125E-01\n'
                ' MASS AND MOMENT VALUES ARE EVALUATED FOR AN AXISYMMETRIC 360. DEGREE MODEL.\n')
        check = solver_mass_evidence(text, 36.13030657288775)
        self.assertEqual(check['solver_mass_kg'], 36.125)
        self.assertLess(check['relative_difference'], 0.001)
        with self.assertRaises(ValueError):
            solver_mass_evidence(text.replace('360.', '180.'), 36.13)
        with self.assertRaises(ValueError):
            solver_mass_evidence(text, 0.03613)

    def test_zero_native_sheet_mass_still_produces_verified_revolved_mass(self):
        class Quantity:
            def __init__(self, value):
                self.Value = value

            def ConvertUnit(self, unit):
                return self

        body = SimpleNamespace(Mass=Quantity(0), SurfaceArea=Quantity(0.002),
                               CentroidX=Quantity(0.15), Material='Structural Steel', Suppressed=False)
        expected = math.pi * (0.2 ** 2 - 0.1 ** 2) * 0.02 * 7850
        text = (' MASS = KILOGRAMS (kg)\n TOTAL MASS = %.12E\n'
                ' AXISYMMETRIC 360. DEGREE MODEL\n') % expected
        path_api = SimpleNamespace(Combine=lambda *parts: os.path.join(*parts),
                                   GetDirectoryName=os.path.dirname,
                                   GetFileNameWithoutExtension=lambda path: Path(path).stem)
        namespace = {
            'report': {}, 'progress': lambda _: None, 'finite': lambda value, _: float(value),
            'ExtAPI': SimpleNamespace(DataModel=SimpleNamespace(GetObjectsByType=lambda _: [body])),
            'DataModelObjectCategory': SimpleNamespace(Body='Body'),
            'Model': SimpleNamespace(Geometry=SimpleNamespace(Mass=Quantity(0), Model2DBehavior='AxiSymmetric')),
            'Path': path_api, 'PROJECT_PATH': str(PROJECT / 'project_v2/2dmodel.wbpj'),
            'working_dir': 'fixture_only', 'File': SimpleNamespace(ReadAllText=lambda _: text),
            'unicode': str, 'engineering_density': engineering_density,
            'revolved_mass': revolved_mass, 'solver_mass_evidence': solver_mass_evidence,
        }
        code = dedent(COLLECT).replace('__METRIC_POLICY__', repr({'collect_mass': True, 'modal': None}))
        exec(compile(code, 'native_mass_fixture', 'exec'), namespace)
        self.assertAlmostEqual(namespace['report']['mass_kg'], expected, places=10)
        evidence = namespace['report']['metric_evidence']['mass_kg']
        self.assertEqual(evidence['raw_body_mass_kg'], 0)
        self.assertEqual(evidence['solver_crosscheck']['status'], 'passed')
