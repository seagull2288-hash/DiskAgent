"""Explicit live NX acceptance via the actual MCP stdio transport."""
import asyncio
import json
import os
import sys
from pathlib import Path
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[2]
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'agent_app.nx_mcp_server', '--root', str(root)], cwd=str(root), env=os.environ.copy())
    plan = {'kind': 'disk', 'name': 'mcp_acceptance_disk', 'outer_diameter': 100,
            'thickness': 10, 'center_hole_diameter': 20, 'bolt_circle_diameter': 60,
            'bolt_hole_diameter': 8, 'bolt_hole_count': 4, 'output_format': 'prt'}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=360)) as session:
            await session.initialize()
            tools = await session.list_tools()
            print('TOOLS', [item.name for item in tools.tools], flush=True)
            for name, args in [('nx_connection_status', {}), ('nx_preview_disk', {'plan': plan}),
                               ('nx_create_disk', {'plan': plan, 'live': True,
                                                   'request_id': 'nx-live-acceptance-20261115-1'})]:
                result = await session.call_tool(name, args)
                print(name, result.model_dump_json(indent=2), flush=True)
                payload = result.structuredContent or json.loads(result.content[0].text)
                if result.isError or not payload.get('ok', False):
                    return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
