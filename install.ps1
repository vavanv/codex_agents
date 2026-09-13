[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter()]
    [string]$TargetRepository = ".",

    [Parameter()]
    [switch]$Recover,

    [Parameter()]
    [switch]$WithCustomAgents
)

$ErrorActionPreference = "Stop"
$Manager = Join-Path $PSScriptRoot "scripts\workflow_manager.py"

if (-not (Test-Path -LiteralPath $Manager -PathType Leaf)) {
    throw "Installer engine not found: $Manager"
}

$PythonCommand = Get-Command "python" -ErrorAction SilentlyContinue

if ($null -eq $PythonCommand) {
    $PythonCommand = Get-Command "python3" -ErrorAction SilentlyContinue
}

if ($null -eq $PythonCommand) {
    throw "Python 3 is required. Install Python 3 and retry."
}

$Arguments = @($Manager, "install", "--target", $TargetRepository)

if ($WhatIfPreference) {
    $Arguments += "--dry-run"
}

if ($Recover) {
    $Arguments += "--recover"
}

if ($WithCustomAgents) {
    $Arguments += "--with-custom-agents"
}

& $PythonCommand.Source @Arguments
exit $LASTEXITCODE
