param(
    [ValidateSet('list','selfcheck','live')][string]$Mode = 'list',
    [string]$Output,
    [string[]]$Tasks,
    [ValidateSet('baseline','context','prompt','full')][string[]]$Variants = @('baseline','context','prompt','full'),
    [int]$Repeat = 1,
    [int]$MaxBatchTokens = 500000,
    [int]$TaskTimeout = 180
)
$ErrorActionPreference = 'Stop'
$benchmarkProject = Split-Path -Parent $PSScriptRoot
$benchmarkPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$benchmarkPrefix = @()
if (-not (Test-Path -LiteralPath $benchmarkPython)) {
    $benchmarkCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($benchmarkCommand -and $benchmarkCommand.Source -notlike '*WindowsApps*') {
        $benchmarkPython = $benchmarkCommand.Source
    } else {
        $benchmarkCommand = Get-Command py -ErrorAction SilentlyContinue
        if (-not $benchmarkCommand) { throw 'Python 3.11+ is required.' }
        $benchmarkPython = $benchmarkCommand.Source
        $benchmarkPrefix = @('-3')
    }
}
$benchmarkSwitch = @{list='--list';selfcheck='--self-check';live='--live'}[$Mode]
$benchmarkCli = @('-m','benchmarks',$benchmarkSwitch,'--repeat',"$Repeat",'--max-batch-tokens',"$MaxBatchTokens",'--task-timeout',"$TaskTimeout")
if ($Output) { $benchmarkCli += @('--output',$Output) }
if ($Tasks) { $benchmarkCli += @('--tasks') + $Tasks }
$benchmarkCli += @('--variants') + $Variants
Push-Location -LiteralPath $benchmarkProject
try {
    & $benchmarkPython @benchmarkPrefix @benchmarkCli
    $benchmarkExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $benchmarkExitCode
