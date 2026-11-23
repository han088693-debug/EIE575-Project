param(
    [ValidateSet('G0', 'G1', 'G2', 'G3')]
    [string[]]$Schemes = @('G0', 'G1', 'G2', 'G3'),
    [int[]]$Seeds = @(0, 1, 2, 3, 4),
    [string]$ToolsRoot = 'E:\EIE575_Tools'
)
$ErrorActionPreference = 'Stop'
$omnet = if ($env:EIE575_OMNET_ROOT) { $env:EIE575_OMNET_ROOT } else { Join-Path $ToolsRoot 'omnetpp-5.6.2' }
$python = if ($env:EIE575_LAUNCHER_PYTHON) { $env:EIE575_LAUNCHER_PYTHON } else { Join-Path $omnet 'tools\win64\mingw64\bin\python2.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw "Missing bundled Python 2: $python" }
$batchArgs = @((Join-Path $PSScriptRoot 'tools\batch.py'), '--tools', $ToolsRoot, '--schemes')
$batchArgs += $Schemes
$batchArgs += '--seeds'
$batchArgs += @($Seeds | ForEach-Object { "$_" })
& $python @batchArgs
if ($LASTEXITCODE -ne 0) { throw 'Batch stopped after an error. Inspect results\batches and the last run log.' }
