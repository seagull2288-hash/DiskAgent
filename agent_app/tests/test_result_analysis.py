"""Measured-result, evidence and cost-accounting tests with no external calls."""
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

from agent_app import result_analysis as analysis
from agent_app.tests.test_contract import FixtureCase
from python_control import controller, runtime


class ResultAnalysisTests(FixtureCase):
    def test_checkpoint_change_during_read_requires_retry(self):
        run,state,_=self.run_fixture()
        changed=dict(state,status='running')
        with patch.object(analysis,'read_checkpoint',side_effect=[state,changed]):
            result=analysis.analyze_run(run.name,settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'RUN_CHANGED')

    def setUp(self):
        super().setUp()
        for item in (
            patch.object(runtime.bridge.Client,'generate',side_effect=AssertionError('no provider')),
            patch.object(controller,'run',side_effect=AssertionError('no optimizer')),
            patch('agent_app.tools.evaluate_native',side_effect=AssertionError('no Workbench')),
        ):
            item.start()
            self.addCleanup(item.stop)

    def run_fixture(self,done=2,*,baseline=True,status='complete'):
        run,state = self.save_run(done=done,target=max(done,4),baseline=baseline,status=status)
        details = ([state['baseline']] if baseline else []) + [r['detail'] for r in state['records']]
        for i,detail in enumerate(details):
            detail['xActual'][0] = round(self.problem['variables'][0]['reference']+i*.01,4)
        state['history'] = [controller.observation(d) for d in details]
        runtime.write_json(run/'checkpoint.json',state)
        return run,state,details

    def save(self,run,state,details):
        state['history'] = [controller.observation(d) for d in details]
        runtime.write_json(run/'checkpoint.json',state)

    def log(self,run,*,name='request_test',usage=None,transport=None,backup=None,direct=False):
        parent = (run/'iterations/iter_001/api' if direct else run/'search/generation_001/center_api')
        folder = parent/name/'proposal_01'
        folder.mkdir(parents=True)
        if usage is not None:
            runtime.write_json(folder/'response.json',{'usage':usage,'do_not_export':'private-provider-field'})
        if transport is not None:
            runtime.write_json(folder/'transport.json',transport)
        if backup is not None:
            runtime.write_json(folder/'answer.json',{'metadata':{'usage':backup}})
        return folder

    def test_metrics_report_area_tradeoff_and_window_best(self):
        run,state,details = self.run_fixture()
        details[0].update(stressMPa=1000.0,displacementMM=.5,area=.01)
        details[1].update(stressMPa=900.0,displacementMM=.4,area=.012)
        details[2].update(stressMPa=950.0,displacementMM=.45,area=.011)
        self.save(run,state,details)
        result = analysis.analyze_run(run.name,settings=self.settings,since_candidate=1)
        self.assertTrue(result.ok,result.to_dict())
        data=result.data
        self.assertEqual(data['best']['iteration'],1)
        self.assertAlmostEqual(data['best']['stress_reduction_from_baseline_percent'],10)
        self.assertAlmostEqual(data['best']['approximate_area_change_from_baseline_percent'],20)
        self.assertEqual(data['window']['completed_candidates_in_window'],1)
        self.assertEqual(data['window']['historical_best_improvement_mpa'],0)
        self.assertEqual(data['feedback']['causal_attribution'],'not_established')
        self.assertEqual(data['constraint_evaluation']['engineering_qualification'],'not_assessed')
        self.assertTrue(data['constraint_evaluation']['all_implemented_rules_passed'])
        report=analysis.render_report(data)
        self.assertIn('不等于质量',report)
        self.assertIn('寿命',report)

    def test_zero_baseline_displacement_has_no_percentage_division(self):
        run,state,details=self.run_fixture(done=1)
        details[0]['displacementMM']=0.0
        self.save(run,state,details)
        result=analysis.analyze_run(run.name,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        self.assertIsNone(result.data['current']['displacement_change_from_baseline_percent'])

    def test_original_center_log_is_counted_once_not_copied_per_candidate(self):
        run,_,_=self.run_fixture()
        usage={'prompt_tokens':2906,'completion_tokens':151,'total_tokens':3057,
               'prompt_tokens_details':{'cached_tokens':200}}
        self.log(run,usage=usage,backup=usage,transport=[{'status':'started'},
                 {'status':'success','elapsed_seconds':1.7}])
        for i in (1,2):
            runtime.write_json(run/f'iterations/iter_{i:03d}/response.json',{'usage':usage})
        result=analysis.analyze_run(run.name,settings=self.settings)
        cost=result.data['costs']['llm']
        self.assertEqual(cost['total_tokens'],3057)
        self.assertEqual(cost['request_log_count'],1)
        self.assertEqual(cost['observed_http_attempts'],1)
        self.assertEqual(cost['token_accounting_status'],'complete')
        self.assertNotIn('private-provider-field',json.dumps(result.to_dict()))

    def test_direct_calls_input_output_aliases_and_missing_total(self):
        run,_,_=self.run_fixture()
        self.log(run,direct=True,usage={'input_tokens':100,'output_tokens':20},
                 transport=[{'status':'started'},{'status':'success'}])
        cost=analysis.analyze_run(run.name,settings=self.settings).data['costs']['llm']
        self.assertEqual(cost['total_tokens'],120)
        self.assertEqual(cost['token_accounting_status'],'complete')

    def test_failed_http_attempts_are_not_zero_cost_and_not_counted_twice(self):
        run,_,_=self.run_fixture()
        self.log(run,transport=[{'status':'started'},{'status':'http_error','http_status':429},
                               {'status':'started'},{'status':'network_error_unknown_delivery'}])
        result=analysis.analyze_run(run.name,settings=self.settings)
        cost=result.data['costs']['llm']
        self.assertEqual(cost['observed_http_attempts'],2)
        self.assertEqual(cost['unknown_delivery_events'],1)
        self.assertEqual(cost['token_accounting_status'],'unknown')
        self.assertIsNone(cost['total_tokens'])
        self.assertTrue(result.warnings)

    def test_answer_usage_is_backup_and_incomplete_transport_is_reported(self):
        run,_,_=self.run_fixture()
        self.log(run,backup={'prompt_tokens':30,'completion_tokens':5,'total_tokens':35},
                 transport=[{'status':'started'}])
        result=analysis.analyze_run(run.name,settings=self.settings)
        cost=result.data['costs']['llm']
        self.assertEqual(cost['total_tokens'],35)
        self.assertEqual(cost['token_accounting_status'],'partial')

    def test_invalid_boolean_usage_never_becomes_a_numeric_token(self):
        run,_,_=self.run_fixture()
        self.log(run,usage={'prompt_tokens':True,'completion_tokens':'10','total_tokens':20},
                 transport=[{'status':'started'},{'status':'success'}])
        cost=analysis.analyze_run(run.name,settings=self.settings).data['costs']['llm']
        self.assertIsNone(cost['input_tokens'])
        self.assertIsNone(cost['output_tokens'])
        self.assertEqual(cost['token_accounting_status'],'partial')

    def test_no_api_logs_is_unknown_and_does_not_claim_free_optimization(self):
        run,_,_=self.run_fixture()
        cost=analysis.analyze_run(run.name,settings=self.settings).data['costs']['llm']
        self.assertEqual(cost['token_accounting_status'],'unknown')
        self.assertIsNone(cost['observed_http_attempts'])
        self.assertIsNone(cost['total_tokens'])

    def test_empty_failed_run_and_baseline_only_have_no_invented_candidate(self):
        run,state,details=self.run_fixture(done=0,baseline=False,status='failed')
        result=analysis.analyze_run(run.name,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        self.assertIsNone(result.data['best'])
        self.assertEqual(result.data['run_status'],'failed')
        self.assertEqual(result.data['candidates'],[])

    def test_engineering_missing_rules_stay_unknown(self):
        run,_,_=self.run_fixture()
        constraints=analysis.analyze_run(run.name,settings=self.settings).data['constraint_evaluation']
        self.assertEqual(constraints['allowable_stress_status'],'unknown')
        self.assertEqual(constraints['life_status'],'unknown')
        self.assertEqual(constraints['mass_status'],'unknown')

    def test_duplicate_committed_design_is_reported_as_rule_failure(self):
        run,state,details=self.run_fixture(done=1)
        details[1]['xActual']=list(details[0]['xActual'])
        self.save(run,state,details)
        result=analysis.analyze_run(run.name,settings=self.settings)
        self.assertTrue(result.ok,result.to_dict())
        self.assertFalse(result.data['constraint_evaluation']['all_implemented_rules_passed'])

    def test_native_evidence_is_optional_readonly_and_checked_when_requested(self):
        run,_,_=self.run_fixture()
        with patch.object(controller,'verify_native',return_value={}) as verifier:
            normal=analysis.analyze_run(run.name,settings=self.settings)
            verifier.assert_not_called()
            verified=analysis.analyze_run(run.name,settings=self.settings,verify_native=True)
        self.assertTrue(normal.ok,normal.to_dict())
        self.assertTrue(verified.ok,verified.to_dict())
        self.assertEqual(verifier.call_count,3)
        self.assertTrue(verified.data['evidence']['native_records_reverified'])

    def test_native_paths_cannot_escape_run_and_invalid_native_record_fails(self):
        run,state,details=self.run_fixture(done=1)
        details[1]['nativeReportFile']=str(self.root/'outside.json')
        self.save(run,state,details)
        with patch.object(controller,'verify_native',return_value={}):
            result=analysis.analyze_run(run.name,settings=self.settings,verify_native=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'EVIDENCE_PATH')

    def test_nonfinite_observations_and_invalid_windows_are_rejected(self):
        run,_,_=self.run_fixture()
        for value in (True,-1,3,1.0):
            with self.subTest(value=value):
                result=analysis.analyze_run(run.name,settings=self.settings,since_candidate=value)
                self.assertFalse(result.ok)
        state=runtime.read_json(run/'checkpoint.json')
        state['history'][0]['stress_mpa']=float('nan')
        (run/'checkpoint.json').write_text(json.dumps(state),encoding='utf-8')
        self.assertFalse(analysis.analyze_run(run.name,settings=self.settings).ok)

    def test_workbench_counts_are_tasks_and_failed_attempts_remain_visible(self):
        run,_,_=self.run_fixture(done=1)
        for name,success in (('eval_a',True),('eval_b',True),('eval_failed',False)):
            folder=run/'evaluations'/name
            runtime.write_json(folder/'evaluation_request.json',{'x':[]})
            if success:
                runtime.write_json(folder/'evaluation.json',{'fixture':True})
        cost=analysis.analyze_run(run.name,settings=self.settings).data['costs']['workbench']
        self.assertEqual(cost['committed_evaluation_tasks'],2)
        self.assertEqual(cost['started_evaluation_tasks'],3)
        self.assertEqual(cost['successful_evaluation_files'],2)

