[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$DryRun
)

# Restart the dashboard on 127.0.0.1:17331: stop only the owned listener, then
# start it through the "BURNRATE Dashboard" sign-in task (or directly when the
# task is not registered). The watchdog uses this; it is also safe to run by hand.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Restart-BurnrateDashboard -TaskName $TaskName -DryRun:$DryRun
