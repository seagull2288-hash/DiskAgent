import unittest
from nx_case_agent import plan

class CasePlanningTests(unittest.TestCase):
    def test_incomplete_natural_descriptions_infer_cases(self):
        a=plan('我要一个两头厚中间薄的二维轮盘')
        b=plan('做一个带一片叶片的三维轮盘局部，尺寸你来补全')
        self.assertEqual(a['model_id'],'geom2d');self.assertEqual(b['model_id'],'new160')
        self.assertEqual(len(a['inferred_parameters']),10)
        self.assertEqual(b['parameters']['tip_radius'],105)
    def test_explicit_parameters_and_optimizer(self):
        p=plan('建立二维轮盘，X1=23，Y2=10')
        self.assertEqual(p['parameters']['X1'],23);self.assertEqual(p['parameters']['Y2'],10)
        self.assertNotIn('X1',p['inferred_parameters'])
        p=plan('带一片叶片的三维轮盘，叶尖半径为110毫米，扭转角取12度',{'web_thickness':8})
        self.assertEqual(p['parameters']['tip_radius'],110)
        self.assertEqual(p['parameters']['blade_twist'],12)
        self.assertEqual(p['parameters']['web_thickness'],8)
    def test_case_defaults_do_not_leak_between_tasks(self):
        plan('二维轮盘',{'X1':23})
        self.assertEqual(plan('二维轮盘')['parameters']['X1'],20.81)
    def test_invalid_or_conflicting_requests(self):
        for description,params in [('建立齿轮',None),('Geom2d和new160',None),('二维轮盘，X1=23，X1=24',None),('二维轮盘，X9=3',None),('二维轮盘',{'Y2':100}),('三维轮盘',{'root_chord':50}),('二维轮盘',{'X1':True}),('二维轮盘',{'X1':float('nan')}),('二维轮盘',{'unknown':4})]:
            with self.subTest(description=description,params=params),self.assertRaises(ValueError):plan(description,params)
    def test_simplifications_are_visible(self):
        p=plan('单叶片轮盘，叶根采用圆角')
        self.assertTrue(any('圆角' in note for note in p['notes']))
        self.assertFalse(any('省略' in note or '未建立' in note for note in p['notes']))
    def test_height_is_derived_and_conflicts_are_rejected(self):
        p=plan('三维轮盘，盘体外缘半径75毫米，叶片高度35毫米')
        self.assertEqual(p['parameters']['tip_radius'],110)
        self.assertNotIn('blade_height',p['parameters'])
        self.assertEqual(p['derived_parameters']['blade_height']['value'],35)
        p=plan('单叶片轮盘',{'blade_height':40,'rim_radius':75})
        self.assertEqual(p['parameters']['tip_radius'],115)
        p=plan('三维轮盘，叶片高度35毫米',{'tip_radius':115})
        self.assertAlmostEqual(p['derived_parameters']['blade_height']['value'],43.3)
        with self.assertRaisesRegex(ValueError,'冲突'):plan('三维轮盘，叶片高度30，叶尖半径105')
        with self.assertRaises(ValueError):plan('三维轮盘',{'blade_height':float('nan')})
    def test_root_fillet_and_sector_angle_language(self):
        p=plan('三维轮盘，先建立约8.5°的局部扇区，叶根圆角半径0.4毫米')
        self.assertEqual(p['parameters']['sector_angle'],8.5)
        self.assertIn('sector_angle',p['supplied_parameters'])
        self.assertEqual(p['parameters']['root_fillet'],.4)
        self.assertEqual(plan('三维轮盘，建立约8.5°的局部扇区',{'sector_angle':9})['parameters']['sector_angle'],9)
        with self.assertRaisesRegex(ValueError,'sector_angle'):plan('三维轮盘，扇区角8度，建立9°的局部扇区')
        with self.assertRaises(ValueError):plan('三维轮盘',{'root_fillet':1.5})
if __name__=='__main__':unittest.main()
