[CmdletBinding()]
param(
    [switch]$AllowLive,
    [string]$TempBase,
    [ValidateRange(1, 300)]
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$support = Join-Path $PSScriptRoot "live_validation_support.py"
$python = (Get-Command python -ErrorAction Stop).Source
$pythonArguments = @(
    "-B",
    $support,
    "create",
    "--timeout",
    $TimeoutSeconds.ToString()
)
if ($AllowLive) {
    $pythonArguments += "--allow-live"
}
if ($PSBoundParameters.ContainsKey("TempBase")) {
    $pythonArguments += @("--temp-base", $TempBase)
}

& $python @pythonArguments
exit $LASTEXITCODE
