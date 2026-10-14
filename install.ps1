[CmdletBinding()]
param(
    [string]$TaskName = 'BURNRATE Dashboard',
    [switch]$NoLaunch,
    [switch]$DryRun
)

# One-command BURNRATE installer.
# Sets up .venv, initializes %LOCALAPPDATA%\BURNRATE\spend.db, registers the
# auto-start scheduled task, starts the background service on 127.0.0.1:17331,
# creates Desktop & Start Menu shortcuts with the custom BURNRATE icon, and
# opens the native BURNRATE app window directly on the Setup Wizard.

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'scripts\install-burnrate.ps1') -TaskName $TaskName -NoLaunch:$NoLaunch -DryRun:$DryRun
