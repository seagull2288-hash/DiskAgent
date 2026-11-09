param([ValidateSet('stdio','streamable-http')][string]$Transport='stdio')
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
$diskPython=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $diskPython)) { throw 'Create .venv and install requirements-mcp.txt first.' }
& $diskPython -B -m agent_app.mcp_server --transport $Transport
exit $LASTEXITCODE
