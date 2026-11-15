"""Standalone Siemens NX 12 MCP server for DiskAgent."""
from __future__ import annotations

import argparse
from pathlib import Path

from .nx_modeling import NXModelingService


def create_mcp(root=None):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    service = NXModelingService(root or Path(__file__).resolve().parents[1])
    mcp = FastMCP('DiskAgent NX Modeling', instructions=(
        'Use nx_preview_disk first. Only nx_create_disk with live=true launches NX. '
        'The server accepts registered parameters, never arbitrary NXOpen code or output paths.'))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    @mcp.tool(annotations=read)
    def nx_connection_status():
        """Check the registered NX 12 installation without launching NX."""
        return service.status()

    @mcp.tool(annotations=read)
    def nx_preview_disk(plan: dict):
        """Validate a parameterized disk and return its normalized plan; no CAD process is started."""
        return service.preview(plan)

    @mcp.tool(annotations=write)
    def nx_create_disk(plan: dict, live: bool = False, request_id: str = ''):
        """Create a registered parameterized disk. live=false only validates; live=true launches NX 12."""
        return service.execute(plan, request_id) if live else service.preview(plan)
    return mcp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path)
    args = parser.parse_args(argv)
    create_mcp(args.root).run(transport='stdio')


if __name__ == '__main__':
    main()
