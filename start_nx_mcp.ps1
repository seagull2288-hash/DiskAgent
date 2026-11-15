$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv-mcp\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Missing .venv-mcp. Create the MCP environment described in README_MCP接入.md first.'
}
& $python -X utf8 -m agent_app.nx_mcp_server --root $root
