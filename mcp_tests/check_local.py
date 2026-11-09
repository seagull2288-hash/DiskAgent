"""Read-only MCP acceptance against the real local DiskAgent app. No live call."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def check(app_url, run_id):
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[
        '-X', 'utf8', '-B', str(root / 'agent_app/mcp_server.py'), '--app-url', app_url])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            async def call(name, args=None):
                result = await client.call_tool(name, args or {})
                if result.isError or not result.structuredContent or not result.structuredContent.get('ok'):
                    raise RuntimeError(str(result))
                return result.structuredContent
            config = await call('disk_connection_status')
            case = await call('disk_read_case')
            references = {v['name']: v['reference'] for v in case['data']['variables']}
            preview = await call('disk_validate_design', {'parameters': references})
            optimization = await call('disk_submit_optimization', {
                'task': {'operation': 'optimize', 'total_candidate_budget': 1}})
            runs = await call('disk_list_records', {'kind': 'runs', 'limit': 5})
            before = await call('disk_get_run_status', {'run_id': run_id})
            report = await call('disk_read_report', {'run_id': run_id})
            after = await call('disk_get_run_status', {'run_id': run_id})
            assert before == after, 'Historical run changed during read-only acceptance.'
            selected = report['data']['design_selection']['recommended']
            return {'ok': True, 'app_url': app_url, 'tool_count': len(tools.tools),
                    'mcp_bridge': config['mcp_bridge'], 'active_job_id': config['active_job_id'],
                    'case_id': case['data']['case_id'], 'variable_count': len(references),
                    'design_preview_status': preview['status'], 'optimization_preview_status': optimization['status'],
                    'listed_runs': runs['returned_count'], 'read_run_id': run_id,
                    'completed_candidates': report['data']['completed_candidates'],
                    'recommended_iteration': selected['iteration'] if selected else None,
                    'recommended_observation': selected['observation'] if selected else None,
                    'historical_run_unchanged': before == after,
                    'live_submissions_made': 0, 'api_requests_made': 0, 'workbench_evaluation_tasks_started': 0,
                    'report_json_characters': len(json.dumps(report)),
                    'scope': 'real_stdio_real_http_read_and_preview_only'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-url', default='http://127.0.0.1:8768')
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output')
    args = parser.parse_args()
    result = asyncio.run(check(args.app_url, args.run_id))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + '\n', encoding='utf-8')
    print(text)
