from __future__ import annotations

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spend_app import service as burnrate_service

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

PUBLIC_SCRIPTS = (
    "install-burnrate.ps1",
    "uninstall-burnrate.ps1",
    "start-burnrate.ps1",
    "stop-burnrate.ps1",
    "restart-burnrate.ps1",
    "status-burnrate.ps1",
    "BurnrateRuntime.ps1",
    "Bench-Burnrate.ps1",
)

PRIVATE_SCRIPTS = (
    "Start-AiTelemetry.ps1",
    "Stop-AiTelemetry.ps1",
    "Status-AiTelemetry.ps1",
    "Test-AiTelemetry.ps1",
    "Install-Binaries.ps1",
    "Enable-AiTelemetryAutostart.ps1",
    "Disable-AiTelemetryAutostart.ps1",
    "Watch-AiTelemetry.ps1",
)

FORBIDDEN_TOKENS = (
    "3060",
    "3050",
    "3000",
    "8888",
    "8889",
    "9090",
    "9470",
    "4317",
    "4318",
    "13133",
    "3443",
    "3445",
    "tailscale",
    "Tailscale",
    ".ts.net",
    "AiTelemetry",
    "Watch-AiTelemetry",
    "Self-Healing Monitor",
    "VehicleDesk",
    "vehicledesk",
    # "kamol" is the public GitHub owner in the release URL, not a private
    # stack token; the local-path guards below cover home-directory leaks.
    "ai-telemetry",
    "0.0.0.0",
    "C:\\Users\\",
    r"C:\Users",
)


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


def _all_scripts_text() -> str:
    return "\n".join(_read(name) for name in PUBLIC_SCRIPTS)


def _powershell() -> str:
    for name in ("pwsh", "powershell"):
        path = shutil.which(name)
        if path:
            return path
    pytest.skip("PowerShell is not available")


def test_private_six_process_scripts_are_gone() -> None:
    for name in PRIVATE_SCRIPTS:
        assert not (SCRIPTS / name).exists(), name
    remaining = {path.name for path in SCRIPTS.glob("*")}
    for name in PRIVATE_SCRIPTS:
        assert name not in remaining


def test_public_lifecycle_scripts_exist() -> None:
    for name in PUBLIC_SCRIPTS:
        assert (SCRIPTS / name).is_file(), name


def test_public_scripts_bind_localhost_17331() -> None:
    corpus = _all_scripts_text()
    assert "127.0.0.1" in corpus
    assert "17331" in corpus
    assert "http://127.0.0.1:17331" in _read("Bench-Burnrate.ps1")
    install = _read("install-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "127.0.0.1" in install
    assert "17331" in install
    start = _read("start-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "127.0.0.1" in start
    assert "17331" in start


def test_public_scripts_use_pythonw_and_task_name() -> None:
    install = _read("install-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "BURNRATE Dashboard" in install
    assert "pythonw" in install
    assert "-m spend_app" in install
    assert "burnrate serve" in install
    assert "AtLogOn" in install
    assert "Limited" in install
    assert "Hidden" in install
    assert r"Local\BURNRATE-Dashboard" in install
    assert "LOCALAPPDATA" in install
    assert "RestartCount" in install
    assert "spend_app.api" in install


def test_install_registers_windowless_watchdog() -> None:
    install = _read("install-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "BURNRATE Watchdog" in install
    assert "spend_app.watchdog" in install
    assert "Register-BurnrateWatchdogTask" in _read("BurnrateRuntime.ps1")
    uninstall = _read("BurnrateRuntime.ps1")
    assert "Unregister-BurnrateWatchdogTask" in uninstall[uninstall.index("function Uninstall-BurnrateDashboard"):]
    # An explicit stop is respected; any start clears it.
    assert "Set-BurnrateStopMarker" in _read("stop-burnrate.ps1")
    assert "Clear-BurnrateStopMarker" in uninstall[uninstall.index("function Start-BurnrateDashboard"):]
    assert "Restart-BurnrateDashboard" in _read("restart-burnrate.ps1")


def test_uninstall_identifies_public_process_and_keeps_db() -> None:
    uninstall = _read("uninstall-burnrate.ps1") + "\n" + _read("BurnrateRuntime.ps1")
    assert "BURNRATE Dashboard" in uninstall
    assert "17331" in uninstall
    assert "spend_app.api" in uninstall
    assert "burnrate serve" in uninstall
    assert "-PurgeData" in uninstall
    assert "Database kept" in uninstall
    assert "spend.db" in uninstall
    purge_index = uninstall.index("if ($PurgeData)")
    kept_index = uninstall.index("Database kept")
    assert kept_index != purge_index
    assert "Remove-Item -LiteralPath $dataRoot" in uninstall[purge_index:]
    assert "Remove-Item -LiteralPath $dataRoot" not in uninstall[:purge_index]


def test_public_scripts_have_no_private_stack_tokens() -> None:
    corpus = _all_scripts_text()
    for token in FORBIDDEN_TOKENS:
        assert token not in corpus, token
    service_text = (ROOT / "spend_app" / "service.py").read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in service_text, token


def test_bench_defaults_to_public_port() -> None:
    script = _read("Bench-Burnrate.ps1")
    assert "$BaseUrl = 'http://127.0.0.1:17331'" in script
    assert "3060" not in script


def test_stop_only_targets_public_listener() -> None:
    runtime = _read("BurnrateRuntime.ps1")
    assert "LocalPort $script:BurnratePort" in runtime
    assert "Get-NetTCPConnection -LocalPort $script:BurnratePort" in runtime
    assert "spend_app.api" in runtime
    assert "burnrate serve" in runtime
    assert "17331" in runtime
    assert "Stop-Process" in runtime
    assert "3060" not in runtime
    assert "not listening on port" in runtime


LIFECYCLE_PROCESS_FIXTURE = r"""
$script:fixtureProcessId = 4242
$script:fixtureListening = $true
function Get-ItemPropertyValue {
    [CmdletBinding()]
    param($LiteralPath, $Name)
    if ($LiteralPath -ne 'HKCU:\Software\BURNRATE Native' -or $Name -ne 'InstallDir') {
        throw 'Unexpected registry lookup'
    }
    return $env:BR42_INSTALL_DIRECTORY
}
function Get-NetTCPConnection {
    [CmdletBinding()]
    param($LocalPort, $State)
    if ($LocalPort -ne [int]$env:BURNRATE_PORT -or $State -ne 'Listen') {
        throw 'Unexpected listener lookup'
    }
    if ($script:fixtureListening) {
        [pscustomobject]@{
            OwningProcess = $script:fixtureProcessId
            LocalAddress = $env:BR42_LISTEN_ADDRESS
            LocalPort = [int]$env:BR42_LISTEN_PORT
        }
    }
}
function Get-CimInstance {
    [CmdletBinding()]
    param($ClassName, $Filter)
    if ($ClassName -ne 'Win32_Process') {
        throw 'Unexpected process lookup'
    }
    if ($script:fixtureListening -and $Filter -eq "ProcessId=$script:fixtureProcessId") {
        [pscustomobject]@{
            ProcessId = $script:fixtureProcessId
            CreationDate = [datetime]'2025-01-01'
            ExecutablePath = $env:BR42_PROCESS_EXE
            CommandLine = $env:BR42_COMMAND_LINE
        }
    }
}
function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$PassThru)
    Add-Content -LiteralPath $env:BR42_TRACE -Value "stop:$Id"
    if ($Id -ne $script:fixtureProcessId) {
        throw "Stopped an unexpected PID: $Id"
    }
    if ($PassThru) {
        $process = [pscustomobject]@{Id = $Id}
        $process | Add-Member ScriptMethod WaitForExit {
            param([int]$Milliseconds)
            $script:fixtureListening = $false
            return $Milliseconds -gt 0
        }
        return $process
    }
}
function Get-BurnrateScheduledTask {
    throw 'This restart should not consult Task Scheduler'
}
function Resolve-BurnratePythonw {
    if (-not $env:BR42_PYTHON) {
        throw 'This packaged-only installation has no Python environment'
    }
    return $env:BR42_PYTHON
}
function Wait-BurnrateHealth {}
function Start-BurnrateDashboardProcess {
    param($Launch)
    Add-Content -LiteralPath $env:BR42_TRACE -Value "start:$($Launch.FilePath)|$($Launch.ArgumentString)"
    $script:fixtureProcessId = 4243
    $script:fixtureListening = $true
    [pscustomobject]@{Id = $script:fixtureProcessId}
}
"""


def _lifecycle_wrapper_fixture(
    tmp_path: Path, *, registered: bool = True
) -> tuple[dict[str, str], Path, Path]:
    local = tmp_path / "Local"
    program = tmp_path / "Custom Program" if registered else local / "Programs" / "BURNRATE"
    program.mkdir(parents=True)
    installed = program / "BURNRATE.exe"
    installed.touch()
    (program / "BURNRATE.installed").write_text(
        '{"installer":"nsis","version":"0.6.0"}', encoding="utf-8"
    )
    run = tmp_path / "data" / "run"
    run.mkdir(parents=True)
    (run / "burnrate.pid").write_text("4242\n", encoding="ascii")
    trace = tmp_path / "actions"
    trace.touch()
    for script in ("stop-burnrate.ps1", "restart-burnrate.ps1"):
        shutil.copyfile(SCRIPTS / script, tmp_path / script)
    # Keep the wrappers and runtime code intact. Only the OS observations and
    # process effects are synthetic, including a read-only HKCU lookup.
    (tmp_path / "BurnrateRuntime.ps1").write_text(
        _read("BurnrateRuntime.ps1") + "\n" + LIFECYCLE_PROCESS_FIXTURE, encoding="utf-8"
    )
    env = {
        **os.environ,
        "LOCALAPPDATA": str(local),
        "BURNRATE_DATA_ROOT": str(run.parent),
        "BURNRATE_PORT": "17425",
        "BR42_INSTALL_DIRECTORY": str(program) if registered else "",
        "BR42_PROCESS_EXE": str(installed),
        "BR42_COMMAND_LINE": f'"{installed}" --service --port 17425',
        "BR42_LISTEN_ADDRESS": "127.0.0.1",
        "BR42_LISTEN_PORT": "17425",
        "BR42_TRACE": str(trace),
        "BR42_PYTHON": "",
    }
    return env, installed, run


def _run_lifecycle_wrapper(tmp_path: Path, env: dict[str, str], verb: str) -> list[str]:
    completed = subprocess.run(
        [_powershell(), "-NoProfile", "-File", str(tmp_path / f"{verb}-burnrate.ps1")],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return Path(env["BR42_TRACE"]).read_text(encoding="utf-8").splitlines()


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell lifecycle scripts are for Windows")
@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
def test_native_restart_launcher_does_not_hold_capture_pipes_open(
    tmp_path: Path, shell_name: str
) -> None:
    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"{shell_name} is not installed")
    child = r"""
$deadline = [DateTime]::UtcNow.AddSeconds(30)
while ([DateTime]::UtcNow -lt $deadline -and -not (Test-Path -LiteralPath $env:BR42_RELEASE)) {
    Start-Sleep -Milliseconds 100
}
"""
    encoded = base64.b64encode(child.encode("utf-16-le")).decode("ascii")
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    release = tmp_path / "allow-child-to-exit"
    launch = f"""
. {quote(SCRIPTS / 'BurnrateRuntime.ps1')}
$launch = [pscustomobject]@{{
    FilePath = {quote(shell)}
    ArgumentString = '-NoProfile -NonInteractive -EncodedCommand {encoded}'
    WorkingDirectory = {quote(tmp_path)}
    IsNative = $true
}}
$child = Start-BurnrateDashboardProcess -Launch $launch
Write-Output "native child $($child.Id)"
"""
    command = base64.b64encode(launch.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
            env={**os.environ, "BR42_RELEASE": str(release)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("native child ")
    finally:
        release.touch()


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell lifecycle scripts are for Windows")
@pytest.mark.parametrize("registered", [True, False], ids=["custom", "default"])
@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_windows_lifecycle_targets_installed_native_collector(
    tmp_path: Path, registered: bool, verb: str
) -> None:
    env, installed, run = _lifecycle_wrapper_fixture(tmp_path, registered=registered)
    actions = _run_lifecycle_wrapper(tmp_path, env, verb)
    assert actions[0] == "stop:4242"
    if verb == "stop":
        assert actions == ["stop:4242"]
        assert not (run / "burnrate.pid").exists()
        assert (run / "stopped").is_file()
    else:
        assert actions == ["stop:4242", f"start:{installed}|--service --port 17425"]
        assert (run / "burnrate.pid").read_text().strip() == "4243"
        assert not (run / "stopped").exists()


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell lifecycle scripts are for Windows")
@pytest.mark.parametrize(
    "unowned",
    [
        "foreign-image",
        "stale-pid",
        "wrong-role",
        "wrong-port-argument",
        "wrong-listener-port",
        "wildcard",
        "unrelated-python",
    ],
)
def test_windows_stop_leaves_unowned_process_running(tmp_path: Path, unowned: str) -> None:
    env, installed, run = _lifecycle_wrapper_fixture(tmp_path)
    if unowned == "foreign-image":
        foreign = tmp_path / "Foreign BURNRATE" / "BURNRATE.exe"
        foreign.parent.mkdir()
        foreign.touch()
        env["BR42_PROCESS_EXE"] = str(foreign)
        env["BR42_COMMAND_LINE"] = f'"{foreign}" --service --port 17425'
    elif unowned == "stale-pid":
        (run / "burnrate.pid").write_text("4241\n", encoding="ascii")
    elif unowned == "wrong-role":
        env["BR42_COMMAND_LINE"] = f'"{installed}" --demo --port 17425'
    elif unowned == "wrong-port-argument":
        env["BR42_COMMAND_LINE"] = f'"{installed}" --service --port 17426'
    elif unowned == "wrong-listener-port":
        env["BR42_LISTEN_PORT"] = "17426"
    elif unowned == "unrelated-python":
        python = tmp_path / "Other Python" / "python.exe"
        env["BR42_PROCESS_EXE"] = str(python)
        env["BR42_COMMAND_LINE"] = f'"{python}" watch.py --check spend_app.api:app'
    else:
        env["BR42_LISTEN_ADDRESS"] = "0.0.0.0"
    assert _run_lifecycle_wrapper(tmp_path, env, "stop") == []
    assert (run / "burnrate.pid").is_file()


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell lifecycle scripts are for Windows")
@pytest.mark.parametrize("verb", ["stop", "restart"])
def test_windows_lifecycle_preserves_python_collector(tmp_path: Path, verb: str) -> None:
    env, _, run = _lifecycle_wrapper_fixture(tmp_path)
    python = tmp_path / "Synthetic Python" / "pythonw.exe"
    env["BR42_PROCESS_EXE"] = env["BR42_PYTHON"] = str(python)
    env["BR42_COMMAND_LINE"] = f'"{python}" -m spend_app.service --host 127.0.0.1 --port 17425'
    # The synthetic Python installation has no sign-in task.
    runtime = tmp_path / "BurnrateRuntime.ps1"
    with runtime.open("a", encoding="utf-8") as stream:
        stream.write("\nfunction Get-BurnrateScheduledTask { param($TaskName) }\n")
    actions = _run_lifecycle_wrapper(tmp_path, env, verb)
    if verb == "stop":
        assert actions == ["stop:4242"]
        assert not (run / "burnrate.pid").exists()
    else:
        assert actions == [
            "stop:4242",
            f"start:{python}|-m spend_app.service --host 127.0.0.1 --port 17425 --app spend_app.api:app",
        ]
        assert (run / "burnrate.pid").read_text().strip() == "4243"


def test_powershell_scripts_parse() -> None:
    shell = _powershell()
    for name in PUBLIC_SCRIPTS:
        path = SCRIPTS / name
        escaped = str(path).replace("'", "''")
        command = (
            "$e = $null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        completed = subprocess.run(
            [shell, "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, f"{name}: {completed.stdout}{completed.stderr}"


@pytest.mark.skipif(os.name != "nt", reason="the PowerShell install scripts are for Windows")
def test_install_dry_run_does_not_register_live_task(tmp_path: Path) -> None:
    shell = _powershell()
    install = SCRIPTS / "install-burnrate.ps1"
    task_name = "BURNRATE-Lane-I3-DryRun"
    env = os.environ.copy()
    env["BURNRATE_DATA_ROOT"] = str(tmp_path / "data")
    try:
        completed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-File",
                str(install),
                "-DryRun",
                "-TaskName",
                task_name,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        output = completed.stdout + completed.stderr
        assert completed.returncode == 0, output
        assert "DryRun" in output
        assert task_name in output
        assert "127.0.0.1" in output
        assert "17331" in output
        assert "would register" in output.lower()
        assert "BURNRATE Watchdog" in output
        listed = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                f"Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty TaskName",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert listed.stdout.strip() == ""
    finally:
        subprocess.run(
            [
                shell,
                "-NoProfile",
                "-Command",
                f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false -ErrorAction SilentlyContinue",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )


def test_service_defaults_and_loopback_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert burnrate_service.DEFAULT_HOST == "127.0.0.1"
    assert burnrate_service.DEFAULT_PORT == 17331
    assert burnrate_service.DEFAULT_APP == "spend_app.api:app"
    assert burnrate_service.MUTEX_NAME == r"Local\BURNRATE-Dashboard"
    assert burnrate_service.data_root() == tmp_path / "BURNRATE"
    assert burnrate_service.pid_file() == tmp_path / "BURNRATE" / "run" / "burnrate.pid"
    assert burnrate_service.is_loopback("127.0.0.1")
    assert burnrate_service.is_loopback("localhost")
    assert not burnrate_service.is_loopback("0.0.0.0")
    assert not burnrate_service.is_loopback("*")
    args = burnrate_service.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 17331
    assert args.app == "spend_app.api:app"
    with pytest.raises(SystemExit, match="127.0.0.1"):
        burnrate_service.serve(host="0.0.0.0", port=17331)


def test_service_write_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = burnrate_service.write_pid()
    assert path == tmp_path / "BURNRATE" / "run" / "burnrate.pid"
    assert path.read_text(encoding="utf-8") == str(os.getpid())


def _pe_subsystem(path: Path) -> int:
    data = path.read_bytes()
    offset = int.from_bytes(data[0x3C:0x40], "little")
    return int.from_bytes(data[offset + 0x18 + 0x44:offset + 0x18 + 0x46], "little")


def test_install_replaces_a_console_pythonw_launcher(tmp_path: Path) -> None:
    # Regression: uv wrote the console launcher as .venv\Scripts\pythonw.exe, so the
    # dashboard and the 5-minute watchdog each opened a console window.
    import sys

    shell = _powershell()
    base_dir = Path(sys.base_prefix)
    console, windowless = base_dir / "python.exe", base_dir / "pythonw.exe"
    if not (console.is_file() and windowless.is_file()):
        pytest.skip("Windows CPython console and windowless interpreters are not available")
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    shutil.copyfile(console, venv / "Scripts" / "pythonw.exe")
    base = tmp_path / "base"
    (base / "Lib" / "venv" / "scripts" / "nt").mkdir(parents=True)
    shutil.copyfile(windowless, base / "Lib" / "venv" / "scripts" / "nt" / "pythonw.exe")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nversion_info = 3.12\n", encoding="utf-8")
    assert _pe_subsystem(venv / "Scripts" / "pythonw.exe") == 3
    runtime = str(SCRIPTS / "BurnrateRuntime.ps1").replace("'", "''")
    command = f". '{runtime}'; Repair-BurnrateWindowlessLauncher -VenvDir '{venv}'"
    completed = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _pe_subsystem(venv / "Scripts" / "pythonw.exe") == 2
    assert len(list((venv / "Scripts").glob("pythonw.exe.console-*"))) == 1
    # A launcher that is already windowless is left alone.
    again = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=60)
    assert again.returncode == 0 and "Replaced" not in again.stdout
    install = _read("BurnrateRuntime.ps1")
    assert "Repair-BurnrateWindowlessLauncher -VenvDir $venvDir" in install[install.index("function Initialize-BurnrateRepoEnvironment"):]
