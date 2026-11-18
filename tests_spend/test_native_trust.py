"""Native binary trust checks: httpx trust_env, NSIS hooks, and PowerShell scripts.

All PowerShell tests run hermetically against temporary directories and an
overridden LOCALAPPDATA; nothing touches the real user profile, repository
build directories, or the network.
"""
from __future__ import annotations

import base64
import hashlib
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


def _ps5_default_module_path(env: dict[str, str]) -> str:
    env_ci = {k.lower(): v for k, v in env.items()}
    user_profile = env_ci.get("userprofile") or str(Path.home())
    program_files = env_ci.get("programfiles") or r"C:\Program Files"
    system_root = env_ci.get("systemroot") or r"C:\Windows"
    return os.pathsep.join(
        [
            str(Path(user_profile) / "Documents" / "WindowsPowerShell" / "Modules"),
            str(Path(program_files) / "WindowsPowerShell" / "Modules"),
            str(Path(system_root) / "system32" / "WindowsPowerShell" / "v1.0" / "Modules"),
        ]
    )


def _ps_run(shell: str, script: str, *, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    run_env = dict(env if env is not None else os.environ)
    if Path(shell).stem.lower() == "powershell":
        for key in [k for k in run_env if k.lower() == "psmodulepath"]:
            del run_env[key]
        run_env["PSModulePath"] = _ps5_default_module_path(run_env)
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=60,
        env=run_env,
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


def _installer_guard(shell, program, data, registered, action):
    env = dict(os.environ)
    env.update(
        BURNRATE_NSIS_PROGRAM_DIR=str(program),
        BURNRATE_NSIS_DATA_DIR=str(data),
        BR_TEST_REGISTERED_DIR=str(registered),
    )
    helper = _ps_quote(ROOT / "packaging" / "native-install-guard.ps1")
    script = rf"""
function Get-ItemProperty {{
    param([string]$LiteralPath)
    if ($LiteralPath.EndsWith('\BURNRATE Native')) {{
        return @{{ InstallDir=$env:BR_TEST_REGISTERED_DIR; Version='0.5.9' }}
    }}
    if ($LiteralPath.EndsWith('\BURNRATENative')) {{
        return @{{ InstallLocation=$env:BR_TEST_REGISTERED_DIR; DisplayVersion='0.5.9' }}
    }}
    throw "Unexpected registry lookup"
}}
& {helper} -Action {_ps_quote(action)}
exit $LASTEXITCODE
"""
    return _ps_run(shell, script, env=env)


def _mark_installed(directory, version="0.5.9"):
    directory.mkdir(exist_ok=True)
    for name in ("BURNRATE.exe", "uninstall.exe"):
        (directory / name).touch()
    (directory / "BURNRATE.installed").write_text(
        f'{{"installer":"nsis","version":"{version}"}}', encoding="utf-8"
    )


def test_nsis_guard_checks_ownership_before_program_file_changes():
    nsi = (ROOT / "packaging" / "burnrate-native.nsi").read_text(encoding="utf-8")
    install = nsi.split('Section "Install"', 1)[1].split("SectionEnd", 1)[0]
    assert install.index("Call RunInstallGuard") < install.index(r'SetOutPath "$INSTDIR"')
    assert install.index('$GuardResult == 2') < install.index(r'RMDir /r "$INSTDIR\_internal"')
    assert install.index("SetErrorLevel 1") < install.index(r'File /r')
    uninstall = nsi.split('Section "Uninstall"', 1)[1]
    assert uninstall.index("Call un.RunInstallGuard") < uninstall.index(r'RMDir /r "$INSTDIR\_internal"')
    assert uninstall.index("SetErrorLevel 1") < uninstall.index(r'RMDir /r "$INSTDIR\_internal"')
    assert "taskkill.exe" not in nsi


def test_nsis_guard_fresh_registered_and_foreign_paths(tmp_path):
    ps = _require_ps5()
    program = tmp_path / "BURNRATE Custom Install"
    data = tmp_path / "Data"
    result = _installer_guard(ps, program, data, program, "Install")
    assert result.returncode == 0, result.stderr
    assert not program.exists()
    result = _installer_guard(ps, program, data, program, "Update")
    assert result.returncode == 1
    program.mkdir()
    result = _installer_guard(ps, program, data, program, "Install")
    assert result.returncode == 0, result.stderr
    _mark_installed(program)
    for action in ("Install", "Update", "Uninstall"):
        result = _installer_guard(ps, program, data, program, action)
        assert result.returncode == 2, result.stderr

    foreign = tmp_path / "Foreign"
    (foreign / "_internal").mkdir(parents=True)
    sentinel = foreign / "_internal" / "other-program.dll"
    sentinel.write_bytes(b"unchanged")
    for action in ("Install", "Update"):
        result = _installer_guard(ps, foreign, data, program, action)
        assert result.returncode == 1
        assert sentinel.read_bytes() == b"unchanged"
    _mark_installed(foreign)
    result = _installer_guard(ps, foreign, data, program, "Uninstall")
    assert result.returncode == 1
    assert sentinel.read_bytes() == b"unchanged"
    _mark_installed(program, version="0.5.8")
    result = _installer_guard(ps, program, data, program, "Update")
    assert result.returncode == 1
    assert "marker" in result.stderr


def test_nsis_guard_rejects_data_alias_and_nested_reparse_points(tmp_path):
    ps = _require_ps5()
    import _winapi

    program = tmp_path / "Programs" / "BURNRATE"
    program.parent.mkdir()
    _mark_installed(program)
    data = tmp_path / "Data"
    data.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_bytes(b"keep")
    alias = tmp_path / "data-alias"
    _winapi.CreateJunction(str(data), str(alias))
    for target in (data, data / "nested", alias, alias / "nested"):
        result = _installer_guard(ps, target, data, program, "Install")
        assert result.returncode == 1
    _winapi.CreateJunction(str(foreign), str(program / "_internal"))
    for action in ("Update", "Uninstall"):
        result = _installer_guard(ps, program, data, program, action)
        assert result.returncode == 1
        assert "junction" in result.stderr
    assert sentinel.read_bytes() == b"keep"


def test_nsis_guard_stops_only_the_installed_image(tmp_path):
    ps = _require_ps5()
    program = tmp_path / "registered"
    _mark_installed(program)
    other = tmp_path / "other"
    other.mkdir()
    ping = Path(os.environ["SystemRoot"]) / "System32" / "PING.EXE"
    installed = program / "BURNRATE.exe"
    foreign = other / "BURNRATE.exe"
    shutil.copyfile(ping, installed)
    shutil.copyfile(ping, foreign)
    children = []
    try:
        for executable in (installed, foreign):
            children.append(subprocess.Popen(
                [str(executable), "-t", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ))
        result = _installer_guard(ps, program, tmp_path / "Data", program, "Install")
        assert result.returncode == 2, result.stderr
        assert children[0].wait(timeout=5) == 0
        assert children[1].poll() is None
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


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


def test_embedded_minisign_verifier_matches_minisign_verify_ps1() -> None:
    def extract(path: Path) -> tuple[str, str]:
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        typedef = re.search(r"\$minisignTypeDef\s*=\s*@'\n(.*?)\n'@", text, re.S)
        assert typedef is not None, f"$minisignTypeDef missing in {path.name}"
        wrapper = re.search(r"(^function Test-MinisignFile\s*\{.*?^\})", text, re.M | re.S)
        assert wrapper is not None, f"Test-MinisignFile missing in {path.name}"
        return typedef.group(1), wrapper.group(1)

    verifier_typedef, verifier_wrapper = extract(SCRIPTS / "minisign-verify.ps1")
    installer_typedef, installer_wrapper = extract(SCRIPTS / "install-native.ps1")
    assert installer_typedef == verifier_typedef
    assert installer_wrapper == verifier_wrapper


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
# install-native.ps1 (self-contained / iex proof) & BurnrateRuntime.ps1 (hermetic)
# ---------------------------------------------------------------------------------------


def test_install_native_has_no_raw_githubusercontent_or_prod_fallback() -> None:
    text = (SCRIPTS / "install-native.ps1").read_text(encoding="utf-8")
    assert "raw.githubusercontent" not in text.lower()
    assert re.search(r"\bprod\b", text, re.IGNORECASE) is None
    assert "fallback" not in text.lower()


def test_install_native_is_self_contained_with_no_dot_source_or_script_root() -> None:
    text = (SCRIPTS / "install-native.ps1").read_text(encoding="utf-8")
    assert "PSScriptRoot" not in text
    assert "minisign-verify.ps1" not in text
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        assert not re.match(r"\.\s+[\$'\"]", stripped), f"Unexpected dot-source: {line}"


def _flip_minisign_signature(sig_b64: str) -> str:
    raw_lines = base64.b64decode(sig_b64).decode("utf-8").splitlines()
    sig_blob = bytearray(base64.b64decode(raw_lines[1]))
    sig_blob[15] ^= 0x01
    raw_lines[1] = base64.b64encode(bytes(sig_blob)).decode("ascii")
    return base64.b64encode(("\n".join(raw_lines) + "\n").encode("utf-8")).decode("ascii")


def _run_install_native_via_iex(
    shell: str,
    case_dir: Path,
    script_text: str,
    *,
    feed_json: str,
    installer_bytes: bytes,
    sig_text: str,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path]:
    case_dir.mkdir(parents=True, exist_ok=True)
    empty_cwd = case_dir / "empty_cwd"
    empty_cwd.mkdir(exist_ok=True)
    local_app_data = case_dir / "localappdata"
    local_app_data.mkdir(exist_ok=True)
    temp_dir = case_dir / "temp"
    temp_dir.mkdir(exist_ok=True)

    script_file = case_dir / "candidate-install-native.ps1"
    script_file.write_text(script_text, encoding="utf-8")
    feed_file = case_dir / "feed.json"
    feed_file.write_text(feed_json, encoding="utf-8")
    installer_fixture = case_dir / "fixture-installer.exe"
    installer_fixture.write_bytes(installer_bytes)
    sig_fixture = case_dir / "fixture-installer.exe.sig"
    sig_fixture.write_text(sig_text, encoding="utf-8")
    start_log = case_dir / "start-process.log"

    harness = f"""
$ErrorActionPreference = 'Stop'
$feedFile = {_ps_quote(feed_file)}
$installerFixture = {_ps_quote(installer_fixture)}
$sigFixture = {_ps_quote(sig_fixture)}
$startLog = {_ps_quote(start_log)}
$candidateScript = Get-Content -LiteralPath {_ps_quote(script_file)} -Raw

function Invoke-WebRequest {{
    param(
        [string]$Uri,
        [string]$OutFile,
        [switch]$UseBasicParsing,
        [int]$TimeoutSec
    )
    if ($Uri -like '*native-latest.json') {{
        return [pscustomobject]@{{ Content = (Get-Content -LiteralPath $feedFile -Raw) }}
    }}
    if ($Uri -like '*.exe.sig') {{
        [IO.File]::Copy($sigFixture, $OutFile, $true)
        return
    }}
    if ($Uri -like '*.exe') {{
        [IO.File]::Copy($installerFixture, $OutFile, $true)
        return
    }}
    throw "Unexpected Invoke-WebRequest Uri: $Uri"
}}

function Start-Process {{
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [switch]$Wait,
        [switch]$PassThru
    )
    $argText = if ($ArgumentList) {{ $ArgumentList -join ' ' }} else {{ '' }}
    Add-Content -LiteralPath $startLog -Value "$FilePath|$argText"
    if ($ArgumentList -contains '/S') {{
        $installed = Join-Path $env:LOCALAPPDATA 'Programs\\BURNRATE\\BURNRATE.exe'
        $parent = Split-Path -Parent $installed
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        [IO.File]::WriteAllBytes($installed, [byte[]]@(0x4D, 0x5A))
        return [pscustomobject]@{{ ExitCode = 0 }}
    }}
    return [pscustomobject]@{{ ExitCode = 0 }}
}}

& {{ Invoke-Expression $candidateScript }}
"""
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_app_data)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)

    res = _ps_run(shell, harness, env=env, cwd=empty_cwd)
    starts = start_log.read_text(encoding="utf-8").splitlines() if start_log.exists() else []
    return res, starts, local_app_data, temp_dir


def test_install_native_via_iex_verifies_signature_and_blocks_tampering_in_ps5_and_pwsh(
    tmp_path: Path,
) -> None:
    shells = _available_shells()
    if not shells:
        pytest.skip("PowerShell is not available")

    version = "9.8.7"
    expected_name = f"myBurnRate_{version}_x64-setup.exe"
    expected_url = f"https://github.com/kamol-nazarov/myBurnRate/releases/download/v{version}/{expected_name}"
    installer_bytes = b"MZ" + b"burnrate-setup-payload-" * 64
    installer_sha = hashlib.sha256(installer_bytes).hexdigest()
    feed_json = json.dumps(
        {
            "version": version,
            "platforms": {
                "windows-x86_64": {
                    "url": expected_url,
                    "sha256": installer_sha,
                    "size": len(installer_bytes),
                }
            },
        }
    )

    test_seed = bytes(range(30, 62))
    test_pub, good_sig = tnu.minisign(
        test_seed,
        installer_bytes,
        alg=b"ED",
        trusted=f"timestamp:1\tfile:{expected_name}".encode("utf-8"),
    )
    tampered_sig = _flip_minisign_signature(good_sig)

    raw_script = (SCRIPTS / "install-native.ps1").read_text(encoding="utf-8")
    test_key_script, replacements = re.subn(
        r"(\$MinisignPublicKey\s*=\s*')[^']+(')",
        rf"\g<1>{test_pub}\g<2>",
        raw_script,
    )
    assert replacements == 1

    for label, shell in shells:
        # 1) Exact unmodified file contents run via `iex` reject a foreign signature before Start-Process.
        res, starts, local_app_data, temp_dir = _run_install_native_via_iex(
            shell,
            tmp_path / f"{label}_unmodified_foreign_sig",
            raw_script,
            feed_json=feed_json,
            installer_bytes=installer_bytes,
            sig_text=good_sig,
        )
        assert res.returncode != 0, f"{label} unexpectedly accepted foreign sig via iex"
        assert starts == [], f"{label} invoked Start-Process before signature check: {starts}"
        assert not (local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe").exists()
        assert list(temp_dir.glob("myburnrate-install-*")) == []

        # 2) Valid signature under pinned key succeeds end-to-end via `iex` (no script directory).
        res, starts, local_app_data, temp_dir = _run_install_native_via_iex(
            shell,
            tmp_path / f"{label}_valid_sig",
            test_key_script,
            feed_json=feed_json,
            installer_bytes=installer_bytes,
            sig_text=good_sig,
        )
        assert res.returncode == 0, f"{label} valid iex failed: {res.stdout}{res.stderr}"
        installed_exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
        assert installed_exe.exists()
        assert len(starts) == 2, f"{label} expected installer + app launch, got: {starts}"
        assert starts[0].endswith(f"{expected_name}|/S")
        assert starts[1] == f"{installed_exe}|"
        assert list(temp_dir.glob("myburnrate-install-*")) == []

        # 3) Tampered signature (valid size & SHA-256) fails before Start-Process via `iex`.
        res, starts, local_app_data, temp_dir = _run_install_native_via_iex(
            shell,
            tmp_path / f"{label}_tampered_sig",
            test_key_script,
            feed_json=feed_json,
            installer_bytes=installer_bytes,
            sig_text=tampered_sig,
        )
        assert res.returncode != 0, f"{label} unexpectedly accepted tampered sig via iex"
        assert starts == [], f"{label} invoked Start-Process on tampered sig: {starts}"
        assert not (local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe").exists()
        assert list(temp_dir.glob("myburnrate-install-*")) == []


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

    if install_native_body is not None:
        (scripts_dir / "install-native.ps1").write_text(install_native_body, encoding="utf-8")

    local_app_data = tmp_path / "localappdata"
    local_app_data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LOCALAPPDATA"] = str(local_app_data)
    env["BURNRATE_DATA_ROOT"] = str(local_app_data / "BURNRATE")
    return repo, local_app_data, env


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
        legacy = local_app_data / "BURNRATE" / "BURNRATE.exe"
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
    assert not (local_app_data / "BURNRATE" / "BURNRATE.exe").exists()


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
