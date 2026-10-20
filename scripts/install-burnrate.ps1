[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$NoLaunch,
    [switch]$DryRun
)

# Per-user scheduled task "BURNRATE Dashboard": AtLogOn, Limited, Hidden.
# Action starts pythonw -m spend_app (spend_app.service) bound to 127.0.0.1:17331.
# Foreground equivalent: burnrate serve
# Self-heal: RestartCount 3 / RestartInterval 1 minute for a failed launch, plus
# the "BURNRATE Watchdog" task (pythonw -m spend_app.watchdog every 5 minutes)
# that restarts a service which stopped answering or whose collection stalled.
# Mutex Local\BURNRATE-Dashboard; PID under $env:LOCALAPPDATA\BURNRATE\run.

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'BurnrateRuntime.ps1')
Install-BurnrateDashboard -TaskName $TaskName -NoLaunch:$NoLaunch -DryRun:$DryRun
