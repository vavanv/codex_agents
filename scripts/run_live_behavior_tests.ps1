[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RunRoot,
    [switch]$AllowLive,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$support = Join-Path $PSScriptRoot "live_validation_support.py"
$python = (Get-Command python -ErrorAction Stop).Source
$pythonArguments = @(
    "-B",
    $support,
    "run",
    "--run-root",
    $RunRoot,
    "--timeout",
    $TimeoutSeconds.ToString()
)
if ($AllowLive) {
    $pythonArguments += "--allow-live"
}

& $python @pythonArguments
exit $LASTEXITCODE
