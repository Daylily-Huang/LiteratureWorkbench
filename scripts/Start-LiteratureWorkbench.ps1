$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot 'backend\.venv\Scripts\pythonw.exe'
$webIndex = Join-Path $projectRoot 'web\dist\index.html'
$logDirectory = Join-Path $env:LOCALAPPDATA 'LiteratureWorkbench\logs'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stdoutLog = Join-Path $logDirectory "launcher-$stamp.out.log"
$stderrLog = Join-Path $logDirectory "launcher-$stamp.err.log"

if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Literature Workbench venv is missing: $pythonw"
}
if (-not (Test-Path -LiteralPath $webIndex -PathType Leaf)) {
    throw "Literature Workbench web build is missing: $webIndex"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
try {
    Start-Process -FilePath $pythonw `
        -ArgumentList @('-m', 'literature_workbench.launcher', 'start') `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog
    Write-Output "Literature Workbench is starting. If no browser opens, inspect: $stdoutLog ; $stderrLog"
} catch {
    throw "Literature Workbench failed to start. Logs: $stdoutLog ; $stderrLog. $($_.Exception.Message)"
}
