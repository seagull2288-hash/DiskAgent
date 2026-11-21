"""python <package>/mcp_server.py; optional dependency: mcp."""
import sys
from pathlib import Path
if str(Path(__file__).resolve().parent.parent) not in sys.path:sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from mcp.server.fastmcp import FastMCP
from nx_case_agent.core import models,plan,execute
mcp=FastMCP('NX12 Precise Learned Case Agent',instructions='Preserve complete geometry for two learned cases. Never substitute simplified geometry. Preview editable versus dependent dimensions and feature-history limitations. Build verifies bidirectional sampled reference deviation, kernel validity, parameter updates and reopening. A precise B-rep instance is not a recovered sketch/revolve/loft history.')
@mcp.tool()
def list_learned_models()->dict:return models()
@mcp.tool()
def preview_model(description:str,parameters:dict=None)->dict:
    """Match a precise learned case; report nominal dimensions and supported parameter scope."""
    return plan(description,parameters)
@mcp.tool()
def build_model(description:str,parameters:dict=None)->dict:
    """Generate complete learned geometry in NX12; verify reference deviation and actual editable dimensions."""
    return execute(description,parameters)
if __name__=='__main__':mcp.run(transport='stdio')
