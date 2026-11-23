param(
    [string]$Python = 'E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw 'Python interpreter not found; pass -Python with an interpreter that has mcp and psutil installed.'
}
$env:EIE575_MCP_SETTINGS = Join-Path $PSScriptRoot 'settings.json'
& $Python -B -X utf8 (Join-Path $PSScriptRoot 'host.py') --start
if ($LASTEXITCODE -ne 0) { throw 'Simulation host did not start. See the configured state_root/host.log.' }
