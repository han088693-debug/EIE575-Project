param(
    [ValidateSet('build', 'prepare', 'run')]
    [string]$Action = 'run',
    [ValidateSet('G0', 'G1', 'G2', 'G3')]
    [string]$Scheme = 'G0',
    [int]$Seed = 0,
    [string]$ToolsRoot = 'E:\EIE575_Tools',
    [switch]$Gui
)
$ErrorActionPreference = 'Stop'
$python = Join-Path $ToolsRoot 'omnetpp-5.6.2\tools\win64\mingw64\bin\python2.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing bundled Python 2: $python. Set -ToolsRoot to the installed tools folder."
}
$scriptArgs = @((Join-Path $PSScriptRoot 'tools\manage.py'), $Action,
    '--tools', $ToolsRoot, '--scheme', $Scheme, '--seed', "$Seed")
if ($Gui) { $scriptArgs += '--gui' }
& $python @scriptArgs
if ($LASTEXITCODE -ne 0) { throw "Task failed; inspect the output above and the newest results folder." }
