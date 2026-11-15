param([ValidateRange(1,65535)][int]$Port=8765)
$ErrorActionPreference='Stop'
$diskAppUrl="http://127.0.0.1:$Port"
function Get-DiskAgentConfig {
    try { return Invoke-RestMethod -Uri "$diskAppUrl/api/config" -TimeoutSec 2 }
    catch { return $null }
}
$diskConfig=Get-DiskAgentConfig
if ($diskConfig -and $diskConfig.app_id -eq 'disk-agent.workbench.v1') {
    Start-Process -FilePath $diskAppUrl
    exit 0
}
$diskCandidates = @()
if ($env:DISKAGENT_PYTHON) { $diskCandidates += $env:DISKAGENT_PYTHON }
$diskCandidates += (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')
$diskCandidates += (Join-Path $env:USERPROFILE '.conda\envs\llm_opt\python.exe')
$diskCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($diskCommand) { $diskCandidates += $diskCommand.Source }
$diskPython = $null
foreach ($candidate in $diskCandidates) {
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $candidate -c 'import sys, numpy, matplotlib; assert sys.version_info >= (3, 10)' 2>$null
    } finally { $ErrorActionPreference = $savedPreference }
    if ($LASTEXITCODE -eq 0) { $diskPython = $candidate; break }
}
if (-not $diskPython) {
    throw 'No usable Python 3.10+ with numpy and matplotlib found. Set DISKAGENT_PYTHON to the full path of a suitable python.exe.'
}
$diskConfigPath = Join-Path $PSScriptRoot 'python_control\optimization_config.json'
if (-not (Test-Path -LiteralPath $diskConfigPath -PathType Leaf)) {
    throw 'Missing python_control\optimization_config.json. Copy optimization_config.example.json and configure the local Workbench path.'
}
$diskLogDir=Join-Path $PSScriptRoot 'agent_app_logs'
New-Item -ItemType Directory -Path $diskLogDir -Force | Out-Null
$diskStdout=Join-Path $diskLogDir 'server.stdout.log'
$diskStderr=Join-Path $diskLogDir 'server.stderr.log'
$diskProcess=Start-Process -FilePath $diskPython -ArgumentList @('-B','-m','agent_app','--app','--port',[string]$Port) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $diskStdout -RedirectStandardError $diskStderr -PassThru
Set-Content -LiteralPath (Join-Path $diskLogDir 'server.pid') -Value $diskProcess.Id -Encoding ASCII
for ($diskTry=0; $diskTry -lt 60; $diskTry++) {
    Start-Sleep -Milliseconds 250
    $diskProcess.Refresh()
    $diskConfig=Get-DiskAgentConfig
    if ($diskConfig -and $diskConfig.app_id -eq 'disk-agent.workbench.v1') {
        Write-Host "DiskAgent ready: $diskAppUrl"
        Start-Process -FilePath $diskAppUrl
        exit 0
    }
    if ($diskProcess.HasExited) { break }
}
Write-Host "DiskAgent did not become ready. Check port $Port and logs: $diskLogDir"
if (Test-Path -LiteralPath $diskStderr) { Get-Content -LiteralPath $diskStderr -Tail 12 }
exit 1
