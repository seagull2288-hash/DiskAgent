import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from nx_case_agent import plan
from nx_case_agent.core import execute,validate_assets
from nx_case_agent.precision_recipes import SECTION_DEFAULTS

class PrecisePlanningTests(unittest.TestCase):
    def test_precise_defaults_preserve_measured_reference(self):
        a=plan('两头厚中间薄的二维轮盘');b=plan('做一个带一片叶片的三维轮盘局部')
        self.assertEqual(a['parameters'],SECTION_DEFAULTS)
        self.assertEqual(b['parameters'],{'hub_length':56.9,'rim_radius':71.68})
        self.assertEqual(a['geometry_mode'],'precise');self.assertEqual(b['geometry_mode'],'precise')
        self.assertEqual(b['parameterization_scope'],'axial_and_radial_scale_only')
        self.assertFalse(b['full_feature_history_recovered'])
    def test_bold_and_natural_aliases_extract_six_dimensions(self):
        p=plan('三维单叶片轮盘，轮毂内孔半径约为 **5.2 mm**，轮毂轴向总长度约为 **56.9 mm**，盘体外缘半径约为 **71.7 mm**，外缘轴向宽度初始取 **20.8 mm**，叶尖到轮盘轴线的距离约为 **105 mm**，采用约 **7.66°** 的局部扇区。')
        self.assertEqual(set(p['supplied_parameters']),{'bore_radius','hub_length','rim_radius','rim_width','tip_radius','sector_angle'})
        self.assertEqual(p['parameters']['rim_radius'],71.68)
        self.assertTrue(p['reference_baseline'])
        self.assertEqual(p['nominal_resolutions'][0]['requested_nominal'],71.7)
    def test_new_values_are_actually_used(self):
        p=plan('三维轮盘，轮毂轴向总长度为 **60 mm**，盘体外缘半径 **75 mm**')
        self.assertEqual(p['parameters'],{'hub_length':60.,'rim_radius':75.})
        self.assertFalse(p['reference_baseline'])
        self.assertAlmostEqual(p['derived_parameters']['tip_radius']['value'],105*75/71.68)
    def test_section_nominal_resolution_and_explicit_edits(self):
        p=plan('二维轮盘，X1=20.81，Y2=8.18')
        self.assertEqual(p['parameters']['X1'],20.81175)
        self.assertEqual(p['parameters']['Y2'],8.1765525)
        self.assertTrue(p['reference_baseline'])
        p=plan('二维轮盘，X1=20.81',{'X1':20.81})
        self.assertEqual(p['parameters']['X1'],20.81);self.assertFalse(p['reference_baseline'])
        self.assertEqual(plan('二维轮盘，X1=23')['parameters']['X1'],23.)
    def test_coupled_edits_are_explicit(self):
        with self.assertRaisesRegex(ValueError,'不是独立参数'):plan('三维轮盘，盘体外缘半径75，内孔半径5.2')
        p=plan('三维轮盘，内孔半径5.2',{'rim_radius':75})
        self.assertAlmostEqual(p['derived_parameters']['bore_radius']['value'],5.2*75/71.68)
        self.assertEqual(p['nominal_resolutions'][0]['reason'],'explicit_independent_parameter_override')
    def test_unsupported_local_edits_never_fall_back(self):
        for text in ['三维轮盘，叶根圆角半径0.3','三维轮盘，叶根弦长20','三维轮盘，扭转角10','三维轮盘，扇区角9']:
            with self.subTest(text=text),self.assertRaises(ValueError):plan(text)
        with self.assertRaisesRegex(ValueError,'不能退回简化模型'):plan('三维轮盘',{'root_fillet':.3})
    def test_invalid_conflicting_or_nonfinite_input(self):
        requests=[('齿轮',None),('Geom2d和new160',None),('二维轮盘，X1=23，X1=24',None),('二维轮盘，X9=3',None),('二维轮盘',{'Y2':100}),('二维轮盘',{'X1':True}),('二维轮盘',{'X1':float('nan')}),('三维轮盘',{'rim_radius':float('inf')}),('二维轮盘',[])]
        for text,params in requests:
            with self.subTest(text=text,params=params),self.assertRaises(ValueError):plan(text,params)
    def test_request_state_is_not_shared(self):
        plan('二维轮盘',{'X1':23})
        self.assertEqual(plan('二维轮盘')['parameters']['X1'],SECTION_DEFAULTS['X1'])
    def test_missing_geometry_asset_prevents_execution(self):
        validate_assets()
        with tempfile.TemporaryDirectory() as folder,patch('nx_case_agent.core.ROOT',Path(folder)),patch('nx_case_agent.core.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'资产缺失'):execute('三维轮盘')
            run.assert_not_called()
if __name__=='__main__':unittest.main()
