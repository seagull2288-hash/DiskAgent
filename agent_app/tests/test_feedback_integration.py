"""Measured batch feedback through the real controller, sampler and HTTP client.

Only the HTTP transport, Workbench evaluator and native-result authentication are
fixtures. No provider endpoint or installed solver can be used by these tests.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app import supervisor, tools
from agent_app.state import WorkflowStore
from agent_app.tests import test_engine_integration as engine_fixture
from python_control import runtime, search


class FeedbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        engine_fixture.ControllerIntegrationTests.setUp(self)
        self.stop_after = None
        self.requests = []
        self.responses = []
        self.response_text = None
        self.response_sequence = []
        self.require_decision_before_candidate = None
        config_path = self.root / 'python_control' / 'search_config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        config.update(strategy='center_surrogate', initial_lhs=0, batch_size=4)
        config_path.write_text(json.dumps(config), encoding='utf-8')
        tools.entry.check_environment.return_value = {
            'python_version': 'fixture', 'api_key_configured': False,
            'api_requests_made': 0, 'finite_element_solves_made': 0,
            'search_strategy': config['strategy'], 'initial_lhs': 0,
            'batch_size': 4, 'pool_size': config['pool_size'],
            'surrogate_min_samples': config['surrogate_min_samples'],
        }
        cfg = dict(runtime.bridge.DEFAULT_CONFIG, provider='openai_responses',
                   base_url='http://127.0.0.1:9/v1', model='fixture-feedback-model',
                   api_key_env='', max_http_attempts=1, max_proposal_attempts=2,
                   max_output_tokens=512, timeout_seconds=1,
                   retry_delay_seconds=0, max_retry_delay_seconds=0,
                   json_mode=True, temperature=0)
        runtime.bridge.load_config.return_value = cfg
        actual_client = runtime.bridge.Client

        def client_factory(api, *, live):
            self.assertIs(live, True)
            return actual_client(api, live=live, transport=self.transport,
                                 sleep=lambda _: self.fail('No HTTP retries expected.'))

        for item in (
            patch.object(runtime, 'fingerprint',
                         return_value={'fixture': 'stable-test-only'}),
            patch.object(runtime.bridge, 'Client', side_effect=client_factory),
        ):
            item.start()
            self.addCleanup(item.stop)

    fake_proposal = engine_fixture.ControllerIntegrationTests.fake_proposal

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        if baseline is not None and self.require_decision_before_candidate is not None:
            state = WorkflowStore(self.settings).load(self.require_decision_before_candidate)
            self.assertTrue(state.decision_trace,
                            'Candidate FEA started before its decision trace was persisted.')
        index = self.evaluations
        detail = engine_fixture.ControllerIntegrationTests.fake_evaluate(
            self, x, settings, problem, output_root, baseline)
        if self.stop_after == index:
            detail['stopRequested'] = True
        return detail

    def transport(self, request, *, timeout):
        # A transport replacement is used instead of replacing Client.generate:
        # assertions below inspect the JSON that would actually cross the wire.
        self.assertEqual(request.full_url, 'http://127.0.0.1:9/v1/responses')
        body = json.loads(request.data.decode('utf-8'))
        self.requests.append(body)
        context = json.loads(body['input'])
        text = (self.response_sequence.pop(0) if self.response_sequence
                else self.response_text)
        if text is None:
            text = json.dumps({
                'center': {v['name']: v['reference'] for v in context['variables']},
                'rationale': 'FIXTURE ONLY: evaluate samples before claiming improvement.',
                'candidate_type': 'explore',
            })
        wire = {
            'id': 'fixture-response-%d' % len(self.requests),
            'model': 'fixture-feedback-model', 'status': 'completed',
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30},
        }
        self.responses.append(wire)

        class Response:
            headers = {'x-request-id': wire['id']}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *args):
                return json.dumps(wire).encode('utf-8')

        return Response()

    def start(self, total=6, *, live=True):
        return supervisor.run_workflow(task={
            'operation': 'optimize', 'total_candidate_budget': total},
            live=live, settings=self.settings)

    def state(self, result):
        self.assertIn('workflow_id', result.data, result.to_dict())
        return WorkflowStore(self.settings).load(result.data['workflow_id'])

    def run_dir(self, result):
        state = self.state(result)
        self.assertIsNotNone(state.run_id, result.to_dict())
        return self.output / state.run_id

    def proposal_dirs(self, result):
        return sorted(self.run_dir(result).glob(
            'search/generation_*/center_api/request_*/proposal_*'))

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_six_candidates_make_two_real_client_requests_and_keep_output_schema(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(len(self.requests), 2)  # No extra reflection-model call.
        self.assertEqual(self.evaluations, 7)  # Baseline + 6 candidate tasks.
        state = self.state(result)
        self.assertEqual(state.target_total_candidates, 6)
        checkpoint = runtime.read_json(self.run_dir(result) / 'checkpoint.json')
        self.assertEqual(checkpoint['nCompleted'], 6)
        for body in self.requests:
            self.assertEqual(body['text']['format']['schema'], search.center_schema())
            self.assertEqual(body['model'], 'fixture-feedback-model')
            self.assertEqual(body['max_output_tokens'], 512)
        folders = self.proposal_dirs(result)
        self.assertEqual(len(folders), 2)
        for folder, body in zip(folders, self.requests):
            prompt = runtime.read_json(folder / 'prompt.json')
            request = runtime.read_json(folder / 'request.json')
            self.assertEqual(prompt['user'], body['input'])
            self.assertEqual(prompt['system'], body['instructions'])
            self.assertEqual(request['body'], body)
            events = runtime.read_json(folder / 'transport.json')
            self.assertEqual(sum(e['status'] == 'started' for e in events), 1)

    def test_second_http_request_contains_previous_measured_batch_feedback(self):
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(len(self.requests), 2)
        second = json.loads(self.requests[1]['input'])
        self.assertIn('batch_feedback', second)
        feedback = second['batch_feedback']
        self.assertEqual(feedback['run_id'], self.state(result).run_id)
        self.assertEqual(feedback['source_completed_candidates'], 4)
        self.assertEqual(feedback['workflow_id'], self.state(result).workflow_id)
        self.assertTrue(feedback['feedback_id'].startswith('fdbk_'))
        self.assertEqual(feedback['best']['stress_mpa'], 960.0)
        self.assertEqual(feedback['source_window']['candidate_iterations'], [1, 2, 3, 4])
        self.assertEqual(feedback['source_window']['historical_best_improvement_mpa'], 40.0)
        self.assertEqual(feedback['budget'], {
            'target_total_candidates': 6, 'completed_candidates': 4,
            'remaining_candidates': 2})
        self.assertEqual(feedback['engineering_qualification'], 'not_assessed')
        self.assertEqual(feedback['causal_attribution'], 'not_established')
        self.assertEqual(feedback['hotspot_attribution'], 'not_available')
        self.assertEqual(second['current_best_measured']['stress_mpa'], 960.0)
        self.assertEqual(second['search_state']['measured_designs'], 5)

    def test_interrupted_one_plus_three_reuses_request_and_existing_plan_bytes(self):
        self.stop_after = 1
        first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual(first.status, 'interrupted')
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        plan = self.run_dir(first) / 'search' / 'generation_001' / 'plan.json'
        before_hash = self.digest(plan)
        self.stop_after = None
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.digest(plan), before_hash)
        self.assertEqual(self.state(resumed).run_id, self.state(first).run_id)

    def test_preview_does_not_make_a_feedback_or_reflection_provider_call(self):
        result = self.start(total=6, live=False)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.status, 'validated')
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))
        self.assertFalse(self.output.exists())

    def test_legacy_cached_plan_is_reused_without_rewriting_or_claiming_feedback(self):
        legacy = tools.execute_task({
            'operation': 'optimize', 'total_candidate_budget': 1},
            live=True, settings=self.settings)
        self.assertTrue(legacy.ok, legacy.to_dict())
        run = Path(legacy.data['run_dir'])
        plan = run / 'search/generation_001/plan.json'
        plan_hash = self.digest(plan)
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        result = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': run.name,
            'additional_candidate_budget': 3}, live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.digest(plan), plan_hash)
        trace = self.state(result).decision_trace
        self.assertIn('cached_legacy', json.dumps(trace))

    def test_saved_successful_answer_and_feedback_are_recovered_without_new_http(self):
        original_write = search.write_json
        injected = []

        def crash_before_plan(path, value):
            path = Path(path)
            if path.name == 'plan.json' and path.parent.name == 'generation_001' and not injected:
                injected.append(str(path))
                raise RuntimeError('FIXTURE crash after successful answer; before plan commit.')
            return original_write(path, value)

        with patch.object(search, 'write_json', side_effect=crash_before_plan):
            first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        self.assertTrue(injected)
        self.assertEqual((len(self.requests), self.evaluations), (1, 1))
        folders = self.proposal_dirs(first)
        self.assertEqual(len(folders), 1)
        self.assertTrue((folders[0] / 'answer.json').is_file())
        self.assertTrue((folders[0] / 'feedback_ref.json').is_file())
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.state(resumed).run_id, self.state(first).run_id)

    def test_center_validation_retry_reuses_same_feedback_without_reflection_api(self):
        self.response_sequence = ['{"center":{},"rationale":"invalid fixture","candidate_type":"explore"}']
        result = self.start(total=1)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 2))
        first, second = [json.loads(body['input']) for body in self.requests]
        self.assertEqual(first['batch_feedback'], second['batch_feedback'])
        self.assertIsNotNone(second['correction_requested'])
        self.assertEqual(self.requests[0]['text']['format']['schema'], search.center_schema())
        self.assertEqual(self.requests[1]['text']['format']['schema'], search.center_schema())

    def test_model_cannot_expand_budget_by_adding_fields_to_center_response(self):
        problem = runtime.bridge.load_problem(self.problem_file)
        self.response_text = json.dumps({
            'center': {v['name']: v['reference'] for v in problem['variables']},
            'rationale': 'FIXTURE invalid extra budget field.', 'candidate_type': 'explore',
            'total_candidate_budget': 999,
        })
        result = self.start(total=4)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.state(result).target_total_candidates, 4)
        self.assertEqual((len(self.requests), self.evaluations), (2, 1))
        self.assertEqual(runtime.read_json(self.run_dir(result) / 'checkpoint.json')['nCompleted'], 0)

    def test_feedback_does_not_modify_engine_source_physics_or_search_configuration(self):
        project = Path(engine_fixture.PROJECT)
        paths = list((project / 'python_control').glob('*.py'))
        paths += [self.problem_file, self.root / 'python_control/search_config.json']
        before = {str(path): self.digest(path) for path in paths}
        definition = self.settings.definition()
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual({str(path): self.digest(path) for path in paths}, before)
        self.assertEqual(self.settings.definition(), definition)
        self.assertEqual(self.state(result).engine_fingerprint, {'fixture': 'stable-test-only'})

    def test_cross_run_feedback_ref_in_a_cached_center_is_rejected(self):
        first = self.start(total=1)
        second = self.start(total=1)
        self.assertTrue(first.ok, first.to_dict())
        self.assertTrue(second.ok, second.to_dict())
        first_ref = self.proposal_dirs(first)[0] / 'feedback_ref.json'
        second_ref = self.proposal_dirs(second)[0] / 'feedback_ref.json'
        second_ref.write_bytes(first_ref.read_bytes())
        before = len(self.requests), self.evaluations
        result = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': self.state(second).run_id,
            'additional_candidate_budget': 3}, live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)
        resumed = supervisor.resume_workflow(self.state(result).workflow_id,
                                             live=True, settings=self.settings)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before,
                         'Resume must not bypass a rejected reference using cached candidate.json.')

    def test_same_run_feedback_from_wrong_generation_is_rejected_on_cache_reuse(self):
        first = self.start(total=6)
        self.assertTrue(first.ok, first.to_dict())
        folders = self.proposal_dirs(first)
        self.assertEqual(len(folders), 2)
        (folders[1] / 'feedback_ref.json').write_bytes((folders[0] / 'feedback_ref.json').read_bytes())
        before = len(self.requests), self.evaluations
        result = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': self.state(first).run_id,
            'additional_candidate_budget': 1}, live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_feedback_artifact_failure_blocks_candidate_fea(self):
        original_write = runtime.write_json
        injected = []

        def fail_feedback_write(path, value):
            if (isinstance(value, dict) and
                    value.get('schema_version') == 'disk-agent.batch-feedback.v1' and
                    not injected):
                injected.append(str(path))
                raise OSError('FIXTURE feedback evidence could not be persisted.')
            return original_write(path, value)

        with patch.object(runtime, 'write_json', side_effect=fail_feedback_write):
            result = self.start(total=4)
        self.assertTrue(injected, 'The full factual feedback must be durably recorded.')
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.evaluations, 1)  # Only the prerequisite baseline.
        self.assertEqual(runtime.read_json(self.run_dir(result) / 'checkpoint.json')['nCompleted'], 0)

    def test_decision_state_failure_blocks_candidate_fea(self):
        original_save = WorkflowStore.save
        injected = []

        def fail_decision_save(store, state):
            if state.decision_trace and not injected:
                injected.append(state.workflow_id)
                raise OSError('FIXTURE decision trace could not be persisted.')
            return original_save(store, state)

        with patch.object(WorkflowStore, 'save', new=fail_decision_save):
            result = self.start(total=4)
        self.assertTrue(injected, 'A search decision must be persisted before candidate evaluation.')
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.evaluations, 1)
        self.assertEqual(runtime.read_json(self.run_dir(result) / 'checkpoint.json')['nCompleted'], 0)

    def test_trusted_feedback_context_cannot_authorize_a_larger_child_budget(self):
        preview = self.start(total=4, live=False)
        self.assertTrue(preview.ok, preview.to_dict())
        state = self.state(preview)
        self.assertIsNotNone(state.feedback_policy)
        result = tools.execute_task({
            'operation': 'optimize', 'total_candidate_budget': 5},
            live=True, settings=self.settings, feedback_context={
                'workflow_id': state.workflow_id, 'policy': state.feedback_policy,
                'target_total_candidates': state.target_total_candidates})
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (0, 0))

    def test_decision_artifact_failure_is_repaired_before_cached_candidate_resume(self):
        from agent_app import feedback_search
        original_save = feedback_search._save
        injected = []

        def fail_decision_artifact(path, value, root):
            if Path(path).parent.name == 'decisions' and not injected:
                injected.append(str(path))
                raise OSError('FIXTURE decision artifact not persisted after candidate creation.')
            return original_save(path, value, root)

        with patch.object(feedback_search, '_save', side_effect=fail_decision_artifact):
            first = self.start(total=1)
        self.assertTrue(injected)
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 1))
        self.assertEqual(self.state(first).decision_trace, [])
        self.require_decision_before_candidate = self.state(first).workflow_id
        resumed = supervisor.resume_workflow(self.state(first).workflow_id,
                                             live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        self.assertTrue(self.state(resumed).decision_trace)

    def test_new_workflow_can_reuse_earlier_workflow_feedback_plan_without_new_http(self):
        first = self.start(total=1)
        self.assertTrue(first.ok, first.to_dict())
        plan = self.run_dir(first) / 'search/generation_001/plan.json'
        before_hash = self.digest(plan)
        result = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': self.state(first).run_id,
            'additional_candidate_budget': 3}, live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 5))
        self.assertEqual(self.digest(plan), before_hash)
        self.assertNotEqual(self.state(result).workflow_id, self.state(first).workflow_id)
        self.assertEqual(self.state(result).decision_trace[0]['feedback_source_workflow_id'],
                         self.state(first).workflow_id)

    def test_trusted_feedback_context_cannot_bind_a_different_cached_run(self):
        first = self.start(total=1)
        second = self.start(total=1)
        self.assertTrue(first.ok, first.to_dict())
        self.assertTrue(second.ok, second.to_dict())
        preview = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': self.state(first).run_id,
            'additional_candidate_budget': 3}, settings=self.settings)
        self.assertTrue(preview.ok, preview.to_dict())
        state = self.state(preview)
        before = len(self.requests), self.evaluations
        result = tools.execute_task({
            'operation': 'optimize', 'run_id': self.state(second).run_id,
            'total_candidate_budget': 2}, live=True, settings=self.settings,
            feedback_context={'workflow_id': state.workflow_id,
                              'policy': state.feedback_policy,
                              'target_total_candidates': state.target_total_candidates})
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), before)

    def test_process_death_before_decision_commit_recovers_without_unaudited_fea(self):
        from agent_app import feedback_search
        original_save = feedback_search._save
        workflow_ids = []

        class ProcessDeath(BaseException):
            """Bypass ordinary controller and supervisor exception recovery."""

        def die_before_decision(path, value, root):
            if Path(path).parent.name == 'decisions':
                raise ProcessDeath('FIXTURE process died before decision commit.')
            return original_save(path, value, root)

        with patch.object(feedback_search, '_save', side_effect=die_before_decision):
            with self.assertRaises(ProcessDeath):
                supervisor.run_workflow(task={
                    'operation': 'optimize', 'total_candidate_budget': 1},
                    live=True, settings=self.settings,
                    on_workflow_created=workflow_ids.append)
        self.assertEqual(len(workflow_ids), 1)
        state = WorkflowStore(self.settings).load(workflow_ids[0])
        self.assertEqual(state.status, 'running')
        self.assertEqual((len(self.requests), self.evaluations), (1, 1))
        self.require_decision_before_candidate = workflow_ids[0]
        resumed = supervisor.resume_workflow(workflow_ids[0], live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        self.assertTrue(self.state(resumed).decision_trace)

    def test_existing_pending_proposal_is_audited_before_resumed_fea(self):
        self.fail_at = 1
        first = self.start(total=1)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        checkpoint = runtime.read_json(self.run_dir(first) / 'checkpoint.json')
        self.assertIsNotNone(checkpoint['pending']['proposal'])
        self.assertEqual(checkpoint['nCompleted'], 0)
        self.assertEqual((len(self.requests), self.evaluations), (1, 2))
        # Emulate a pre-feature/partially restored workflow: the engine has a
        # pending proposal, but its workflow trace must be recovered from the
        # canonical feedback/prompt/plan evidence before another FEA starts.
        for trace in state.decision_trace:
            path = WorkflowStore(self.settings).directory(state.workflow_id) / 'decisions' / (trace['decision_id'] + '.json')
            if path.exists():
                path.unlink()
        state.decision_trace = []
        WorkflowStore(self.settings).save(state)
        self.require_decision_before_candidate = state.workflow_id
        self.fail_at = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (1, 3))
        self.assertTrue(self.state(resumed).decision_trace)


if __name__ == '__main__':
    unittest.main()
