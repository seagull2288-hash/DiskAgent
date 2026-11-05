"""Isolated adapter tests. No external API requests or Workbench processes."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_app.schemas import CASE_ID, TaskSpec, TaskValidationError, ToolResult
from agent_app import tools, validation
from python_control.runtime import Settings


PROJECT = Path(__file__).resolve().parents[2]


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='disk_adapter_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'python_llm_runs'
        self.output.mkdir()
        self.problem_file = self.root / 'problem.json'
        self.problem = json.loads((PROJECT / 'api_bridge/problem.json').read_text(encoding='utf-8-sig'))
        self.problem_file.write_text(json.dumps(self.problem), encoding='utf-8')
        self.settings = Settings(
            root=self.root, candidate_count=10,
            project_file=self.root/'project_v2'/'2dmodel.wbpj',
            workbench_executable=self.root/'not_a_real_solver.exe',
            output_root=self.output, progress_log=self.root/'progress.log',
            api_config=self.root/'not_real_api_config.json', problem_file=self.problem_file,
            key_environment_variable='DISK_ADAPTER_TEST_UNUSED_KEY', plot_on_complete=False)

    def save_run(self, name='run_test', done=1, target=4, status='complete', baseline=True, directory=None):
        run = directory or self.output/name
        run.mkdir(parents=True, exist_ok=True)
        (run/'problem.json').write_text(json.dumps(self.problem), encoding='utf-8')
        x = [v['reference'] for v in self.problem['variables']]
        details = []
        for i in range(done + int(baseline)):
            changed = list(x)
            changed[0] = round(changed[0] + 0.0001*i, 4)
            details.append({'xActual': changed, 'stressMPa': 700.0 - 10*i,
                            'displacementMM': 0.2 + 0.001*i, 'area': 0.01,
                            'runDir': str(run/'evaluations'/f'eval_{i}'),
                            'nativeReportFile': str(run/'evaluations'/f'eval_{i}'/'mechanical_verified.json')})
        history = [tools.controller.observation(detail) for detail in details]
        state = {'schema': tools.controller.SCHEMA, 'controller':'python', 'runDir':str(run),
                 'status':status, 'nCompleted':done, 'nLLM':target,
                 'baseline': details[0] if baseline else None,
                 'history':history, 'records':[{'detail':d} for d in details[int(baseline):]],
                 'pending':None}
        (run/'checkpoint.json').write_text(json.dumps(state), encoding='utf-8')
        return run, state

    def set_latest(self, run):
        (self.output/'latest_run.json').write_text(json.dumps({'runDir':str(run)}), encoding='utf-8')


class TaskSchemaTests(unittest.TestCase):
    def test_round_trip_task_and_dataclass_revalidation(self):
        task = TaskSpec.from_dict({'operation':'optimize', 'total_candidate_budget':4})
        self.assertEqual(TaskSpec.from_dict(task), task)
        self.assertEqual(task.case_id, CASE_ID)
        self.assertNotIn('run_id', task.to_dict())
        with self.assertRaises(TaskValidationError):
            TaskSpec.from_dict(TaskSpec('optimize', total_candidate_budget=True))

    def test_rejects_invalid_task_matrix(self):
        invalid = [None, [], {}, {'operation':'delete'},
                   {'operation':'check','command':'launch something'},
                   {'operation':'check','case_id':'another_case'},
                   {'operation':'check','objective':'minimize_mass'},
                   {'operation':'check','schema_version':'future-version'},
                   {'operation':'check','run_id':'run_test'},
                   {'operation':'status','total_candidate_budget':1},
                   {'operation':'optimize'},
                   {'operation':'optimize','total_candidate_budget':4,'additional_candidate_budget':1,'run_id':'run_test'},
                   {'operation':'optimize','additional_candidate_budget':2}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(TaskValidationError):
                TaskSpec.from_dict(value)

    def test_budget_rejects_bool_float_string_and_nonpositive(self):
        for key in ('total_candidate_budget','additional_candidate_budget'):
            for value in (True,False,0,-1,1.0,'1'):
                request={'operation':'optimize',key:value}
                if key.startswith('additional'):
                    request['run_id']='run_test'
                with self.subTest(key=key,value=value), self.assertRaises(TaskValidationError):
                    TaskSpec.from_dict(request)

    def test_run_references_must_be_names_not_paths(self):
        for value in ('../run_test','..\\run_test','C:\\run_test','/tmp/run_test','run_test/file','checkpoint.mat',42):
            with self.subTest(value=value), self.assertRaises(TaskValidationError):
                TaskSpec.from_dict({'operation':'status','run_id':value})


class ValidationTests(FixtureCase):
    def test_invalid_task_is_rejected_before_loading_environment(self):
        with patch.object(validation,'load_settings',side_effect=AssertionError('must not inspect settings')) as load:
            result=tools.execute_task({'operation':'optimize','total_candidate_budget':True})
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'BUDGET')
        load.assert_not_called()

    def test_new_budget_is_total_candidates_and_does_not_change_original_settings(self):
        result=validation.validate_task({'operation':'optimize','total_candidate_budget':4}, settings=self.settings)
        self.assertEqual(result.settings.candidate_count,4)
        self.assertEqual(result.target_total_candidates,4)
        self.assertEqual(result.completed_candidates,0)
        self.assertIsNone(result.run_dir)
        self.assertEqual(self.settings.candidate_count,10)

    def test_additional_budget_uses_completed_plus_delta_and_checks_compatibility(self):
        run,_=self.save_run(done=3,target=10)
        with patch.object(tools.controller,'restore_state',return_value={'nCompleted':3}) as restore:
            normalized=validation.validate_task({'operation':'optimize','run_id':run.name,'additional_candidate_budget':2},settings=self.settings)
        self.assertEqual(normalized.target_total_candidates,5)
        self.assertEqual(normalized.settings.candidate_count,5)
        self.assertEqual(restore.call_args.args[0].candidate_count,5)
        self.assertEqual(restore.call_args.args[1],str(run))

    def test_insufficient_or_reached_total_is_rejected_before_restore(self):
        run,_=self.save_run(done=3,target=10)
        for budget in (1,3):
            with self.subTest(budget=budget), patch.object(tools.controller,'restore_state') as restore:
                result=tools.execute_task({'operation':'optimize','run_id':run.name,'total_candidate_budget':budget},settings=self.settings)
                self.assertFalse(result.ok)
                self.assertEqual(result.error['code'],'BUDGET_REACHED')
                restore.assert_not_called()

    def test_incompatible_checkpoint_is_rejected_before_authentication(self):
        run,_=self.save_run(done=1,target=4)
        with patch.object(tools.controller,'restore_state',side_effect=RuntimeError('fingerprint changed')), \
             patch.object(tools.runtime,'api_key',side_effect=AssertionError('no authentication')) as auth:
            result=tools.execute_task({'operation':'optimize','run_id':run.name,'additional_candidate_budget':1},live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertIn('fingerprint changed',result.error['message'])
        auth.assert_not_called()

    def test_latest_pointer_cannot_escape_output_root(self):
        outside=self.root/'elsewhere'/'run_escape'
        self.save_run(directory=outside)
        self.set_latest(outside)
        result=tools.get_run_status(settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_PATH')

    def test_symlink_escape_is_rejected_after_resolve(self):
        outside=self.root/'outside'/'run_destination'
        self.save_run(directory=outside)
        link=self.output/'run_link'
        try:
            link.symlink_to(outside,target_is_directory=True)
        except OSError as exc:
            self.skipTest('Symlink creation is not available: '+str(exc))
        result=tools.get_run_status('run_link',settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_PATH')

    def test_checkpoint_directory_mismatch_is_rejected(self):
        run,state=self.save_run()
        state['runDir']=str(self.output/'run_someone_else')
        (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
        result=tools.get_run_status(run.name,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_REFERENCE')

    def test_status_rejects_another_checkpoint_schema(self):
        run,state=self.save_run()
        state['schema']='unrelated.run.v1'
        (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
        result=tools.get_run_status(run.name,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'CHECKPOINT_SCHEMA')

    def test_completed_candidates_require_a_baseline(self):
        run,_=self.save_run(done=1,target=4,baseline=False)
        result=tools.get_run_status(run.name,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'CHECKPOINT_RESULT')

    def test_status_rejects_missing_geometry_even_when_observation_and_detail_agree(self):
        run,state=self.save_run()
        state['history'][0]['parameters']=None
        state['baseline']['xActual']=None
        (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
        result=tools.get_run_status(run.name,settings=self.settings)
        self.assertFalse(result.ok)

    def test_status_rejects_invalid_observation_numbers(self):
        cases=[('stress_mpa','stressMPa',0),('stress_mpa','stressMPa',-1),
               ('stress_mpa','stressMPa',float('nan')),
               ('stress_mpa','stressMPa',float('inf')),
               ('stress_mpa','stressMPa','not a number'),
               ('displacement_mm','displacementMM',-0.1),
               ('area_m2','area',0)]
        for i,(history_key,detail_key,value) in enumerate(cases):
            with self.subTest(field=history_key,value=value):
                run,state=self.save_run(name=f'run_invalid_number_{i}')
                state['history'][0][history_key]=value
                state['baseline'][detail_key]=value
                (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
                result=tools.get_run_status(run.name,settings=self.settings)
                self.assertFalse(result.ok)

    def test_status_rejects_boolean_checkpoint_counts(self):
        run,state=self.save_run()
        state['nCompleted']=True
        (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
        result=tools.get_run_status(run.name,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'CHECKPOINT_COUNTS')


class ToolTests(FixtureCase):
    def test_optimize_defaults_to_plan_without_api_or_solver(self):
        with patch.object(tools.entry,'check_environment',side_effect=AssertionError('no live preflight')) as preflight, \
             patch.object(tools.runtime,'api_key',side_effect=AssertionError('no auth')) as auth, \
             patch.object(tools.controller,'run',side_effect=AssertionError('no controller run')) as run, \
             patch.object(tools,'evaluate_native',side_effect=AssertionError('no solver')) as solver:
            result=tools.run_optimization({'operation':'optimize','total_candidate_budget':4},settings=self.settings)
        self.assertTrue(result.ok)
        self.assertEqual(result.status,'validated')
        self.assertEqual(result.data['additional_candidates_to_complete'],4)
        self.assertEqual(result.data['baseline_additional'],1)
        self.assertFalse(result.data['live_execution_started'])
        for call in (preflight,auth,run,solver): call.assert_not_called()

    def test_status_returns_committed_facts_without_execution_or_resume(self):
        run,state=self.save_run(done=2,target=4,status='interrupted')
        self.set_latest(run)
        with patch.object(tools.entry,'check_environment',side_effect=AssertionError('no preflight')), \
             patch.object(tools.controller,'restore_state',side_effect=AssertionError('not resuming')), \
             patch.object(tools.controller,'run',side_effect=AssertionError('not running')), \
             patch.object(tools.runtime,'api_key',side_effect=AssertionError('no auth')), \
             patch.object(tools,'evaluate_native',side_effect=AssertionError('no solver')):
            result=tools.get_run_status(settings=self.settings)
        self.assertTrue(result.ok)
        self.assertEqual(result.status,'status_read')
        self.assertEqual(result.data['run_status'],'interrupted')
        self.assertEqual(result.data['completed_candidates'],2)
        self.assertEqual(result.data['best']['stress_mpa'],680)
        self.assertAlmostEqual(result.data['best']['reduction_percent'],20/700*100)
        self.assertFalse(result.data['native_records_reverified_by_adapter'])
        self.assertEqual(result.data['sources']['checkpoint'],str(run/'checkpoint.json'))
        self.assertNotIn('api_config',result.data)

    def test_read_case_returns_declarations_not_reverified_physics(self):
        result=tools.read_case(settings=self.settings)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data['variables']),9)
        self.assertFalse(result.data['physics_reverified_by_this_read'])
        self.assertEqual(result.data['objective']['direction'],'minimize')
        self.assertEqual(result.data['solver_execution'],'serial')

    def test_check_returns_readonly_environment_report(self):
        report=dict(python_version='3.10',api_key_configured=True,api_requests_made=0,
                    finite_element_solves_made=0,search_strategy='center_surrogate',initial_lhs=0,
                    batch_size=4,pool_size=32,surrogate_min_samples=24)
        with patch.object(tools.entry,'check_environment',return_value=report) as preflight, \
             patch.object(tools.controller,'run',side_effect=AssertionError('no controller run')):
            result=tools.execute_task({'operation':'check'},settings=self.settings)
        self.assertTrue(result.ok)
        self.assertEqual(result.status,'checked')
        self.assertEqual(result.data['environment']['finite_element_solves_made'],0)
        preflight.assert_called_once_with(self.settings)

    def live_mocks(self, events, run, *, failure=None, plot_failure=False):
        @contextmanager
        def auth(settings,target):
            events.append('auth_enter')
            self.assertEqual(target,'UNUSED_TEST_KEY')
            try: yield
            finally: events.append('auth_exit')
        def controller_run(settings,resume,*,evaluator):
            events.append('controller')
            self.assertEqual(settings.candidate_count,1)
            self.assertIsNone(resume)
            if failure: raise failure
            return run
        def plot(run_dir):
            events.append('plot')
            if plot_failure: raise RuntimeError('plot unavailable')
            return [run_dir/'plots'/'convergence.png']
        return [patch.object(tools.entry,'check_environment',side_effect=lambda s: events.append('preflight')),
                patch.object(tools.runtime.bridge,'load_config',return_value={'api_key_env':'UNUSED_TEST_KEY'}),
                patch.object(tools.runtime,'api_key',side_effect=auth),
                patch.object(tools.controller,'run',side_effect=controller_run),
                patch.object(tools,'plot_history',side_effect=plot),
                patch.object(tools,'evaluate_native',side_effect=AssertionError('real solver forbidden'))]

    def test_live_preserves_preflight_auth_run_and_plot_order(self):
        from contextlib import ExitStack
        run,_=self.save_run(done=1,target=1)
        events=[]
        with ExitStack() as stack:
            for p in self.live_mocks(events,run): stack.enter_context(p)
            result=tools.execute_task({'operation':'optimize','total_candidate_budget':1},live=True,
                                      settings=replace(self.settings,plot_on_complete=True))
        self.assertTrue(result.ok,result.error)
        self.assertEqual(events,['preflight','auth_enter','controller','auth_exit','plot'])
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.data['run_id'],run.name)

    def test_plot_failure_is_warning_after_successful_commit(self):
        from contextlib import ExitStack
        run,_=self.save_run(done=1,target=1)
        with ExitStack() as stack:
            for p in self.live_mocks([],run,plot_failure=True): stack.enter_context(p)
            result=tools.execute_task({'operation':'optimize','total_candidate_budget':1},live=True,
                                      settings=replace(self.settings,plot_on_complete=True))
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.status,'completed')
        self.assertEqual(len(result.warnings),1)
        self.assertIn('plot unavailable',result.warnings[0])

    def test_failed_new_call_does_not_claim_an_unrelated_latest_run(self):
        from contextlib import ExitStack
        unrelated,_=self.save_run(name='run_unrelated',done=1,target=1)
        self.set_latest(unrelated)
        events=[]
        with ExitStack() as stack:
            for p in self.live_mocks(events,unrelated,failure=RuntimeError('controller failed before evaluation')):
                stack.enter_context(p)
            result=tools.execute_task({'operation':'optimize','total_candidate_budget':1},live=True,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.data,{})
        self.assertEqual(events,['preflight','auth_enter','controller','auth_exit'])

    def test_live_flag_and_process_busy_guard(self):
        task={'operation':'optimize','total_candidate_budget':1}
        self.assertEqual(tools.execute_task(task,live='false',settings=self.settings).error['code'],'LIVE_FLAG')
        acquired=tools._LIVE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with patch.object(tools.entry,'check_environment',side_effect=AssertionError('must reject first')):
                result=tools.execute_task(task,live=True,settings=self.settings)
        finally: tools._LIVE_LOCK.release()
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'BUSY')

    def test_direct_tool_rejects_nonoptimization_task(self):
        result=tools.run_optimization({'operation':'check'},settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'OPERATION')


class CliTests(FixtureCase):
    def test_cli_keeps_operational_logging_out_of_json_stdout(self):
        from agent_app.__main__ import main
        task=self.root/'task.json'
        task.write_text('{"operation":"check"}',encoding='utf-8')
        def fake(*args,**kwargs):
            print('operational progress')
            return ToolResult(True,'check','checked',{'count':0})
        out,err=io.StringIO(),io.StringIO()
        with patch('agent_app.__main__.execute_task',side_effect=fake),redirect_stdout(out),redirect_stderr(err):
            code=main(['--task',str(task)])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out.getvalue())['status'],'checked')
        self.assertIn('operational progress',err.getvalue())

    def test_cli_invalid_json_is_structured_failure(self):
        from agent_app.__main__ import main
        task=self.root/'invalid.json'
        task.write_text('{ invalid json',encoding='utf-8')
        out=io.StringIO()
        with redirect_stdout(out): code=main(['--task',str(task)])
        self.assertEqual(code,1)
        self.assertFalse(json.loads(out.getvalue())['ok'])


if __name__=='__main__':
    unittest.main()
