"""Workflow orchestration over real controller state and isolated fake boundaries.

The provider, native evidence verifier, and Workbench evaluator are explicit
fixtures. No test can contact a real model or start the installed solver.
"""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app import supervisor, tools
from agent_app.state import WorkflowStore
from agent_app.tests import test_engine_integration as engine_fixture
from python_control import controller, runtime, search


class SupervisorIntegrationTests(unittest.TestCase):
    # Compose the isolated fixture; do not inherit and rerun its older tests.
    def setUp(self):
        engine_fixture.ControllerIntegrationTests.setUp(self)
        self.stop_after = None
        self.flat_stress = False
        self.center_requests = 0
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
        for item in (
            patch.object(search, 'request_center', side_effect=self.fake_center),
            patch.object(runtime, 'fingerprint',
                         return_value={'fixture': 'stable-test-only'}),
            patch.object(runtime.bridge.Client, 'generate',
                         side_effect=AssertionError('NO REAL MODEL IN SUPERVISOR TESTS')),
        ):
            item.start()
            self.addCleanup(item.stop)

    fake_proposal = engine_fixture.ControllerIntegrationTests.fake_proposal

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        index = self.evaluations
        detail = engine_fixture.ControllerIntegrationTests.fake_evaluate(
            self, x, settings, problem, output_root, baseline)
        if self.flat_stress:
            detail['stressMPa'] = 1000.0
        if self.stop_after == index:
            detail['stopRequested'] = True
        return detail

    def fake_center(self, api, problem, history, folder, generation, sigma,
                    client_factory=None, selection_context=None):
        self.center_requests += 1
        center = search.parse_center(json.dumps({
            'center': {v['name']: v['reference'] for v in problem['variables']},
            'rationale': 'FIXTURE ONLY; sampled center, not measured physics.',
            'candidate_type': 'explore',
        }), problem)
        center['apiAttemptDirectory'] = None
        return center

    def start(self, total=6, *, live=True, request=None):
        kwargs = {'settings': self.settings, 'live': live}
        if request is None:
            kwargs['task'] = {'operation': 'optimize',
                              'total_candidate_budget': total}
        else:
            kwargs['request'] = request
        return supervisor.run_workflow(**kwargs)

    def state(self, result):
        self.assertIn('workflow_id', result.data, result.to_dict())
        return WorkflowStore(self.settings).load(result.data['workflow_id'])

    def checkpoint(self, result):
        state = self.state(result)
        self.assertIsNotNone(state.run_id, result.to_dict())
        return runtime.read_json(self.output / state.run_id / 'checkpoint.json')

    def runs(self):
        return list(self.output.glob('run_*')) if self.output.exists() else []

    @staticmethod
    def optimize_calls(spy):
        calls = []
        for item in spy.call_args_list:
            payload = item.args[0]
            if hasattr(payload, 'to_dict'):
                payload = payload.to_dict()
            if payload.get('operation') == 'optimize':
                calls.append((payload, item.kwargs))
        return calls

    def test_default_preview_freezes_budget_without_provider_or_evaluator(self):
        with patch.object(supervisor, 'execute_task', wraps=tools.execute_task) as spy:
            result = self.start(live=False)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.status, 'validated')
        state = self.state(result)
        self.assertEqual(state.target_total_candidates, 6)
        self.assertIsNone(state.run_id)
        self.assertEqual((self.center_requests, self.evaluations), (0, 0))
        self.assertEqual(self.runs(), [])
        self.assertTrue(all(not opts.get('live', False)
                            for _, opts in self.optimize_calls(spy)))

    def test_natural_language_plan_only_overrides_live_and_skips_task_tools(self):
        before = tools.entry.check_environment.call_count
        with patch.object(supervisor, 'execute_task', wraps=tools.execute_task) as spy:
            result = self.start(request='新建优化6个候选，只看计划', live=True)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.status, 'planned')
        self.assertEqual((self.center_requests, self.evaluations), (0, 0))
        self.assertEqual(tools.entry.check_environment.call_count, before)
        spy.assert_not_called()

    def test_six_candidates_use_absolute_four_plus_two_batches_and_one_run(self):
        with patch.object(supervisor, 'execute_task', wraps=tools.execute_task) as spy:
            result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.status, 'completed')
        state = self.state(result)
        self.assertEqual(state.status, 'completed')
        self.assertEqual(state.target_total_candidates, 6)
        self.assertEqual(self.checkpoint(result)['nCompleted'], 6)
        calls = [(payload, opts) for payload, opts in self.optimize_calls(spy)
                 if opts.get('live') is True]
        self.assertEqual([p['total_candidate_budget'] for p, _ in calls], [4, 6])
        self.assertTrue(all('additional_candidate_budget' not in p for p, _ in calls))
        self.assertEqual(calls[1][0]['run_id'], state.run_id)
        self.assertEqual(len(state.batches), 2)
        self.assertIsNone(state.pending_batch)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual((self.center_requests, self.evaluations), (2, 7))

    def test_interrupted_after_one_resumes_other_three_with_same_center(self):
        self.stop_after = 1  # Baseline is index 0; commit candidate 1, then stop.
        first = self.start(total=4)
        self.assertFalse(first.ok, first.to_dict())
        self.assertEqual(first.status, 'interrupted')
        state = self.state(first)
        run_id = state.run_id
        self.assertEqual(self.checkpoint(first)['nCompleted'], 1)
        self.assertEqual((self.center_requests, self.evaluations), (1, 2))
        self.stop_after = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(resumed.status, 'completed')
        self.assertEqual(self.state(resumed).run_id, run_id)
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 4)
        self.assertEqual((self.center_requests, self.evaluations), (1, 5))
        self.assertEqual(len(self.runs()), 1)

    def test_failure_keeps_run_and_pending_budget_until_explicit_live_resume(self):
        self.fail_at = 2
        first = self.start(total=6)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        self.assertEqual(state.status, 'failed')
        self.assertEqual(self.checkpoint(first)['nCompleted'], 1)
        self.assertIsNotNone(state.pending_batch)
        before = self.center_requests, self.evaluations
        preview = supervisor.resume_workflow(state.workflow_id, settings=self.settings)
        self.assertTrue(preview.ok, preview.to_dict())
        self.assertEqual(preview.status, 'validated')
        self.assertEqual(self.state(preview).status, 'failed')
        self.assertEqual((self.center_requests, self.evaluations), before)
        self.fail_at = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(self.state(resumed).run_id, state.run_id)
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 6)
        self.assertEqual(self.state(resumed).target_total_candidates, 6)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.center_requests, 2)

    def test_failed_baseline_is_bound_before_first_expensive_evaluation(self):
        self.fail_at = 0
        first = self.start(total=1)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        self.assertIsNotNone(state.run_id)
        self.assertEqual(self.checkpoint(first)['nCompleted'], 0)
        self.assertIsNone(self.checkpoint(first)['baseline'])
        self.assertEqual((self.center_requests, self.evaluations), (0, 1))
        self.fail_at = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(self.state(resumed).run_id, state.run_id)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 1)

    def test_completed_pending_batch_is_analyzed_without_reexecuting_its_budget(self):
        self.stop_after = 4
        first = self.start(total=6)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        self.assertEqual(self.checkpoint(first)['nCompleted'], 4)
        self.assertIsNotNone(state.pending_batch)
        self.stop_after = None
        with patch.object(supervisor, 'execute_task', wraps=tools.execute_task) as spy:
            resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                                 settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        live_calls = [p for p, opts in self.optimize_calls(spy)
                      if opts.get('live') is True]
        self.assertEqual([p['total_candidate_budget'] for p in live_calls], [6])
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 6)
        self.assertEqual((self.center_requests, self.evaluations), (2, 7))

    def test_completed_workflow_resume_is_cached_and_costs_nothing(self):
        completed = self.start(total=1)
        self.assertTrue(completed.ok, completed.to_dict())
        before = self.center_requests, self.evaluations
        with patch.object(supervisor, 'execute_task', wraps=tools.execute_task) as spy:
            resumed = supervisor.resume_workflow(completed.data['workflow_id'],
                                                 live=True, settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(resumed.status, 'completed')
        self.assertEqual((self.center_requests, self.evaluations), before)
        spy.assert_not_called()

    def test_readonly_workflow_status_preserves_historical_failure(self):
        self.fail_at = 1
        failed = self.start(total=4)
        state = self.state(failed)
        before = self.center_requests, self.evaluations
        result = supervisor.get_workflow_status(state.workflow_id, settings=self.settings)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.data['workflow_status'], 'failed')
        self.assertEqual(self.state(result).status, 'failed')
        self.assertEqual((self.center_requests, self.evaluations), before)

    def test_unbound_running_call_needs_review_and_never_claims_latest(self):
        preview = self.start(total=4, live=False)
        store = WorkflowStore(self.settings)
        state = store.load(preview.data['workflow_id'])
        state.status = 'running'
        state.pending_batch = {'start_completed_candidates': 0,
                               'target_total_candidates': 4}
        store.save(state)
        unrelated = tools.execute_task({'operation': 'optimize',
                                        'total_candidate_budget': 1},
                                       live=True, settings=self.settings)
        self.assertTrue(unrelated.ok, unrelated.to_dict())
        before = self.center_requests, self.evaluations
        result = supervisor.resume_workflow(state.workflow_id, live=True,
                                            settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.state(result).status, 'needs_review')
        self.assertIsNone(self.state(result).run_id)
        self.assertEqual((self.center_requests, self.evaluations), before)
        self.assertEqual(len(self.runs()), 1)

    def test_external_progress_beyond_pending_target_stops_without_new_work(self):
        self.stop_after = 1
        first = self.start(total=6)
        state = self.state(first)
        self.stop_after = None
        external = tools.execute_task({'operation': 'optimize', 'run_id': state.run_id,
                                       'total_candidate_budget': 5},
                                      live=True, settings=self.settings)
        self.assertTrue(external.ok, external.to_dict())
        before = self.center_requests, self.evaluations
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertFalse(resumed.ok, resumed.to_dict())
        self.assertEqual(self.state(resumed).status, 'needs_review')
        self.assertEqual((self.center_requests, self.evaluations), before)
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 5)
        self.assertEqual(self.state(resumed).target_total_candidates, 6)

    def test_additional_budget_is_frozen_once_and_not_added_again_on_resume(self):
        initial = self.start(total=1)
        original_run = self.state(initial).run_id
        self.stop_after = 2
        first = supervisor.run_workflow(task={
            'operation': 'optimize', 'run_id': original_run,
            'additional_candidate_budget': 5}, live=True, settings=self.settings)
        self.assertFalse(first.ok, first.to_dict())
        state = self.state(first)
        self.assertEqual(state.start_completed_candidates, 1)
        self.assertEqual(state.target_total_candidates, 6)
        self.assertEqual(self.checkpoint(first)['nCompleted'], 2)
        self.stop_after = None
        resumed = supervisor.resume_workflow(state.workflow_id, live=True,
                                             settings=self.settings)
        self.assertTrue(resumed.ok, resumed.to_dict())
        self.assertEqual(self.checkpoint(resumed)['nCompleted'], 6)
        self.assertEqual(self.state(resumed).target_total_candidates, 6)
        self.assertEqual((self.center_requests, self.evaluations), (2, 7))
        self.assertEqual(len(self.runs()), 1)

    def test_no_improvement_never_expands_the_authorized_candidate_budget(self):
        self.flat_stress = True
        result = self.start(total=6)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.checkpoint(result)['nCompleted'], 6)
        self.assertEqual(self.state(result).target_total_candidates, 6)
        self.assertEqual((self.center_requests, self.evaluations), (2, 7))

    def test_engine_change_after_preview_blocks_live_resume(self):
        preview = self.start(total=4, live=False)
        with patch.object(runtime, 'fingerprint',
                          return_value={'fixture': 'changed-engine'}):
            result = supervisor.resume_workflow(preview.data['workflow_id'],
                                                live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual(self.state(result).status, 'needs_review')
        self.assertEqual((self.center_requests, self.evaluations), (0, 0))
        self.assertEqual(self.runs(), [])

    def test_external_advance_just_before_batch_is_rejected_before_more_cost(self):
        initial = self.start(total=1)
        run_id = self.state(initial).run_id
        after_external = []

        def raced(payload, **kwargs):
            value = payload.to_dict() if hasattr(payload, 'to_dict') else payload
            if value.get('operation') == 'optimize' and kwargs.get('live'):
                external = tools.execute_task({
                    'operation': 'optimize', 'run_id': run_id,
                    'total_candidate_budget': 2}, live=True, settings=self.settings)
                self.assertTrue(external.ok, external.to_dict())
                after_external[:] = [self.center_requests, self.evaluations]
            return tools.execute_task(payload, **kwargs)

        with patch.object(supervisor, 'execute_task', side_effect=raced):
            result = supervisor.run_workflow(task={
                'operation': 'optimize', 'run_id': run_id,
                'additional_candidate_budget': 5}, live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertTrue(after_external)
        self.assertEqual([self.center_requests, self.evaluations], after_external)
        self.assertEqual(self.checkpoint(result)['nCompleted'], 2)
        self.assertEqual(self.state(result).target_total_candidates, 6)

    def test_request_and_task_conflict_is_rejected_before_any_model_or_solver(self):
        result = supervisor.run_workflow(
            request='优化4个候选',
            task={'operation': 'optimize', 'total_candidate_budget': 4},
            live=True, settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((self.center_requests, self.evaluations), (0, 0))
        self.assertEqual(self.runs(), [])

    def test_invalid_live_flag_is_rejected_before_any_model_or_solver(self):
        result = supervisor.run_workflow(
            task={'operation': 'optimize', 'total_candidate_budget': 1},
            live='yes', settings=self.settings)
        self.assertFalse(result.ok, result.to_dict())
        self.assertEqual((self.center_requests, self.evaluations), (0, 0))
        self.assertEqual(self.runs(), [])

    def test_tampered_parent_target_cannot_expand_original_taskspec_budget(self):
        preview=self.start(total=4,live=False)
        store=WorkflowStore(self.settings)
        state=store.load(preview.data['workflow_id'])
        state.target_total_candidates=100
        store.save(state)
        resumed=supervisor.resume_workflow(state.workflow_id,live=True,settings=self.settings)
        self.assertFalse(resumed.ok)
        self.assertEqual(resumed.error['code'],'WORKFLOW_BUDGET')
        self.assertEqual((self.center_requests,self.evaluations),(0,0))

    def test_analysis_write_recovery_does_not_repeat_completed_fea_or_provider(self):
        with patch.object(supervisor,'_write_analysis',side_effect=OSError('fixture disk failure')):
            first=self.start(total=4)
        self.assertFalse(first.ok)
        state=self.state(first)
        self.assertEqual(self.checkpoint(first)['nCompleted'],4)
        before=self.center_requests,self.evaluations
        resumed=supervisor.resume_workflow(state.workflow_id,live=True,settings=self.settings)
        self.assertTrue(resumed.ok,resumed.to_dict())
        self.assertEqual((self.center_requests,self.evaluations),before)
        self.assertEqual(len(self.runs()),1)

    def test_failed_run_binding_persistence_blocks_baseline_evaluator(self):
        with patch.object(supervisor,'_bind_run',side_effect=OSError('fixture state write failure')):
            first=self.start(total=1)
        self.assertFalse(first.ok)
        self.assertEqual((self.center_requests,self.evaluations),(0,0))
        self.assertEqual(len(self.runs()),1)  # Empty checkpoint, no expensive work.
        resumed=supervisor.resume_workflow(first.data['workflow_id'],live=True,settings=self.settings)
        self.assertFalse(resumed.ok)
        self.assertEqual(resumed.status,'needs_review')
        self.assertEqual((self.center_requests,self.evaluations),(0,0))


if __name__ == '__main__':
    unittest.main()
