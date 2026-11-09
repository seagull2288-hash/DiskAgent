"""Actual proposal parsing/correction with local final-answer fixtures only."""
import json
from pathlib import Path

from agent_app import direct_proposals
from agent_app.tests.test_contract import FixtureCase
from python_control import controller, runtime


class SequenceClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.http_attempts = 0
        self.prompts = []
        self.schemas = []

    def generate(self, prompt, folder, schema):
        self.http_attempts += 1
        self.prompts.append(json.loads(prompt['user']))
        self.schemas.append(schema)
        return self.answers.pop(0), {'http_attempts_in_call': 1, 'usage': {'total_tokens': 5}}


class DirectBoundsTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.parameters = {v['name']: v['reference'] for v in self.problem['variables']}
        self.history = [{'parameters': self.parameters, 'stress_mpa': 740., 'displacement_mm': .46, 'area_m2': .006}]
        self.cfg = dict(runtime.bridge.DEFAULT_CONFIG, max_proposal_attempts=2, api_key_env='',
                        history_compatibility_confirmed=True)

    def answer(self, **changes):
        return json.dumps({'parameters': dict(self.parameters, **changes), 'rationale': 'Fixture proposal.',
                           'candidate_type': 'exploit'})

    def call(self, answers):
        client = SequenceClient(answers)
        folder = self.root / 'request'
        result = direct_proposals.propose(self.cfg, self.problem, self.history, folder, live=True, client=client)
        return result, client, folder

    def test_last_small_overrun_is_repaired_validated_and_audited(self):
        result, client, folder = self.call([self.answer(Y1=39.8), self.answer(Y1=39.6)])
        self.assertEqual(client.http_attempts, 2)
        self.assertEqual(result['parameters_requested']['Y1'], 39.6)
        self.assertEqual(result['parameters']['Y1'], 40.)
        self.assertEqual(result['source'], 'llm')
        self.assertEqual(result['proposal_attempt'], 2)
        controller.validate_proposal(result, self.problem, self.history)
        audit = runtime.read_json(folder / 'proposal_02' / 'repair.json')
        self.assertEqual(audit['changes'][0]['requested'], 39.6)
        self.assertEqual(audit['changes'][0]['applied'], 40.)
        self.assertEqual(runtime.read_json(folder / 'manifest.json')['status'], 'candidate_ready')
        self.assertEqual(runtime.read_json(folder / 'candidate.json'), result)
        self.assertEqual(client.schemas[-1]['properties']['parameters']['properties']['Y1']['minimum'], 40.)
        self.assertEqual(client.prompts[-1]['correction_requested']['code'], 'BOUNDS')

    def test_valid_model_correction_keeps_its_dimensions(self):
        result, client, _ = self.call([self.answer(Y1=39.8), self.answer(Y1=40.1)])
        self.assertEqual(client.http_attempts, 2)
        self.assertEqual(result['parameters']['Y1'], 40.1)
        self.assertNotIn('automatic_boundary_repair', result)

    def test_first_valid_answer_needs_one_call(self):
        result, client, _ = self.call([self.answer(Y1=44.)])
        self.assertEqual(client.http_attempts, 1)
        self.assertEqual(result['parameters']['Y1'], 44.)

    def test_duplicate_correction_names_the_matching_measured_design(self):
        result, client, _ = self.call([self.answer(), self.answer(X1=self.parameters['X1'] + .01)])
        self.assertEqual(client.http_attempts, 2)
        feedback = client.prompts[1]['duplicate_enforcement']
        self.assertEqual(feedback['history_rows_checked'], 1)
        self.assertEqual(feedback['rejected_designs'][0]['matching_records'][0]['iteration'], 0)
        self.assertEqual(feedback['rejected_designs'][0]['parameters'], self.parameters)
        self.assertEqual(result['duplicate_guard_policy'], direct_proposals.DUPLICATE_POLICY)
        controller.validate_proposal(result, self.problem, self.history)

    def test_rounding_duplicate_is_explained_before_a_new_llm_proposal(self):
        result, client, _ = self.call([self.answer(X1=self.parameters['X1'] + .00001),
                                      self.answer(X1=self.parameters['X1'] + .01)])
        feedback = client.prompts[1]['duplicate_enforcement']
        self.assertEqual(feedback['numerical_decimals'], 4)
        self.assertEqual(feedback['rejected_designs'][0]['parameters']['X1'], self.parameters['X1'])
        self.assertEqual(result['parameters']['X1'], round(self.parameters['X1'] + .01, 4))

    def test_resumed_request_carries_prior_duplicate_feedback_for_same_iteration(self):
        scope = self.root / 'iter_081' / 'api'
        rejection = scope / 'request_previous' / 'proposal_02' / 'rejection.json'
        runtime.write_json(rejection, {'code': 'DUPLICATE', 'previous_response': self.answer()})
        client = SequenceClient([self.answer(X1=self.parameters['X1'] + .01)])
        result = direct_proposals.propose(self.cfg, self.problem, self.history, scope / 'request_new',
                                           live=True, client=client)
        feedback = client.prompts[0]['duplicate_enforcement']
        self.assertEqual(feedback['rejected_designs'][0]['matching_records'][0]['iteration'], 0)
        self.assertEqual(client.http_attempts, 1)
        self.assertEqual(result['source'], 'llm')

    def test_chinese_response_labels_normalize_without_changing_dimensions(self):
        parameters = dict(self.parameters, Y1=44.)
        text = json.dumps({'参数': parameters, '理由': '保留数值，仅兼容字段名称。', '候选类型': '利用'}, ensure_ascii=False)
        result, client, folder = self.call([text])
        self.assertEqual(client.http_attempts, 1)
        self.assertEqual(result['parameters'], parameters)
        self.assertEqual(result['candidate_type'], 'exploit')
        self.assertFalse(result['automatic_schema_normalization']['parameters_changed'])
        self.assertEqual(runtime.read_json(folder / 'proposal_01' / 'schema.json')['root_field_mapping']['参数'], 'parameters')
        controller.validate_proposal(result, self.problem, self.history)

    def test_chinese_aliases_still_pass_through_original_duplicate_checks(self):
        text = json.dumps({'参数': self.parameters, '理由': '重复样例。', '候选类型': '探索'}, ensure_ascii=False)
        result, client, _ = self.call([text, self.answer(Y1=44.)])
        self.assertEqual(client.http_attempts, 2)
        self.assertEqual(client.prompts[1]['correction_requested']['code'], 'DUPLICATE')
        self.assertNotIn('automatic_schema_normalization', result)
        self.assertEqual(result['parameters']['Y1'], 44.)

    def test_ambiguous_or_unknown_fields_remain_rejected(self):
        ambiguous = {'参数': self.parameters, 'parameters': self.parameters, '理由': '歧义样例。', '候选类型': '利用'}
        unknown = {'参数': self.parameters, '理由': '额外字段样例。', '候选类型': '利用', 'command': 'ignored instruction'}
        for value in (ambiguous, unknown):
            with self.subTest(value=value):
                text, _ = direct_proposals.normalize_schema(json.dumps(value, ensure_ascii=False))
                with self.assertRaises(runtime.bridge.BridgeError) as caught:
                    runtime.bridge.parse_candidate(text, self.problem, self.history)
                self.assertEqual(caught.exception.code, 'CANDIDATE_SCHEMA')

    def test_alias_normalization_does_not_fill_missing_or_coerce_numeric_fields(self):
        for parameters in (dict(self.parameters, X1='20.8'), {'X1': 20.8}):
            with self.subTest(parameters=parameters):
                text, _ = direct_proposals.normalize_schema(json.dumps(
                    {'参数': parameters, '理由': '格式校验样例。', '候选类型': '利用'}, ensure_ascii=False))
                with self.assertRaises(runtime.bridge.BridgeError):
                    runtime.bridge.parse_candidate(text, self.problem, self.history)

    def test_overflowing_json_number_keeps_original_rejection_and_llm_retry(self):
        value = {'参数': dict(self.parameters, X1=1e99), '理由': '数值校验样例。', '候选类型': '利用'}
        text = json.dumps(value, ensure_ascii=False).replace('1e+99', '1e999')
        normalized, audit = direct_proposals.normalize_schema(text)
        self.assertEqual(normalized, text)
        self.assertIsNone(audit)
        result, client, folder = self.call([text, self.answer(Y1=44.)])
        self.assertEqual(client.http_attempts, 2)
        self.assertEqual(runtime.read_json(folder / 'proposal_01' / 'rejection.json')['code'], 'CANDIDATE_SCHEMA')
        self.assertEqual(result['parameters']['Y1'], 44.)

    def test_projection_duplicate_is_rejected_and_never_becomes_a_candidate(self):
        self.history[0]['parameters'] = dict(self.parameters, Y1=40.)
        with self.assertRaises(runtime.bridge.BridgeError) as caught:
            self.call([self.answer(Y1=39.8), self.answer(Y1=39.6)])
        self.assertEqual(caught.exception.code, 'BOUNDS')
        request = self.root / 'request'
        self.assertFalse((request / 'candidate.json').exists())
        self.assertEqual(runtime.read_json(request / 'proposal_02' / 'repair_skip.json')['code'], 'DUPLICATE')

    def test_large_overrun_does_not_get_projected(self):
        with self.assertRaises(runtime.bridge.BridgeError) as caught:
            self.call([self.answer(Y1=39.), self.answer(Y1=38.)])
        self.assertEqual(caught.exception.code, 'BOUNDS')
        self.assertFalse((self.root / 'request' / 'candidate.json').exists())

    def test_invalid_json_gets_original_llm_correction_before_bounds_repair(self):
        result, client, folder = self.call(['{invalid', self.answer(Y1=39.6)])
        self.assertEqual(client.prompts[-1]['correction_requested']['code'], 'INVALID_JSON')
        self.assertEqual(result['parameters']['Y1'], 40.)
        self.assertEqual(runtime.read_json(folder / 'proposal_01' / 'rejection.json')['code'], 'INVALID_JSON')

    def test_actual_failed_answer_can_be_repaired_without_changing_other_dimensions(self):
        parameters = dict(self.parameters, X1=15., X2=44., X3=48., X4=18., X5=2.,
                          Y1=39.6, Y2=5.6, Y3=5.05, Y4=15.)
        text = json.dumps({'parameters': parameters, 'rationale': 'Recorded-shape fixture.', 'candidate_type': 'exploit'})
        corrected, audit = direct_proposals.repair_bounds(text, self.problem, self.history)
        accepted = runtime.bridge.parse_candidate(corrected, self.problem, self.history)
        self.assertEqual(accepted['parameters'], dict(parameters, Y1=40.))
        self.assertEqual([change['name'] for change in audit['changes']], ['Y1'])
