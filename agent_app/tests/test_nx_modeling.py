from pathlib import Path
import tempfile
import unittest

from agent_app.nx_modeling.nx12 import compile_journal
from agent_app.nx_modeling.schema import DiskPlan, ModelingPlanError
from agent_app.nx_modeling.service import NXModelingService


class NXModelingTests(unittest.TestCase):
    def plan(self):
        return {'kind': 'disk', 'name': 'test_disk', 'outer_diameter': 300, 'thickness': 20,
                'center_hole_diameter': 60, 'bolt_circle_diameter': 200,
                'bolt_hole_diameter': 14, 'bolt_hole_count': 6, 'output_format': 'prt'}

    def test_preview_is_pure_and_normalized(self):
        with tempfile.TemporaryDirectory() as folder:
            result = NXModelingService(folder).preview(self.plan())
            self.assertTrue(result['ok'])
            self.assertEqual(result['status'], 'validated')
            self.assertFalse((Path(folder) / 'nx_modeling_jobs').exists())
            self.assertEqual(len(result['plan_sha256']), 64)

    def test_rejects_unregistered_code_and_geometry(self):
        value = self.plan()
        value['script'] = 'import os'
        with self.assertRaises(ModelingPlanError):
            DiskPlan.parse(value)
        value = self.plan()
        value['bolt_circle_diameter'] = 295
        with self.assertRaises(ModelingPlanError):
            DiskPlan.parse(value)

    def test_journal_contains_values_but_no_external_input(self):
        plan = DiskPlan.parse(self.plan())
        text = compile_journal(plan, Path(r'C:\safe\test_disk.prt'))
        self.assertIn('CreateCylinderBuilder', text)
        self.assertIn('bolt_hole_count', text)
        self.assertNotIn('subprocess', text)


if __name__ == '__main__':
    unittest.main()
