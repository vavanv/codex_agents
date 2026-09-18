[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunRoot,
    [switch]$ApplyCleanup
)

$ErrorActionPreference = "Stop"
$support = Join-Path $PSScriptRoot "live_validation_support.py"
$python = (Get-Command python -ErrorAction Stop).Source
$pythonArguments = @(
    "-B",
    $support,
    "cleanup",
    "--run-root",
    $RunRoot
)
if ($ApplyCleanup) {
    $pythonArguments += "--apply-cleanup"
}

& $python @pythonArguments
exit $LASTEXITCODE
