# Runs outside the virtual environment it updates. All release bytes were signed
# and verified by the desktop before spawning this process.
$ErrorActionPreference = 'Stop'
$job = $PSScriptRoot
$ready = Join-Path $job 'ready'
$failed = Join-Path $job 'failed'
$runtime = Join-Path $env:LOCALAPPDATA 'BURNRATE\native-python'
$updates = Join-Path (Split-Path $runtime -Parent) 'updates'
$resultFile = Join-Path $updates 'wheel-update-error.txt'
$backupRuntime = Join-Path $job 'previous-runtime'
$python = Join-Path $runtime 'Scripts\python.exe'
$pythonw = Join-Path $runtime 'Scripts\pythonw.exe'
$lock = $null
$wheelLock = $null
$desktop = $null
$desktopParent = $null
$desktopExited = $false
$installStarted = $false
$phase = 'preparing'

function Get-ProcessRecord([int]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId"
}

function Get-CollectorRecords {
    $listeners = @(Get-NetTCPConnection -State Listen | Where-Object {
        $_.LocalPort -eq 17331 -and $_.LocalAddress -in '127.0.0.1','::1','0.0.0.0','::','::ffff:127.0.0.1'
    })
    $owners = @($listeners.OwningProcess | Sort-Object -Unique)
    if ($owners.Count -eq 0) { return }
    if ($owners.Count -ne 1) { throw 'Several processes are listening on the collector port.' }
    $record = Get-ProcessRecord $owners[0]
    $pattern = '^(?:"[^"]+"|\S+)\s+-m\s+spend_app\.service\s+--port\s+17331\s*$'
    if (-not $record -or $record.CommandLine -notmatch $pattern) { throw 'The collector could not be identified.' }
    if ($record.ExecutablePath -in @($python, $pythonw)) { return $record }
    $baseDirs = @((Split-Path $request.basePython -Parent))
    if ($request.basePythonDirs) {
        foreach ($dir in @($request.basePythonDirs)) {
            if ($dir) { $baseDirs += [string]$dir }
        }
    }
    $allowedBase = @($baseDirs | Sort-Object -Unique | ForEach-Object {
        (Join-Path $_ 'python.exe')
        (Join-Path $_ 'pythonw.exe')
    })
    if ($record.ExecutablePath -notin $allowedBase) {
        throw 'The listener is outside the managed Python runtime.'
    }
    $parent = Get-ProcessRecord $record.ParentProcessId
    if (-not $parent -or $parent.ExecutablePath -notin @($python, $pythonw) -or
        $parent.CommandLine -notmatch $pattern -or $parent.CreationDate -gt $record.CreationDate) {
        throw 'The listener is outside the managed Python runtime.'
    }
    $record
    $parent
}

function Stop-VerifiedCollector($record) {
    $p = Get-Process -Id $record.ProcessId -ErrorAction SilentlyContinue
    if (-not $p) { return }
    try {
        $null = $p.Handle
        $current = Get-ProcessRecord $record.ProcessId
        if (-not $current -or $current.CreationDate -ne $record.CreationDate -or
            $current.ExecutablePath -ne $record.ExecutablePath -or $current.CommandLine -ne $record.CommandLine) {
            return
        }
        $p.Kill()
        if (-not $p.WaitForExit(8000)) { throw 'The collector did not exit.' }
    } catch [System.InvalidOperationException] {
        if (-not $p.HasExited) { throw }
    } finally {
        $p.Dispose()
    }
}

function Start-Desktop {
    $app = Start-Process -FilePath $pythonw -ArgumentList '-I -m spend_app desktop' -WorkingDirectory (Split-Path $runtime -Parent) -PassThru
    if ($app.WaitForExit(2500) -and $app.ExitCode -ne 0) { throw 'The desktop did not launch.' }
}

try {
    $request = Get-Content -LiteralPath (Join-Path $job 'request.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($job -notmatch '\\wheel-[a-zA-Z0-9_]+$' -or (Split-Path $job -Parent) -ne $updates -or
        $request.runtime -ne $runtime -or $request.version -notmatch '^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$' -or
        $request.wheel -ne "burnrate-$($request.version)-py3-none-any.whl" -or $request.sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'The update request did not match the managed runtime.'
    }
    foreach ($directory in $runtime, $updates, $job) {
        if ((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'The runtime or update folder is redirected.'
        }
    }
    $wheel = Join-Path $job $request.wheel
    $lock = [IO.File]::Open((Join-Path $updates 'wheel-update.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $wheelLock = [IO.File]::Open($wheel, 'Open', 'Read', 'Read')
    $desktop = Get-Process -Id ([int]$request.desktopPid)
    $null = $desktop.Handle
    $desktopParent = $null
    $desktopRecord = Get-ProcessRecord ([int]$request.desktopPid)
    if ($desktopRecord -and $desktopRecord.ParentProcessId) {
        $parentRec = Get-ProcessRecord $desktopRecord.ParentProcessId
        if ($parentRec -and $parentRec.ExecutablePath -in @($python, $pythonw)) {
            $desktopParent = Get-Process -Id $parentRec.ProcessId -ErrorAction SilentlyContinue
        }
    }
    $null = @(Get-CollectorRecords)
    New-Item -Path $ready -ItemType File | Out-Null
    if (-not $desktop.WaitForExit(45000)) { throw 'The desktop did not exit.' }
    if ($desktopParent) {
        [void]$desktopParent.WaitForExit(5000)
    }
    $desktopExited = $true

    $phase = 'stopping the collector'
    foreach ($record in @(Get-CollectorRecords)) { Stop-VerifiedCollector $record }
    if (@(Get-CollectorRecords).Count) { throw 'A collector is still listening.' }

    $phase = 'backing up usage and preferences'
    @'
import shutil, sqlite3, sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

db = Path(sys.argv[1]).resolve(strict=True)
folder = db.parent / "backups"
folder.mkdir(exist_ok=True)
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
backup = folder / f"pre-wheel-update-{stamp}.db"
with backup.open("xb"):
    pass
with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
    with closing(sqlite3.connect(backup, timeout=10)) as dest:
        source.backup(dest, pages=1024)
        if dest.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SystemExit("The backup failed its integrity check")
prefs = db.parent / "native-window.json"
if prefs.is_file():
    shutil.copy2(prefs, folder / f"native-window.pre-wheel-update-{stamp}.json")
'@ | & $python -I - ([string]$request.database)
    if ($LASTEXITCODE) { throw 'Could not create a verified database backup.' }

    $phase = 'backing up the desktop runtime'
    Copy-Item -LiteralPath $runtime -Destination $backupRuntime -Recurse
    if (-not (Test-Path -LiteralPath (Join-Path $backupRuntime 'Scripts\pythonw.exe'))) {
        throw 'The previous runtime did not back up completely.'
    }

    $phase = 'rebuilding the desktop runtime'
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($sha256.ComputeHash($wheelLock)).Replace('-', '')
    } finally {
        $sha256.Dispose()
    }
    if ($digest -ne $request.sha256) {
        throw 'The staged wheel changed after signature verification.'
    }
    $installStarted = $true
    & $python -I -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec("pip") else 1)'
    if ($LASTEXITCODE) {
        & $python -I -m ensurepip --upgrade *> (Join-Path $job 'ensurepip.log')
        if ($LASTEXITCODE) { throw 'Could not prepare pip in the managed runtime.' }
    }
    & $python -I -m pip --isolated --disable-pip-version-check install --no-index --no-deps --force-reinstall --no-input $wheel *> (Join-Path $job 'pip.log')
    if ($LASTEXITCODE) { throw 'The wheel installation failed.' }
    & $python -I -m pip --isolated --disable-pip-version-check check *>> (Join-Path $job 'pip.log')
    if ($LASTEXITCODE) { throw 'The updated runtime needs different dependencies.' }
    & $python -I -c 'import sys; import spend_app; from spend_app.wealthfront_window import load_ui_html; assert spend_app.__version__ == sys.argv[1]; assert load_ui_html()' ([string]$request.version)
    if ($LASTEXITCODE) { throw 'The updated desktop did not pass its startup checks.' }
    Set-Content -LiteralPath $resultFile -Value '' -Encoding UTF8
    Start-Desktop
} catch {
    $message = "Update stopped while $phase. The previous version is available."
    Set-Content -LiteralPath (Join-Path $job 'worker-error.txt') -Value $_.Exception.Message -Encoding UTF8
    if ($installStarted) {
        try {
            $saved = Get-Item -LiteralPath $backupRuntime
            $failedRuntime = Join-Path $job 'failed-runtime'
            if ($saved.Parent.FullName -ne $job -or ($saved.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
                (Test-Path -LiteralPath $failedRuntime) -or (Get-Item -LiteralPath $runtime).FullName -ne $runtime) {
                throw 'The saved runtime could not be verified for restoration.'
            }
            Move-Item -LiteralPath $runtime -Destination $failedRuntime
            Move-Item -LiteralPath $backupRuntime -Destination $runtime
        } catch {
            $message = 'The desktop update needs recovery. The previous runtime is saved in the updates folder.'
        }
    }
    Set-Content -LiteralPath $failed -Value $message -Encoding UTF8
    if ($lock) { Set-Content -LiteralPath $resultFile -Value $message -Encoding UTF8 }
    if ($desktopExited -and (Test-Path -LiteralPath $pythonw)) { Start-Desktop }
    exit 1
} finally {
    if ($desktopParent) { $desktopParent.Dispose() }
    if ($desktop) { $desktop.Dispose() }
    if ($wheelLock) { $wheelLock.Dispose() }
    if ($lock) { $lock.Dispose() }
}
