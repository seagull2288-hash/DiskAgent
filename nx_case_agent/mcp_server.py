"""python <package>/mcp_server.py; optional dependency: mcp."""
import sys
from pathlib import Path
if str(Path(__file__).resolve().parent.parent) not in sys.path:sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from mcp.server.fastmcp import FastMCP
from nx_case_agent.core import models,plan,execute
mcp=FastMCP('NX12 Learned Case Agent',instructions='Two learned recipes with inferred defaults. Preview plans and report simplifications. Build produces native parametric geometry, not a trained LLM weight model.')
@mcp.tool()
def list_learned_models()->dict:return models()
@mcp.tool()
def preview_model(description:str,parameters:dict=None)->dict:
    """Infer omitted dimensions from a known case; validate overrides without NX execution."""
    return plan(description,parameters)
@mcp.tool()
def build_model(description:str,parameters:dict=None)->dict:
    """Build a new NX12 parametric PRT; verify parameter update and reopening."""
    return execute(description,parameters)
if __name__=='__main__':mcp.run(transport='stdio')
