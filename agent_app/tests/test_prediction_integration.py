"""Before-FEA shadow predictions over the real controller and fake boundaries.

No test contacts a provider or launches Workbench. Predictions are audit data;
the existing sampler, budgets, committed measurements and cached plans stay
authoritative.
"""
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app import prediction_audit, supervisor, tools
from agent_app.schemas import TaskValidationError
from agent_app.tests import test_feedback_integration as feedback_fixture
from python_control import runtime


class PredictionIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing real-controller/fake-HTTP/native fixture without
        # inheriting and rerunning its unrelated tests.
        feedback_fixture.FeedbackIntegrationTests.setUp(self)
        self.pre_records_seen = []

    fake_proposal = feedback_fixture.FeedbackIntegrationTests.fake_proposal
    transport = feedback_fixture.FeedbackIntegrationTests.transport
    start = feedback_fixture.FeedbackIntegrationTests.start
    state = feedback_fixture.FeedbackIntegrationTests.state
    run_dir = feedback_fixture.FeedbackIntegrationTests.run_dir
    proposal_dirs = feedback_fixture.FeedbackIntegrationTests.proposal_dirs
    digest = staticmethod(feedback_fixture.FeedbackIntegrationTests.digest)

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        if baseline is not None:
            run = Path(output_root).resolve().parent
            checkpoint = runtime.read_json(run / 'checkpoint.json')
            k = checkpoint['nCompleted'] + 1
            path = run / 'prediction_audit' / ('iter_%03d.json' % k)
            self.assertTrue(path.is_file(), 'Candidate FEA started before its shadow pre-record existed.')
            record = runtime.read_json(path)
            self.assertEqual(record['run_id'], run.name)
            self.assertEqual(record['iteration'], k)
            self.assertEqual(record['candidate_x'], list(x))
            self.assertEqual(record['basis_completed_candidates'], k - 1)
            self.assertEqual(record['basis_history_length'], k)
            self.pre_records_seen.append(record)
        return feedback_fixture.FeedbackIntegrationTests.fake_evaluate(
            self, x, settings, problem, output_root, baseline)

    def audit(self, result):
        run = self.run_dir(result)
        state = runtime.read_json(run / 'checkpoint.json')
        problem = runtime.bridge.load_problem(run / 'problem.json')
        return prediction_audit.read_prediction_records(run, state, problem)

    def pre_file(self, result, k=1):
        return self.run_dir(result) / 'prediction_audit' / ('iter_%03d.json' % k)

    def failed_first_candidate(self):
        self.fail_at = self.evaluations + 1
        result = self.start(total=1)
        self.assertFalse(result.ok, result.to_dict())
        self.assertTrue(self.pre_file(result).is_file())
        self.fail_at = None
        return result

    def use_smooth_direct_fixture(self):
        # Historical audit tests use deterministic fixture dimensions under the
        # legacy strategy. Production direct mode now skips prediction entirely.
        path = self.root / 'python_control/search_config.json'
        config = runtime.read_json(path)
        config['strategy'] = 'center_surrogate'
        config['selection_policy'] = 'legacy_v1'
        runtime.write_json(path, config)
        owner = self

        class SmoothLegacyProposer:
            def __init__(self, cfg):
                self.config = cfg

            def __call__(self, *args, **kwargs):
                return owner.fake_proposal(*args, **kwargs)

        disabled_feedback = supervisor.default_feedback_policy(enabled=False)
        for item in (patch('python_control.search.SearchProposer', SmoothLegacyProposer),
                     patch.object(supervisor, 'default_feedback_policy', return_value=disabled_feedback)):
            item.start()
            self.addCleanup(item.stop)

    def test_pre_records_are_durable_before_fea_and_insufficient_data_has_no_predictions(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 7))
        self.assertEqual([r['iteration'] for r in self.pre_records_seen], [1, 2, 3, 4, 5, 6])
        for record in self.pre_records_seen:
            self.assertEqual(record['schema_version'], 'disk-agent.prediction-record.v1')
            self.assertEqual(record['prediction']['status'], 'insufficient_data')
            self.assertEqual(record['prediction']['predictions'], [])
        audit = self.audit(result)
        self.assertEqual(audit['summary']['pre_evaluation_prediction_records'], 6)
        self.assertEqual(audit['summary']['numeric_prediction_records'], 0)
        self.assertEqual(audit['summary']['compared_records'], 0)
        self.assertEqual(audit['summary']['missing_pre_prediction_count'], 0)
        for row in audit['records']:
            self.assertEqual(row['status'], 'not_ready')
            self.assertIsNone(row['predicted_stress_mpa'])
            self.assertIsNone(row['predictive_std_mpa'])
            self.assertIsNone(row['error_mpa'])
            self.assertIsNotNone(row['actual_stress_mpa'])

    def test_evaluator_failure_resume_reuses_pre_record_without_retraining(self):
        first = self.failed_first_candidate()
        before = self.pre_file(first).read_bytes()
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        with patch.object(prediction_audit, 'predict_snapshot',
                          side_effect=AssertionError('Existing before-solve prediction must not be regenerated.')):
            resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                                 live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 3))
        self.assertEqual(self.pre_file(resumed).read_bytes(), before)
        self.assertEqual(self.audit(resumed)['summary']['pre_evaluation_prediction_records'], 1)

    def test_pre_record_write_failure_blocks_candidate_fea_and_recovers(self):
        original_write = runtime.write_json
        injected = []

        def fail_prediction_write(path, value):
            if (isinstance(value, dict) and
                    value.get('schema_version') == 'disk-agent.prediction-record.v1' and not injected):
                injected.append(str(path))
                raise OSError('FIXTURE prediction pre-record was not persisted.')
            return original_write(path, value)

        with patch.object(runtime, 'write_json', side_effect=fail_prediction_write):
            first = self.start(total=1)
        self.assertTrue(injected)
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 1))
        self.assertFalse(self.pre_file(first).exists())
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))

    def test_get_of_historical_missing_pre_record_never_backfills_predictions(self):
        result = self.start(total=2)
        self.assertTrue(result.ok, result.to_dict())
        self.pre_file(result, 1).unlink()
        before = len(self.requests), self.evaluations
        with patch.object(prediction_audit, 'predict_snapshot',
                          side_effect=AssertionError('Read-only history must not train or create past predictions.')):
            first = self.audit(result)
            second = self.audit(result)
        self.assertEqual(first, second)
        self.assertEqual(first['summary']['missing_pre_prediction_count'], 1)
        self.assertEqual(first['records'][0]['status'], 'missing_pre_prediction')
        self.assertIsNone(first['records'][0]['predicted_stress_mpa'])
        self.assertFalse(self.pre_file(result, 1).exists())
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_already_evaluated_pending_detail_does_not_backfill_missing_pre_record(self):
        first = tools.execute_task({'operation': 'optimize', 'total_candidate_budget': 1},
                                   live=True, settings=self.settings)
        self.assertTrue(first.ok, first.to_dict())
        run = Path(first.data['run_dir'])
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        record = checkpoint['records'].pop()
        checkpoint['history'].pop()
        checkpoint.update(nCompleted=0, status='interrupted', pending={
            'iteration': 1, 'directory': record['roundDirectory'],
            'phase': 'evaluated', 'proposal': record['proposal'],
            'api_output': record['apiRun'], 'detail': record['detail']})
        runtime.write_json(run / 'checkpoint.json', checkpoint)
        pre = run / 'prediction_audit/iter_001.json'
        pre.unlink()
        before = len(self.requests), self.evaluations
        with patch.object(prediction_audit, 'predict_snapshot',
                          side_effect=AssertionError('A completed pending solve cannot receive a retrospective forecast.')):
            resumed = tools.execute_task({
                'operation': 'optimize', 'run_id': run.name,
                'additional_candidate_budget': 1}, live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)
        self.assertFalse(pre.exists())
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        audit = prediction_audit.read_prediction_records(
            run, checkpoint, runtime.bridge.load_problem(run / 'problem.json'))
        self.assertEqual(audit['summary']['missing_pre_prediction_count'], 1)
        self.assertEqual(audit['records'][0]['status'], 'missing_pre_prediction')

    def test_shadow_audit_preserves_one_plus_three_cache_budget_and_existing_record_bytes(self):
        self.stop_after = 1
        first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        plan = self.run_dir(first) / 'search/generation_001/plan.json'
        plan_hash = self.digest(plan)
        pre_hash = self.digest(self.pre_file(first))
        self.stop_after = None
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.digest(plan), plan_hash)
        self.assertEqual(self.digest(self.pre_file(resumed)), pre_hash)
        self.assertEqual(self.state(resumed).target_total_candidates, 4)
        checkpoint = runtime.read_json(self.run_dir(resumed) / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 4)
        for k, record in enumerate(checkpoint['records'], 1):
            self.assertEqual(runtime.read_json(self.pre_file(resumed, k))['candidate_x'],
                             record['proposal']['x'])

    def test_modified_pre_record_fields_are_rejected_before_resumed_fea(self):
        changes = {
            'run_id': 'run_another', 'candidate_x': None,
            'basis_history_signature': '0' * 64,
            'proposal_signature': '0' * 64,
            'problem_signature': '0' * 64,
            'config_signature': '0' * 64,
        }
        for field, replacement in changes.items():
            with self.subTest(field=field):
                first = self.failed_first_candidate()
                path = self.pre_file(first)
                value = runtime.read_json(path)
                if field == 'candidate_x':
                    replacement = list(value[field])
                    replacement[0] = round(replacement[0] + 0.0001, 4)
                value[field] = replacement
                runtime.write_json(path, value)
                before = len(self.requests), self.evaluations
                resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                                     live=True, settings=self.settings)
                self.assertFalse(resumed.ok, resumed.to_dict())
                self.assertEqual((len(self.requests), self.evaluations), before)

    def test_valid_signed_record_from_another_run_is_rejected_before_fea(self):
        first = self.failed_first_candidate()
        second = self.failed_first_candidate()
        self.pre_file(second).write_bytes(self.pre_file(first).read_bytes())
        before = len(self.requests), self.evaluations
        resumed = supervisor.resume_workflow(self.state(second).workflow_id,
                                             live=True, settings=self.settings)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_signed_but_wrong_nested_model_data_signature_is_rejected(self):
        first = self.failed_first_candidate()
        path = self.pre_file(first)
        value = runtime.read_json(path)
        value['prediction']['data_signature'] = '0' * 64
        value['record_signature'] = prediction_audit._digest({
            key: item for key, item in value.items() if key != 'record_signature'})
        runtime.write_json(path, value)
        before = len(self.requests), self.evaluations
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_real_ready_shadow_predictions_are_compared_only_after_controller_commit(self):
        self.use_smooth_direct_fixture()
        result = self.start(total=25)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (0, 26))
        self.assertEqual(self.proposals, 25)
        audit = self.audit(result)
        last = audit['records'][-1]
        self.assertEqual(last['iteration'], 25)
        self.assertEqual(last['prediction_status'], 'ready')
        self.assertEqual(last['status'], 'compared')
        self.assertEqual(last['actual_stress_mpa'], 750.0)
        self.assertAlmostEqual(last['error_mpa'], 750.0 - last['predicted_stress_mpa'])
        self.assertAlmostEqual(last['absolute_error_mpa'], abs(last['error_mpa']))
        self.assertAlmostEqual(last['relative_error_percent'], abs(last['error_mpa']) / 750.0 * 100)
        errors = [row['error_mpa'] for row in audit['records'] if row['status'] == 'compared']
        self.assertTrue(errors)
        self.assertEqual(audit['summary']['compared_records'], len(errors))
        self.assertAlmostEqual(audit['summary']['mean_absolute_error_mpa'],
                               sum(abs(error) for error in errors) / len(errors))
        self.assertAlmostEqual(audit['summary']['root_mean_squared_error_mpa'],
                               math.sqrt(sum(error * error for error in errors) / len(errors)))
        self.assertAlmostEqual(audit['summary']['max_absolute_error_mpa'], max(map(abs, errors)))
        pre = runtime.read_json(self.pre_file(result, 25))
        self.assertEqual(pre['basis_completed_candidates'], 24)
        self.assertEqual(pre['prediction']['measured_samples'], 25)
        self.assertFalse(pre['prediction']['validation']['predictions_are_measurements'])

    def test_uncommitted_pending_detail_never_becomes_actual_prediction_error(self):
        self.use_smooth_direct_fixture()
        self.fail_at = 25
        first = self.start(total=25)
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (0, 26))
        run = self.run_dir(first)
        checkpoint = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 24)
        pending_audit = self.audit(first)['records'][-1]
        self.assertEqual(pending_audit['prediction_status'], 'ready')
        self.assertEqual(pending_audit['status'], 'pending')
        self.assertIsNone(pending_audit['actual_stress_mpa'])
        self.assertIsNone(pending_audit['error_mpa'])
        # This marker is deliberately not a committed observation. The reader
        # must never turn a pending detail field into prediction accuracy data.
        checkpoint['pending']['detail'] = {'stressMPa': 123456.0}
        checkpoint['pending']['phase'] = 'evaluated'
        runtime.write_json(run / 'checkpoint.json', checkpoint)
        after = self.audit(first)
        row = after['records'][-1]
        self.assertEqual(row['status'], 'awaiting_commit')
        self.assertIsNone(row['actual_stress_mpa'])
        self.assertIsNone(row['error_mpa'])
        self.assertEqual(after['summary']['compared_records'],
                         sum(item['status'] == 'compared' for item in after['records']))
        self.assertEqual((len(self.requests), self.evaluations), (0, 26))

    def test_committed_geometry_mismatch_is_rejected_with_and_without_pre_record(self):
        for keep_pre_record in (True, False):
            with self.subTest(keep_pre_record=keep_pre_record):
                result = self.start(total=1)
                self.assertTrue(result.ok, result.to_dict())
                run = self.run_dir(result)
                checkpoint = runtime.read_json(run / 'checkpoint.json')
                # Keep detail/history mutually consistent while making both
                # describe another geometry than the unchanged proposal/pre-
                # record. Hash-only checks on the pre-record cannot catch this.
                changed_x1 = round(checkpoint['records'][0]['detail']['xActual'][0] + 0.0001, 4)
                checkpoint['records'][0]['detail']['xActual'][0] = changed_x1
                checkpoint['history'][1]['parameters']['X1'] = changed_x1
                runtime.write_json(run / 'checkpoint.json', checkpoint)
                if not keep_pre_record:
                    self.pre_file(result).unlink()
                before = len(self.requests), self.evaluations
                checkpoint_bytes = (run / 'checkpoint.json').read_bytes()
                with self.assertRaises(TaskValidationError) as rejected:
                    self.audit(result)
                self.assertEqual(rejected.exception.code, 'PREDICTION_AUDIT')
                self.assertEqual((len(self.requests), self.evaluations), before)
                self.assertEqual((run / 'checkpoint.json').read_bytes(), checkpoint_bytes)


if __name__ == '__main__':
    unittest.main()
