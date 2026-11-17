"""The frozen native app the installer ships (packaging/)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import shlex
import sys
import types
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest

from spend_app import build, service

ROOT = Path(__file__).resolve().parents[1]


def _parse_dockerfile_copy_sources(dockerfile_text: str) -> tuple[list[tuple[int, str]], int]:
    """Return ``[(line_index, src), ...]`` for every ``COPY`` instruction and the first ``pip install`` line."""
    raw_lines = dockerfile_text.splitlines()
    logical: list[tuple[int, str]] = []
    idx = 0
    while idx < len(raw_lines):
        start_idx = idx
        buf = raw_lines[idx]
        while buf.rstrip().endswith("\\") and idx + 1 < len(raw_lines):
            buf = buf.rstrip()[:-1] + " " + raw_lines[idx + 1].lstrip()
            idx += 1
        logical.append((start_idx, buf.strip()))
        idx += 1

    copy_sources: list[tuple[int, str]] = []
    pip_install_line = -1
    for line_no, stmt in logical:
        if not stmt or stmt.startswith("#"):
            continue
        if pip_install_line == -1 and re.search(r"\bpip\s+install\b", stmt):
            pip_install_line = line_no
        upper = stmt.upper()
        if upper.startswith("COPY ") or upper.startswith("COPY\t"):
            rest = stmt[4:].strip()
            if rest.startswith("["):
                tokens = json.loads(rest)
            else:
                tokens = shlex.split(rest)
            args = [tok for tok in tokens if not tok.startswith("--")]
            assert len(args) >= 2, f"Malformed COPY instruction: {stmt!r}"
            for src in args[:-1]:
                clean = src.rstrip("/") or "."
                copy_sources.append((line_no, clean))
    assert pip_install_line != -1, "Dockerfile must run pip install"
    return copy_sources, pip_install_line


def _dockerignore_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    pat = pattern.strip().lstrip("/").rstrip("/")
    parts = pat.split("/")
    regex_parts: list[str] = ["^"]
    for i, part in enumerate(parts):
        if part == "**":
            if i == len(parts) - 1:
                regex_parts.append(".*")
            else:
                regex_parts.append("(?:.+/)?")
            continue
        seg = ""
        for ch in part:
            if ch == "*":
                seg += "[^/]*"
            elif ch == "?":
                seg += "[^/]"
            else:
                seg += re.escape(ch)
        if i < len(parts) - 1:
            if parts[i + 1] == "**":
                regex_parts.append(seg + "(?:/|$)")
            else:
                regex_parts.append(seg + "/")
        else:
            regex_parts.append(seg + "(?:/.*)?$")
    return re.compile("".join(regex_parts))


def _dockerignore_admits(rel_posix: str, rules: list[str]) -> bool:
    """Evaluate ``.dockerignore`` rules (last matching rule wins, ``!`` un-ignores)."""
    rel = PurePosixPath(rel_posix).as_posix().lstrip("./")
    ignored = False
    for raw_rule in rules:
        line = raw_rule.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line[1:].strip() if negated else line
        if not pattern:
            continue
        if _dockerignore_pattern_to_regex(pattern).match(rel):
            ignored = not negated
    return not ignored


def _load_hatch_build(monkeypatch: pytest.MonkeyPatch):
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ImportError:

        class _StubBuildHookInterface:
            def __init__(self, root, config, build_config, metadata, directory, target_name):
                self.root = root
                self.config = config
                self.build_config = build_config
                self.metadata = metadata
                self.directory = directory
                self.target_name = target_name

        hatchling_pkg = types.ModuleType("hatchling")
        builders_pkg = types.ModuleType("hatchling.builders")
        hooks_pkg = types.ModuleType("hatchling.builders.hooks")
        plugin_pkg = types.ModuleType("hatchling.builders.hooks.plugin")
        iface_mod = types.ModuleType("hatchling.builders.hooks.plugin.interface")
        iface_mod.BuildHookInterface = _StubBuildHookInterface
        for mod_name, mod in (
            ("hatchling", hatchling_pkg),
            ("hatchling.builders", builders_pkg),
            ("hatchling.builders.hooks", hooks_pkg),
            ("hatchling.builders.hooks.plugin", plugin_pkg),
            ("hatchling.builders.hooks.plugin.interface", iface_mod),
        ):
            monkeypatch.setitem(sys.modules, mod_name, mod)

    spec = importlib.util.spec_from_file_location("test_pkg_hatch_build", ROOT / "hatch_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_packaged_build_reads_its_recorded_commit(tmp_path: Path) -> None:
    assert build.commit_at(tmp_path) is None
    (tmp_path / "BUILD_COMMIT").write_text("0123456789abcdef0123\n", encoding="utf-8")
    assert build.commit_at(tmp_path) == "0123456789ab"


def test_another_data_root_gets_its_own_service_mutex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    assert service.mutex_name() == service.MUTEX_NAME
    monkeypatch.setenv("BURNRATE_DATA_ROOT", str(tmp_path))
    separate = service.mutex_name()
    assert separate.startswith(service.MUTEX_NAME + "-") and separate != service.MUTEX_NAME
    # The data root override moves the database, settings and logs together.
    assert service.data_root() == tmp_path
    from spend_app.config import default_app_dir, default_database_path

    assert default_app_dir() == tmp_path
    monkeypatch.delenv("SPEND_DATABASE_PATH", raising=False)
    monkeypatch.delenv("BURNRATE_DEV", raising=False)
    assert default_database_path() == tmp_path / "spend.db"


def test_the_spec_ships_what_the_service_reads() -> None:
    spec = (ROOT / "packaging" / "burnrate-native.spec").read_text(encoding="utf-8")
    for needed in (
        '"pricing"',
        'collect_data_files("tzdata")',
        'copy_metadata("APScheduler")',
        "console=False",
        "assets",
    ):
        assert needed in spec
    assert "spend_web" not in spec
    entry = (ROOT / "packaging" / "burnrate_native.py").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support()" in entry


def test_b1_docker_context_copy_sources_admitted_and_omit_spend_web() -> None:
    dockerfile_text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore_text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    rules = dockerignore_text.splitlines()

    assert "spend_web" not in dockerfile_text
    assert "spend_web" not in dockerignore_text

    copy_sources, pip_install_line = _parse_dockerfile_copy_sources(dockerfile_text)
    assert copy_sources, "Dockerfile must contain COPY instructions"

    copied_paths = {src for _, src in copy_sources}
    for required in ("hatch_build.py", "packaging/build_config.py"):
        assert required in copied_paths, f"{required} missing from Dockerfile COPY"
        assert _dockerignore_admits(required, rules), (
            f".dockerignore must admit {required}"
        )

    for line_no, src in copy_sources:
        assert line_no < pip_install_line, (
            f"COPY {src} (line {line_no + 1}) must appear before pip install (line {pip_install_line + 1})"
        )
        target = ROOT / src
        assert target.exists(), f"Dockerfile COPY source does not exist in repo: {src}"
        if target.is_file():
            assert _dockerignore_admits(src, rules), (
                f".dockerignore must admit copied file {src}"
            )
        else:
            files = [
                p
                for p in target.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            ]
            assert files, f"Copied directory {src} has no non-cache files"
            for child in files:
                rel = child.relative_to(ROOT).as_posix()
                assert _dockerignore_admits(rel, rules), (
                    f".dockerignore must admit {rel} inside copied directory {src}"
                )

    # Sanity check: .dockerignore still blocks unrelated files and caches
    assert not _dockerignore_admits("spend_app/__pycache__/api.cpython-312.pyc", rules)
    assert not _dockerignore_admits("tests_spend/test_packaging.py", rules)
    assert not _dockerignore_admits("packaging/burnrate-native.spec", rules)


def test_b1_hatch_build_hook_imports_packaging_build_config_and_validates_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hatch_build = _load_hatch_build(monkeypatch)
    assert hasattr(hatch_build, "CustomBuildHook")

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "https://hub.example.com:8449")
    hook = hatch_build.CustomBuildHook(
        str(ROOT), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data: dict = {}
    try:
        hook.initialize("standard", build_data)
        force_include = build_data["force_include"]
        assert len(force_include) == 1
        src_path, dest_path = next(iter(force_include.items()))
        assert dest_path == "spend_app/_build_config.py"
        generated = Path(src_path).read_text(encoding="utf-8")
        assert "https://hub.example.com:8449" in generated
    finally:
        hook.finalize("standard", build_data, "")

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "http://insecure.example.com")
    bad_hook = hatch_build.CustomBuildHook(
        str(ROOT), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    with pytest.raises(ValueError, match="https://"):
        bad_hook.initialize("standard", {})


def test_the_update_feed_points_each_platform_at_its_signed_bundle(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_latest_json", ROOT / "packaging" / "make_latest_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    name = "myBurnRate_0.5.0_x64-setup.exe"
    installer = tmp_path / name
    installer.write_bytes(b"bundle")
    sig_text = base64.b64encode(
        (
            "untrusted comment: signature from tauri secret key\n"
            "RUR0\n"
            f"trusted comment: timestamp:1\tfile:{name}\n"
            "AAAA\n"
        ).encode("utf-8")
    ).decode("ascii")
    (tmp_path / (name + ".sig")).write_text(sig_text + "\n", encoding="utf-8")

    feed = module.build_native_feed(tmp_path, tag="v0.5.0", repo="owner/repo", now=datetime(2026, 11, 1, tzinfo=UTC))
    assert feed["version"] == "0.5.0" and feed["pub_date"] == "2026-11-01T00:00:00Z"
    assert set(feed["platforms"]) == {"windows-x86_64"}
    windows = feed["platforms"]["windows-x86_64"]
    assert windows["url"] == f"https://github.com/owner/repo/releases/download/v0.5.0/{name}"
    assert windows["signature"] == sig_text
    assert windows["sha256"] == hashlib.sha256(b"bundle").hexdigest()
    assert windows["size"] == len(b"bundle")

    argv = sys.argv
    sys.argv = ["make_latest_json", "--native", "--tag", "v0.5.0", "--repo", "owner/repo", "--dir", str(tmp_path)]
    try:
        assert module.main() == 0
    finally:
        sys.argv = argv
    written = json.loads((tmp_path / "native-latest.json").read_text(encoding="utf-8"))
    assert written["version"] == "0.5.0"
    assert written["platforms"]["windows-x86_64"] == windows

    (tmp_path / (name + ".sig")).unlink()
    with pytest.raises(SystemExit, match="no update signature"):
        module.build_native_feed(tmp_path, tag="v0.5.0", repo="owner/repo")
