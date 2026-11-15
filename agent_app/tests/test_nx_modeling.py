from pathlib import Path
import tempfile
import unittest

from agent_app.nx_modeling.nx12 import compile_journal
from agent_app.nx_modeling.schema import DiskPlan, ModelingPlanError
from agent_app.nx_modeling.service import NXModelingService
from agent_app.nx_modeling.language import plan_local, plan_llm
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch
import json


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

    def test_description_and_clarification(self):
        parsed = plan_local('创建圆盘，外径100毫米，厚度10毫米，中心孔径20毫米，孔数4，孔径8毫米，节圆直径60毫米')
        self.assertEqual(parsed['status'], 'planned')
        self.assertEqual(parsed['plan']['bolt_hole_count'], 4)
        self.assertEqual(plan_local('创建圆盘，外径100')['status'], 'clarify')
        self.assertEqual(plan_local('创建圆盘，外径100，厚度10，倒角2')['status'], 'clarify')
        self.assertEqual(plan_local('创建圆盘，外径100，厚度10，孔数4')['status'], 'clarify')

    def test_rejects_overlapping_bolt_holes(self):
        value = self.plan()
        value.update(bolt_hole_count=128, bolt_hole_diameter=20)
        with self.assertRaises(ModelingPlanError):
            DiskPlan.parse(value)

    def test_model_output_is_validated(self):
        class FakeClient:
            def __init__(self, cfg, live): pass
            def generate(self, prompt, folder):
                return json.dumps({'status': 'planned', 'plan': self_plan,
                                   'questions': [], 'notes': []}), {}
        self_plan = self.plan()
        settings = SimpleNamespace(root=Path('.'), api_config=Path('unused'))
        config = {'max_output_tokens': 2048, 'api_key_env': 'unused'}
        with patch('python_control.runtime.bridge.load_config', return_value=config), \
             patch('python_control.runtime.api_key', return_value=nullcontext()):
            self.assertEqual(plan_llm('圆盘', settings, FakeClient)['status'], 'planned')
            self_plan['script'] = 'danger'
            with self.assertRaises(ModelingPlanError):
                plan_llm('圆盘', settings, FakeClient)


if __name__ == '__main__':
    unittest.main()
