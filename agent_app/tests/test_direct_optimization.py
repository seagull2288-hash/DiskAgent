"""Direct LLM proposals through real workflow/bridge with fake HTTP and FEA."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app import optimization_trace, supervisor, tools
from agent_app.tests import test_engine_integration as fixture
from python_control import runtime


REAL_PROPOSE = runtime.bridge.propose


class DirectOptimizationTests(unittest.TestCase):
    fake_proposal = fixture.ControllerIntegrationTests.fake_proposal
    fake_evaluate = fixture.ControllerIntegrationTests.fake_evaluate

    def setUp(self):
        fixture.ControllerIntegrationTests.setUp(self)
        self.requests = []
        self.duplicate_first = False
        config = dict(runtime.bridge.DEFAULT_CONFIG, provider='openai_responses',
                      base_url='http://127.0.0.1:9/v1', model='direct-fixture', api_key_env='',
                      max_proposal_attempts=2, max_http_attempts=1, json_mode=True)
        runtime.bridge.load_config.return_value = config
        tools.entry.check_environment.return_value = {
            'python_version': 'fixture', 'api_key_configured': False,
            'api_requests_made': 0, 'finite_element_solves_made': 0,
            'search_strategy': 'direct', 'initial_lhs': 0, 'batch_size': 4,
            'pool_size': 32, 'surrogate_min_samples': 24,
        }
        client_type = runtime.bridge.Client

        def factory(cfg, *, live):
            return client_type(cfg, live=live, transport=self.transport,
                               sleep=lambda _: self.fail('Unexpected transport retry.'))

        for item in (patch.object(runtime.bridge, 'propose', side_effect=REAL_PROPOSE),
                     patch.object(runtime, 'fingerprint', return_value={'fixture': 'stable-test-only'}),
                     patch.object(runtime.bridge, 'Client', side_effect=factory),
                     patch('agent_app.prediction_audit.record_before_solve',
                           side_effect=AssertionError('Direct mode must skip GP audit.')),
                     patch('python_control.search.SearchProposer.__call__',
                           side_effect=AssertionError('Direct mode must skip program selection.'))):
            item.start()
            self.addCleanup(item.stop)

    def transport(self, request, *, timeout):
        self.assertEqual(request.full_url, 'http://127.0.0.1:9/v1/responses')
        body = json.loads(request.data.decode('utf-8'))
        context = json.loads(body['input'])
        self.requests.append(context)
        parameters = {v['name']: v['reference'] for v in context['variables']}
        if not (self.duplicate_first and len(self.requests) == 1):
            parameters['X1'] = round(parameters['X1'] + .01 * context['candidate_iteration'], 4)
        answer = json.dumps({'parameters': parameters, 'rationale': 'Measured-history fixture proposal.',
                             'candidate_type': 'exploit'})
        wire = {'id': 'fixture-%d' % len(self.requests), 'model': 'direct-fixture', 'status': 'completed',
                'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': answer}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}}

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, *args):
                return json.dumps(wire).encode('utf-8')

        return Response()

    def test_one_llm_request_per_candidate_with_latest_measured_feedback(self):
        result = supervisor.run_workflow(request='新建优化3个候选', live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (3, 4))
        self.assertEqual([p['candidate_iteration'] for p in self.requests], [1, 2, 3])
        self.assertEqual([p['optimization_feedback']['completed_candidates'] for p in self.requests], [0, 1, 2])
        run = self.output / result.data['run_id']
        state = runtime.read_json(run / 'checkpoint.json')
        self.assertEqual([r['proposal']['source'] for r in state['records']], ['llm'] * 3)
        self.assertEqual(runtime.read_json(run / 'search_config.json')['strategy'], 'direct')
        self.assertFalse((run / 'prediction_audit').exists())
        self.assertFalse((run / 'search').exists())
        self.assertFalse(result.data['feedback_policy']['enabled'])
        before = {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()}
        trace = optimization_trace.inspect_optimization_trace(run.name, settings=self.settings).data
        self.assertEqual(trace['llm_direct_candidates'], 3)
        self.assertEqual(trace['feasible_candidates'], 3)
        self.assertEqual(trace['best_iteration'], 3)
        self.assertEqual(trace['records'][1]['anchor_iteration'], 1)
        self.assertAlmostEqual(trace['records'][1]['parameter_changes']['X1'], .01)
        self.assertEqual(before, {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()})

    def test_direct_mode_stays_free_of_gp_after_twenty_four_samples(self):
        result = supervisor.run_workflow(request='新建优化25个候选', live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (25, 26))
        run = self.output / result.data['run_id']
        self.assertFalse((run / 'prediction_audit').exists())
        self.assertEqual(runtime.read_json(run / 'checkpoint.json')['nCompleted'], 25)

    def test_duplicate_is_corrected_by_llm_without_spending_an_extra_candidate(self):
        self.duplicate_first = True
        result = tools.execute_task({'operation': 'optimize', 'total_candidate_budget': 1},
                                    live=True, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual((len(self.requests), self.evaluations), (2, 2))
        self.assertEqual(self.requests[1]['correction_requested']['code'], 'DUPLICATE')
        self.assertEqual(result.data['completed_candidates'], 1)

    def test_solver_retry_reuses_saved_llm_dimensions_and_baseline(self):
        self.fail_at = 1
        first = tools.execute_task({'operation': 'optimize', 'total_candidate_budget': 1},
                                   live=True, settings=self.settings)
        self.assertFalse(first.ok)
        run = Path(first.data['run_dir'])
        pending = optimization_trace.inspect_optimization_trace(run.name, settings=self.settings).data['records'][0]
        self.assertEqual(pending['status'], 'pending')
        self.assertIsNone(pending['stress_mpa'])
        self.fail_at = None
        resumed = tools.execute_task({'operation': 'optimize', 'run_id': run.name, 'total_candidate_budget': 1},
                                     live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.evaluations, 3)

    def test_policy_anchor_is_kept_when_low_stress_design_is_infeasible(self):
        from python_control.objectives import default_policy
        problem = runtime.bridge.load_problem(self.problem_file)
        policy = default_policy()
        policy['collect_mass'] = True
        policy['constraints'] = [{'metric': 'mass_kg', 'operator': '<=', 'value': 1.0,
                                  'upper': None, 'reference': 'baseline_ratio', 'source': 'fixture'}]
        problem['optimization_policy'] = policy
        history = []
        for i, (stress, mass) in enumerate(((1000, 10), (500, 12), (900, 9))):
            parameters = {v['name']: v['reference'] for v in problem['variables']}
            parameters['X1'] += .01 * i
            history.append({'parameters': parameters, 'stress_mpa': stress, 'mass_kg': mass})
        cfg = dict(runtime.bridge.DEFAULT_CONFIG, history_prompt_limit=1)
        context = json.loads(runtime.bridge.make_prompt(problem, history, cfg)['user'])
        self.assertEqual(context['current_best_measured'], history[2])
        self.assertEqual(context['observations'], [history[2]])
        self.assertEqual(context['optimization_feedback']['anchor_iteration'], 2)
        self.assertEqual(context['optimization_feedback']['recent_evaluations'][1]['status'], 'infeasible')


if __name__ == '__main__':
    unittest.main()
