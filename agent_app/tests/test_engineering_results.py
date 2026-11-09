"""Result cards keep measured values and unknown reference constraints distinct."""
import unittest

from agent_app.engineering_results import result_cards
from agent_app.tests.test_objectives import dual_policy, mass_limit
from python_control.objectives import archive, selection_summary


class EngineeringResultTests(unittest.TestCase):
    def test_single_design_keeps_mass_as_measured_and_relative_constraint_unknown(self):
        policy = dual_policy()
        policy['constraints'] = [mass_limit()]
        card = result_cards({'ok': True, 'data': {'observation': {
            'parameters': {}, 'stress_mpa': 700, 'mass_kg': 10, 'area_m2': .02},
            'optimization_policy': policy, 'native_verified': True, 'native_report_file': 'native.json'}})[0]
        self.assertEqual(card['observation']['mass_kg'], 10)
        self.assertEqual(card['constraint_status'], 'missing')
        self.assertIsNone(card['evaluation']['constraint_checks'][0]['margin'])
        self.assertEqual(card['engineering_qualification'], 'not_assessed')

    def test_no_measured_mass_is_never_inferred_from_area(self):
        card = result_cards({'job': {'job_id': 'job_one', 'result': {'ok': True, 'data': {
            'observation': {'stress_mpa': 700, 'area_m2': .02}}}}})[0]
        self.assertNotIn('mass_kg', card['observation'])
        self.assertEqual(card['job_id'], 'job_one')
        self.assertEqual(card['constraint_status'], 'not_configured')

    def test_infeasible_stress_minimum_and_feasible_recommendation_keep_their_evaluations(self):
        history = [{'stress_mpa': 1000, 'mass_kg': 10}, {'stress_mpa': 800, 'mass_kg': 12},
                   {'stress_mpa': 900, 'mass_kg': 9}]
        policy = dual_policy()
        policy['constraints'] = [mass_limit()]
        cards = result_cards({'ok': True, 'data': {'run_id': 'run_test',
            'design_selection': selection_summary(history, archive(history, policy))}})
        self.assertEqual([card['iteration'] for card in cards], [1, 2])
        self.assertEqual([card['evaluation']['status'] for card in cards], ['infeasible', 'feasible'])
        self.assertEqual(result_cards({'ok': False}), [])
