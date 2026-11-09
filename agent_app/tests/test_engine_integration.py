"""Exercise the real controller's commits/resume with isolated fake boundaries."""
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_app import planner, tools
from python_control import controller, runtime


PROJECT = Path(__file__).resolve().parents[2]


class ControllerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='disk_agent_engine_fixture_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root/'python_llm_runs'
        self.problem_file = self.root/'problem.json'
        self.problem_file.write_bytes((PROJECT/'api_bridge/problem.json').read_bytes())
        (self.root/'python_control').mkdir()
        config = json.loads((PROJECT/'python_control/search_config.json').read_text(encoding='utf-8-sig'))
        config['strategy'] = 'direct'
        (self.root/'python_control/search_config.json').write_text(json.dumps(config),encoding='utf-8')
        self.settings = runtime.Settings(
            root=self.root,candidate_count=10,project_file=self.root/'fixture.wbpj',
            workbench_executable=self.root/'NO_REAL_SOLVER.exe',output_root=self.output,
            progress_log=self.root/'fixture.log',api_config=self.root/'fixture-api.json',
            problem_file=self.problem_file,key_environment_variable='UNUSED_FIXTURE_KEY',plot_on_complete=False)
        self.evaluations = 0
        self.proposals = 0
        self.fail_at = None

        @contextmanager
        def fixture_auth(*args):
            yield  # No key lookup, environment mutation, or external request.

        def native_check(detail, baseline=None):
            self.assertTrue(detail.get('fixture_native'))
            return {'actual':{'baseline_after':{'fixture':'FAKE_SOLVER_ONLY'},
                              'mesh_after':{'fixture':'NO_REAL_MESH'}}}

        patches = [patch.object(tools.entry,'check_environment',return_value={}),
                   patch.object(runtime,'api_key',fixture_auth),
                   patch.object(runtime.bridge,'load_config',return_value={'api_key_env':'UNUSED_FIXTURE_KEY'}),
                   patch.object(runtime.bridge,'propose',side_effect=self.fake_proposal),
                   patch.object(controller,'fingerprint',return_value={'fixture':'stable-test-only'}),
                   patch.object(controller,'verify_native',side_effect=native_check),
                   patch.object(tools,'evaluate_native',side_effect=self.fake_evaluate)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.logs = redirect_stdout(io.StringIO())
        self.logs.__enter__()
        self.addCleanup(self.logs.__exit__,None,None,None)

    def fake_proposal(self, api, problem, history, output, *, live, client=None):
        self.assertTrue(live)
        self.proposals += 1
        Path(output).mkdir(parents=True,exist_ok=True)
        parameters = {v['name']:v['reference'] for v in problem['variables']}
        parameters['X1'] = round(parameters['X1'] + 0.01*len(history),4)
        candidate = runtime.bridge.parse_candidate(json.dumps({
            'parameters':parameters,'rationale':'FIXTURE ONLY; no LLM call.',
            'candidate_type':'explore'}),problem,history)
        candidate.update(source='llm',mode='live',proposal_attempt=1)
        return candidate

    def fake_evaluate(self, x, settings, problem, output_root, baseline=None):
        index = self.evaluations
        self.evaluations += 1
        if self.fail_at == index:
            raise RuntimeError('Injected fixture evaluator failure; no real solver started.')
        folder = Path(output_root)/('fixture_eval_%03d'%index)
        folder.mkdir(parents=True,exist_ok=False)
        native = folder/'mechanical_verified.json'
        native.write_text('{"fixture_only":true}',encoding='utf-8')
        return {'xActual':list(x),'stressMPa':1000.0-10*index,'displacementMM':0.2,
                'area':0.01,'runDir':str(folder),'nativeReportFile':str(native),
                'fixture_native':True,'stopRequested':False}

    def new_run(self, total=2):
        result = tools.execute_task({'operation':'optimize','total_candidate_budget':total},
                                    live=True,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        return result

    def test_adapter_passes_persisted_evaluation_identity_to_the_native_wrapper(self):
        directories = []

        def native(x, settings, problem, output_root, baseline=None, *, evaluation_dir=None):
            self.assertIsNotNone(evaluation_dir)
            snapshot = runtime.read_json(Path(output_root).parent / 'checkpoint.json')
            saved = snapshot['pending'] if baseline is not None else snapshot['baseline_evaluation']
            self.assertEqual(saved['evaluation_dir'], evaluation_dir)
            directories.append(evaluation_dir)
            return self.fake_evaluate(x, settings, problem, output_root, baseline)

        native.supports_durable_evaluation = True
        with patch.object(tools, 'evaluate_native', new=native):
            result = self.new_run(total=1)
        self.assertEqual(len(set(directories)), 2)
        self.assertEqual(result.data['completed_candidates'], 1)

    def test_real_controller_commits_baseline_candidates_and_status(self):
        result = self.new_run()
        self.assertEqual(self.evaluations,3)
        self.assertEqual(self.proposals,2)
        self.assertEqual(result.data['completed_candidates'],2)
        self.assertEqual(result.data['baseline_stress_mpa'],1000)
        self.assertEqual(result.data['best']['stress_mpa'],980)
        run = Path(result.data['run_dir'])
        self.assertTrue((run/'history.csv').is_file())
        before = self.evaluations,self.proposals
        status = tools.get_run_status(run.name,settings=self.settings)
        self.assertTrue(status.ok,status.to_dict())
        self.assertEqual((self.evaluations,self.proposals),before)

    def test_real_controller_resumes_using_completed_plus_delta(self):
        first = self.new_run()
        run_id = first.data['run_id']
        result = tools.execute_task({'operation':'optimize','run_id':run_id,'additional_candidate_budget':2},
                                    live=True,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        self.assertEqual(result.data['target_total_candidates'],4)
        self.assertEqual(result.data['completed_candidates'],4)
        self.assertEqual(self.evaluations,5)  # Baseline is not rerun.
        self.assertEqual(self.proposals,4)

    def test_natural_language_planner_drives_new_status_and_resume_controller(self):
        # The real planner, validator, adapter and controller share temporary state;
        # only provider/native solver boundaries are fake, as in the other tests.
        first = planner.execute_request('新建优化2个候选',live=True,settings=self.settings)
        self.assertTrue(first.ok,first.to_dict())
        self.assertEqual(first.data['completed_candidates'],2)
        self.assertEqual((self.evaluations,self.proposals),(3,2))
        status = planner.execute_request('查询上一次运行状态',settings=self.settings)
        self.assertTrue(status.ok,status.to_dict())
        self.assertEqual(status.data['run_id'],first.data['run_id'])
        self.assertEqual((self.evaluations,self.proposals),(3,2))
        resumed = planner.execute_request('继续上一次，再算1个候选',live=True,settings=self.settings)
        self.assertTrue(resumed.ok,resumed.to_dict())
        self.assertEqual(resumed.data['run_id'],first.data['run_id'])
        self.assertEqual(resumed.data['completed_candidates'],3)
        self.assertEqual((self.evaluations,self.proposals),(4,3))
        preview = planner.execute_request('新建优化4个候选，先别实算',live=True,settings=self.settings)
        self.assertTrue(preview.ok,preview.to_dict())
        self.assertEqual(preview.status,'planned')
        self.assertEqual((self.evaluations,self.proposals),(4,3))

    def test_existing_os_project_lock_still_rejects_second_executor(self):
        with runtime.project_lock(self.root):
            result = tools.execute_task({'operation':'optimize','total_candidate_budget':1},
                                        live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(self.evaluations,0)
        self.assertEqual(self.proposals,0)
        self.assertNotIn('run_id',result.data)

    def test_failed_evaluation_preserves_committed_history_and_own_run(self):
        self.fail_at = 2
        result = tools.execute_task({'operation':'optimize','total_candidate_budget':3},
                                    live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.data['completed_candidates'],1)
        self.assertEqual(result.data['best']['stress_mpa'],990)
        self.assertEqual(result.data['run_status'],'failed')
        self.assertTrue(Path(result.data['sources']['checkpoint']).is_file())

    def test_resume_race_rejected_before_new_provider_or_solver_call(self):
        first = self.new_run()
        original = controller.run
        counts_after_external = []

        def raced(settings,resume,**kwargs):
            # Emulate the original CLI completing one point before our controller
            # obtains its lock. This still uses the fake API/solver in temp storage.
            original(replace(settings,candidate_count=3),resume,evaluator=self.fake_evaluate)
            counts_after_external[:] = [self.proposals,self.evaluations]
            return original(settings,resume,**kwargs)

        with patch.object(controller,'run',side_effect=raced):
            result = tools.execute_task({'operation':'optimize','run_id':first.data['run_id'],
                                         'additional_candidate_budget':2},live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_CHANGED')
        self.assertEqual([self.proposals,self.evaluations],counts_after_external)
        self.assertEqual(result.data['completed_candidates'],3)

    def test_resume_race_with_no_callbacks_cannot_report_success(self):
        first = self.new_run()
        original = controller.run

        def raced(settings,resume,**kwargs):
            original(settings,resume,evaluator=self.fake_evaluate)
            return original(settings,resume,**kwargs)  # No new provider/evaluator callbacks.

        with patch.object(controller,'run',side_effect=raced):
            result = tools.execute_task({'operation':'optimize','run_id':first.data['run_id'],
                                         'additional_candidate_budget':2},live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_CHANGED')

    def test_already_evaluated_pending_commit_is_valid_resume_work(self):
        first = self.new_run()
        run = Path(first.data['run_dir'])
        state = json.loads((run/'checkpoint.json').read_text(encoding='utf-8'))
        record = state['records'].pop()
        state['history'].pop()
        state['nCompleted'] = 1
        state['status'] = 'interrupted'
        state['pending'] = {'iteration':2,'directory':record['roundDirectory'],
                            'phase':'evaluated','proposal':record['proposal'],
                            'api_output':record['apiRun'],'detail':record['detail']}
        runtime.write_json(run/'checkpoint.json',state)
        before = self.proposals,self.evaluations
        result = tools.execute_task({'operation':'optimize','run_id':run.name,
                                     'additional_candidate_budget':1},live=True,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        self.assertEqual(result.data['completed_candidates'],2)
        self.assertEqual((self.proposals,self.evaluations),before)


if __name__ == '__main__':
    unittest.main()
