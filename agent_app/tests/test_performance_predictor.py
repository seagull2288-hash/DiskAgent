"""Local GP and read-only provenance checks; no provider or Workbench calls."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from agent_app import performance_predictor as predictor
from agent_app.schemas import TaskValidationError
from agent_app.tests.test_contract import FixtureCase
from python_control import controller, runtime
from python_control.sampling import signature
from python_control.search import DEFAULT_CONFIG


class PredictorTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.cfg = deepcopy(DEFAULT_CONFIG)
        self.cfg['strategy'] = 'center_surrogate'
        for item in (
            patch.object(runtime.bridge.Client, 'generate', side_effect=AssertionError('no provider')),
            patch.object(controller, 'run', side_effect=AssertionError('no optimizer')),
            patch.object(controller, 'evaluate', side_effect=AssertionError('no Workbench')),
        ):
            item.start()
            self.addCleanup(item.stop)

    def history(self, count=25, *, constant=False):
        rows = []
        for i in range(count):
            parameters = {v['name']: v['reference'] for v in self.problem['variables']}
            parameters['X1'] = round(15 + 10 * i / max(1, count - 1), 4)
            rows.append({'parameters': parameters,
                'stress_mpa': 700.0 if constant else 700.0 + (parameters['X1'] - 20) ** 2,
                'displacement_mm': .2, 'area_m2': .01})
        return rows

    def candidate(self):
        values = [v['reference'] for v in self.problem['variables']]
        values[0] = 15.1234
        return values

    def predict(self, history=None, **kwargs):
        return predictor.predict_snapshot(self.history() if history is None else history,
            kwargs.pop('problem', self.problem), kwargs.pop('cfg', self.cfg),
            kwargs.pop('candidates', [self.candidate()]), **kwargs)

    def run_snapshot(self, done=2, *, baseline=True):
        run, state = self.save_run(done=done, target=max(4, done), baseline=baseline)
        details = ([state['baseline']] if baseline else []) + [r['detail'] for r in state['records']]
        for i, detail in enumerate(details):
            detail['xActual'][0] = round(self.problem['variables'][0]['reference'] + .01 * i, 4)
        for i, record in enumerate(state['records'], 1):
            record['iteration'] = i
            record['proposal'] = {'x': list(record['detail']['xActual'])}
        state['history'] = [controller.observation(d) for d in details]
        state.update(definition=self.settings.definition(), fingerprint={'test': 'same'})
        runtime.write_json(run / 'checkpoint.json', state)
        runtime.write_json(run / 'search_config.json', self.cfg)
        return run, state

    def inspect(self, run, *, include_records=False):
        with patch.object(runtime, 'fingerprint', return_value={'test': 'same'}), \
                patch.object(controller, 'verify_native', return_value={}):
            return predictor.inspect_run(run.name, settings=self.settings, include_records=include_records)

    def test_real_gp_ready_uses_original_validation_and_returns_local_prediction(self):
        history = self.history()
        original = deepcopy(history)
        result = self.predict(history)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['schema_version'], 'disk-agent.performance.v1')
        self.assertEqual(result['role'], 'shadow_only')
        self.assertEqual(result['measured_samples'], 25)
        self.assertEqual(result['unique_samples'], 25)
        self.assertEqual(result['minimum_samples'], 24)
        self.assertEqual(result['validation']['fit_samples'], 20)
        self.assertEqual(result['validation']['forward_holdout_samples'], 5)
        self.assertEqual(result['data_signature'], signature(history))
        self.assertEqual(history, original)
        point = result['predictions'][0]
        self.assertEqual(point['x'], self.candidate())
        self.assertGreater(point['predicted_stress_mpa'], 0)
        self.assertGreaterEqual(point['predictive_std_mpa'], 0)
        self.assertGreaterEqual(point['distance_to_measurements'], 0)
        self.assertIs(type(point['within_training_neighborhood']), bool)
        self.assertIn('不是95%', ''.join(result['limitations']))
        self.assertEqual(len(result['model_code_signature']), 64)

    def test_insufficient_data_no_baseline_and_no_candidate_fit_do_not_invent_numbers(self):
        with patch.object(predictor.surrogate, 'fit_validated', side_effect=AssertionError('must not fit')):
            for count, status in ((0, 'no_baseline'), (1, 'insufficient_data'), (23, 'insufficient_data')):
                result = self.predict(self.history(count))
                self.assertEqual(result['status'], status)
                self.assertEqual(result['predictions'], [])
        ready = self.predict(candidates=None)
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(ready['predictions'], [])

    def test_uninformative_or_validation_failed_model_has_no_predictions(self):
        constant = self.predict(self.history(constant=True))
        self.assertEqual(constant['status'], 'validation_failed')
        self.assertEqual(constant['reason'], 'nonfinite_or_uninformative_objectives')
        self.assertEqual(constant['predictions'], [])
        cfg = deepcopy(self.cfg)
        cfg['surrogate_max_rmse_mpa'] = .00000001
        failed = self.predict(cfg=cfg)
        self.assertEqual(failed['status'], 'validation_failed')
        self.assertEqual(failed['reason'], 'validation_error_above_limit')
        self.assertEqual(failed['predictions'], [])

    def test_model_exception_and_nonfinite_predictions_are_unavailable_without_numeric_predictions(self):
        with patch.object(predictor.surrogate, 'fit_validated', side_effect=RuntimeError('model failed')):
            result = self.predict()
        self.assertEqual(result['status'], 'validation_failed')
        self.assertEqual(result['predictions'], [])
        class Broken:
            def predict(self, _):
                return np.array([float('nan')]), np.array([1.0])
        with patch.object(predictor.surrogate, 'fit_validated', return_value=(Broken(), {'enabled': True})):
            result = self.predict()
        self.assertEqual(result['status'], 'validation_failed')
        self.assertEqual(result['predictions'], [])
        self.assertNotIn('NaN', json.dumps(result, allow_nan=False))

    def test_canonical_order_mapping_unit_objective_and_precision_are_required(self):
        mutations = [lambda p: p.update(schema_version='other'),
            lambda p: p.update(input_order=list(reversed(p['input_order']))),
            lambda p: p.update(workbench_parameter_ids=list(reversed(p['workbench_parameter_ids']))),
            lambda p: p.update(decimals=True), lambda p: p.update(decimals=3),
            lambda p: p.update(objective={'name': 'mass'}),
            lambda p: p['variables'][0].update(source_cad_unit='m'),
            lambda p: p['variables'][0].update(workbench_id='P1'),
            lambda p: p['variables'][0].update(lower=True),
            lambda p: p['variables'][0].update(lower=30)]
        for mutate in mutations:
            problem = deepcopy(self.problem)
            mutate(problem)
            with self.subTest(mutate=mutate), self.assertRaises((TaskValidationError, runtime.bridge.BridgeError)):
                self.predict(problem=problem)
        candidate = self.candidate()
        candidate[0] = 15.12345
        with self.assertRaises(TaskValidationError):
            self.predict(candidates=[candidate])

    def test_bad_history_types_out_of_bounds_nonfinite_and_prediction_contamination_rejected(self):
        mutations = [lambda h: h[0].update(stress_mpa=True),
            lambda h: h[0].update(stress_mpa=float('nan')),
            lambda h: h[0].update(displacement_mm=-1),
            lambda h: h[0].update(area_m2=0),
            lambda h: h[0].update(predicted_stress_mpa=100),
            lambda h: h[0]['parameters'].update(X1='15'),
            lambda h: h[0]['parameters'].update(X1=14),
            lambda h: h[0]['parameters'].update(X1=15.00001),
            lambda h: h[0]['parameters'].update(X9=1)]
        for mutate in mutations:
            history = self.history()
            mutate(history)
            with self.subTest(mutate=mutate), self.assertRaises((TaskValidationError, runtime.bridge.BridgeError)):
                self.predict(history)

    def test_duplicates_in_history_or_candidate_cannot_count_as_new_measured_data(self):
        history = self.history()
        history[-1] = deepcopy(history[0])
        with self.assertRaises(TaskValidationError) as exc:
            self.predict(history)
        self.assertEqual(exc.exception.code, 'PREDICTION_DUPLICATE')
        for candidates in ([self.candidate(), self.candidate()],
            [[row['parameters'][name] for name in predictor._NAMES] for row in self.history()[:1]]):
            with self.subTest(candidates=candidates), self.assertRaises(TaskValidationError):
                self.predict(candidates=candidates)

    def test_config_never_lowers_minimum_and_rejects_bool_and_invalid_folds(self):
        for change in ({'surrogate_min_samples': 12}, {'surrogate_min_samples': True},
            {'surrogate_max_samples': 23}, {'surrogate_folds': 24}, {'seed': True},
            {'surrogate_length_scales': [0]}, {'surrogate_max_rmse_mpa': float('inf')}):
            cfg = deepcopy(self.cfg)
            cfg.update(change)
            with self.subTest(change=change), self.assertRaises(TaskValidationError):
                self.predict(cfg=cfg)

    def test_model_provenance_changes_with_data_problem_and_config(self):
        baseline = self.predict()
        history = self.history()
        history[1]['stress_mpa'] += .01
        self.assertNotEqual(baseline['data_signature'], self.predict(history)['data_signature'])
        cfg = deepcopy(self.cfg)
        cfg['seed'] += 1
        changed = self.predict(cfg=cfg)
        self.assertNotEqual(baseline['config_signature'], changed['config_signature'])
        self.assertEqual(baseline['model_code_signature'], changed['model_code_signature'])

    def test_inspect_is_read_only_reverifies_native_and_saved_config_wins(self):
        run, state = self.run_snapshot()
        before = {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*') if p.is_file()}
        with patch.object(runtime, 'fingerprint', return_value={'test': 'same'}), \
                patch.object(controller, 'verify_native', return_value={}) as verify, \
                patch.object(predictor, 'load_search_config', side_effect=AssertionError('saved config wins')):
            result = predictor.inspect_run(run.name, settings=self.settings, include_records=False)
        self.assertEqual(result['status'], 'insufficient_data', result)
        self.assertEqual(result['source_completed_candidates'], 2)
        self.assertEqual(result['measured_samples'], 3)
        self.assertEqual(result['source_history_signature'], signature(state['history']))
        self.assertEqual(verify.call_count, 3)
        self.assertTrue(result['evidence']['native_records_reverified'])
        after = {str(p.relative_to(run)): p.read_bytes() for p in run.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_inspect_historical_no_prerecord_keeps_predictions_and_errors_null(self):
        run, _ = self.run_snapshot()
        result = self.inspect(run, include_records=True)
        self.assertEqual(result['status'], 'insufficient_data', result)
        self.assertEqual(result['record_summary']['pre_evaluation_prediction_records'], 0)
        self.assertEqual(result['record_summary']['missing_pre_prediction_count'], 2)
        self.assertEqual(len(result['records']), 2)
        for row in result['records']:
            self.assertIsNone(row['predicted_stress_mpa'])
            self.assertIsNone(row['error_mpa'])
        self.assertFalse((run / 'prediction_audit').exists())

    def test_inspect_corruption_native_cross_run_and_incompatible_definition_have_no_predictions(self):
        changes = [lambda s: s.update(definition={}), lambda s: s.update(fingerprint={}),
            lambda s: s['records'][0]['detail'].update(nativeReportFile=str(self.output / 'run_other' / 'native.json')),
            lambda s: s['history'][0].update(stress_mpa=701)]
        for i, mutate in enumerate(changes):
            run, state = self.run_snapshot()
            mutate(state)
            runtime.write_json(run / 'checkpoint.json', state)
            result = self.inspect(run)
            with self.subTest(i=i):
                self.assertEqual(result['status'], 'unavailable')
                self.assertEqual(result['predictions'], [])
        run, _ = self.run_snapshot()
        with patch.object(runtime, 'fingerprint', return_value={'test': 'same'}), \
                patch.object(controller, 'verify_native', side_effect=RuntimeError('bad native report')):
            result = predictor.inspect_run(run.name, settings=self.settings, include_records=False)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['predictions'], [])

    def test_inspect_detects_checkpoint_change_and_rejects_non_bool_flags(self):
        run, state = self.run_snapshot()
        def mutate(*args):
            state['status'] = 'changed'
            runtime.write_json(run / 'checkpoint.json', state)
            return {}
        with patch.object(runtime, 'fingerprint', return_value={'test': 'same'}), \
                patch.object(controller, 'verify_native', side_effect=mutate):
            result = predictor.inspect_run(run.name, settings=self.settings, include_records=False)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'PREDICTION_BUSY')
        self.assertEqual(result['predictions'], [])
        for flag in (1, None, 'yes'):
            with self.subTest(flag=flag), self.assertRaises(TaskValidationError):
                predictor.inspect_run(run.name, settings=self.settings, include_records=flag)
        with self.assertRaises(TaskValidationError):
            predictor.inspect_run('../run_other', settings=self.settings)

    def test_inspect_rejects_redirected_run_input_files_before_any_native_read(self):
        run, _ = self.run_snapshot()
        original_resolve = Path.resolve
        for filename in ('checkpoint.json', 'problem.json', 'search_config.json'):
            target = run / filename
            def redirected(path, *args, **kwargs):
                if path == target:
                    return self.root / 'outside' / filename
                return original_resolve(path, *args, **kwargs)
            with self.subTest(filename=filename), \
                    patch.object(Path, 'resolve', new=redirected), \
                    patch.object(controller, 'verify_native', side_effect=AssertionError('must reject before native read')) as native:
                result = predictor.inspect_run(run.name, settings=self.settings, include_records=False)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['reason'], 'PREDICTION_PATH')
            self.assertEqual(result['predictions'], [])
            native.assert_not_called()

    def test_inspect_rejects_proposal_geometry_different_from_committed_actual(self):
        run, state = self.run_snapshot()
        state['records'][0]['proposal']['x'][0] += .1
        runtime.write_json(run / 'checkpoint.json', state)
        result = self.inspect(run, include_records=True)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['reason'], 'PREDICTION_EVIDENCE')
        self.assertEqual(result['records'], [])

