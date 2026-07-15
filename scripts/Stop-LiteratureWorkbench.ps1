$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Literature Workbench venv is missing: $python"
}

& $python -m literature_workbench.launcher stop
if ($LASTEXITCODE -ne 0) {
    throw 'No validated Literature Workbench instance was stopped.'
}
