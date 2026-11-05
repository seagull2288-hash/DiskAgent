param(
    [string]$ResumeDir = '',
    [ValidateRange(0, 100000)][int]$CandidateCount = 0,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$pythonExecutable = Join-Path $env:USERPROFILE '.conda\envs\llm_opt\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw 'llm_opt Python was not found. Select the existing Anaconda environment first.'
}
$pythonEntry = Join-Path $PSScriptRoot 'run_python_optimization.py'
$pythonArguments = @('-u', '-B', $pythonEntry)
if ($Check) { $pythonArguments += '--check' } else { $pythonArguments += '--live' }
if ($ResumeDir) { $pythonArguments += @('--resume', $ResumeDir) }
if ($CandidateCount -gt 0) { $pythonArguments += @('--rounds', [string]$CandidateCount) }
& $pythonExecutable @pythonArguments
if ($LASTEXITCODE -ne 0) {
    throw "Python controller stopped with exit code $LASTEXITCODE. Check its progress log and checkpoint.json."
}
