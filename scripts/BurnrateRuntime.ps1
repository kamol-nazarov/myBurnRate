# Shared helpers for the public BURNRATE dashboard lifecycle scripts.
# Default bind is 127.0.0.1:17331. Self-heal has two parts: the dashboard task
# RestartCount (3 restarts, 1 minute interval) covers a failed launch, and the
# "BURNRATE Watchdog" task (pythonw -m spend_app.watchdog every 5 minutes)
# restarts a service that stopped answering or whose collection stalled.

$ErrorActionPreference = 'Stop'

$script:BurnrateTaskName = 'BURNRATE Dashboard'
$script:BurnrateHost = '127.0.0.1'
$script:BurnratePort = 17331
$script:BurnrateMutexName = 'Local\BURNRATE-Dashboard'
$script:BurnrateAppSpec = 'spend_app.api:app'
$script:BurnrateLaunchModule = 'spend_app.service'
$script:BurnrateRestartCount = 3
$script:BurnrateRestartInterval = New-TimeSpan -Minutes 1
$script:BurnrateWatchdogTaskName = 'BURNRATE Watchdog'
$script:BurnrateWatchdogModule = 'spend_app.watchdog'
$script:BurnrateWatchdogInterval = New-TimeSpan -Minutes 5

function Get-BurnrateDataRoot {
    if (-not [string]::IsNullOrWhiteSpace($env:BURNRATE_DATA_ROOT)) {
        return $env:BURNRATE_DATA_ROOT
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is not set'
    }
    Join-Path $env:LOCALAPPDATA 'BURNRATE'
}

function Get-BurnrateRunDir {
    Join-Path (Get-BurnrateDataRoot) 'run'
}

function Get-BurnrateLogDir {
    Join-Path (Get-BurnrateDataRoot) 'logs'
}

function Get-BurnratePidFile {
    Join-Path (Get-BurnrateRunDir) 'burnrate.pid'
}

function Get-BurnrateStopMarker {
    Join-Path (Get-BurnrateRunDir) 'stopped'
}

function Set-BurnrateStopMarker {
    # An explicit stop: the watchdog leaves the dashboard down until it starts again.
    Initialize-BurnrateDirectories
    Set-Content -LiteralPath (Get-BurnrateStopMarker) -Value ([DateTime]::UtcNow.ToString('o')) -Encoding ascii
}

function Clear-BurnrateStopMarker {
    $marker = Get-BurnrateStopMarker
    if (Test-Path -LiteralPath $marker) {
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
    }
}

function Get-BurnrateDbPath {
    Join-Path (Get-BurnrateDataRoot) 'spend.db'
}

function Get-BurnrateHealthUri {
    "http://$($script:BurnrateHost):$($script:BurnratePort)/healthz"
}

function Initialize-BurnrateDirectories {
    [void](New-Item -ItemType Directory -Force -Path (Get-BurnrateRunDir))
    [void](New-Item -ItemType Directory -Force -Path (Get-BurnrateLogDir))
}

function Get-BurnrateRepoRoot {
    Split-Path -Parent $PSScriptRoot
}

function Get-BurnrateWorkingDirectory {
    $repoRoot = Get-BurnrateRepoRoot
    if (Test-Path -LiteralPath (Join-Path $repoRoot 'spend_app')) {
        return $repoRoot
    }
    return Get-BurnrateDataRoot
}

function ConvertTo-BurnratePythonw {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $leaf = Split-Path -Leaf $Executable
    $dir = Split-Path -Parent $Executable
    if ($leaf -ieq 'pythonw.exe') {
        return $Executable
    }
    $pythonw = Join-Path $dir 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonw) {
        return $pythonw
    }
    return $Executable
}

function Test-BurnratePython312 {
    param([Parameter(Mandatory = $true)][string]$Executable)
    $probe = $Executable
    $leaf = Split-Path -Leaf $Executable
    if ($leaf -ieq 'pythonw.exe') {
        $sibling = Join-Path (Split-Path -Parent $Executable) 'python.exe'
        if (Test-Path -LiteralPath $sibling) {
            $probe = $sibling
        }
    }
    try {
        $version = (& $probe -c 'import sys; print(str(sys.version_info.major) + chr(46) + str(sys.version_info.minor))').Trim()
    } catch {
        return $false
    }
    return $version -eq '3.12'
}

function Resolve-BurnratePythonw {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($env:BURNRATE_PYTHON)) {
        $candidates += $env:BURNRATE_PYTHON
    }
    $candidates += Join-Path (Join-Path (Get-BurnrateDataRoot) 'venv') 'Scripts\pythonw.exe'
    $candidates += Join-Path (Join-Path (Get-BurnrateRepoRoot) '.venv') 'Scripts\pythonw.exe'

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $fromPy = (& py -3.12 -c 'import sys; print(sys.executable)').Trim()
            if ($fromPy) {
                $candidates += $fromPy
            }
        } catch {
        }
    }

    foreach ($name in @('pythonw', 'python')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) {
            $candidates += $cmd.Source
        }
    }

    $observed = @()
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate) -or -not (Test-Path -LiteralPath $candidate)) {
            continue
        }
        $pythonw = ConvertTo-BurnratePythonw -Executable $candidate
        if (-not (Test-Path -LiteralPath $pythonw)) {
            $observed += "$candidate (no pythonw)"
            continue
        }
        if (Test-BurnratePython312 -Executable $pythonw) {
            return (Resolve-Path -LiteralPath $pythonw).Path
        }
        $observed += "$pythonw (not 3.12)"
    }
    $detail = if ($observed.Count -gt 0) { $observed -join '; ' } else { 'no candidates' }
    throw "BURNRATE requires CPython 3.12 with pythonw.exe. Set BURNRATE_PYTHON. Checked: $detail"
}

function Get-BurnrateArgumentList {
    # Windowless autostart: pythonw -m spend_app (spend_app.service).
    # Foreground equivalent: burnrate serve
    # --app spend_app.api:app is the process marker uninstall matches.
    @(
        '-m', $script:BurnrateLaunchModule,
        '--host', $script:BurnrateHost,
        '--port', [string]$script:BurnratePort,
        '--app', $script:BurnrateAppSpec
    )
}

function ConvertTo-BurnrateArgumentString {
    param([Parameter(Mandatory = $true)][string[]]$Parts)
    ($Parts | ForEach-Object {
        if ($_ -match '[\s"]') {
            '"' + ($_ -replace '"', '\"') + '"'
        } else {
            $_
        }
    }) -join ' '
}

function Get-BurnrateLaunchSpec {
    param([switch]$AllowMissingPython)
    $arguments = @(Get-BurnrateArgumentList)
    $pythonw = $null
    try {
        $pythonw = Resolve-BurnratePythonw
    } catch {
        if (-not $AllowMissingPython) {
            throw
        }
        $pythonw = 'pythonw.exe'
        Write-Host "pythonw not resolved: $_"
    }
    [pscustomobject]@{
        FilePath         = $pythonw
        ArgumentList     = $arguments
        ArgumentString   = ConvertTo-BurnrateArgumentString -Parts $arguments
        WorkingDirectory = Get-BurnrateWorkingDirectory
        Host             = $script:BurnrateHost
        Port             = $script:BurnratePort
        MutexName        = $script:BurnrateMutexName
        PidFile          = Get-BurnratePidFile
        DataRoot         = Get-BurnrateDataRoot
    }
}

function Test-BurnrateCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $markers = @(
        'spend_app.api',
        'burnrate serve',
        'spend_app.service',
        '-m spend_app'
    )
    foreach ($marker in $markers) {
        if ($CommandLine.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            return $true
        }
    }
    return $false
}

function Get-BurnrateListeners {
    try {
        $rows = @(Get-NetTCPConnection -LocalPort $script:BurnratePort -State Listen -ErrorAction Stop)
    } catch {
        return @()
    }
    $rows | Where-Object {
        $_.LocalAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '::1'
    }
}

function Get-BurnrateProcessById {
    param([int]$ProcessId)
    Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-BurnrateOwnedProcess {
    param($Process)
    if (-not $Process) {
        return $false
    }
    return Test-BurnrateCommandLine -CommandLine $Process.CommandLine
}

function Read-BurnratePidFile {
    $pidFile = Get-BurnratePidFile
    if (-not (Test-Path -LiteralPath $pidFile)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $pidFile -Raw -ErrorAction SilentlyContinue)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }
    $raw = $raw.Trim()
    if ($raw -notmatch '^\d+$') {
        return $null
    }
    return [int]$raw
}

function Write-BurnratePidFile {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    Initialize-BurnrateDirectories
    Set-Content -LiteralPath (Get-BurnratePidFile) -Value ([string]$ProcessId) -Encoding ascii
}

function Remove-BurnratePidFile {
    $pidFile = Get-BurnratePidFile
    if (Test-Path -LiteralPath $pidFile) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

function Test-BurnrateHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri (Get-BurnrateHealthUri)
        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Wait-BurnrateHealth {
    param([int]$Seconds = 30)
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-BurnrateHealth) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "Timed out waiting for $(Get-BurnrateHealthUri)"
}

function Assert-BurnrateLoopbackBind {
    $listeners = @(Get-BurnrateListeners)
    if ($listeners.Count -eq 0) {
        throw "BURNRATE is not listening on $($script:BurnrateHost):$($script:BurnratePort)"
    }
    foreach ($listener in $listeners) {
        if ($listener.LocalAddress -ne '127.0.0.1' -and $listener.LocalAddress -ne '::1') {
            throw "BURNRATE did not bind exclusively to $($script:BurnrateHost):$($script:BurnratePort) (got $($listener.LocalAddress))"
        }
    }
}

function Get-CurrentWindowsIdentityName {
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Get-BurnrateScheduledTask {
    param([string]$TaskName = $script:BurnrateTaskName)
    Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

function Test-BurnrateTaskPayload {
    param($Task)
    if (-not $Task) {
        return $false
    }
    $action = @($Task.Actions) | Select-Object -First 1
    if (-not $action) {
        return $false
    }
    $execute = [string]$action.Execute
    $arguments = [string]$action.Arguments
    if ($execute.IndexOf('pythonw', [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    if ($arguments.IndexOf([string]$script:BurnratePort, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    if ($arguments.IndexOf($script:BurnrateHost, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $false
    }
    return Test-BurnrateCommandLine -CommandLine "$execute $arguments"
}

function Register-BurnrateDashboardTask {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [Parameter(Mandatory = $true)]$Launch,
        [switch]$DryRun
    )

    if (-not $DryRun) {
        $existing = Get-BurnrateScheduledTask -TaskName $TaskName
        if ($existing -and -not (Test-BurnrateTaskPayload -Task $existing)) {
            throw "Scheduled task '$TaskName' exists but is not a BURNRATE dashboard task. Aborting."
        }
    }

    $identity = Get-CurrentWindowsIdentityName
    $action = New-ScheduledTaskAction -Execute $Launch.FilePath -Argument $Launch.ArgumentString -WorkingDirectory $Launch.WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -RestartCount $script:BurnrateRestartCount `
        -RestartInterval $script:BurnrateRestartInterval `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -Hidden
    $description = 'BURNRATE dashboard. Windowless pythonw bound to 127.0.0.1:17331 at sign-in. RestartCount 3 / RestartInterval 1 minute covers a failed launch; the BURNRATE Watchdog task restarts a stalled service.'
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description

    if ($DryRun) {
        Write-Host "DryRun: would register scheduled task '$TaskName' (AtLogOn, Limited, Hidden, RestartCount $($script:BurnrateRestartCount))"
        Write-Host "DryRun: action $($Launch.FilePath) $($Launch.ArgumentString)"
        return
    }

    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName'"
}

function Unregister-BurnrateDashboardTask {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$DryRun
    )
    $existing = Get-BurnrateScheduledTask -TaskName $TaskName
    if (-not $existing) {
        Write-Host "Scheduled task '$TaskName' is not registered."
        return
    }
    if ($DryRun) {
        Write-Host "DryRun: would unregister scheduled task '$TaskName'"
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'"
}

function Test-BurnrateWatchdogPayload {
    param($Task)
    if (-not $Task) {
        return $false
    }
    $action = @($Task.Actions) | Select-Object -First 1
    if (-not $action) {
        return $false
    }
    $execute = [string]$action.Execute
    $arguments = [string]$action.Arguments
    return ($execute.IndexOf('pythonw', [StringComparison]::OrdinalIgnoreCase) -ge 0) -and
        ($arguments.IndexOf($script:BurnrateWatchdogModule, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function Register-BurnrateWatchdogTask {
    param(
        [string]$TaskName = $script:BurnrateWatchdogTaskName,
        [Parameter(Mandatory = $true)]$Launch,
        [switch]$DryRun
    )

    if (-not $DryRun) {
        $existing = Get-BurnrateScheduledTask -TaskName $TaskName
        if ($existing -and -not (Test-BurnrateWatchdogPayload -Task $existing)) {
            throw "Scheduled task '$TaskName' exists but is not a BURNRATE watchdog task. Aborting."
        }
    }

    $identity = Get-CurrentWindowsIdentityName
    # pythonw keeps every run windowless; powershell.exe would flash a console.
    $action = New-ScheduledTaskAction -Execute $Launch.FilePath -Argument "-m $($script:BurnrateWatchdogModule)" -WorkingDirectory $Launch.WorkingDirectory
    $trigger = New-ScheduledTaskTrigger -Once -At ([DateTime]::Now.AddMinutes(1)) -RepetitionInterval $script:BurnrateWatchdogInterval
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
        -StartWhenAvailable `
        -Hidden
    $description = 'BURNRATE watchdog. Every 5 minutes, restarts the dashboard on 127.0.0.1:17331 when it stops answering or its collection stalls. Leaves an explicitly stopped dashboard alone; at most 3 restarts an hour.'
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description

    if ($DryRun) {
        Write-Host "DryRun: would register scheduled task '$TaskName' (every $($script:BurnrateWatchdogInterval.TotalMinutes) minutes, Limited, Hidden)"
        Write-Host "DryRun: action $($Launch.FilePath) -m $($script:BurnrateWatchdogModule)"
        return
    }

    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Host "Registered scheduled task '$TaskName'"
}

function Unregister-BurnrateWatchdogTask {
    param(
        [string]$TaskName = $script:BurnrateWatchdogTaskName,
        [switch]$DryRun
    )
    $existing = Get-BurnrateScheduledTask -TaskName $TaskName
    if (-not $existing) {
        Write-Host "Scheduled task '$TaskName' is not registered."
        return
    }
    if (-not (Test-BurnrateWatchdogPayload -Task $existing)) {
        Write-Host "Scheduled task '$TaskName' is not a BURNRATE watchdog task; leaving it."
        return
    }
    if ($DryRun) {
        Write-Host "DryRun: would unregister scheduled task '$TaskName'"
        return
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered scheduled task '$TaskName'"
}

function Start-BurnrateDashboard {
    param([switch]$DryRun)

    if (-not $DryRun) {
        Initialize-BurnrateDirectories
    }
    $launch = Get-BurnrateLaunchSpec -AllowMissingPython:$DryRun

    if ($DryRun) {
        Write-Host "DryRun: would start pythonw -m spend_app ($($launch.FilePath) $($launch.ArgumentString))"
        Write-Host "DryRun: working directory $($launch.WorkingDirectory)"
        Write-Host "DryRun: mutex $($script:BurnrateMutexName); pid file $($launch.PidFile)"
        return
    }

    Clear-BurnrateStopMarker
    $listeners = @(Get-BurnrateListeners)

    if ($listeners.Count -gt 0) {
        $foreign = $false
        foreach ($listener in $listeners) {
            $process = Get-BurnrateProcessById -ProcessId ([int]$listener.OwningProcess)
            if (Test-BurnrateOwnedProcess -Process $process) {
                Write-BurnratePidFile -ProcessId ([int]$listener.OwningProcess)
                Write-Host "BURNRATE dashboard already listening on $($script:BurnrateHost):$($script:BurnratePort) (PID $($listener.OwningProcess))"
                return
            }
            $foreign = $true
        }
        if ($foreign) {
            $owner = $listeners[0]
            throw "Port $($script:BurnratePort) is already owned by PID $($owner.OwningProcess). Not stealing it."
        }
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $launch.FilePath
    $psi.Arguments = $launch.ArgumentString
    $psi.WorkingDirectory = $launch.WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $started = [System.Diagnostics.Process]::Start($psi)
    if (-not $started) {
        throw "Failed to start $($launch.FilePath)"
    }
    Write-BurnratePidFile -ProcessId $started.Id
    try {
        Wait-BurnrateHealth
        Assert-BurnrateLoopbackBind
    } catch {
        throw
    }
    Write-Host "BURNRATE dashboard listening on $($script:BurnrateHost):$($script:BurnratePort) (PID $($started.Id))"
}

function Stop-BurnrateDashboard {
    param([switch]$DryRun)

    $targets = @()
    $pidValue = Read-BurnratePidFile
    if ($pidValue) {
        $fromFile = Get-BurnrateProcessById -ProcessId $pidValue
        if (Test-BurnrateOwnedProcess -Process $fromFile) {
            $ownsPort = @(Get-BurnrateListeners | Where-Object { [int]$_.OwningProcess -eq $pidValue }).Count -gt 0
            if ($ownsPort) {
                $targets += $pidValue
            } else {
                Write-Host "PID file $pidValue looks like BURNRATE but is not listening on port $($script:BurnratePort); leaving it running."
            }
        } elseif ($fromFile -and -not $fromFile.CommandLine) {
            Write-Host "Refusing to stop PID $pidValue because the command line is unreadable."
        } elseif ($fromFile) {
            Write-Host "PID file $pidValue is not a BURNRATE dashboard process; leaving it running."
        }
    }

    foreach ($listener in @(Get-BurnrateListeners)) {
        $processId = [int]$listener.OwningProcess
        $process = Get-BurnrateProcessById -ProcessId $processId
        if ((Test-BurnrateOwnedProcess -Process $process) -and ($targets -notcontains $processId)) {
            $targets += $processId
        }
    }

    if ($targets.Count -eq 0) {
        if (-not $DryRun) {
            Remove-BurnratePidFile
        }
        Write-Host 'BURNRATE dashboard is not running.'
        return
    }

    foreach ($processId in $targets) {
        if ($DryRun) {
            Write-Host "DryRun: would stop BURNRATE dashboard PID $processId (port $($script:BurnratePort) only)"
            continue
        }
        Stop-Process -Id $processId -ErrorAction SilentlyContinue
        Write-Host "Stopped BURNRATE dashboard (PID $processId)"
    }
    if (-not $DryRun) {
        Remove-BurnratePidFile
    }
}

function Stop-BurnrateStartingInstance {
    # A dashboard stuck before it listens still holds the single-instance mutex,
    # so every new start exits at once. Only the restart path calls this, after
    # the listener is gone; it matches the service launch line on this port and
    # never the watchdog (spend_app.watchdog) or other spend_app commands.
    param([switch]$DryRun)
    $port = [string]$script:BurnratePort
    $stuck = @(Get-CimInstance -ClassName Win32_Process -Filter "CommandLine like '%$($script:BurnrateLaunchModule)%'" -ErrorAction SilentlyContinue | Where-Object {
        $line = [string]$_.CommandLine
        $_.ProcessId -ne $PID -and $line.IndexOf("--port $port", [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    foreach ($process in $stuck) {
        if ($DryRun) {
            Write-Host "DryRun: would stop unresponsive BURNRATE dashboard PID $($process.ProcessId)"
            continue
        }
        Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
        Write-Host "Stopped unresponsive BURNRATE dashboard (PID $($process.ProcessId))"
    }
}

function Restart-BurnrateDashboard {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$DryRun
    )
    Stop-BurnrateDashboard -DryRun:$DryRun
    Stop-BurnrateStartingInstance -DryRun:$DryRun
    $task = Get-BurnrateScheduledTask -TaskName $TaskName
    $viaTask = $task -and (Test-BurnrateTaskPayload -Task $task)
    if ($DryRun) {
        if ($viaTask) {
            Write-Host "DryRun: would start scheduled task '$TaskName'"
        } else {
            Start-BurnrateDashboard -DryRun
        }
        return
    }
    if (-not $viaTask) {
        Start-BurnrateDashboard
        return
    }
    # Start through the sign-in task so the service runs exactly as it does at
    # logon, owned by Task Scheduler rather than by this short-lived script.
    Clear-BurnrateStopMarker
    if ($task.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Start-ScheduledTask -TaskName $TaskName
    try {
        Wait-BurnrateHealth -Seconds 120
        Write-Host "Restarted BURNRATE dashboard through scheduled task '$TaskName'"
        return
    } catch {
        Write-Host "The dashboard started by '$TaskName' did not answer; starting it directly."
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Stop-BurnrateStartingInstance
    Start-BurnrateDashboard
}

function Get-BurnrateStatus {
    param([string]$TaskName = $script:BurnrateTaskName)
    $task = Get-BurnrateScheduledTask -TaskName $TaskName
    $watchdog = Get-BurnrateScheduledTask -TaskName $script:BurnrateWatchdogTaskName
    $listeners = @(Get-BurnrateListeners)
    $listener = $listeners | Select-Object -First 1
    $pidValue = if ($listener) { [int]$listener.OwningProcess } else { Read-BurnratePidFile }
    $healthy = Test-BurnrateHealth
    [pscustomobject]@{
        task         = $TaskName
        registered   = [bool]$task
        hidden       = if ($task) { [bool]$task.Settings.Hidden } else { $null }
        restartCount = if ($task) { $task.Settings.RestartCount } else { $null }
        watchdog     = if ($watchdog) { [string]$watchdog.State } else { 'not registered' }
        stopped      = Test-Path -LiteralPath (Get-BurnrateStopMarker)
        host         = $script:BurnrateHost
        port         = $script:BurnratePort
        listening    = [bool]$listener
        address      = if ($listener) { $listener.LocalAddress } else { $null }
        pid          = $pidValue
        health       = if ($healthy) { 'ok' } else { 'down' }
        pidFile      = Get-BurnratePidFile
        dataRoot     = Get-BurnrateDataRoot
        mutex        = $script:BurnrateMutexName
    }
}

function Get-BurnratePeSubsystem {
    # 2 = Windows GUI (no console), 3 = console.
    param([Parameter(Mandatory = $true)][string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    $peOffset = [BitConverter]::ToInt32($bytes, 0x3C)
    return [int][BitConverter]::ToUInt16($bytes, $peOffset + 0x18 + 0x44)
}

function Repair-BurnrateWindowlessLauncher {
    # Some uv builds write the console launcher as .venv\Scripts\pythonw.exe, so a
    # windowless start opens a console window every time (and closing that window
    # ends the process). Swap in the base interpreter's windowless venv launcher.
    param(
        [Parameter(Mandatory = $true)][string]$VenvDir,
        [switch]$DryRun
    )
    $pythonw = Join-Path $VenvDir 'Scripts\pythonw.exe'
    $config = Join-Path $VenvDir 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $pythonw) -or -not (Test-Path -LiteralPath $config)) {
        return
    }
    if ((Get-BurnratePeSubsystem -Path $pythonw) -eq 2) {
        return
    }
    $homeLine = Get-Content -LiteralPath $config | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
    $launcher = if ($homeLine) { Join-Path (($homeLine -split '=', 2)[1].Trim()) 'Lib\venv\scripts\nt\pythonw.exe' } else { $null }
    if (-not $launcher -or -not (Test-Path -LiteralPath $launcher) -or (Get-BurnratePeSubsystem -Path $launcher) -ne 2) {
        Write-Host "Warning: $pythonw opens a console window and no windowless venv launcher was found to replace it."
        return
    }
    if ($DryRun) {
        Write-Host "DryRun: would replace the console launcher $pythonw with $launcher"
        return
    }
    # A running launcher cannot be overwritten, but it can be renamed aside.
    $backup = "$pythonw.console-$([DateTime]::UtcNow.ToString('yyyyMMddHHmmss'))"
    Move-Item -LiteralPath $pythonw -Destination $backup
    Copy-Item -LiteralPath $launcher -Destination $pythonw
    Write-Host "Replaced the console launcher $pythonw with the windowless venv launcher."
}

function Initialize-BurnrateRepoEnvironment {
    param([switch]$DryRun)
    $repoRoot = Get-BurnrateRepoRoot
    $pyproject = Join-Path $repoRoot 'pyproject.toml'
    if (-not (Test-Path -LiteralPath $pyproject)) {
        return
    }
    $venvDir = Join-Path $repoRoot '.venv'
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    $venvPythonw = Join-Path $venvDir 'Scripts\pythonw.exe'
    $venvBurnrate = Join-Path $venvDir 'Scripts\burnrate.exe'

    if ($DryRun) {
        Write-Host "DryRun: would ensure Python 3.12 virtualenv at $venvDir and initialize database"
        Repair-BurnrateWindowlessLauncher -VenvDir $venvDir -DryRun
        return
    }

    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if (-not (Test-Path -LiteralPath $venvPythonw)) {
        Write-Host 'Creating Python 3.12 virtual environment (.venv)...'
        if ($uvCmd) {
            & $uvCmd.Source venv --python 3.12 $venvDir
        } else {
            $pyCmd = Get-Command py -ErrorAction SilentlyContinue
            if ($pyCmd) {
                & $pyCmd.Source -3.12 -m venv $venvDir
            } else {
                & python -m venv $venvDir
            }
        }
    }

    Write-Host 'Installing BURNRATE package into .venv...'
    Push-Location $repoRoot
    try {
        if ($uvCmd) {
            & $uvCmd.Source pip install . --python $venvPython
        } else {
            & $venvPython -m pip install .
        }
    } finally {
        Pop-Location
    }

    Repair-BurnrateWindowlessLauncher -VenvDir $venvDir

    if (Test-Path -LiteralPath $venvPython) {
        Write-Host 'Initializing BURNRATE database...'
        & $venvPython -m spend_app init | Out-Null
    }
}

function Stop-BurnrateDesktopProcesses {
    Get-Process -Name 'burnrate-desktop' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='msedgewebview2.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*burnrate-desktop.exe*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Resolve-BurnrateDesktopBinary {
    $repoRoot = Get-BurnrateRepoRoot
    $dataRoot = Get-BurnrateDataRoot
    $installedExe = Join-Path $dataRoot 'burnrate-desktop.exe'
    $localReleaseExe = Join-Path $repoRoot 'src-tauri\target\release\burnrate-desktop.exe'

    if (Test-Path -LiteralPath $localReleaseExe) {
        Stop-BurnrateDesktopProcesses
        Copy-Item -LiteralPath $localReleaseExe -Destination $installedExe -Force
        return $installedExe
    }

    $expectedVersion = $null
    $tauriConfPath = Join-Path $repoRoot 'src-tauri\tauri.conf.json'
    if (Test-Path -LiteralPath $tauriConfPath) {
        try {
            $tauriConf = Get-Content -LiteralPath $tauriConfPath -Raw | ConvertFrom-Json
            $expectedVersion = [string]$tauriConf.version
        } catch {
        }
    }

    $installedVersion = $null
    if (Test-Path -LiteralPath $installedExe) {
        try {
            $installedVersion = [string](Get-Item -LiteralPath $installedExe).VersionInfo.ProductVersion
        } catch {
        }
        if ($expectedVersion -and $installedVersion -eq $expectedVersion) {
            return $installedExe
        }
    }

    $originUrl = ''
    try {
        $originUrl = (& git -C $repoRoot config --get remote.origin.url 2>$null).Trim()
    } catch {
    }
    $repoBase = if ($originUrl -match '^https://github\.com/[^/]+/[^/.]+') {
        $Matches[0]
    } else {
        'https://github.com/kamol-nazarov/myBurnRateOfficial'
    }
    $releaseUrl = "$repoBase/releases/latest/download/burnrate-desktop.exe"
    $tmpExe = "$installedExe.download"
    try {
        Write-Host 'Downloading native BURNRATE desktop shell...'
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 25 -Uri $releaseUrl -OutFile $tmpExe
        if ((Test-Path -LiteralPath $tmpExe) -and ((Get-Item -LiteralPath $tmpExe).Length -gt 1000000)) {
            Stop-BurnrateDesktopProcesses
            Move-Item -LiteralPath $tmpExe -Destination $installedExe -Force
            return $installedExe
        }
    } catch {
        Remove-Item -LiteralPath $tmpExe -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $installedExe) {
        return $installedExe
    }

    $cargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
    $manifestPath = Join-Path $repoRoot 'src-tauri\Cargo.toml'
    if ($cargoCmd -and (Test-Path -LiteralPath $manifestPath)) {
        try {
            Write-Host 'Building native BURNRATE desktop shell with Cargo...'
            & $cargoCmd.Source build --release --manifest-path $manifestPath
            if (Test-Path -LiteralPath $localReleaseExe) {
                Copy-Item -LiteralPath $localReleaseExe -Destination $installedExe -Force
                return $installedExe
            }
        } catch {
        }
    }

    return $null
}

function Install-BurnrateShortcuts {
    param([switch]$DryRun)
    $repoRoot = Get-BurnrateRepoRoot
    $dataRoot = Get-BurnrateDataRoot
    $iconSource = Join-Path $repoRoot 'src-tauri\icons\icon.ico'
    $iconDest = Join-Path $dataRoot 'burnrate.ico'

    if ($DryRun) {
        Write-Host "DryRun: would install custom icon ($iconDest) and Desktop/Start Menu shortcuts"
        return
    }

    if (Test-Path -LiteralPath $iconSource) {
        Copy-Item -LiteralPath $iconSource -Destination $iconDest -Force
    }

    $desktopExe = Resolve-BurnrateDesktopBinary
    $targetPath = $desktopExe
    $arguments = ''
    if (-not $targetPath) {
        $edgePaths = @(
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
        )
        foreach ($candidate in $edgePaths) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) {
                $targetPath = $candidate
                $arguments = "--app=http://$($script:BurnrateHost):$($script:BurnratePort)/?app=1"
                break
            }
        }
    }
    if (-not $targetPath) {
        return
    }

    try {
        $wsh = New-Object -ComObject WScript.Shell
        $shortcutDirs = @(
            [Environment]::GetFolderPath('Programs'),
            [Environment]::GetFolderPath('Desktop')
        )
        if (-not ("BurnrateShortcutAppId" -as [type])) {
            Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public class BurnrateShortcutAppId {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class CShellLink { }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
    internal interface IPropertyStore {
        int GetCount(out uint cProps);
        int GetAt(uint iProp, out PROPERTYKEY pkey);
        int GetValue(ref PROPERTYKEY key, out PropVariant pv);
        int SetValue(ref PROPERTYKEY key, ref PropVariant pv);
        int Commit();
    }
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr pszVal; }
    public static void SetAppId(string lnkPath, string appId) {
        var link = new CShellLink();
        var persist = (IPersistFile)link;
        persist.Load(lnkPath, 2);
        var store = (IPropertyStore)link;
        var pkey = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };
        var pv = new PropVariant { vt = 31, pszVal = Marshal.StringToCoTaskMemUni(appId) };
        store.SetValue(ref pkey, ref pv);
        store.Commit();
        persist.Save(lnkPath, true);
        Marshal.FreeCoTaskMem(pv.pszVal);
    }
}
"@
        }
        $lnkPaths = New-Object System.Collections.Generic.List[string]
        foreach ($dir in $shortcutDirs) {
            if (-not [string]::IsNullOrWhiteSpace($dir) -and (Test-Path -LiteralPath $dir)) {
                $lnkPaths.Add((Join-Path $dir 'BURNRATE.lnk'))
            }
        }
        $extraLnks = @(
            (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Start BURNRATE.lnk'),
            (Join-Path $env:APPDATA 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\BURNRATE.lnk')
        )
        foreach ($extra in $extraLnks) {
            if ($extra -and (Test-Path -LiteralPath $extra)) {
                $lnkPaths.Add($extra)
            }
        }
        foreach ($lnkPath in $lnkPaths) {
            $sc = $wsh.CreateShortcut($lnkPath)
            $sc.TargetPath = $targetPath
            $sc.Arguments = if ($arguments) { $arguments } else { '' }
            $sc.WorkingDirectory = Get-BurnrateWorkingDirectory
            if (Test-Path -LiteralPath $iconDest) {
                $sc.IconLocation = "$iconDest,0"
            }
            $sc.Description = 'BURNRATE AI Spend Telemetry Dashboard'
            $sc.Save()
            try {
                [BurnrateShortcutAppId]::SetAppId($lnkPath, 'dev.burnrate.desktop')
            } catch {
            }
        }
        Write-Host 'Installed BURNRATE shortcuts (Desktop & Start Menu) with custom logo.'
    } catch {
        Write-Host "Note: shortcut creation skipped ($($_.Exception.Message))"
    }
}

function Start-BurnrateDesktopWindow {
    param(
        [switch]$OpenSetup,
        [switch]$DryRun
    )
    $setupUri = "http://$($script:BurnrateHost):$($script:BurnratePort)/setup"
    $appUri = if ($OpenSetup) { $setupUri } else { "http://$($script:BurnrateHost):$($script:BurnratePort)/?app=1" }

    if ($DryRun) {
        Write-Host "DryRun: would launch BURNRATE desktop window at $appUri"
        return
    }

    if (-not [string]::IsNullOrWhiteSpace($env:PYTEST_CURRENT_TEST)) {
        return
    }

    $desktopExe = Resolve-BurnrateDesktopBinary
    if ($desktopExe -and (Test-Path -LiteralPath $desktopExe)) {
        Get-Process -Name 'burnrate-desktop' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $desktopExe
        $psi.WorkingDirectory = Get-BurnrateWorkingDirectory
        $psi.UseShellExecute = $false
        if ($OpenSetup) {
            $psi.EnvironmentVariables['BURNRATE_OPEN_SETUP'] = '1'
        }
        [void][System.Diagnostics.Process]::Start($psi)
        Write-Host "Launched BURNRATE native desktop app ($appUri)"
        return
    }

    Start-Process $appUri | Out-Null
    Write-Host "Opened BURNRATE Setup Wizard ($appUri)"
}

function Install-BurnrateDashboard {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$NoLaunch,
        [switch]$DryRun
    )
    if (-not $DryRun) {
        Initialize-BurnrateDirectories
    }
    Initialize-BurnrateRepoEnvironment -DryRun:$DryRun
    $launch = Get-BurnrateLaunchSpec -AllowMissingPython:$DryRun
    Write-Host "pythonw: $($launch.FilePath)"
    Write-Host "bind: $($script:BurnrateHost):$($script:BurnratePort)"
    Write-Host "mutex: $($script:BurnrateMutexName)"
    Write-Host "pid file: $($launch.PidFile)"
    Write-Host "data root: $($launch.DataRoot)"
    Write-Host "self-heal: scheduled task RestartCount $($script:BurnrateRestartCount) / RestartInterval $($script:BurnrateRestartInterval)"
    Write-Host "watchdog: '$($script:BurnrateWatchdogTaskName)' every $($script:BurnrateWatchdogInterval.TotalMinutes) minutes"
    Register-BurnrateDashboardTask -TaskName $TaskName -Launch $launch -DryRun:$DryRun
    Register-BurnrateWatchdogTask -Launch $launch -DryRun:$DryRun
    if ($DryRun) {
        Start-BurnrateDashboard -DryRun
        Install-BurnrateShortcuts -DryRun
        if (-not $NoLaunch) {
            Start-BurnrateDesktopWindow -OpenSetup -DryRun
        }
        return
    }
    if (Test-BurnrateHealth) {
        Stop-BurnrateDashboard
    }
    Start-BurnrateDashboard
    Install-BurnrateShortcuts
    if (-not $NoLaunch) {
        Start-BurnrateDesktopWindow -OpenSetup
    }
}

function Uninstall-BurnrateDashboard {
    param(
        [string]$TaskName = $script:BurnrateTaskName,
        [switch]$PurgeData,
        [switch]$DryRun
    )
    Unregister-BurnrateWatchdogTask -DryRun:$DryRun
    Stop-BurnrateDashboard -DryRun:$DryRun
    Unregister-BurnrateDashboardTask -TaskName $TaskName -DryRun:$DryRun
    $dbPath = Get-BurnrateDbPath
    $dataRoot = Get-BurnrateDataRoot
    if ($PurgeData) {
        if ($DryRun) {
            Write-Host "DryRun: would purge data root $dataRoot"
            return
        }
        if (Test-Path -LiteralPath $dataRoot) {
            Remove-Item -LiteralPath $dataRoot -Recurse -Force
            Write-Host "Purged $dataRoot"
        }
        return
    }
    Write-Host "Database kept at $dbPath (pass -PurgeData to delete)."
}
