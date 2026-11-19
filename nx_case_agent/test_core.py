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
if __name__=='__main__':unittest.main()
