param(
    [int]$Port = 8765,
    [string]$Project = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
$runtimePython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
if (Test-Path -LiteralPath $runtimePython) {
    & $runtimePython -m xcode.server --port $Port --project $Project
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*WindowsApps*') {
        & $pythonCommand.Source -m xcode.server --port $Port --project $Project
    } else {
        $pyCommand = Get-Command py -ErrorAction SilentlyContinue
        if (-not $pyCommand) { throw 'Python 3.11+ is required. Install Python or use the Codex bundled runtime.' }
        & $pyCommand.Source -3 -m xcode.server --port $Port --project $Project
    }
}
} finally {
    Pop-Location
}
