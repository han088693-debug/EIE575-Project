param(
    [string]$Compiler,
    [string]$ToolsRoot = 'E:\EIE575_Tools'
)

$ErrorActionPreference = 'Stop'
if (-not $Compiler) {
    $omnet = if ($env:EIE575_OMNET_ROOT) { $env:EIE575_OMNET_ROOT } else { Join-Path $ToolsRoot 'omnetpp-5.6.2' }
    $Compiler = Join-Path $omnet 'tools\win64\mingw64\bin\g++.exe'
}
$testRoot = $PSScriptRoot
$buildDir = Join-Path $testRoot 'build'
$source = Join-Path $testRoot 'core_test.cpp'
$binary = Join-Path $buildDir 'core_test.exe'

if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) {
    throw "C++ compiler not found: $Compiler. Pass -Compiler with your g++.exe path."
}

New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$oldPath = $env:PATH
try {
    $env:PATH = (Split-Path -Parent $Compiler) + ';' + $oldPath
    & $Compiler -std=c++11 -O2 -Wall -Wextra -pedantic $source -o $binary
    if ($LASTEXITCODE -ne 0) { throw "Core test compilation failed with exit code $LASTEXITCODE." }
    & $binary
    if ($LASTEXITCODE -ne 0) { throw "Core tests failed with exit code $LASTEXITCODE." }
}
finally {
    $env:PATH = $oldPath
}
