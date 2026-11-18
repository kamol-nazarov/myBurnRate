[CmdletBinding()]
param(
    [switch]$DryRun
)

# Stop the owned Python dashboard or installed native collector on the
# configured loopback port (17331 by default).

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Stop-BurnrateDashboard -DryRun:$DryRun
if (-not $DryRun) {
    # Deliberate stop: the watchdog leaves the dashboard down until it starts again.
    Set-BurnrateStopMarker
}
