$ErrorActionPreference = 'Stop'

$startScript = Join-Path $PSScriptRoot 'Start-LiteratureWorkbench.ps1'
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "Start script is missing: $startScript"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Literature Workbench.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = (Get-Command powershell.exe).Source
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
$shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Start Literature Workbench on localhost'
$shortcut.Save()

Write-Output "Desktop shortcut installed: $shortcutPath"
