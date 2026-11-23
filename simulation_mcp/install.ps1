param(
    [string]$InstallDir = 'E:\港理课程\课程文件\eie575\project\simulation_mcp',
    [string]$StateRoot = 'E:\EIE575_Tools\workspace\eie575_mcp_runs',
    [string]$ConfigFile = 'E:\港理课程\.codex\config.toml'
)

$ErrorActionPreference = 'Stop'
$pythonExe = 'E:\港理课程\.venvs\courseware-mcp\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw '找不到已安装的 MCP Python 解释器。'
}
$installerPath = Join-Path $PSScriptRoot 'install.py'
$installerArgs = @('-B', '-X', 'utf8', $installerPath,
    '--install-dir', $InstallDir, '--state-root', $StateRoot, '--config-file', $ConfigFile)
& $pythonExe @installerArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$hostScript = Join-Path $InstallDir 'start_host.ps1'
if (-not (Test-Path -LiteralPath $hostScript -PathType Leaf)) {
    throw 'MCP 已安装，但安装包缺少 start_host.ps1，无法启动独立宿主。'
}
& $hostScript
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host '请重载本地 MCP 连接或重新打开课程项目，使 eie575-simulation 工具进入工具列表。'
