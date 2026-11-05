param([ValidateRange(1,65535)][int]$Port=8765)
$ErrorActionPreference='Stop'
$diskPython='C:\Users\18940\.conda\envs\llm_opt\python.exe'
$diskAppUrl="http://127.0.0.1:$Port"
if (-not (Test-Path -LiteralPath $diskPython -PathType Leaf)) {
    throw "Python environment not found: $diskPython"
}
function Get-DiskAgentConfig {
    try { return Invoke-RestMethod -Uri "$diskAppUrl/api/config" -TimeoutSec 2 }
    catch { return $null }
}
$diskConfig=Get-DiskAgentConfig
if ($diskConfig -and $diskConfig.app_id -eq 'disk-agent.workbench.v1') {
    Start-Process -FilePath $diskAppUrl
    exit 0
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
