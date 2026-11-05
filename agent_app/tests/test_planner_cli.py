"""Natural-language CLI dispatch tests with no real provider or solver calls."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from agent_app import __main__ as cli
from agent_app import planner, tools, validation
from agent_app.schemas import ToolResult
from agent_app.tests.test_contract import FixtureCase


PROJECT = Path(__file__).resolve().parents[2]
TEST_PYTHON = Path(r'C:\Users\18940\.conda\envs\llm_opt\python.exe')


class NaturalLanguageCliDispatchTests(unittest.TestCase):
    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(args)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    @contextmanager
    def dispatchers(self):
        with patch.object(cli, 'execute_request', return_value=ToolResult(
                True, 'optimize', 'validated', {'text': '中文任务'})) as request, \
             patch.object(cli, 'plan_task', return_value=ToolResult(
                True, 'plan', 'planned', {'text': '只生成计划'})) as plan, \
             patch.object(cli, 'execute_task', return_value=ToolResult(
                True, 'check', 'checked')) as task, \
             patch.object(cli, 'read_case', return_value=ToolResult(
                True, 'read_case', 'case_read')) as case:
            yield {'request': request, 'plan': plan, 'task': task, 'case': case}

    def test_request_defaults_to_offline_planning_and_no_live_execution(self):
        text = '新建优化四个候选'
        with self.dispatchers() as calls:
            code, out, _ = self.invoke(['--request', text])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'validated')
        calls['request'].assert_called_once_with(text, live=False, use_llm=False,
                                                context_run_id=None)
        for key in ('plan', 'task', 'case'):
            calls[key].assert_not_called()

    def test_planner_and_optimizer_live_flags_are_independent(self):
        text = '新建优化四个候选'
        combinations = [([], False, False),
                        (['--planner-live'], True, False),
                        (['--live'], False, True),
                        (['--planner-live', '--live'], True, True)]
        for flags, planner_live, solver_live in combinations:
            with self.subTest(flags=flags), self.dispatchers() as calls:
                code, out, _ = self.invoke(['--request', text, *flags])
                self.assertEqual(code, 0)
                self.assertTrue(json.loads(out)['ok'])
                calls['request'].assert_called_once_with(
                    text, live=solver_live, use_llm=planner_live, context_run_id=None)
                calls['plan'].assert_not_called()
                calls['task'].assert_not_called()

    def test_run_id_is_forwarded_as_explicit_request_context(self):
        text, run_id = '继续追加二十个候选', 'run_existing_2026'
        with self.dispatchers() as calls:
            code, _, _ = self.invoke(['--request', text, '--run-id', run_id])
        self.assertEqual(code, 0)
        calls['request'].assert_called_once_with(text, live=False, use_llm=False,
                                                context_run_id=run_id)

    def test_plan_only_calls_planner_without_task_executor(self):
        text = '新建优化四个候选'
        with self.dispatchers() as calls:
            code, out, _ = self.invoke(['--request', text, '--plan-only'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'planned')
        calls['plan'].assert_called_once_with(text, use_llm=False, context_run_id=None)
        for key in ('request', 'task', 'case'):
            calls[key].assert_not_called()

    def test_plan_only_can_enable_planning_api_without_solver_live(self):
        text, run_id = '继续追加二十个候选', 'run_existing_2026'
        with self.dispatchers() as calls:
            code, _, _ = self.invoke(['--request', text, '--plan-only',
                                     '--planner-live', '--run-id', run_id])
        self.assertEqual(code, 0)
        calls['plan'].assert_called_once_with(text, use_llm=True, context_run_id=run_id)
        calls['request'].assert_not_called()
        calls['task'].assert_not_called()

    def test_invalid_mode_and_flag_combinations_do_not_dispatch(self):
        invalid = [
            [],
            ['--planner-live'],
            ['--run-id', 'run_existing'],
            ['--plan-only'],
            ['--case', '--planner-live'],
            ['--case', '--run-id', 'run_existing'],
            ['--case', '--plan-only'],
            ['--task', 'unused.json', '--planner-live'],
            ['--task', 'unused.json', '--run-id', 'run_existing'],
            ['--task', 'unused.json', '--plan-only'],
            ['--request', '优化四个候选', '--plan-only', '--live'],
            ['--request', '优化四个候选', '--case'],
            ['--request', '优化四个候选', '--task', 'unused.json'],
            ['--case', '--task', 'unused.json'],
        ]
        for args in invalid:
            with self.subTest(args=args), self.dispatchers() as calls:
                code, out, err = self.invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('error:', err)
                for mock in calls.values():
                    mock.assert_not_called()

    def test_case_mode_still_reads_case_instead_of_planning(self):
        with self.dispatchers() as calls:
            code, out, _ = self.invoke(['--case'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'case_read')
        calls['case'].assert_called_once_with()
        calls['request'].assert_not_called()
        calls['plan'].assert_not_called()

    def test_json_task_mode_still_uses_task_executor(self):
        task = {'operation': 'check'}
        with self.dispatchers() as calls, patch.object(cli.bridge, 'read_json', return_value=task):
            code, out, _ = self.invoke(['--task', 'task.json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'checked')
        calls['task'].assert_called_once_with(task, live=False)
        calls['request'].assert_not_called()
        calls['plan'].assert_not_called()

    def test_request_progress_is_stderr_and_stdout_is_one_unicode_json(self):
        def fake_request(text, **kwargs):
            print('正在解析中文任务')
            return ToolResult(True, 'optimize', 'validated', {'message': '预算四个候选'})

        with patch.object(cli, 'execute_request', side_effect=fake_request):
            code, out, err = self.invoke(['--request', '优化四个候选'])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result['data']['message'], '预算四个候选')
        self.assertIn('预算四个候选', out)
        self.assertNotIn('正在解析中文任务', out)
        self.assertIn('正在解析中文任务', err)

    def test_plan_only_progress_is_stderr_and_stdout_is_one_json(self):
        def fake_plan(text, **kwargs):
            print('规划日志')
            return ToolResult(True, 'plan', 'planned', {'message': '任务单已生成'})

        with patch.object(cli, 'plan_task', side_effect=fake_plan):
            code, out, err = self.invoke(['--request', '优化四个候选', '--plan-only'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'planned')
        self.assertNotIn('规划日志', out)
        self.assertIn('规划日志', err)

    def test_request_exception_is_a_structured_failure(self):
        with patch.object(cli, 'execute_request', side_effect=RuntimeError('中文解析错误')):
            code, out, _ = self.invoke(['--request', '优化四个候选'])
        result = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error']['code'], 'RuntimeError')
        self.assertEqual(result['error']['message'], '中文解析错误')

    def test_clarification_result_returns_failure_exit_code_without_execution(self):
        response = ToolResult(False, 'plan', 'clarification_needed',
                              {'questions': ['本次需要多少个候选？']},
                              {'code': 'CLARIFICATION_REQUIRED', 'message': '缺少预算'})
        with patch.object(cli, 'execute_request', return_value=response), \
             patch.object(cli, 'execute_task') as execute:
            code, out, _ = self.invoke(['--request', '帮我优化'])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['status'], 'clarification_needed')
        execute.assert_not_called()

    def test_interrupted_result_keeps_existing_exit_code(self):
        response = ToolResult(False, 'optimize', 'interrupted',
                              error={'code': 'INTERRUPTED', 'message': '已中断'})
        with patch.object(cli, 'execute_request', return_value=response):
            code, out, _ = self.invoke(['--request', '优化四个候选'])
        self.assertEqual(code, 130)
        self.assertEqual(json.loads(out)['status'], 'interrupted')


class NaturalLanguageCliReadOnlyIntegrationTests(FixtureCase):
    def test_default_natural_language_dispatch_is_real_but_api_and_solver_stay_unused(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(validation, 'load_settings', return_value=self.settings), \
             patch.object(planner.runtime.bridge, 'Client', side_effect=AssertionError('API forbidden')) as provider, \
             patch.object(planner.runtime, 'api_key', side_effect=AssertionError('authentication forbidden')) as auth, \
             patch.object(tools.controller, 'run', side_effect=AssertionError('solver forbidden')) as engine, \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--request', '新建优化四个候选'])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'validated')
        self.assertFalse(result['data']['live_execution_started'])
        self.assertEqual(result['data']['target_total_candidates'], 4)
        self.assertEqual(result['data']['planning']['planner_mode'], 'rules')
        provider.assert_not_called()
        auth.assert_not_called()
        engine.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_textual_plan_only_stops_before_executor_even_with_cli_live_flag(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(validation, 'load_settings', return_value=self.settings), \
             patch.object(planner, 'execute_task', side_effect=AssertionError('executor forbidden')) as executor, \
             patch.object(planner.runtime.bridge, 'Client', side_effect=AssertionError('API forbidden')) as provider, \
             redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--request', '新建优化四个候选，只生成计划', '--live'])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result['status'], 'planned')
        self.assertTrue(result['data']['plan_only'])
        executor.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [])

    @unittest.skipUnless(TEST_PYTHON.is_file(), 'Configured llm_opt Python is unavailable')
    def test_unicode_subprocess_smoke_overrides_cp1252_without_real_execution(self):
        # The real CLI and offline parser run in a child process. Explicit guards
        # ensure a regression cannot contact a provider or launch the engine.
        child = '''
import sys
from unittest.mock import patch
from agent_app.__main__ import main
with patch('agent_app.planner.runtime.bridge.Client', side_effect=AssertionError('API forbidden')), \\
     patch('agent_app.planner.runtime.api_key', side_effect=AssertionError('authentication forbidden')), \\
     patch('agent_app.tools.controller.run', side_effect=AssertionError('solver forbidden')):
    raise SystemExit(main(sys.argv[1:]))
'''
        env = dict(os.environ, PYTHONIOENCODING='cp1252', PYTHONDONTWRITEBYTECODE='1')
        completed = subprocess.run(
            [str(TEST_PYTHON), '-B', '-c', child, '--request', '新建优化四个候选，只生成计划'],
            cwd=PROJECT, env=env, capture_output=True, encoding='utf-8', timeout=30,
            check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['data']['task']['total_candidate_budget'], 4)
        self.assertEqual(result['data']['planner_mode'], 'rules')
        self.assertIsNone(result['data']['planning_log_dir'])
        self.assertIn('已生成任务单', completed.stdout)
        self.assertNotIn('UnicodeEncodeError', completed.stderr)


if __name__ == '__main__':
    unittest.main()
