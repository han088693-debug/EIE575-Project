param(
    [Parameter(Position=0)]
    [ValidateSet('validate','plan','run','verify','analyze','calibrate','package','probe','restore-check','help')]
    [string]$Action = 'help',
    [string]$Config, [string]$Batch, [string]$Output, [string]$Job,
    [string]$Scope, [string]$Checkpoint, [string]$Destination, [string]$Sha256,
    [string]$Scenario, [string]$Load, [string]$Scheme, [int]$Seed, [double]$Density,
    [switch]$DryRun, [switch]$Resume, [switch]$Help, [switch]$Once, [switch]$Status, [switch]$RetryFailed,
    [int]$ExtendTimeout,
    [string]$Python
)
$ErrorActionPreference='Stop'
if (-not $Python) { $Python=$env:EIE575_ANALYSIS_PYTHON }
if (-not $Python) {
    $Python=Join-Path 'E:\港理课程' '.venvs\eie546-lab1\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { Write-Error "Missing analysis Python: $Python" -ErrorAction Continue; exit 10 }
$cli=@((Join-Path $PSScriptRoot 'tools\experiment.py'))
if ($Action -ne 'help') { $cli+=$Action }
if ($Action -eq 'help' -or $Help) { $cli+='--help' }
foreach($key in @('Config','Batch','Output','Job','Scope','Checkpoint','Destination','Sha256','Scenario','Load','Scheme','Seed','Density')){
    if($PSBoundParameters.ContainsKey($key)){
        $cli+="--$($key.ToLowerInvariant())"
        $cli+=[string]$PSBoundParameters[$key]
    }
}
if($DryRun){$cli+='--dry-run'}
if($Resume){$cli+='--resume'}
if($Once){$cli+='--once'}
if($Status){$cli+='--status'}
if($RetryFailed){$cli+='--retry-failed'}
if($PSBoundParameters.ContainsKey('ExtendTimeout')){$cli+=@('--extend-timeout',[string]$ExtendTimeout)}
& $Python @cli
exit $LASTEXITCODE

