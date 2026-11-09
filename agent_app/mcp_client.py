"""Async SDK client used by the isolated browser conversation worker."""
from __future__ import annotations

from contextlib import AsyncExitStack
import json
from pathlib import Path
import sys


class MCPClient:
    def __init__(self, app_url):
        self.app_url = app_url
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=sys.executable, args=[
            '-X', 'utf8', '-B', str(Path(__file__).with_name('mcp_server.py')),
            '--app-url', self.app_url])
        try:
            reader, writer = await self.stack.enter_async_context(stdio_client(params))
            self.session = await self.stack.enter_async_context(ClientSession(reader, writer))
            await self.session.initialize()
            catalog = await self.session.list_tools()
            self.tools = {tool.name: tool.model_dump(by_alias=True) for tool in catalog.tools}
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *args):
        return await self.stack.__aexit__(*args)

    async def call(self, name, arguments):
        from jsonschema import validate
        if name not in self.tools:
            raise ValueError('Unknown MCP tool ' + name)
        validate(arguments, self.tools[name]['inputSchema'])
        result = await self.session.call_tool(name, arguments)
        if result.isError:
            message = ' '.join(block.text for block in result.content if block.type == 'text')
            return {'ok': False, 'error': {'code': 'MCP_TOOL_ERROR', 'message': message}}
        if isinstance(result.structuredContent, dict):
            return result.structuredContent
        for block in result.content:
            if block.type == 'text':
                value = json.loads(block.text)
                if isinstance(value, dict):
                    return value
        raise ValueError('MCP tool returned no structured object.')
