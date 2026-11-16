"""Native binary trust checks: httpx trust_env, NSIS hooks, and PowerShell scripts.

All PowerShell tests run hermetically against temporary directories and an
overridden LOCALAPPDATA; nothing touches the real user profile, repository
build directories, or the network.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from spend_app import native_update as nu
from tests_spend import test_native_update as tnu

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

PS_TRUST_FILES = (
    ROOT / "install.ps1",
    SCRIPTS / "install-burnrate.ps1",
    SCRIPTS / "BurnrateRuntime.ps1",
    SCRIPTS / "install-native.ps1",
    SCRIPTS / "minisign-verify.ps1",
)


def _ps_run(shell: str, script: str, *, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def _available_shells() -> list[tuple[str, str]]:
    if os.name != "nt":
        return []
    shells: list[tuple[str, str]] = []
    ps5 = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps5:
        shells.append(("powershell", ps5))
    pwsh = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if pwsh:
        shells.append(("pwsh", pwsh))
    return shells


def _require_ps5() -> str:
    if os.name != "nt":
        pytest.skip("Windows PowerShell tests require Windows")
    ps5 = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps5:
        pytest.skip("powershell.exe 5.1 is not available")
    return ps5


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------------------
# native_update._httpx_fetch
# ---------------------------------------------------------------------------------------


def test_httpx_fetch_disables_trust_env_and_redirects(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class DummyResponse:
        status_code = 200
        headers = {"Content-Type": "application/json"}

        def iter_bytes(self, _size):
            yield b"{}"

        def close(self):
            captured["response_closed"] = True

    class DummyClient:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs

        def build_request(self, method, url):
            captured["request"] = (method, url)
            return ("req", method, url)

        def send(self, req, stream=False):
            captured["send"] = (req, stream)
            return DummyResponse()

        def close(self):
            captured["client_closed"] = True

    monkeypatch.setattr(httpx, "Client", DummyClient)
    fetched = nu._httpx_fetch(nu.FEED_URL, 7.5)
    try:
        kwargs = captured["client_kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        assert kwargs["timeout"] == 7.5
        assert b"".join(fetched.iter_chunks(1024)) == b"{}"
    finally:
        fetched.close()
    assert captured.get("response_closed") is True
    assert captured.get("client_closed") is True


# ---------------------------------------------------------------------------------------
# src-tauri/nsis-hooks.nsh
# ---------------------------------------------------------------------------------------


def test_nsis_hooks_stop_processes_uses_system_taskkill() -> None:
    nsh = (ROOT / "src-tauri" / "nsis-hooks.nsh").read_text(encoding="utf-8")
    match = re.search(r"!macro\s+BURNRATE_STOP_PROCESSES\b(.*?)!macroend", nsh, re.S)
    assert match is not None, "BURNRATE_STOP_PROCESSES macro missing"
    body = match.group(1)
    assert "'\"$SYSDIR\\taskkill.exe\" /F /IM burnrate-desktop.exe'" in body
    assert "'\"$SYSDIR\\taskkill.exe\" /F /IM burnrate-service.exe'" in body


# ---------------------------------------------------------------------------------------
# PowerShell syntax parsing (5.1 + pwsh)
# ---------------------------------------------------------------------------------------


def test_all_trust_powershell_scripts_parse_in_available_shells() -> None:
    shells = _available_shells()
    if not shells:
        pytest.skip("PowerShell is not available")
    for label, shell in shells:
        for script_path in PS_TRUST_FILES:
            cmd = (
                "$errors = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile({_ps_quote(script_path)}, [ref]$null, [ref]$errors); "
                "if ($errors -and $errors.Count -gt 0) { $errors | ForEach-Object { $_.ToString() }; exit 1 }"
            )
            res = _ps_run(shell, cmd)
            assert res.returncode == 0, f"{label} failed to parse {script_path.name}: {res.stdout}{res.stderr}"


# ---------------------------------------------------------------------------------------
# minisign-verify.ps1 & pinned public key
# ---------------------------------------------------------------------------------------


def test_embedded_minisign_public_key_matches_python_module() -> None:
    pattern = re.compile(r"\$MinisignPublicKey\s*=\s*'([^']+)'")
    for path in (SCRIPTS / "minisign-verify.ps1", SCRIPTS / "install-native.ps1"):
        text = path.read_text(encoding="utf-8")
        match = pattern.search(text)
        assert match is not None, f"$MinisignPublicKey missing in {path.name}"
        assert match.group(1) == nu.PUBLIC_KEY_B64, path.name


@pytest.mark.parametrize("alg", [b"ED", b"Ed"])
def test_minisign_verify_ps1_verifies_real_signatures_in_ps5_and_pwsh(tmp_path: Path, alg: bytes) -> None:
    shells = _available_shells()
    if not shells:
        pytest.skip("PowerShell is not available")
    pub, sig = tnu.ossl_files(alg)
    payload = tmp_path / "fixture-setup.exe"
    payload.write_bytes(tnu.OSSL_DATA)
    sig_path = tmp_path / "fixture-setup.exe.sig"
    sig_path.write_text(sig, encoding="utf-8")
    verifier = SCRIPTS / "minisign-verify.ps1"

    for label, shell in shells:
        ok_cmd = (
            f". {_ps_quote(verifier)}; "
            f"Test-MinisignFile -Path {_ps_quote(payload)} "
            f"-SignaturePath {_ps_quote(sig_path)} "
            "-ExpectedFile 'fixture-setup.exe' "
            f"-PublicKey {_ps_quote(pub)}"
        )
        ok_res = _ps_run(shell, ok_cmd)
        assert ok_res.returncode == 0, f"{label} ({alg!r}): {ok_res.stdout}{ok_res.stderr}"


def test_minisign_verify_ps1_rejects_tampered_file_wrong_name_and_wrong_pubkey(tmp_path: Path) -> None:
    shells = _available_shells()
    if not shells:
        pytest.skip("PowerShell is not available")
    seed = bytes(range(20, 52))
    data = b"MZ" + b"burnrate-installer" * 50
    pub, sig = tnu.minisign(seed, data, alg=b"ED", trusted=b"timestamp:1\tfile:myBurnRate_1.0.0_x64-setup.exe")
    other_pub, _ = tnu.minisign(bytes(range(60, 92)), data, alg=b"ED", trusted=b"timestamp:1\tfile:myBurnRate_1.0.0_x64-setup.exe")

    good_file = tmp_path / "good.exe"
    good_file.write_bytes(data)
    tampered_file = tmp_path / "tampered.exe"
    tampered_file.write_bytes(data[:-1] + b"\x00")
    sig_path = tmp_path / "good.exe.sig"
    sig_path.write_text(sig, encoding="utf-8")
    verifier = SCRIPTS / "minisign-verify.ps1"

    for label, shell in shells:
        # Sanity check that the un-tampered fixture passes in this shell
        ok_cmd = (
            f". {_ps_quote(verifier)}; "
            f"Test-MinisignFile -Path {_ps_quote(good_file)} -SignaturePath {_ps_quote(sig_path)} "
            f"-ExpectedFile 'myBurnRate_1.0.0_x64-setup.exe' -PublicKey {_ps_quote(pub)}"
        )
        assert _ps_run(shell, ok_cmd).returncode == 0, label

        bad_cases = [
            ("tampered file", tampered_file, "myBurnRate_1.0.0_x64-setup.exe", pub),
            ("wrong filename", good_file, "other_1.0.0_x64-setup.exe", pub),
            ("wrong pubkey", good_file, "myBurnRate_1.0.0_x64-setup.exe", other_pub),
        ]
        for case_name, target_file, expected_file, key in bad_cases:
            bad_cmd = (
                f". {_ps_quote(verifier)}; "
                f"Test-MinisignFile -Path {_ps_quote(target_file)} -SignaturePath {_ps_quote(sig_path)} "
                f"-ExpectedFile {_ps_quote(expected_file)} -PublicKey {_ps_quote(key)}"
            )
            res = _ps_run(shell, bad_cmd)
            assert res.returncode != 0, f"{label} unexpectedly accepted {case_name}"


# ---------------------------------------------------------------------------------------
# install-native.ps1 & BurnrateRuntime.ps1 (hermetic)
# ---------------------------------------------------------------------------------------


def test_install_native_has_no_raw_githubusercontent_or_prod_fallback() -> None:
    text = (SCRIPTS / "install-native.ps1").read_text(encoding="utf-8")
    assert "raw.githubusercontent" not in text.lower()
    assert re.search(r"\bprod\b", text, re.IGNORECASE) is None
    assert "fallback" not in text.lower()


def test_install_native_fails_nonzero_without_sibling_minisign_verify(tmp_path: Path) -> None:
    shell = _require_ps5()
    isolated = tmp_path / "alone"
    isolated.mkdir(parents=True)
    alone = isolated / "install-native.ps1"
    shutil.copy2(SCRIPTS / "install-native.ps1", alone)

    local_app_data = tmp_path / "localappdata"
    local_app_data.mkdir()
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_app_data)

    res = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(alone), "-NoLaunch"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    output = res.stdout + res.stderr
    assert res.returncode != 0
    assert "minisign-verify.ps1" in output


def test_powershell_scripts_do_not_inspect_git_remote_origin() -> None:
    for path in PS_TRUST_FILES:
        text = path.read_text(encoding="utf-8")
        assert "remote.origin.url" not in text
        assert "git remote" not in text.lower()


def _isolated_runtime(tmp_path: Path, *, install_native_body: str | None = None) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    for name in ("BurnrateRuntime.ps1", "install-burnrate.ps1"):
        shutil.copy2(SCRIPTS / name, scripts_dir / name)
    shutil.copy2(ROOT / "install.ps1", repo / "install.ps1")

    tauri_conf = repo / "src-tauri" / "tauri.conf.json"
    tauri_conf.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "src-tauri" / "tauri.conf.json", tauri_conf)

    if install_native_body is not None:
        (scripts_dir / "install-native.ps1").write_text(install_native_body, encoding="utf-8")

    local_app_data = tmp_path / "localappdata"
    local_app_data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_app_data)
    env["BURNRATE_DATA_ROOT"] = str(local_app_data / "BURNRATE")
    return repo, local_app_data, env


def _build_versioned_exe(shell: str, output_exe: Path, version: str) -> None:
    output_exe.parent.mkdir(parents=True, exist_ok=True)
    csharp = (
        f'[assembly: System.Reflection.AssemblyInformationalVersion("{version}")]\n'
        "public class BurnrateTestStub { public static void Main() {} }"
    )
    cmd = (
        f"Add-Type -TypeDefinition {_ps_quote(csharp)} "
        f"-OutputAssembly {_ps_quote(output_exe)} -OutputType ConsoleApplication"
    )
    res = _ps_run(shell, cmd)
    assert res.returncode == 0, res.stdout + res.stderr


def test_resolve_desktop_binary_returns_programs_exe_when_present(tmp_path: Path) -> None:
    shell = _require_ps5()
    repo, local_app_data, env = _isolated_runtime(
        tmp_path, install_native_body="throw 'must not be invoked'"
    )
    programs_exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    programs_exe.parent.mkdir(parents=True)
    programs_exe.write_bytes(b"MZ")

    runtime = repo / "scripts" / "BurnrateRuntime.ps1"
    res = _ps_run(shell, f". {_ps_quote(runtime)}; Resolve-BurnrateDesktopBinary", env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == str(programs_exe)


def test_resolve_desktop_binary_ignores_legacy_and_cleans_no_binary_on_installer_failure(tmp_path: Path) -> None:
    shell = _require_ps5()
    for index, body in enumerate(("throw 'offline in test'", "exit 1")):
        case_dir = tmp_path / f"case_{index}"
        repo, local_app_data, env = _isolated_runtime(case_dir, install_native_body=body)
        legacy = local_app_data / "BURNRATE" / "burnrate-desktop.exe"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_bytes(b"MZ")

        runtime = repo / "scripts" / "BurnrateRuntime.ps1"
        cmd = (
            f". {_ps_quote(runtime)}; "
            "$resolved = Resolve-BurnrateDesktopBinary; "
            "if ($null -eq $resolved) { 'NULL' } else { $resolved }"
        )
        res = _ps_run(shell, cmd, env=env)
        assert res.returncode == 0, res.stdout + res.stderr
        assert res.stdout.strip().splitlines()[-1] == "NULL"
        assert not (local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe").exists()

    # Without any legacy file either, failed install-native leaves neither folder/binary populated.
    clean_dir = tmp_path / "clean"
    repo, local_app_data, env = _isolated_runtime(clean_dir, install_native_body="throw 'offline in test'")
    runtime = repo / "scripts" / "BurnrateRuntime.ps1"
    cmd = (
        f". {_ps_quote(runtime)}; "
        "$resolved = Resolve-BurnrateDesktopBinary; "
        "if ($null -eq $resolved) { 'NULL' } else { $resolved }"
    )
    res = _ps_run(shell, cmd, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip().splitlines()[-1] == "NULL"
    assert not (local_app_data / "Programs" / "BURNRATE").exists()
    assert not (local_app_data / "BURNRATE" / "burnrate-desktop.exe").exists()


def test_resolve_desktop_binary_ignores_target_release_unless_dev_is_passed(tmp_path: Path) -> None:
    shell = _require_ps5()
    repo, local_app_data, env = _isolated_runtime(tmp_path, install_native_body="exit 1")
    conf = json.loads((repo / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    version = str(conf["version"])

    target_release = repo / "src-tauri" / "target" / "release" / "burnrate-desktop.exe"
    _build_versioned_exe(shell, target_release, version)

    runtime = repo / "scripts" / "BurnrateRuntime.ps1"
    no_dev_cmd = (
        f". {_ps_quote(runtime)}; "
        "$resolved = Resolve-BurnrateDesktopBinary; "
        "if ($null -eq $resolved) { 'NULL' } else { $resolved }"
    )
    no_dev = _ps_run(shell, no_dev_cmd, env=env)
    assert no_dev.returncode == 0, no_dev.stdout + no_dev.stderr
    assert no_dev.stdout.strip().splitlines()[-1] == "NULL"
    assert not (local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe").exists()

    dev = _ps_run(shell, f". {_ps_quote(runtime)}; Resolve-BurnrateDesktopBinary -Dev", env=env)
    assert dev.returncode == 0, dev.stdout + dev.stderr
    assert dev.stdout.strip() == str(target_release)


def test_dev_switch_exists_on_installers_and_runtime_functions() -> None:
    shell = _require_ps5()
    runtime = SCRIPTS / "BurnrateRuntime.ps1"
    top_install = ROOT / "install.ps1"
    sub_install = SCRIPTS / "install-burnrate.ps1"
    cmd = f"""
. {_ps_quote(runtime)}
$targets = @(
    {_ps_quote(top_install)},
    {_ps_quote(sub_install)},
    'Install-BurnrateDashboard',
    'Install-BurnrateShortcuts',
    'Start-BurnrateDesktopWindow',
    'Resolve-BurnrateDesktopBinary'
)
foreach ($t in $targets) {{
    $info = Get-Command $t -ErrorAction Stop
    if (-not $info.Parameters.ContainsKey('Dev')) {{
        throw "Missing -Dev on $t"
    }}
    if (-not $info.Parameters['Dev'].SwitchParameter) {{
        throw "-Dev on $t is not a [switch]"
    }}
}}
'OK'
"""
    res = _ps_run(shell, cmd)
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip() == "OK"
