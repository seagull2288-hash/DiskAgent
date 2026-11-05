"""Plan natural language or run a JSON task; optimization is validation-only by default."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys

from python_control.runtime import bridge, safe_text
from .schemas import ToolResult
from .tools import execute_task, read_case
from .planner import execute_request, plan_task
from .supervisor import run_workflow, resume_workflow, get_workflow_status
from .result_analysis import analyze_run


def main(argv=None):
    # Windows redirected consoles can default to cp1252; requests/results contain Chinese.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--task', help='UTF-8 JSON task file. optimize is validation-only without --live.')
    mode.add_argument('--case', action='store_true', help='Read the current case without starting work.')
    mode.add_argument('--request', help='One natural-language check, optimize or status request.')
    mode.add_argument('--analyze-run', metavar='RUN_ID', help='Analyze saved measured results without API/FEA calls.')
    mode.add_argument('--workflow-status', metavar='WORKFLOW_ID', help='Read a saved workflow and its bound run.')
    mode.add_argument('--resume-workflow', metavar='WORKFLOW_ID', help='Resume a saved workflow; preview unless --live.')
    mode.add_argument('--app', action='store_true', help='Start the local browser workbench at http://127.0.0.1:8765.')
    parser.add_argument('--workflow', action='store_true', help='Persist and supervise --request or --task, in batches.')
    parser.add_argument('--port', type=int, help='Local app port; requires --app (default 8765).')
    parser.add_argument('--verify-native', action='store_true', help='Recheck saved native evidence; requires --analyze-run.')
    parser.add_argument('--live', action='store_true', help='Allow a valid optimize task to call API and Workbench.')
    parser.add_argument('--planner-live', action='store_true', help='Allow the planning LLM to call the configured API; requires --request.')
    parser.add_argument('--run-id', help='Explicit run context for a natural-language continuation/status request.')
    parser.add_argument('--plan-only', action='store_true', help='Return a TaskSpec without calling its tool; requires --request.')
    args = parser.parse_args(argv)
    if args.request is None and (args.planner_live or args.run_id is not None or args.plan_only):
        parser.error('--planner-live, --run-id and --plan-only require --request.')
    if args.plan_only and args.live:
        parser.error('--plan-only cannot be combined with --live.')
    if args.workflow and args.request is None and args.task is None:
        parser.error('--workflow requires --request or --task.')
    if args.workflow and args.plan_only:
        parser.error('Use the default workflow preview instead of combining --workflow and --plan-only.')
    if args.verify_native and args.analyze_run is None:
        parser.error('--verify-native requires --analyze-run.')
    if args.port is not None and (not args.app or not 1<=args.port<=65535):
        parser.error('--port requires --app and a value from 1 to 65535.')
    if args.live and (args.app or args.analyze_run is not None or args.workflow_status is not None):
        parser.error('--live applies only to task/request execution or workflow resume.')
    if args.app:
        from .web import serve
        try:
            serve(port=args.port or 8765)
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception as exc:
            print('Local app error: '+safe_text(exc),file=sys.stderr)
            return 1
    try:
        # Operational logging goes to stderr; stdout stays a single JSON result.
        with redirect_stdout(sys.stderr):
            if args.analyze_run is not None:
                result = analyze_run(args.analyze_run,verify_native=args.verify_native)
            elif args.workflow_status is not None:
                result = get_workflow_status(args.workflow_status)
            elif args.resume_workflow is not None:
                result = resume_workflow(args.resume_workflow,live=args.live)
            elif args.workflow:
                if args.request is not None:
                    result = run_workflow(args.request,live=args.live,use_llm=args.planner_live,
                                          context_run_id=args.run_id)
                else:
                    result = run_workflow(task=bridge.read_json(Path(args.task)),live=args.live)
            elif args.request is not None:
                kwargs = {'use_llm':args.planner_live, 'context_run_id':args.run_id}
                result = (plan_task(args.request, **kwargs) if args.plan_only else
                          execute_request(args.request, live=args.live, **kwargs))
                if args.plan_only:
                    result.data['plan_only'] = True
            else:
                result = read_case() if args.case else execute_task(bridge.read_json(Path(args.task)), live=args.live)
    except Exception as exc:
        result = ToolResult(False, 'unknown', 'failed', error={'code': getattr(exc, 'code', type(exc).__name__),
                                                              'message': safe_text(exc)})
    print(json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result.ok else (130 if result.status == 'interrupted' else 1)


if __name__ == '__main__':
    raise SystemExit(main())
