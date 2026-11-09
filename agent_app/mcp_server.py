"""DiskAgent MCP adapter. No model API is called by external candidate tools."""
from __future__ import annotations
import argparse


def create_mcp(settings=None):
    from mcp.server.fastmcp import FastMCP
    from python_control import runtime
    from .external import ExternalService
    service = ExternalService(settings or runtime.load_settings())
    server = FastMCP('DiskAgent', instructions='Read the case, preview candidate parameters, explicitly submit a live solve and poll status. Internal model API is never called by these tools. Use a unique candidate_ID (candidate_ followed by 32 lowercase hex digits); reuse the same ID when retrying the same request.', host='127.0.0.1', port=8766)

    @server.tool()
    def get_case() -> dict:
        """Read nine design variables, bounds, units and engineering limitations."""
        return service.case()

    @server.tool()
    def preview_candidate(parameters: dict[str, float]) -> dict:
        """Validate/quantize all nine parameters. Does not start a solver or model API."""
        return service.preview(parameters)

    @server.tool()
    def submit_candidate(parameters: dict[str, float], candidate_id: str, live: bool = False) -> dict:
        """Submit ONE Workbench solve, only with live=true. Idempotent ID prevents duplicate retry solves. No internal LLM/API. Long task: poll get_candidate_status."""
        return service.submit(parameters, candidate_id, live=live)

    @server.tool()
    def get_candidate_status(candidate_id: str) -> dict:
        """Read persisted status and verified native results. Does not resubmit."""
        return service.status(candidate_id)

    @server.tool()
    def list_candidates() -> dict:
        """Read latest external candidates and results shared with the web frontend."""
        return service.list()

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--transport', choices=('stdio', 'streamable-http'), default='stdio')
    args = parser.parse_args()
    from python_control import runtime
    server = create_mcp(runtime.load_settings(args.config))
    server.run(transport=args.transport)


if __name__ == '__main__':
    main()
