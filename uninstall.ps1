[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter()]
    [string]$TargetRepository = ".",

    [Parameter()]
    [switch]$Recover
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "scripts\workflow_manager.py"

if (-not (Test-Path -LiteralPath $Manager -PathType Leaf)) {
    throw "Uninstaller engine not found: $Manager"
}

$PythonCommand = Get-Command "python" -ErrorAction SilentlyContinue

if ($null -eq $PythonCommand) {
    $PythonCommand = Get-Command "python3" -ErrorAction SilentlyContinue
}

if ($null -eq $PythonCommand) {
    throw "Python 3 is required. Install Python 3 and retry."
}

$Arguments = @($Manager, "uninstall", "--target", $TargetRepository)

if ($WhatIfPreference) {
    $Arguments += "--dry-run"
}

if ($Recover) {
    $Arguments += "--recover"
}

& $PythonCommand.Source @Arguments
exit $LASTEXITCODE
