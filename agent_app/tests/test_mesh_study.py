"""Mesh change reporting carries an explicit unconfigured tolerance."""
import unittest

from agent_app.mesh_study import compare_meshes, render_report, run_study
from agent_app.tests import test_modal_profile as fixture


class MeshStudyTests(unittest.TestCase):
    def test_refinement_changes_and_optional_tolerance_are_computed_from_measurements(self):
        cases = []
        for size, stress, frequency in [(.5, 100, 1000), (.4, 90, 990), (.3, 89, 989)]:
            cases.append({'label': 'design', 'mesh_size_mm': size,
                'observation': {'stress_mpa': stress, 'modal_frequencies_hz': [frequency * i for i in range(1, 7)]}})
        result = compare_meshes(cases)
        self.assertEqual(len(result), 7)
        self.assertTrue(result[0]['changes_decreasing'])
        self.assertIsNone(result[0]['within_configured_tolerance'])
        self.assertAlmostEqual(result[0]['relative_changes'][-1], 1 / 89)
        self.assertFalse(compare_meshes(cases, .005)[0]['within_configured_tolerance'])
        self.assertTrue(compare_meshes(cases, .02)[0]['within_configured_tolerance'])

    def test_preview_has_six_design_tasks_and_does_not_create_a_study(self):
        owner = fixture.ModalProfileTests()
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        report = run_study(settings=owner.settings)
        self.assertEqual(report['status'], 'preview')
        self.assertEqual(report['request']['design_evaluation_tasks'], 6)
        self.assertEqual(report['request']['llm_requests'], 0)
        self.assertIsNone(report['request']['relative_tolerance'])
        self.assertFalse(owner.settings.output_root.exists())
        with self.assertRaises(ValueError):
            run_study(settings=owner.settings, mesh_sizes=[.5, .5, .3])
