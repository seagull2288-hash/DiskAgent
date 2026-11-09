"""V3 local support provenance over real numerical and before-solve paths."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import numpy as np

from agent_app import performance_predictor as predictor, prediction_audit
from agent_app.schemas import TaskValidationError
from agent_app.tests import test_contract as fixtures
from python_control import controller, runtime
from python_control.search import DEFAULT_CONFIG


class LocalPredictionTests(unittest.TestCase):
    save_run = fixtures.FixtureCase.save_run

    def setUp(self):
        fixtures.FixtureCase.setUp(self)
        self.cfg = dict(deepcopy(DEFAULT_CONFIG), selection_policy='hybrid_local_v3',
                        strategy='center_surrogate')
        self.history = []
        for index in range(50):
            parameters = {v['name']: v['reference'] for v in self.problem['variables']}
            parameters['X1'] = round(parameters['X1'] - .24 + .01 * index, 4)
            self.history.append({'parameters': parameters,
                'stress_mpa': 700 + 20 * (parameters['X1'] - 20.813),
                'displacement_mm': .2, 'area_m2': .01})
        self.inside = [v['reference'] for v in self.problem['variables']]
        self.inside[0] = 20.8463
        self.outside = list(self.inside)
        self.outside[0] = 22.813
        for item in (
            patch.object(runtime.bridge.Client, 'generate', side_effect=AssertionError('no provider')),
            patch.object(controller, 'evaluate', side_effect=AssertionError('no Workbench')),
        ):
            item.start()
            self.addCleanup(item.stop)

    def predict(self, candidates=None):
        return predictor.predict_snapshot(self.history, self.problem, self.cfg,
            [self.inside, self.outside] if candidates is None else candidates)

    def pending_run(self, x):
        run, state = self.save_run(done=49, target=50)
        details = [state['baseline']] + [record['detail'] for record in state['records']]
        for detail, observation in zip(details, self.history):
            detail.update(xActual=[observation['parameters'][name] for name in self.problem['input_order']],
                          stressMPa=observation['stress_mpa'], displacementMM=observation['displacement_mm'],
                          area=observation['area_m2'])
        for iteration, record in enumerate(state['records'], 1):
            record.update(iteration=iteration, proposal={'x': list(record['detail']['xActual'])})
        state.update(history=deepcopy(self.history), pending={
            'iteration': 50, 'proposal': {'x': list(x)}, 'detail': None})
        runtime.write_json(run / 'checkpoint.json', state)
        runtime.write_json(run / 'search_config.json', self.cfg)
        return run, state

    def test_real_local_model_separates_distance_from_validated_region(self):
        with patch.object(predictor.surrogate, 'fit_validated',
                          side_effect=AssertionError('v3 must use local validation')):
            result = self.predict()
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['model_version'], predictor.local_surrogate.MODEL_VERSION)
        self.assertEqual(result['model_scope'], 'local')
        inside, outside = result['predictions']
        self.assertTrue(inside['within_validated_region'])
        self.assertTrue(inside['eligible_for_ranking'])
        self.assertTrue(outside['within_training_neighborhood'])
        self.assertFalse(outside['within_validated_region'])
        self.assertFalse(outside['eligible_for_ranking'])
        self.assertEqual(result['validation']['uncertainty_calibration']['coverage_is_certified'], False)

    def test_model_mask_must_match_saved_measured_support(self):
        fit = predictor.local_surrogate.fit_local_validated

        def invalid_mask(*args, **kwargs):
            model, report = fit(*args, **kwargs)
            self.assertIsNotNone(model, report)
            model.support_mask = lambda z: np.ones(len(z), dtype=bool)
            return model, report

        with patch.object(predictor.local_surrogate, 'fit_local_validated', side_effect=invalid_mask):
            result = self.predict()
        self.assertEqual(result['status'], 'validation_failed')
        self.assertEqual(result['predictions'], [])

    def test_real_before_solve_record_preserves_outside_support_and_reuses_bytes(self):
        run, state = self.pending_run(self.outside)
        value = prediction_audit.record_before_solve(run, self.outside, self.problem, settings=self.settings)
        self.assertEqual(value['prediction']['status'], 'ready', value)
        self.assertFalse(value['prediction']['predictions'][0]['within_validated_region'])
        self.assertFalse(value['prediction']['predictions'][0]['eligible_for_ranking'])
        path = run / 'prediction_audit' / 'iter_050.json'
        original = path.read_bytes()
        with patch.object(prediction_audit, 'predict_snapshot', side_effect=AssertionError('immutable audit')):
            reused = prediction_audit.record_before_solve(run, self.outside, self.problem, settings=self.settings)
        self.assertEqual(value, reused)
        self.assertEqual(path.read_bytes(), original)
        record = prediction_audit.read_prediction_records(run, state, self.problem)['records'][-1]
        self.assertFalse(record['within_validated_region'])
        self.assertFalse(record['eligible_for_ranking'])
        self.assertEqual(record['model_scope'], 'local')
        self.assertIsNone(record['error_mpa'])

    def test_resigned_boolean_or_support_tampering_is_rejected_before_solve(self):
        run, state = self.pending_run(self.outside)
        value = prediction_audit.record_before_solve(run, self.outside, self.problem, settings=self.settings)
        self.assertEqual(value['prediction']['status'], 'ready', value)
        for mutate in (
            lambda p: p['predictions'][0].update(within_validated_region=True, eligible_for_ranking=True),
            lambda p: p['validation']['support'].update(radius_fraction=.6),
            lambda p: p['validation']['support']['anchor_normalized'].__setitem__(0, .1),
            lambda p: p['validation'].update(final_training_history_indices_0based=[True] * 50),
        ):
            changed = deepcopy(value)
            mutate(changed['prediction'])
            changed['record_signature'] = prediction_audit._digest({
                key: item for key, item in changed.items() if key != 'record_signature'})
            runtime.write_json(run / 'prediction_audit' / 'iter_050.json', changed)
            with self.subTest(mutation=mutate), self.assertRaises(TaskValidationError):
                prediction_audit.record_before_solve(run, self.outside, self.problem, settings=self.settings)


if __name__ == '__main__':
    unittest.main()
