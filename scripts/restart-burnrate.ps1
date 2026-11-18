[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$DryRun
)

# Restart the owned listener on the configured loopback port. Reuse the
# installed native collector or the Python dashboard's sign-in task.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Restart-BurnrateDashboard -TaskName $TaskName -DryRun:$DryRun
