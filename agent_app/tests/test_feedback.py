"""Pure factual feedback contracts, independent of providers and Workbench."""
from copy import deepcopy
import hashlib
import json
from unittest import TestCase
from unittest.mock import patch

from agent_app import feedback
from agent_app.schemas import TaskValidationError


def analysis_data(stresses=(1000.0, 900.0, 950.0, 880.0, 890.0), *, since=0):
    """Minimal explicitly measured analyze_run.data with a nonmonotonic history."""
    history = []
    prior = stresses[0] if stresses else None
    for i, stress in enumerate(stresses):
        point = {'iteration': i, 'stress_mpa': stress, 'displacement_mm': .5 - i * .01,
                 'approximate_area_m2': .01 + i * .0001,
                 'improved_historical_best': stress < prior,
                 'parameters': {'X1': 20.0 + i}}
        point.update(stress_reduction_from_baseline_percent=(stresses[0] - stress) / stresses[0] * 100,
            displacement_change_from_baseline_percent=(point['displacement_mm'] - .5) / .5 * 100,
            approximate_area_change_from_baseline_percent=(point['approximate_area_m2'] - .01) / .01 * 100)
        history.append(point)
        prior = min(prior, stress)
    done = max(0, len(history) - 1)
    best = min(history, key=lambda x: x['stress_mpa']) if history else None
    before = min(history[:since + 1], key=lambda x: x['stress_mpa']) if history else None
    gain = before['stress_mpa'] - best['stress_mpa'] if before else None
    return {'run_id': 'run_fixture', 'run_dir': 'D:\\runs\\run_fixture', 'run_status': 'complete',
        'completed_candidates': done, 'target_total_candidates': max(done, 4),
        'baseline': history[0] if history else None, 'best': best,
        'current': history[-1] if history else None, 'candidates': history[1:],
        'window': {'since_candidate': since, 'completed_candidates_in_window': done - since,
            'candidate_iterations': list(range(since + 1, done + 1)),
            'best_before_iteration': before['iteration'] if before else None,
            'best_before_stress_mpa': before['stress_mpa'] if before else None,
            'best_after_stress_mpa': best['stress_mpa'] if best else None,
            'historical_best_improvement_mpa': gain,
            'historical_best_improvement_percent': gain / before['stress_mpa'] * 100 if before else None,
            'no_new_candidates': since == done},
        'constraint_evaluation': {'implemented_rules': [
            {'rule': name, 'status': 'passed'} for name in sorted(feedback._RULES)],
            'all_implemented_rules_passed': True if history else None,
            'engineering_constraints': [{'name': 'allowable stress', 'status': 'unknown',
                                         'reason': 'not_implemented_or_not_configured'}],
            'engineering_qualification': 'not_assessed', 'allowable_stress_status': 'unknown',
            'mass_status': 'unknown', 'life_status': 'unknown'},
        'evidence': {'basis': 'committed_controller_checkpoint',
            'checkpoint_file': 'D:\\runs\\run_fixture\\checkpoint.json',
            'native_records_reverified': False, 'native_records_checked': 0,
            'native_verification_status': 'not_reverified', 'physical_accuracy_certified': False},
        'feedback': {'objective': {'name': 'maximum_von_mises_stress', 'direction': 'minimize', 'unit': 'MPa'}},
        'costs': {'not_forwarded': 'provider-private'}, 'summary_zh': 'Do not forward free text.'}


class FeedbackTests(TestCase):
    def build(self, source=None, **kwargs):
        values = dict(workflow_id='wf_fixture', generation=1, history_signature='a' * 64,
                      target_total_candidates=12)
        values.update(kwargs)
        return feedback.build_feedback(analysis_data() if source is None else source, **values)

    def resign(self, value):
        raw = {key: item for key, item in value.items() if key != 'feedback_id'}
        value['feedback_id'] = 'fdbk_' + hashlib.sha256(json.dumps(raw, sort_keys=True,
            separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()[:24]
        return value

    def assert_invalid(self, value):
        with self.assertRaises(TaskValidationError):
            feedback.validate_feedback(self.resign(value))

    def test_measurements_window_budget_and_bounded_recent_history(self):
        value = self.build(analysis_data(since=1))
        self.assertEqual(value['source_completed_candidates'], 4)
        self.assertEqual(value['source_window']['candidate_iterations'], [2, 3, 4])
        self.assertEqual(value['source_window']['historical_best_improvement_mpa'], 20)
        self.assertEqual(value['best']['iteration'], 3)
        self.assertEqual(value['best']['stress_reduction_from_baseline_percent'], 12)
        self.assertEqual(value['recent_measured'], [
            {'iteration': 2, 'stress_mpa': 950.0, 'improved_historical_best': False},
            {'iteration': 3, 'stress_mpa': 880.0, 'improved_historical_best': True},
            {'iteration': 4, 'stress_mpa': 890.0, 'improved_historical_best': False}])
        self.assertEqual(value['budget'], {'target_total_candidates': 12,
            'completed_candidates': 4, 'remaining_candidates': 8})
        self.assertIn('围绕实测最优', value['program_advice'])

    def test_deterministic_digest_and_no_mutation_or_free_text(self):
        source = analysis_data()
        before = deepcopy(source)
        value = self.build(source)
        reverse = dict(reversed(list(source.items())))
        self.assertEqual(value, self.build(reverse))
        self.assertEqual(source, before)
        serialized = json.dumps(value)
        for private in ('X1', 'provider-private', 'Do not forward'):
            self.assertNotIn(private, serialized)
        copy = feedback.validate_feedback(value)
        copy['budget']['remaining_candidates'] = 0
        self.assertEqual(value['budget']['remaining_candidates'], 8)

    def test_prompt_keeps_identity_and_measurements_without_absolute_paths(self):
        value = self.build()
        context = feedback.feedback_prompt_context(value)
        self.assertEqual(context['feedback_id'], value['feedback_id'])
        self.assertEqual(context['schema_version'], feedback.FEEDBACK_SCHEMA)
        self.assertNotIn('checkpoint_file', context['evidence'])
        self.assertNotIn('D:', json.dumps(context))
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 6500)
        self.assertFalse(context['evidence']['native_records_reverified'])

    def test_no_improvement_advice_does_not_claim_local_optimum(self):
        value = self.build(analysis_data((1000, 900, 950), since=1))
        self.assertEqual(value['source_window']['historical_best_improvement_mpa'], 0)
        self.assertIn('继续探索', value['program_advice'])
        self.assertNotIn('围绕实测最优', value['program_advice'])
        self.assertIn('不能据此判断局部最优', value['program_advice'])

    def test_empty_window_and_no_committed_records_are_explicit(self):
        value = self.build(analysis_data(since=4))
        self.assertEqual(value['recent_measured'], [])
        self.assertEqual(value['source_window']['historical_best_improvement_mpa'], 0)
        empty = self.build(analysis_data(()))
        self.assertIsNone(empty['best'])
        self.assertIsNone(empty['source_window']['historical_best_improvement_mpa'])
        self.assertIn('先取得并核对基准', empty['program_advice'])

    def test_zero_baseline_displacement_preserves_unknown_relative_change(self):
        data = analysis_data()
        data['baseline']['displacement_mm'] = 0.0
        data['best']['displacement_change_from_baseline_percent'] = None
        self.assertIsNone(self.build(data)['best']['displacement_change_from_baseline_percent'])

    def test_native_reverification_flag_is_strict_and_provenance_matches_run(self):
        data = analysis_data()
        data['evidence'].update(native_records_reverified=True, native_records_checked=5,
                                native_verification_status='passed')
        self.assertTrue(self.build(data)['evidence']['native_records_reverified'])
        for patch_value in (
            {'native_records_reverified': 1}, {'native_records_checked': 4},
            {'native_verification_status': 'not_reverified'},
            {'checkpoint_file': 'D:\\runs\\run_other\\checkpoint.json'},
            {'checkpoint_file': 'run_fixture/checkpoint.json'},
            {'checkpoint_file': 'D:\\runs\\..\\run_fixture\\checkpoint.json'},
            {'physical_accuracy_certified': True}):
            with self.subTest(patch_value=patch_value):
                bad = deepcopy(data)
                bad['evidence'].update(patch_value)
                with self.assertRaises(TaskValidationError):
                    self.build(bad)

    def test_failed_rule_rejected_unknown_never_becomes_pass(self):
        for all_passed, status in ((False, 'passed'), (None, 'failed'), (True, 'unknown')):
            with self.subTest(status=status, all_passed=all_passed):
                data = analysis_data()
                data['constraint_evaluation']['all_implemented_rules_passed'] = all_passed
                data['constraint_evaluation']['implemented_rules'][0]['status'] = status
                with self.assertRaises(TaskValidationError):
                    self.build(data)
        data = analysis_data()
        data['constraint_evaluation']['all_implemented_rules_passed'] = None
        data['constraint_evaluation']['implemented_rules'][0]['status'] = 'unknown'
        value = self.build(data)
        self.assertEqual(value['implemented_rules'][0]['status'], 'unknown')
        self.assertTrue(all(item['status'] == 'unknown' for item in value['unknown_engineering_constraints']))
        self.assertIn('life', [item['name'] for item in value['unknown_engineering_constraints']])

    def test_invalid_scalar_types_bounds_and_identifiers_are_rejected(self):
        for kwargs in ({'workflow_id': '../wf_bad'}, {'workflow_id': 'run_wrong'},
            {'generation': True}, {'generation': 0}, {'generation': 1.0},
            {'history_signature': 'A' * 64}, {'history_signature': 'a' * 63},
            {'target_total_candidates': True}, {'target_total_candidates': 3},
            {'target_total_candidates': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TaskValidationError):
                self.build(**kwargs)
        for name, bad in (('run_id', 'D:/run_bad'), ('completed_candidates', True),
                          ('target_total_candidates', 3), ('schema_version', 'alias')):
            data = analysis_data()
            data[name] = bad
            with self.subTest(name=name), self.assertRaises(TaskValidationError):
                self.build(data)

    def test_nonfinite_bool_and_non_json_values_rejected_even_in_unused_source(self):
        for bad in (float('nan'), float('inf'), float('-inf'), object()):
            data = analysis_data()
            data['ignored'] = bad
            with self.subTest(bad=bad), self.assertRaises(TaskValidationError):
                self.build(data)
        data = analysis_data()
        data['baseline']['stress_mpa'] = True
        with self.assertRaises(TaskValidationError):
            self.build(data)
        cyclic = analysis_data()
        cyclic['cycle'] = cyclic
        with self.assertRaises(TaskValidationError):
            self.build(cyclic)

    def test_analysis_metadata_measurement_and_window_inconsistencies_rejected(self):
        changes = [lambda d: d.update(baseline=None),
            lambda d: d['current'].update(iteration=3),
            lambda d: d['best'].update(stress_reduction_from_baseline_percent=99),
            lambda d: d['candidates'][1].update(improved_historical_best=True),
            lambda d: d['window'].update(candidate_iterations=[True, 2, 3, 4]),
            lambda d: d['window'].update(completed_candidates_in_window=True),
            lambda d: d['window'].update(best_before_iteration=True),
            lambda d: d['window'].update(historical_best_improvement_mpa=999),
            lambda d: d['window'].update(no_new_candidates=0),
            lambda d: d['feedback'].update(objective={'name': 'mass'}),
            lambda d: d['constraint_evaluation']['implemented_rules'][0].update(rule=[]),
            lambda d: d['constraint_evaluation']['engineering_constraints'][0].update(name=[]),
            lambda d: d['candidates'].pop()]
        for change in changes:
            data = analysis_data()
            change(data)
            with self.subTest(change=change), self.assertRaises(TaskValidationError):
                self.build(data)

    def test_integrity_schema_and_field_whitelists(self):
        value = self.build()
        value['budget']['remaining_candidates'] = 100
        with self.assertRaises(TaskValidationError) as error:
            feedback.validate_feedback(value)
        self.assertEqual(error.exception.code, 'FEEDBACK_INTEGRITY')
        for mutate in (
            lambda v: v.update(schema_version='disk-agent.feedback.v1'),
            lambda v: v.update(code='print(1)'),
            lambda v: v['best'].update(parameters={'X1': 1}),
            lambda v: v['evidence'].update(extra='free text'),
            lambda v: v['budget'].update(remaining_candidates=True),
            lambda v: v.update(program_advice='Move X1 to remove the hotspot.'),
            lambda v: v.update(engineering_qualification='passed'),
            lambda v: v.update(causal_attribution='verified'),
            lambda v: v['unknown_engineering_constraints'][0].update(status='passed'),
            lambda v: v['recent_measured'][0].update(improved_historical_best=1)):
            value = self.build()
            mutate(value)
            with self.subTest(mutate=mutate):
                self.assert_invalid(value)

    def test_oversize_feedback_and_context_fail_without_truncating_facts(self):
        value = self.build()
        with patch.object(feedback, 'MAX_FEEDBACK_CHARS', 10):
            with self.assertRaises(TaskValidationError) as error:
                feedback.validate_feedback(value)
        self.assertEqual(error.exception.code, 'FEEDBACK_SIZE')
        with patch.object(feedback, 'MAX_PROMPT_CHARS', 10):
            with self.assertRaises(TaskValidationError) as error:
                feedback.feedback_prompt_context(value)
        self.assertEqual(error.exception.code, 'FEEDBACK_SIZE')
        self.assertEqual(value, self.build())

    def test_recomputed_id_does_not_bypass_semantic_checks(self):
        for change in (
            lambda v: v['source_window'].update(historical_best_improvement_percent=90),
            lambda v: v['source_window'].update(candidate_iterations=[1, 2]),
            lambda v: v['recent_measured'][-1].update(stress_mpa=100),
            lambda v: v['best'].update(stress_reduction_from_baseline_percent=80),
            lambda v: v['evidence'].update(native_records_reverified=1)):
            value = self.build()
            change(value)
            with self.subTest(change=change):
                self.assert_invalid(value)

    def test_context_changes_never_change_full_feedback(self):
        value = self.build()
        context = feedback.feedback_prompt_context(value)
        context['recent_measured'].clear()
        self.assertEqual(len(value['recent_measured']), 3)

    def test_huge_count_with_short_list_is_rejected_without_materializing_range(self):
        value = self.build()
        value['source_completed_candidates'] = 10**12
        value['current']['iteration'] = 10**12
        value['budget'].update(target_total_candidates=10**12,
            completed_candidates=10**12, remaining_candidates=0)
        value['source_window']['completed_candidates_in_window'] = 10**12
        self.assert_invalid(value)

    def test_bool_best_before_iteration_rejected_even_when_equal_to_one(self):
        data = analysis_data(since=1)
        data['window']['best_before_iteration'] = True
        with self.assertRaises(TaskValidationError):
            self.build(data)

