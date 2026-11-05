"""CLI compatibility and opt-in workflow/app routing, without external work."""
from contextlib import redirect_stdout,redirect_stderr
import io
import json
from unittest import TestCase
from unittest.mock import patch

from agent_app import __main__ as cli
from agent_app.schemas import ToolResult


class WorkflowCliTests(TestCase):
    def test_app_mode_starts_local_service_without_submitting_a_workflow(self):
        with patch('agent_app.web.serve') as serve,patch.object(cli,'run_workflow') as workflow:
            code,_,_=self.invoke(['--app','--port','8766'])
        self.assertEqual(code,0)
        serve.assert_called_once_with(port=8766)
        workflow.assert_not_called()

    def invoke(self,args):
        out,err=io.StringIO(),io.StringIO()
        with redirect_stdout(out),redirect_stderr(err):
            try:
                code=cli.main(args)
            except SystemExit as exc:
                code=exc.code
        return code,out.getvalue(),err.getvalue()

    def test_workflow_request_uses_supervisor_and_is_preview_by_default(self):
        with patch.object(cli,'run_workflow',return_value=ToolResult(True,'workflow','validated')) as start:
            code,out,_=self.invoke(['--request','新建优化6个候选','--workflow'])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(out)['status'],'validated')
        start.assert_called_once_with('新建优化6个候选',live=False,use_llm=False,context_run_id=None)

    def test_json_workflow_preserves_task_and_live_authorization(self):
        task={'operation':'optimize','total_candidate_budget':4}
        with patch.object(cli.bridge,'read_json',return_value=task), \
                patch.object(cli,'run_workflow',return_value=ToolResult(True,'workflow','completed')) as start:
            code,_,_=self.invoke(['--task','unused.json','--workflow','--live'])
        self.assertEqual(code,0)
        start.assert_called_once_with(task=task,live=True)

    def test_resume_workflow_never_inherits_live_implicitly(self):
        with patch.object(cli,'resume_workflow',return_value=ToolResult(True,'workflow','validated')) as resume:
            code,_,_=self.invoke(['--resume-workflow','wf_existing'])
        self.assertEqual(code,0)
        resume.assert_called_once_with('wf_existing',live=False)

    def test_history_analysis_verify_flag_and_workflow_status_are_readonly_modes(self):
        for args,name,expected in (
            (['--analyze-run','run_existing','--verify-native'],'analyze_run',{'verify_native':True}),
            (['--workflow-status','wf_existing'],'get_workflow_status',{}),
        ):
            with self.subTest(args=args),patch.object(cli,name,return_value=ToolResult(True,'read','read')) as call:
                code,_,_=self.invoke(args)
                self.assertEqual(code,0)
                call.assert_called_once_with(args[1],**expected)

    def test_new_incompatible_flags_are_rejected_before_dispatch(self):
        bad=[['--case','--workflow'],['--app','--live'],['--app','--port','0'],
             ['--case','--port','8765'],['--case','--verify-native'],
             ['--analyze-run','run_a','--live'],['--workflow-status','wf_a','--live'],
             ['--request','优化4次','--workflow','--plan-only']]
        for args in bad:
            with self.subTest(args=args):
                code,out,_=self.invoke(args)
                self.assertEqual(code,2)
                self.assertEqual(out,'')
