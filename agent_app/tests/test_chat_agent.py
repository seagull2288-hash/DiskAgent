"""Conversation execution boundaries, durable requests and separate task queues."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

from agent_app.chat_agent import ChatService
from agent_app.chat_worker import ACTION_TOOLS, READ_TOOLS, action_arguments, preview_card, rule_plan, run_turn
from agent_app.schemas import TaskValidationError
from agent_app.tests.test_contract import FixtureCase


def tool_call(name, arguments, identifier='call_fixture'):
    return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': identifier, 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}]}


class Model:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append(deepcopy(messages))
        return next(self.messages), {'prompt_tokens': 30, 'completion_tokens': 10, 'total_tokens': 40}


class Client:
    def __init__(self, result):
        names = READ_TOOLS | ACTION_TOOLS | {'disk_validate_design'}
        self.tools = {name: {'name': name, 'inputSchema': {'type': 'object'}} for name in names}
        self.result, self.calls = result, []

    async def call(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return deepcopy(self.result)


class ChatTurnTests(unittest.TestCase):
    def run_turn(self, messages, result=None, **request):
        client = Client(result or {'ok': True, 'data': {'case_id': 'disk_2d_current'}})
        model = Model(messages)
        events = []
        asyncio.run(run_turn(dict(message='读取模型', context={}, **request), client, model, events.append))
        return client, model, events

    def test_model_can_discover_use_mcp_and_answer_from_returned_facts(self):
        client, model, events = self.run_turn([
            tool_call('disk_read_case', {}), {'role': 'assistant', 'content': '已读取当前轮盘。'}])
        self.assertEqual(client.calls, [('disk_read_case', {})])
        self.assertEqual(model.calls[1][-1]['role'], 'tool')
        self.assertIn('disk_2d_current', model.calls[1][-1]['content'])
        self.assertEqual(events[-1], {'type': 'answer', 'text': '已读取当前轮盘。'})
        self.assertEqual(events[0]['transport'], 'mcp_stdio')

    def test_model_cannot_grant_itself_live_execution(self):
        client, _, events = self.run_turn([tool_call('disk_evaluate_design', {'parameters': {}, 'live': True}),
                                          {'role': 'assistant', 'content': '请查看任务预览。'}])
        self.assertEqual(client.calls, [])
        self.assertTrue(any(event.get('result', {}).get('error', {}).get('code') == 'CHAT_TOOL' for event in events))

    def test_validated_dimensions_are_frozen_and_bound_policy_is_carried(self):
        policy = {'test': 'bound policy'}
        result = {'ok': True, 'status': 'validated', 'data': {
            'workbench_evaluation_tasks': 1, 'parameters': {'X1': 20.1235}, 'optimization_policy': policy}}
        client = Client(result)
        events = []
        model = Model([tool_call('disk_validate_design', {'parameters': {'X1': 20.123456}}),
                       {'role': 'assistant', 'content': '参数校验通过。'}])
        asyncio.run(run_turn({'message': '预览尺寸', 'context': {'optimization_policy': policy}},
                             client, model, events.append))
        self.assertEqual(client.calls[0][0], 'disk_evaluate_design')
        self.assertIs(client.calls[0][1]['live'], False)
        self.assertEqual(client.calls[0][1]['optimization_policy'], policy)
        preview = next(event['preview'] for event in events if event['type'] == 'preview')
        self.assertEqual(preview['arguments']['parameters']['X1'], 20.1235)
        self.assertEqual(preview['budget']['design_evaluation_tasks'], 1)

    def test_execution_submits_exact_approved_arguments_once_without_another_model_call(self):
        preview = {'preview_id': 'preview_fixed', 'tool': 'disk_evaluate_design',
                   'arguments': {'parameters': {'X1': 20}, 'optimization_policy': None}}
        before = deepcopy(preview)
        client = Client({'ok': True, 'job_id': 'job_' + '1' * 24})
        events = []
        asyncio.run(run_turn({'action': 'execute', 'approved_preview': preview,
                             'execution_request_id': 'chat_exec_stable'}, client, None, events.append))
        self.assertEqual(client.calls, [('disk_evaluate_design', dict(preview['arguments'],
                                                                     live=True, request_id='chat_exec_stable'))])
        self.assertEqual(preview, before)
        self.assertTrue(any(event['type'] == 'job_submitted' for event in events))
        self.assertFalse(any(event['type'] == 'model_started' for event in events))

    def test_budget_summary_distinguishes_candidates_baseline_and_remaining_workflow_budget(self):
        result = {'ok': True, 'status': 'validated', 'data': {
            'additional_candidates_to_complete': 3, 'baseline_additional': 1}}
        card = preview_card('disk_submit_optimization', {'task': {}}, result)
        self.assertEqual(card['budget'], {'additional_candidates': 3, 'baseline_evaluations': 1})
        result['data'].update(live_execution_started=True, preview={'remaining_candidates': 2, 'baseline_additional': 0})
        card = preview_card('disk_resume_workflow', {'workflow_id': 'wf_test'}, result)
        self.assertEqual(card['budget'], {'additional_candidates': 2, 'baseline_evaluations': 0})
        result['data']['preview']['remaining_candidates'] = 0
        self.assertIsNone(preview_card('disk_resume_workflow', {}, result))

    def test_failed_validation_and_unknown_tools_produce_no_executable_card(self):
        self.assertIsNone(preview_card('disk_evaluate_design', {}, {'ok': False}))
        client, _, events = self.run_turn([tool_call('execute_shell', {'command': 'ignored'}),
                                          {'role': 'assistant', 'content': '该操作无法处理。'}])
        self.assertEqual(client.calls, [])
        self.assertFalse(any(event['type'] == 'preview' for event in events))

    def test_rule_preview_uses_mcp_without_model_requests_and_freezes_resume_total(self):
        task = {'operation': 'optimize', 'run_id': 'run_saved', 'additional_candidate_budget': 2}
        plan = {'ok': True, 'tool': 'disk_submit_optimization', 'arguments': {'task': task, 'auto_recover': True}}
        client = Client({'ok': True, 'status': 'validated', 'data': {'task': task,
            'run_id': 'run_saved', 'target_total_candidates': 7,
            'additional_candidates_to_complete': 2, 'baseline_additional': 0}})
        events = []
        asyncio.run(run_turn({'message': '追加2个候选', 'rule_plan': plan}, client, None, events.append))
        self.assertIs(client.calls[0][1]['live'], False)
        preview = next(event['preview'] for event in events if event['type'] == 'preview')
        self.assertEqual(preview['arguments']['task']['total_candidate_budget'], 7)
        self.assertNotIn('additional_candidate_budget', preview['arguments']['task'])
        self.assertFalse(any(event['type'] == 'model_started' for event in events))

    def test_selected_policy_and_recovery_setting_override_model_arguments(self):
        from python_control.objectives import default_policy
        policy = default_policy()
        name, arguments = action_arguments('disk_submit_optimization', {'task': {
            'operation': 'optimize', 'total_candidate_budget': 2}, 'auto_recover': False},
            {'optimization_policy': policy, 'auto_recover': True})
        self.assertEqual(name, 'disk_submit_optimization')
        self.assertEqual(arguments['task']['optimization_policy'], policy)
        self.assertIs(arguments['auto_recover'], True)

    def test_job_summary_turn_reads_owned_job_before_asking_model(self):
        client, model, events = self.run_turn([{'role': 'assistant', 'content': '任务已完成。'}],
            result={'ok': True, 'job': {'status': 'completed', 'result': {'ok': True}}},
            action='job_result', job_id='job_' + '1' * 24)
        self.assertEqual(client.calls, [('disk_get_job', {'job_id': 'job_' + '1' * 24})])
        self.assertIn('completed', model.calls[0][-1]['content'])


class ChatServiceTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.service = ChatService(self.settings, 'http://127.0.0.1:9999', lambda _: None)
        self.service.python = Path(sys.executable)
        self.session_id = self.service.create()['session']['session_id']
        self.release = threading.Event()
        self.release.set()
        self.received = []
        worker_patch = patch.object(self.service, '_work', side_effect=self.fake_work)
        self.worker = worker_patch.start()
        self.addCleanup(worker_patch.stop)

    def tearDown(self):
        self.release.set()
        self.wait()
        super().tearDown()

    def fake_work(self, session_id, request_id, request, folder):
        self.release.wait(3)
        self.received.append(request)
        if request['action'] == 'message':
            self.service._event(session_id, request_id, {'type': 'preview', 'preview': {
                'preview_id': 'preview_test', 'tool': 'disk_evaluate_design',
                'arguments': {'parameters': {'X1': 20}, 'optimization_policy': None},
                'budget': {'design_evaluation_tasks': 1}}})
        else:
            self.service._event(session_id, request_id, {'type': 'job_submitted', 'job_id': 'job_' + '1' * 24,
                'workflow_id': None, 'request_id': request['execution_request_id']})
        self.service._event(session_id, request_id, {'type': 'answer', 'text': '本条处理完成。'})
        self.service._finish(session_id, request_id)

    def wait(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.service.snapshot(self.session_id)['session']
            if state['status'] != 'working':
                return state
            time.sleep(.01)
        self.fail('Conversation did not complete.')

    def test_same_message_is_idempotent_and_changed_arguments_conflict(self):
        request = {'request_id': 'message_one', 'message': '读取模型'}
        self.service.submit(self.session_id, request)
        self.wait()
        self.service.submit(self.session_id, request)
        self.assertEqual(self.worker.call_count, 1)
        with self.assertRaises(TaskValidationError) as raised:
            self.service.submit(self.session_id, dict(request, message='另一条消息'))
        self.assertEqual(raised.exception.code, 'CHAT_REQUEST_CONFLICT')

    def test_busy_chat_does_not_hold_engine_queue_and_rejects_another_message(self):
        self.release.clear()
        self.service.submit(self.session_id, {'request_id': 'message_one', 'message': '读取模型'})
        with self.assertRaises(TaskValidationError) as raised:
            self.service.submit(self.session_id, {'request_id': 'message_two', 'message': '再读一次'})
        self.assertEqual(raised.exception.code, 'CHAT_BUSY')
        self.release.set()

    def test_execution_uses_saved_preview_and_precommits_stable_request(self):
        self.service.submit(self.session_id, {'request_id': 'preview_one', 'message': '预览一次评价'})
        state = self.wait()
        preview = state['pending_preview']
        self.service.submit(self.session_id, {'request_id': 'execute_one', 'action': 'execute',
                                             'preview_id': preview['preview_id']})
        state = self.wait()
        request = self.received[-1]
        self.assertEqual(request['approved_preview']['arguments'], preview['arguments'])
        saved = self.service._read(self.session_id)['pending_preview']
        self.assertEqual(request['execution_request_id'], saved['execution_request_id'])
        self.assertEqual(len(state['jobs']), 1)
        self.service.submit(self.session_id, {'request_id': 'execute_again', 'action': 'execute',
                                             'preview_id': preview['preview_id']})
        self.assertEqual(self.worker.call_count, 2)
        with self.assertRaises(TaskValidationError):
            self.service.submit(self.session_id, {'request_id': 'tampered', 'action': 'execute',
                'preview_id': preview['preview_id'], 'parameters': {'X1': 25}})

    def test_next_message_receives_old_preview_as_context_and_invalidates_execute_button(self):
        self.service.submit(self.session_id, {'request_id': 'preview_one', 'message': '预览一次评价'})
        preview = self.wait()['pending_preview']
        self.service.submit(self.session_id, {'request_id': 'next_message', 'message': '将上组尺寸X1再增加0.2'})
        self.wait()
        self.assertEqual(self.received[-1]['context']['previous_preview']['arguments'], preview['arguments'])

    def test_clearing_run_binding_does_not_reuse_previous_run(self):
        self.save_run('run_bound')
        self.service.submit(self.session_id, {'request_id': 'bind_run', 'message': '读取模型',
                                             'context_run_id': 'run_bound'})
        self.wait()
        self.service.submit(self.session_id, {'request_id': 'clear_run', 'message': '读取模型',
                                             'context_run_id': None})
        self.wait()
        self.assertIsNone(self.received[-1]['context']['run_id'])

    def test_changed_preview_context_is_rejected_before_worker_submission(self):
        self.service.submit(self.session_id, {'request_id': 'preview_one', 'message': '预览一次评价',
                                             'auto_recover': True})
        preview = self.wait()['pending_preview']
        with self.assertRaises(TaskValidationError) as raised:
            self.service.submit(self.session_id, {'request_id': 'stale_settings', 'action': 'execute',
                'preview_id': preview['preview_id'], 'auto_recover': False})
        self.assertEqual(raised.exception.code, 'CHAT_PREVIEW_CONTEXT')
        self.assertEqual(self.worker.call_count, 1)

    def test_explicit_execute_current_preview_message_uses_the_frozen_action(self):
        self.service.submit(self.session_id, {'request_id': 'preview_one', 'message': '预览一次评价'})
        preview = self.wait()['pending_preview']
        self.service.submit(self.session_id, {'request_id': 'execute_by_text', 'message': '执行当前预览'})
        self.wait()
        self.assertEqual(self.received[-1]['action'], 'execute')
        self.assertEqual(self.received[-1]['approved_preview']['arguments'], preview['arguments'])

    def test_abandoned_owner_is_visible_and_paths_and_unbound_job_are_rejected(self):
        state = self.service._read(self.session_id)
        state.update(status='working', owner_instance='another-server')
        self.service._save(state)
        self.assertEqual(self.service.snapshot(self.session_id)['session']['status'], 'interrupted')
        with self.assertRaises(TaskValidationError):
            self.service.snapshot('../anything')
        with self.assertRaises(TaskValidationError):
            self.service.submit(self.session_id, {'request_id': 'other_job', 'action': 'job_result',
                'job_id': 'job_' + '1' * 24}, internal=True)

    def test_transient_windows_read_conflict_retries_the_same_saved_state(self):
        from python_control import runtime
        state = self.service._read(self.session_id)
        cause = OSError('temporary Windows sharing conflict')
        cause.winerror = 32
        error = runtime.bridge.BridgeError('FILE_READ', 'Cannot read UTF-8 JSON file.')
        error.__cause__ = cause
        with patch('agent_app.chat_agent.sys.platform', 'win32'), patch.object(runtime, 'read_json', side_effect=[error, state]) as read:
            recovered = self.service.snapshot(self.session_id)
        self.assertEqual(recovered['session']['session_id'], self.session_id)
        self.assertEqual(read.call_count, 2)


class RuleCommandTests(FixtureCase):
    def test_explicit_budget_uses_rules_and_complex_engineering_request_uses_model(self):
        plan = rule_plan('新建优化4个候选', self.settings, {'auto_recover': False})
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['arguments']['task']['total_candidate_budget'], 4)
        self.assertIs(plan['arguments']['auto_recover'], False)
        for text in ('分析推荐方案为什么应力降低', '将X1增加0.2 mm，预览一次评价',
                     '新建优化4个候选，应力不超过700 MPa'):
            self.assertIsNone(rule_plan(text, self.settings, {}))

    def test_unclear_budget_stays_a_clarification_without_model_or_live_work(self):
        for text in ('追加3个候选', '新建优化0个候选', '继续优化4个候选'):
            plan = rule_plan(text, self.settings, {})
            self.assertIs(plan['ok'], False, plan)
            self.assertTrue(plan['questions'])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_real_workflow_id_and_environment_commands_are_recognized(self):
        identifier = 'wf_20261109_160000_1234567890abcdef'
        plan = rule_plan('预览恢复工作流 ' + identifier, self.settings, {'auto_recover': True})
        self.assertEqual(plan['arguments']['workflow_id'], identifier)
        check = rule_plan('检查当前轮盘优化环境', self.settings, {})
        self.assertEqual(check['tool'], 'disk_check_environment')
