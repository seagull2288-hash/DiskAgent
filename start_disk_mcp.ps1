param([int]$AppPort = 8768)
$ErrorActionPreference = 'Stop'
if ($AppPort -lt 1 -or $AppPort -gt 65535) { throw 'AppPort must be from 1 to 65535.' }
$mcpPython = Join-Path $PSScriptRoot '.venv-mcp\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $mcpPython)) {
    throw 'Install the isolated MCP environment first; see README_MCP接入.md.'
}
# No Write-Output banners: stdout belongs exclusively to MCP JSON-RPC.
& $mcpPython -X utf8 -B (Join-Path $PSScriptRoot 'agent_app\mcp_server.py') --app-url "http://127.0.0.1:$AppPort"
exit $LASTEXITCODE
