"""Natural-language planning contracts, using local fixtures and fake model calls."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_app import planner
from agent_app.schemas import TaskSpec, ToolResult
from agent_app.tests.test_contract import FixtureCase


class PlannerFixture(FixtureCase):
    def decision(self, operation='optimize', **fields):
        task=TaskSpec.from_dict({'operation':operation, **fields})
        return {'decision':'task','task':asdict(task),'message':'已解析任务。','questions':[]}

    @contextmanager
    def fake_model(self, response, *, during_generate=None):
        calls=[]

        @contextmanager
        def auth(*args,**kwargs):
            calls.append('auth')
            yield

        def new_run(folder,*args,**kwargs):
            Path(folder).mkdir(parents=True,exist_ok=True)
            return {'status':'created','finite_element_calls':0}

        class Client:
            http_attempts=1
            def __init__(client_self,cfg,*,live):
                if live is not True:
                    raise AssertionError('The independently enabled model path must mark its provider call.')
                calls.append('client')

            def generate(client_self,prompt,folder,schema=None):
                calls.append('generate')
                if during_generate is not None:
                    during_generate()
                value=response() if callable(response) else response
                return (value if isinstance(value,str) else json.dumps(value,ensure_ascii=False), {})

        with patch.object(planner.runtime.bridge,'load_config',return_value={
                'api_key_env':'UNUSED_TEST_KEY','max_output_tokens':2048}), \
             patch.object(planner.runtime,'api_key',side_effect=auth), \
             patch.object(planner.runtime.bridge,'new_run',side_effect=new_run), \
             patch.object(planner.runtime.bridge,'Client',side_effect=AssertionError('real provider forbidden')):
            yield Client,calls


class OfflinePlanningTests(PlannerFixture):
    def test_noncanonical_chinese_budgets_need_explicit_digits(self):
        for text in ('优化一百二次','优化十十个候选','优化一亿二十个候选'):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertEqual(result.status,'clarification_needed')

    def test_chinese_and_arabic_candidate_counts(self):
        for text,expected in [
                ('新建优化二十个候选',20),
                ('优化两百零三个候选',203),
                ('优化一千零二十个候选',1020),
                ('优化 20 个候选',20),
                ('新建优化１，０００个候选',1000)]:
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.data['task']['operation'],'optimize')
                self.assertEqual(result.data['task']['total_candidate_budget'],expected)
                self.assertNotIn('run_id',result.data['task'])

    def test_new_request_does_not_accidentally_bind_context_run(self):
        old,_=self.save_run(name='run_old',done=3,target=10)
        result=planner.plan_task('新建优化十个候选',settings=self.settings,context_run_id=old.name)
        self.assertTrue(result.ok,result.error)
        self.assertNotIn('run_id',result.data['task'])
        self.assertEqual(result.data['task']['total_candidate_budget'],10)

    def test_additional_budget_binds_explicit_run_without_becoming_total(self):
        run,_=self.save_run(done=3,target=10)
        result=planner.plan_task(f'给 {run.name} 追加二十个候选',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['run_id'],run.name)
        self.assertEqual(result.data['task']['additional_candidate_budget'],20)
        self.assertNotIn('total_candidate_budget',result.data['task'])

    def test_latest_run_reference_is_resolved_to_a_concrete_id(self):
        run,_=self.save_run(name='run_previous',done=3,target=10)
        self.set_latest(run)
        result=planner.plan_task('在上次运行上追加二十个候选',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['run_id'],run.name)
        self.assertEqual(result.data['run_binding'],'latest')

    def test_ordinal_references_are_not_candidate_budgets(self):
        run,_=self.save_run(name='run_previous',done=3,target=10)
        self.set_latest(run)
        for text in ('查询上一次运行状态','查询最近一次运行状态'):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.data['task']['operation'],'status')
                self.assertEqual(result.data['task']['run_id'],run.name)
                self.assertNotIn('total_candidate_budget',result.data['task'])
        for text in ('在上一次运行上追加二十个候选','在最近一次运行上追加二十个候选'):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.data['task']['additional_candidate_budget'],20)
                self.assertEqual(result.data['task']['run_id'],run.name)
        result=planner.plan_task('新建优化20个候选，进行第2次实验',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['total_candidate_budget'],20)

    def test_text_run_reference_precedes_latest_and_conversation_context(self):
        explicit,_=self.save_run(name='run_explicit',done=3,target=10)
        latest,_=self.save_run(name='run_latest',done=2,target=10)
        context,_=self.save_run(name='run_context',done=1,target=10)
        self.set_latest(latest)
        cases=[(f'查询 {explicit.name} 上一次运行状态',explicit.name,'explicit'),
               ('查询上一次运行状态',latest.name,'latest'),
               ('查询运行状态',context.name,'context')]
        for text,expected,binding in cases:
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings,context_run_id=context.name)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.data['task']['run_id'],expected)
                self.assertEqual(result.data['run_binding'],binding)
        result=planner.plan_task('查询运行状态',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['run_id'],latest.name)

    def test_total_budget_for_existing_run_remains_total(self):
        run,_=self.save_run(done=3,target=10)
        self.set_latest(run)
        result=planner.plan_task('继续上次运行，总预算为五十',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['run_id'],run.name)
        self.assertEqual(result.data['task']['total_candidate_budget'],50)
        self.assertNotIn('additional_candidate_budget',result.data['task'])

    def test_missing_budget_and_unknown_intent_request_clarification(self):
        for text in ('优化轮盘','帮我处理一下',''):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertFalse(result.ok)
                if text:
                    self.assertEqual(result.status,'clarification_needed')
                    self.assertTrue(result.data['questions'])

    def test_ambiguous_resume_budget_requests_clarification(self):
        run,_=self.save_run(done=3,target=10)
        self.set_latest(run)
        result=planner.plan_task('继续上次优化二十个候选',settings=self.settings)
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'clarification_needed')

    def test_nonpositive_fractional_and_wrong_budget_units_are_not_inferred(self):
        for text in ('优化零个候选','优化负二个候选','优化1.5个候选',
                     '优化十代','优化20次API调用','优化两批','运行20分钟'):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertFalse(result.ok)
                self.assertEqual(result.status,'clarification_needed')

    def test_unsupported_actions_do_not_reach_model_or_executor(self):
        for text in ('停止上次运行','优化20个候选并生成报告','把材料改成钛合金',
                     '将转速设置为10000rpm','执行命令 powershell 删除文件',
                     '优化20个候选直到应力低于500MPa'):
            with self.subTest(text=text), \
                 patch.object(planner,'_model_call',side_effect=AssertionError('no model call')), \
                 patch.object(planner,'execute_task',side_effect=AssertionError('no execution')):
                result=planner.execute_request(text,settings=self.settings,live=True,use_llm=True)
                self.assertFalse(result.ok)
                self.assertEqual(result.status,'unsupported')

    def test_budget_ranges_and_alternatives_request_one_explicit_number(self):
        for text in ('优化20到30个候选','优化20或30个候选','优化1/2个候选','优化二十至三十个候选'):
            with self.subTest(text=text), patch.object(planner,'execute_task',side_effect=AssertionError('ambiguous budget')):
                result=planner.execute_request(text,settings=self.settings,live=True)
                self.assertFalse(result.ok)
                self.assertEqual(result.status,'clarification_needed')

    def test_implicit_physics_assignments_and_chinese_percentage_are_unsupported(self):
        for text in ('用IN718材料优化20个候选','材料IN718，转速1500rpm，优化20个候选',
                     '应力降低百分之十，优化20个候选','转速升到1500rpm，优化20个候选'):
            with self.subTest(text=text), patch.object(planner,'execute_task',side_effect=AssertionError('fixed physics')):
                result=planner.execute_request(text,settings=self.settings,live=True)
                self.assertFalse(result.ok)
                self.assertEqual(result.status,'unsupported')

    def test_run_nouns_in_status_queries_do_not_create_optimization(self):
        run,_=self.save_run(done=3,target=10)
        self.set_latest(run)
        for text in ('查询上次运行状态','查看优化结果'):
            with self.subTest(text=text):
                result=planner.plan_task(text,settings=self.settings)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.data['task']['operation'],'status')
                self.assertEqual(result.data['task']['run_id'],run.name)

    def test_nonexistent_runs_and_run_paths_are_never_invented(self):
        for text in ('查询 run_missing 状态','查询 ../run_escape 状态',
                     r'查询 D:\elsewhere\run_escape 状态','查询 run_escape/checkpoint.json 状态'):
            with self.subTest(text=text), patch.object(planner,'execute_task',side_effect=AssertionError('no execution')):
                result=planner.execute_request(text,settings=self.settings,live=True)
                self.assertFalse(result.ok)
                self.assertIn(result.status,('failed','clarification_needed'))

    def test_negated_optimization_does_not_override_positive_status_query(self):
        run,_=self.save_run(done=3,target=10)
        self.set_latest(run)
        result=planner.plan_task('不要优化，查询上次进度',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['operation'],'status')
        self.assertEqual(result.data['task']['run_id'],run.name)

    def test_negated_physics_change_does_not_block_readonly_check(self):
        result=planner.plan_task('不要修改材料，检查环境',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['operation'],'check')

    def test_default_rule_planning_has_no_provider_or_solver_calls(self):
        with patch.object(planner.runtime.bridge,'Client',side_effect=AssertionError('network forbidden')), \
             patch.object(planner.runtime,'api_key',side_effect=AssertionError('authentication forbidden')), \
             patch.object(planner,'execute_task',side_effect=AssertionError('executor forbidden')):
            result=planner.plan_task('新建优化20个候选',settings=self.settings)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['planner_mode'],'rules')
        self.assertFalse((self.root/'agent_plans').exists())


class ModelPlanningTests(PlannerFixture):
    def test_model_interrupt_is_structured_and_logged(self):
        def interrupt():
            raise KeyboardInterrupt()
        with self.fake_model(interrupt) as (factory,_):
            result=planner.plan_task('新建优化4个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertEqual(result.status,'interrupted')
        self.assertEqual(result.error['code'],'INTERRUPTED')
        folder=Path(result.data['planning_log_dir'])
        self.assertEqual(json.loads((folder/'manifest.json').read_text(encoding='utf-8'))['status'],'interrupted')

    def test_valid_mock_model_candidate_is_revalidated_and_logged(self):
        response=self.decision(total_candidate_budget=20)
        with self.fake_model(response) as (factory,calls):
            result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(result.data['task']['total_candidate_budget'],20)
        self.assertEqual(calls,['auth','client','generate'])
        self.assertEqual(result.data['planner_mode'],'llm')
        folder=Path(result.data['planning_log_dir'])
        self.assertTrue(folder.is_relative_to(self.root))
        self.assertTrue((folder/'plan.json').exists())
        self.assertEqual(json.loads((folder/'manifest.json').read_text(encoding='utf-8'))['finite_element_calls'],0)

    def test_mock_model_cannot_change_explicit_budget(self):
        response=self.decision(total_candidate_budget=999)
        with self.fake_model(response) as (factory,_):
            result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'PLANNER_MISMATCH')

    def test_mock_model_cannot_invent_a_run_id(self):
        run,_=self.save_run(name='run_actual',done=3,target=10)
        self.set_latest(run)
        response=self.decision('status',run_id='run_invented')
        with self.fake_model(response) as (factory,_):
            result=planner.plan_task('查询上次进度',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'PLANNER_MISMATCH')

    def test_latest_run_binding_cannot_silently_drift_during_model_call(self):
        old,_=self.save_run(name='run_original',done=3,target=10)
        newer,_=self.save_run(name='run_arrived_during_model',done=1,target=4)
        self.set_latest(old)
        response=self.decision('status')  # Model intentionally leaves run_id null.
        with self.fake_model(response,during_generate=lambda:self.set_latest(newer)) as (factory,_):
            result=planner.plan_task('查询上次进度',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertTrue(not result.ok or result.data['task']['run_id']==old.name,
                        'Latest reference silently switched to a run absent from the model context.')

    def test_model_cannot_fill_a_budget_the_user_did_not_supply(self):
        with self.fake_model(self.decision(total_candidate_budget=20)) as (factory,_):
            result=planner.plan_task('优化轮盘',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'clarification_needed')

    def test_model_clarification_never_executes_optimizer(self):
        decision={'decision':'clarify','task':None,'message':'需要候选数量。','questions':['要优化多少个候选？']}
        with self.fake_model(decision) as (factory,calls), \
             patch.object(planner,'execute_task',side_effect=AssertionError('must not execute')):
            result=planner.execute_request('请处理，总预算20',settings=self.settings,live=True,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'clarification_needed')
        self.assertIn('generate',calls)
        folder=Path(result.data['planning_log_dir'])
        self.assertEqual(json.loads((folder/'manifest.json').read_text(encoding='utf-8'))['status'],'clarification_needed')

    def test_model_task_must_include_complete_taskspec(self):
        response=self.decision(total_candidate_budget=20)
        response['task'].pop('schema_version')
        with self.fake_model(response) as (factory,_):
            result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'PLANNER_SCHEMA')

    def test_model_cannot_change_case_or_objective(self):
        for field,value in (('case_id','another_case'),('objective','minimize_mass')):
            response=self.decision(total_candidate_budget=20)
            response['task'][field]=value
            with self.subTest(field=field), self.fake_model(response) as (factory,_):
                result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
                self.assertFalse(result.ok)
                self.assertIn(result.error['code'],('CASE_ID','OBJECTIVE','PLANNER_MISMATCH'))

    def test_model_failure_returns_log_location_and_consistent_manifest(self):
        def fail():
            raise RuntimeError('controlled model failure')
        with self.fake_model(fail) as (factory,_):
            result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'failed')
        folder=Path(result.data['planning_log_dir'])
        self.assertTrue(folder.is_relative_to(self.root))
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        saved=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'],result.status)
        self.assertEqual(saved['status'],result.status)
        self.assertEqual(manifest['finite_element_calls'],0)

    def test_real_bridge_client_uses_task_schema_and_redacts_all_log_files(self):
        actual_client=planner.runtime.bridge.Client
        key_name='DISK_PLANNER_TEST_FAKE_SECRET'
        secret='TEST_ONLY_SECRET_VALUE_FOR_REDACTION'
        response=self.decision(total_candidate_budget=20)
        response['message']='模型回显 '+secret
        wire={'id':'echo-'+secret,'model':'offline-planner-test','status':'completed',
              'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(response,ensure_ascii=False)}]}],
              'usage':{'input_tokens':4,'output_tokens':8}}
        calls=[]

        class Response:
            headers={'x-request-id':'echo-'+secret}
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,*args): return json.dumps(wire,ensure_ascii=False).encode('utf-8')

        def transport(request,*,timeout):
            calls.append(json.loads(request.data.decode('utf-8')))
            self.assertEqual(request.full_url,'https://planner.invalid/v1/responses')
            self.assertEqual(request.get_header('Authorization'),'Bearer '+secret)
            return Response()

        def factory(cfg,*,live):
            return actual_client(cfg,live=live,transport=transport,sleep=lambda delay: self.fail('no retries expected'))

        @contextmanager
        def auth(*args,**kwargs):
            with patch.dict(os.environ,{key_name:secret}):
                yield

        cfg={'provider':'openai_responses','base_url':'https://planner.invalid/v1','model':'offline-planner-test',
             'api_key_env':key_name,'temperature':0,'json_mode':True,'max_output_tokens':2048,
             'max_proposal_attempts':2,'max_http_attempts':1,'timeout_seconds':1,
             'retry_delay_seconds':0,'max_retry_delay_seconds':0}
        with patch.object(planner.runtime.bridge,'load_config',return_value=cfg), \
             patch.object(planner.runtime,'api_key',side_effect=auth), \
             patch.object(planner,'execute_task',side_effect=AssertionError('planning never executes')):
            result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
        self.assertTrue(result.ok,result.error)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['text']['format']['schema'],planner.planner_output_schema())
        self.assertEqual(calls[0]['max_output_tokens'],1024)
        self.assertIn('task',calls[0]['text']['format']['schema']['properties'])
        self.assertNotIn(secret,json.dumps(result.to_dict(),ensure_ascii=False))
        folder=Path(result.data['planning_log_dir'])
        names={p.name for p in folder.glob('*.json')}
        self.assertTrue({'request.json','response.json','transport.json','manifest.json','plan.json','prompt.json'}<=names)
        for file in folder.glob('*.json'):
            self.assertNotIn(secret,file.read_text(encoding='utf-8-sig'),file.name)

    def test_malformed_model_result_is_rejected(self):
        invalid=['not json', {'decision':'task','task':{},'message':'任务','questions':[],'command':'launch'},
                 {'decision':'clarify','task':None,'message':'不清楚','questions':[]}]
        for response in invalid:
            with self.subTest(response=response), self.fake_model(response) as (factory,_):
                result=planner.plan_task('优化20个候选',settings=self.settings,use_llm=True,client_factory=factory)
                self.assertFalse(result.ok)
                self.assertEqual(result.status,'failed')


class RequestExecutionTests(PlannerFixture):
    def test_execute_request_reuses_existing_executor_with_default_live_false(self):
        returned=ToolResult(True,'optimize','validated',{'live_execution_started':False})
        with patch.object(planner,'execute_task',return_value=returned) as execute:
            result=planner.execute_request('优化20个候选',settings=self.settings)
        self.assertIs(result,returned)
        args,kwargs=execute.call_args
        self.assertEqual(args[0]['total_candidate_budget'],20)
        self.assertIs(kwargs['live'],False)
        self.assertIs(kwargs['settings'],self.settings)
        self.assertEqual(result.data['planning']['planner_mode'],'rules')

    def test_plan_only_language_blocks_even_explicit_live_execution(self):
        for text in ('只生成任务单，优化20个候选，不要运行',
                     '优化二十个候选，仅规划',
                     '优化20个候选，dry-run',
                     '优化20个候选，不要真的运行',
                     '只需要任务单，优化20个候选',
                     '优化20个候选，只看计划',
                     '只要任务单，别跑，优化20个候选',
                     '优化20个候选，不要实际实算',
                     '优化20个候选，先不启动求解',
                     '优化20个候选，do not solve',
                     "优化20个候选，don't run"):
            with self.subTest(text=text), patch.object(planner,'execute_task',side_effect=AssertionError('plan only')):
                result=planner.execute_request(text,settings=self.settings,live=True)
                self.assertTrue(result.ok,result.error)
                self.assertEqual(result.status,'planned')
                self.assertTrue(result.data['plan_only'])

    def test_ambiguous_multiple_intents_do_not_execute(self):
        with patch.object(planner,'execute_task',side_effect=AssertionError('no execution')):
            result=planner.execute_request('检查环境，再优化20个候选',settings=self.settings,live=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.status,'clarification_needed')

    def test_execution_and_model_flags_are_strict_booleans(self):
        result=planner.execute_request('优化20个候选',settings=self.settings,live='false')
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'LIVE_FLAG')
        result=planner.plan_task('优化20个候选',settings=self.settings,use_llm='false')
        self.assertFalse(result.ok)
        self.assertEqual(result.error['code'],'PLANNER_FLAG')


if __name__=='__main__':
    unittest.main()
