"""Authenticode signing for the native build, driven entirely by environment variables.

build_native.py signs BURNRATE.exe before it is packaged and the installer after it is built.
Nothing configured means an unsigned build with one clear line saying so; ``--require-signature``
turns that into a failure. Pick exactly one method:

  (a) a certificate in the Windows store    BURNRATE_SIGN_THUMBPRINT=<40 hex>
      (BURNRATE_SIGN_MACHINE_STORE=1 looks in LocalMachine instead of CurrentUser)
  (b) a PFX file                            BURNRATE_SIGN_PFX=<path>  BURNRATE_SIGN_PFX_PASSWORD=<secret>
  (c) Azure Trusted Signing                 BURNRATE_SIGN_AZURE_DLIB=<Azure.CodeSigning.Dlib.dll>
                                            BURNRATE_SIGN_AZURE_METADATA=<metadata.json>
      (the Azure identity comes from the usual AZURE_* variables, which signtool inherits)

Optional: BURNRATE_SIGNTOOL (path to signtool.exe) and BURNRATE_SIGN_TIMESTAMP_URL (an RFC 3161
server; default http://timestamp.digicert.com, or Microsoft's for Azure).

The digest is SHA-256 and the signature is timestamped. Every signed file is verified afterwards.
Secrets are read from the environment only: the PFX password never reaches a command line (the
PFX is imported through PowerShell, signed by thumbprint, and removed again), and nothing secret
is printed.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ENV_THUMBPRINT = "BURNRATE_SIGN_THUMBPRINT"
ENV_MACHINE_STORE = "BURNRATE_SIGN_MACHINE_STORE"
ENV_PFX = "BURNRATE_SIGN_PFX"
ENV_PFX_PASSWORD = "BURNRATE_SIGN_PFX_PASSWORD"
ENV_AZURE_DLIB = "BURNRATE_SIGN_AZURE_DLIB"
ENV_AZURE_METADATA = "BURNRATE_SIGN_AZURE_METADATA"
ENV_SIGNTOOL = "BURNRATE_SIGNTOOL"
ENV_TIMESTAMP = "BURNRATE_SIGN_TIMESTAMP_URL"

DEFAULT_TIMESTAMP = "http://timestamp.digicert.com"
AZURE_TIMESTAMP = "http://timestamp.acs.microsoft.com"
SECRET_VARIABLES = (ENV_PFX_PASSWORD,)
UNSIGNED_NOTICE = (
    "Authenticode: no signing configured, so this build is UNSIGNED "
    "(set BURNRATE_SIGN_THUMBPRINT, BURNRATE_SIGN_PFX or BURNRATE_SIGN_AZURE_DLIB; see docs/code-signing.md)."
)
_THUMBPRINT = re.compile(r"^[0-9A-Fa-f]{40}$")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class SigningError(RuntimeError):
    pass


def _clean(text: str, env: Mapping[str, str]) -> str:
    for name in SECRET_VARIABLES:
        secret = env.get(name)
        if secret:
            text = text.replace(secret, "***")
    return text.strip()


def find_signtool(
    env: Mapping[str, str], which: Callable[[str], str | None] = shutil.which
) -> str:
    explicit = env.get(ENV_SIGNTOOL)
    if explicit:
        if not Path(explicit).is_file():
            raise SigningError(f"{ENV_SIGNTOOL} does not point at a file")
        return explicit
    found = which("signtool")
    if found:
        return found
    candidates = sorted(
        glob.glob(r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe"),
        key=lambda path: [
            int(part) if part.isdigit() else 0 for part in re.split(r"[.\\]", path)
        ],
    )
    if candidates:
        return candidates[-1]
    raise SigningError(
        "signtool.exe not found (install the Windows SDK or set BURNRATE_SIGNTOOL)"
    )


class Signer:
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        require: bool = False,
        runner: Runner = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.require = require
        self._run = runner
        self._which = which
        self._sleep = sleep
        self._log = log
        self._announced = False

    # -- configuration -------------------------------------------------------------

    @property
    def method(self) -> str | None:
        env = self.env
        chosen = [
            name
            for name, present in (
                ("thumbprint", bool(env.get(ENV_THUMBPRINT))),
                ("pfx", bool(env.get(ENV_PFX))),
                ("azure", bool(env.get(ENV_AZURE_DLIB) or env.get(ENV_AZURE_METADATA))),
            )
            if present
        ]
        if len(chosen) > 1:
            raise SigningError(
                f"Configure one signing method, not several: {', '.join(chosen)}"
            )
        return chosen[0] if chosen else None

    @property
    def configured(self) -> bool:
        return self.method is not None

    def check_ready(self) -> None:
        """Fail before a long build when signing is required but not possible."""
        method = self.method
        if method is None:
            if self.require:
                raise SigningError(
                    "--require-signature was given but no signing method is configured."
                )
            return
        if method == "thumbprint" and not _THUMBPRINT.match(self.env[ENV_THUMBPRINT]):
            raise SigningError(f"{ENV_THUMBPRINT} must be 40 hex characters")
        if method == "pfx":
            if not Path(self.env[ENV_PFX]).is_file():
                raise SigningError(f"{ENV_PFX} does not point at a file")
            if not self.env.get(ENV_PFX_PASSWORD):
                raise SigningError(f"{ENV_PFX_PASSWORD} is required with {ENV_PFX}")
        if method == "azure":
            for name in (ENV_AZURE_DLIB, ENV_AZURE_METADATA):
                if not self.env.get(name) or not Path(self.env[name]).is_file():
                    raise SigningError(f"{name} must point at an existing file")
        find_signtool(self.env, self._which)

    # -- signing -------------------------------------------------------------------

    def _timestamp(self) -> str:
        return self.env.get(ENV_TIMESTAMP) or (
            AZURE_TIMESTAMP if self.method == "azure" else DEFAULT_TIMESTAMP
        )

    def _signtool(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        tool = find_signtool(self.env, self._which)
        return self._run(
            [tool, *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            timeout=300,
        )

    def _powershell(
        self, script: str, extra_env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return self._run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            env={**self.env, **extra_env},
            check=False,
            timeout=120,
        )

    def _sign_args(self, path: Path, thumbprint: str | None) -> list[str]:
        base = ["sign", "/fd", "SHA256", "/tr", self._timestamp(), "/td", "SHA256"]
        method = self.method
        if method == "azure":
            return [
                *base,
                "/dlib",
                self.env[ENV_AZURE_DLIB],
                "/dmdf",
                self.env[ENV_AZURE_METADATA],
                str(path),
            ]
        store = ["/s", "My"] + (
            ["/sm"] if self.env.get(ENV_MACHINE_STORE) == "1" else []
        )
        return [
            *base,
            "/sha1",
            thumbprint or self.env[ENV_THUMBPRINT],
            *store,
            str(path),
        ]

    def _attempt_sign(self, path: Path, thumbprint: str | None) -> None:
        last = ""
        for attempt in range(1, 4):  # timestamp servers are flaky
            result = self._signtool(self._sign_args(path, thumbprint))
            if result.returncode == 0:
                return
            last = _clean((result.stdout or "") + (result.stderr or ""), self.env)
            if attempt < 3:
                self._sleep(2.0 * attempt)
        raise SigningError(f"signtool could not sign {path.name}: {last[-600:]}")

    def _import_pfx(self) -> tuple[str, bool]:
        script = (
            "$ErrorActionPreference='Stop';"
            "$pw=ConvertTo-SecureString -String $env:BURNRATE_SIGN_PFX_PASSWORD -AsPlainText -Force;"
            "$c=[System.Security.Cryptography.X509Certificates.X509Certificate2]::new($env:BURNRATE_SIGN_PFX,$pw);"
            "$had=Test-Path ('Cert:\\CurrentUser\\My\\'+$c.Thumbprint);"
            "if(-not $had){Import-PfxCertificate -FilePath $env:BURNRATE_SIGN_PFX -CertStoreLocation Cert:\\CurrentUser\\My -Password $pw | Out-Null};"
            "Write-Output ($c.Thumbprint+' '+[int](-not $had))"
        )
        result = self._powershell(script, {})
        text = _clean(result.stdout or "", self.env)
        parts = text.split()
        if result.returncode != 0 or len(parts) != 2 or not _THUMBPRINT.match(parts[0]):
            raise SigningError(
                "Could not read the PFX certificate (check the file and password)."
            )
        return parts[0].upper(), parts[1] == "1"

    def _forget_pfx(self, thumbprint: str) -> None:
        if _THUMBPRINT.match(thumbprint):
            self._powershell(
                "Remove-Item -LiteralPath ('Cert:\\CurrentUser\\My\\'+$env:BURNRATE_SIGN_FORGET) -ErrorAction SilentlyContinue",
                {"BURNRATE_SIGN_FORGET": thumbprint},
            )

    def verify(self, path: Path) -> None:
        result = self._signtool(["verify", "/pa", str(path)])
        if result.returncode != 0:
            detail = _clean((result.stdout or "") + (result.stderr or ""), self.env)
            raise SigningError(
                f"{path.name} has no valid Authenticode signature: {detail[-400:]}"
            )

    def sign(self, path: Path) -> bool:
        """Sign and verify ``path``. Returns False (after one notice) when nothing is configured."""
        path = Path(path)
        if not path.is_file():
            raise SigningError(f"{path} does not exist")
        method = self.method
        if method is None:
            if self.require:
                raise SigningError(
                    f"{path.name} would be unsigned and --require-signature was given."
                )
            if not self._announced:
                self._log(UNSIGNED_NOTICE)
                self._announced = True
            return False
        self.check_ready()
        if method == "pfx":
            thumbprint, imported = self._import_pfx()
            try:
                self._attempt_sign(path, thumbprint)
            finally:
                if imported:
                    self._forget_pfx(thumbprint)
        else:
            self._attempt_sign(path, None)
        self.verify(path)
        self._log(f"Authenticode: signed and verified {path.name} ({method}).")
        return True

    def require_signed(self, paths: Sequence[Path]) -> None:
        """For ``--require-signature``: every file must verify, whoever signed it."""
        for path in paths:
            self.verify(Path(path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sign files with Authenticode (see the module docstring)."
    )
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args(argv)
    signer = Signer(require=args.require_signature)
    try:
        signer.check_ready()
        for path in args.files:
            signer.sign(path)
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
