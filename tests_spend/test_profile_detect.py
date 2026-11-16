"""Fake-home tests for bounded local profile detection (spend_app.profile_detect)."""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spend_app import claude_accounts, connection_paths, opencode_profiles, profile_detect
from spend_app.connection_paths import identity
from spend_app.profile_detect import Candidate, detect, detect_all, reset_cache


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _resolved_folder_id(path: Path) -> str:
    return _sha256(identity(path.resolve(strict=True)))


def _write_claude_state(directory: Path, uuid: str | None, *, tier: str = "default_claude_max_20x",
                        name: str = ".claude.json", write_file: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if write_file:
        doc: dict = {}
        if uuid is not None:
            doc["oauthAccount"] = {
                "accountUuid": uuid,
                "organizationRateLimitTier": tier,
                "emailAddress": "synthetic-user@example.invalid",
            }
        path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _write_opencode_db(path: Path, console_key: str | None, *, with_table: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        if with_table:
            db.execute(
                "CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, value TEXT, "
                "connector_id TEXT, method_id TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)"
            )
            if console_key is not None:
                db.execute(
                    "INSERT INTO credential(id, integration_id, value, active, time_created) VALUES(?,?,?,?,?)",
                    ("cred-1", "opencode", json.dumps({"type": "key", "key": console_key}), 1, 1),
                )
    return path


def _write_codex_auth(directory: Path, account_id: str | None = None, *, extra_tokens: dict | None = None,
                      api_key: str | None = None, write_file: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "auth.json"
    if write_file:
        tokens: dict = dict(extra_tokens or {})
        if account_id is not None:
            tokens["account_id"] = account_id
        doc: dict = {"OPENAI_API_KEY": api_key, "tokens": tokens}
        path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _make_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows junctions require os.name == 'nt'")
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not connection_paths.is_junction(link):
        pytest.skip(f"Windows junction creation not supported here: {result.stderr or result.stdout}")


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_cache()
    claude_accounts.reset_cache()
    opencode_profiles.reset_cache()
    yield
    reset_cache()
    claude_accounts.reset_cache()
    opencode_profiles.reset_cache()


# ---------------------------------------------------------------------------
# 1. Default-only discovery for all 3 providers
# ---------------------------------------------------------------------------


def test_claude_default_only_with_and_without_sample(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    (claude_dir / "projects" / "proj-a").mkdir(parents=True)
    _write_claude_state(home, "acct-claude-default", tier="default_claude_max_20x")

    # Without .jsonl sample
    found_empty = detect("claude_local", home=home, environ={})
    assert found_empty == [
        Candidate(
            provider="claude_local",
            folder=claude_dir,
            location=str(claude_dir / "projects"),
            key="default",
            account_digest=_sha256("acct-claude-default"),
            plan="Claude Max",
            has_sample=False,
            is_default=True,
            candidate_id=_sha256("acct-claude-default"),
        )
    ]

    # With .jsonl sample
    (claude_dir / "projects" / "proj-a" / "session.jsonl").write_text("{}\n", encoding="utf-8")
    found_with_sample = detect("claude_local", home=home, environ={})
    assert len(found_with_sample) == 1
    assert found_with_sample[0].has_sample is True
    assert found_with_sample[0].account_digest == _sha256("acct-claude-default")


def test_claude_config_dir_env_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    custom = tmp_path / "custom-claude"
    (custom / "projects").mkdir(parents=True)
    (custom / "projects" / "turn.jsonl").write_text("{}\n", encoding="utf-8")
    _write_claude_state(custom, "acct-claude-custom", tier="default_claude_pro")

    found = detect("claude_local", home=home, environ={"CLAUDE_CONFIG_DIR": str(custom)})
    assert found == [
        Candidate(
            provider="claude_local",
            folder=custom,
            location=str(custom / "projects"),
            key="default",
            account_digest=_sha256("acct-claude-custom"),
            plan="Claude Pro",
            has_sample=True,
            is_default=True,
            candidate_id=_sha256("acct-claude-custom"),
        )
    ]


def test_opencode_default_only_standard_xdg_and_opencode_db(tmp_path: Path) -> None:
    home = tmp_path / "home"
    # Default ~/.local/share/opencode exists, no opencode.db yet -> has_sample is False
    default_dir = home / ".local" / "share" / "opencode"
    default_dir.mkdir(parents=True)
    found_no_db = detect("opencode_local", home=home, environ={})
    assert found_no_db == [
        Candidate(
            provider="opencode_local",
            folder=default_dir,
            location=str(default_dir / "opencode.db"),
            key="default",
            account_digest=None,
            plan=None,
            has_sample=False,
            is_default=True,
            candidate_id=_resolved_folder_id(default_dir),
        )
    ]

    # Populate default opencode.db
    default_db = _write_opencode_db(default_dir / "opencode.db", "oc-console-default")
    found_default = detect("opencode_local", home=home, environ={})
    assert found_default == [
        Candidate(
            provider="opencode_local",
            folder=default_dir,
            location=str(default_db),
            key="default",
            account_digest=_sha256("oc-console-default"),
            plan=None,
            has_sample=True,
            is_default=True,
            candidate_id=_sha256("oc-console-default"),
        )
    ]

    # XDG_DATA_HOME override
    xdg = tmp_path / "xdg"
    xdg_db = _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-console-xdg")
    found_xdg = detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})
    assert found_xdg == [
        Candidate(
            provider="opencode_local",
            folder=xdg / "opencode",
            location=str(xdg_db),
            key="default",
            account_digest=_sha256("oc-console-xdg"),
            plan=None,
            has_sample=True,
            is_default=True,
            candidate_id=_sha256("oc-console-xdg"),
        )
    ]

    # OPENCODE_DB override (takes precedence over XDG_DATA_HOME)
    explicit_db = _write_opencode_db(tmp_path / "explicit" / "custom.db", "oc-console-explicit")
    found_db_env = detect(
        "opencode_local",
        home=home,
        environ={"XDG_DATA_HOME": str(xdg), "OPENCODE_DB": str(explicit_db)},
    )
    assert found_db_env == [
        Candidate(
            provider="opencode_local",
            folder=explicit_db.parent,
            location=str(explicit_db),
            key="default",
            account_digest=_sha256("oc-console-explicit"),
            plan=None,
            has_sample=True,
            is_default=True,
            candidate_id=_sha256("oc-console-explicit"),
        )
    ]


def test_opencode_auth_json_and_env_key_digests(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    # Default DB has no credential row, but OPENCODE_API_KEY is set (applies to default only)
    _write_opencode_db(xdg / "opencode" / "opencode.db", None)
    # Extra DB has no credential row, but has auth.json opencode-go key
    extra_db = _write_opencode_db(home / ".opencode-go-2" / "opencode" / "opencode.db", None)
    (extra_db.parent / "auth.json").write_text(
        json.dumps({"opencode-go": {"type": "api", "key": "go-auth-key"}}),
        encoding="utf-8",
    )

    found = detect(
        "opencode_local",
        home=home,
        environ={"XDG_DATA_HOME": str(xdg), "OPENCODE_API_KEY": "env-only-default-key"},
    )
    assert [(c.key, c.account_digest) for c in found] == [
        ("default", _sha256("env-only-default-key")),
        ("opencode-go-2", _sha256("go-auth-key")),
    ]


def test_codex_default_only_and_codex_home_override(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    (codex_dir / "sessions" / "2026").mkdir(parents=True)
    _write_codex_auth(codex_dir, account_id="codex-acct-default")

    found_no_sample = detect("codex_local", home=home, environ={})
    assert found_no_sample == [
        Candidate(
            provider="codex_local",
            folder=codex_dir,
            location=str(codex_dir / "sessions"),
            key="default",
            account_digest=_sha256("codex-acct-default"),
            plan=None,
            has_sample=False,
            is_default=True,
            candidate_id=_sha256("codex-acct-default"),
        )
    ]

    (codex_dir / "sessions" / "2026" / "rollout.jsonl").write_text("{}\n", encoding="utf-8")
    found_with_sample = detect("codex_local", home=home, environ={})
    assert found_with_sample[0].has_sample is True

    # CODEX_HOME override
    custom_codex = tmp_path / "custom-codex"
    (custom_codex / "sessions").mkdir(parents=True)
    (custom_codex / "sessions" / "s.jsonl").write_text("{}\n", encoding="utf-8")
    _write_codex_auth(custom_codex, account_id="codex-acct-custom")

    found_custom = detect("codex_local", home=home, environ={"CODEX_HOME": str(custom_codex)})
    assert found_custom == [
        Candidate(
            provider="codex_local",
            folder=custom_codex,
            location=str(custom_codex / "sessions"),
            key="default",
            account_digest=_sha256("codex-acct-custom"),
            plan=None,
            has_sample=True,
            is_default=True,
            candidate_id=_sha256("codex-acct-custom"),
        )
    ]


# ---------------------------------------------------------------------------
# 2. Extra folders + BURNRATE_* env vars
# ---------------------------------------------------------------------------


def test_claude_extra_folders_and_env_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    _write_claude_state(home, "uuid-default", tier="default_claude_max_20x")
    _write_claude_state(home / ".claude-work", "uuid-work", tier="default_claude_pro")
    ((home / ".claude-work") / "projects").mkdir()
    ((home / ".claude-work") / "projects" / "a.jsonl").write_text("{}\n", encoding="utf-8")

    ext_a = tmp_path / "profiles" / "Client A"
    ext_b = tmp_path / "profiles" / "team_b"
    _write_claude_state(ext_a, "uuid-ext-a", tier="default_claude_team")
    _write_claude_state(ext_b, "uuid-ext-b", tier="default_claude_enterprise")

    env = {"BURNRATE_CLAUDE_ACCOUNT_DIRS": os.pathsep.join([str(ext_a), str(ext_b)])}
    found = detect("claude_local", home=home, environ=env)
    assert [(c.key, c.is_default, c.plan, c.has_sample, c.account_digest) for c in found] == [
        ("default", True, "Claude Max", False, _sha256("uuid-default")),
        ("claude-code-work", False, "Claude Pro", True, _sha256("uuid-work")),
        ("claude-code-client-a", False, "Claude Team", False, _sha256("uuid-ext-a")),
        ("claude-code-team-b", False, "Claude Enterprise", False, _sha256("uuid-ext-b")),
    ]


def test_opencode_extra_folders_both_db_layouts_and_env_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-default")

    # Nested layout: <dir>/opencode/opencode.db
    _write_opencode_db(home / ".opencode-go-2" / "opencode" / "opencode.db", "oc-2")
    # Direct layout: <dir>/opencode.db
    _write_opencode_db(home / ".opencode-work" / "opencode.db", "oc-work")
    # Extra folder without DB should be ignored
    (home / ".opencode-empty").mkdir(parents=True)

    ext_1 = tmp_path / "extra" / "Client One"
    ext_2 = tmp_path / "extra" / "Direct Two"
    _write_opencode_db(ext_1 / "opencode" / "opencode.db", "oc-ext-1")
    _write_opencode_db(ext_2 / "opencode.db", "oc-ext-2")

    env = {
        "XDG_DATA_HOME": str(xdg),
        "BURNRATE_OPENCODE_PROFILE_DIRS": os.pathsep.join([str(ext_1), str(ext_2)]),
    }
    found = detect("opencode_local", home=home, environ=env)
    assert [(c.key, c.is_default, c.location, c.has_sample) for c in found] == [
        ("default", True, str(xdg / "opencode" / "opencode.db"), True),
        ("opencode-go-2", False, str(home / ".opencode-go-2" / "opencode" / "opencode.db"), True),
        ("opencode-go-work", False, str(home / ".opencode-work" / "opencode.db"), True),
        ("opencode-go-client-one", False, str(ext_1 / "opencode" / "opencode.db"), True),
        ("opencode-go-direct-two", False, str(ext_2 / "opencode.db"), True),
    ]


def test_codex_extra_folders_and_env_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_codex_auth(home / ".codex", account_id="cx-default")
    _write_codex_auth(home / ".codex-work", account_id="cx-work")
    (home / ".codex-work" / "sessions").mkdir()
    (home / ".codex-work" / "sessions" / "r.jsonl").write_text("{}\n", encoding="utf-8")

    ext_a = tmp_path / "extra-codex" / "Personal Alt"
    ext_b = tmp_path / "extra-codex" / "ci_runner"
    _write_codex_auth(ext_a, account_id="cx-ext-a")
    _write_codex_auth(ext_b, account_id="cx-ext-b")

    env = {"BURNRATE_CODEX_PROFILE_DIRS": os.pathsep.join([str(ext_a), str(ext_b)])}
    found = detect("codex_local", home=home, environ=env)
    assert [(c.key, c.is_default, c.location, c.has_sample, c.account_digest) for c in found] == [
        ("default", True, str(home / ".codex" / "sessions"), False, _sha256("cx-default")),
        ("codex-work", False, str(home / ".codex-work" / "sessions"), True, _sha256("cx-work")),
        ("codex-personal-alt", False, str(ext_a / "sessions"), False, _sha256("cx-ext-a")),
        ("codex-ci-runner", False, str(ext_b / "sessions"), False, _sha256("cx-ext-b")),
    ]


# ---------------------------------------------------------------------------
# 3. Same-account duplicates
# ---------------------------------------------------------------------------


def test_same_account_duplicate_keeps_default_over_extra_all_providers(tmp_path: Path) -> None:
    home = tmp_path / "home"

    # Claude: default and extra share uuid
    (home / ".claude").mkdir(parents=True)
    _write_claude_state(home, "shared-claude")
    _write_claude_state(home / ".claude-copy", "shared-claude")
    _write_claude_state(home / ".claude-other", "other-claude")
    claude_found = detect("claude_local", home=home, environ={})
    assert [(c.key, c.is_default) for c in claude_found] == [
        ("default", True),
        ("claude-code-other", False),
    ]

    # OpenCode: default and extra share console key
    xdg = tmp_path / "xdg"
    _write_opencode_db(xdg / "opencode" / "opencode.db", "shared-oc")
    _write_opencode_db(home / ".opencode-copy" / "opencode" / "opencode.db", "shared-oc")
    _write_opencode_db(home / ".opencode-other" / "opencode.db", "other-oc")
    oc_found = detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})
    assert [(c.key, c.is_default) for c in oc_found] == [
        ("default", True),
        ("opencode-go-other", False),
    ]

    # Codex: default and extra share account_id
    _write_codex_auth(home / ".codex", account_id="shared-codex")
    _write_codex_auth(home / ".codex-copy", account_id="shared-codex")
    _write_codex_auth(home / ".codex-other", account_id="other-codex")
    codex_found = detect("codex_local", home=home, environ={})
    assert [(c.key, c.is_default) for c in codex_found] == [
        ("default", True),
        ("codex-other", False),
    ]


def test_same_account_duplicate_among_extras_keeps_first_in_sort_order(tmp_path: Path) -> None:
    home = tmp_path / "home"

    # Claude: default has missing identity, two extras share same account
    (home / ".claude").mkdir(parents=True)
    _write_claude_state(home / ".claude-alpha", "dup-claude")
    _write_claude_state(home / ".claude-beta", "dup-claude")
    claude_found = detect("claude_local", home=home, environ={})
    assert [c.key for c in claude_found] == ["default", "claude-code-alpha"]

    # OpenCode: default has different account, two extras share same account
    xdg = tmp_path / "xdg"
    _write_opencode_db(xdg / "opencode" / "opencode.db", "distinct-oc")
    _write_opencode_db(home / ".opencode-alpha" / "opencode.db", "dup-oc")
    _write_opencode_db(home / ".opencode-beta" / "opencode.db", "dup-oc")
    oc_found = detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})
    assert [c.key for c in oc_found] == ["default", "opencode-go-alpha"]

    # Codex: default has missing account_id, two extras share same account_id
    _write_codex_auth(home / ".codex", account_id=None)
    _write_codex_auth(home / ".codex-alpha", account_id="dup-codex")
    _write_codex_auth(home / ".codex-beta", account_id="dup-codex")
    codex_found = detect("codex_local", home=home, environ={})
    assert [c.key for c in codex_found] == ["default", "codex-alpha"]


# ---------------------------------------------------------------------------
# 4. Missing identity file / missing fields -> account_digest=None, not collapsed
# ---------------------------------------------------------------------------


def test_missing_identity_keeps_candidates_uncollapsed_with_folder_id(tmp_path: Path) -> None:
    home = tmp_path / "home"

    # Claude: missing .claude.json and .claude.json without accountUuid (sorted: .claude-empty-uuid < .claude-no-file)
    (home / ".claude").mkdir(parents=True)  # no ~/.claude.json
    _write_claude_state(home / ".claude-empty-uuid", None)  # {} without oauthAccount
    (home / ".claude-no-file").mkdir(parents=True)
    claude_found = detect("claude_local", home=home, environ={})
    assert [(c.key, c.account_digest, c.candidate_id) for c in claude_found] == [
        ("default", None, _resolved_folder_id(home / ".claude")),
        ("claude-code-empty-uuid", None, _resolved_folder_id(home / ".claude-empty-uuid")),
        ("claude-code-no-file", None, _resolved_folder_id(home / ".claude-no-file")),
    ]
    assert len({c.candidate_id for c in claude_found}) == 3

    # OpenCode: DBs with no credentials (one missing table, one empty credential table)
    xdg = tmp_path / "xdg"
    _write_opencode_db(xdg / "opencode" / "opencode.db", None, with_table=False)
    _write_opencode_db(home / ".opencode-one" / "opencode.db", None, with_table=True)
    _write_opencode_db(home / ".opencode-two" / "opencode" / "opencode.db", None, with_table=False)
    oc_found = detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})
    assert [(c.key, c.account_digest, c.candidate_id) for c in oc_found] == [
        ("default", None, _resolved_folder_id(xdg / "opencode")),
        ("opencode-go-one", None, _resolved_folder_id(home / ".opencode-one")),
        ("opencode-go-two", None, _resolved_folder_id(home / ".opencode-two")),
    ]
    assert len({c.candidate_id for c in oc_found}) == 3

    # Codex: missing auth.json and auth.json without tokens.account_id (sorted: .codex-no-acct < .codex-no-auth)
    (home / ".codex").mkdir(parents=True)  # no auth.json
    _write_codex_auth(home / ".codex-no-acct", account_id=None, extra_tokens={"id_token": "tok"})
    (home / ".codex-no-auth").mkdir(parents=True)
    codex_found = detect("codex_local", home=home, environ={})
    assert [(c.key, c.account_digest, c.candidate_id) for c in codex_found] == [
        ("default", None, _resolved_folder_id(home / ".codex")),
        ("codex-no-acct", None, _resolved_folder_id(home / ".codex-no-acct")),
        ("codex-no-auth", None, _resolved_folder_id(home / ".codex-no-auth")),
    ]
    assert len({c.candidate_id for c in codex_found}) == 3


# ---------------------------------------------------------------------------
# 5. Symlinks and Windows junctions
# ---------------------------------------------------------------------------


def test_real_windows_junction_candidate_skipped_for_all_providers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    targets = tmp_path / "targets"

    # Real extra profiles
    _write_claude_state(home / ".claude-real", "c-real")
    _write_opencode_db(home / ".opencode-real" / "opencode.db", "oc-real")
    _write_codex_auth(home / ".codex-real", account_id="cx-real")

    # Junction targets outside home
    _write_claude_state(targets / "claude-junc", "c-junc")
    _write_opencode_db(targets / "opencode-junc" / "opencode.db", "oc-junc")
    _write_codex_auth(targets / "codex-junc", account_id="cx-junc")

    _make_junction(home / ".claude-junc", targets / "claude-junc")
    _make_junction(home / ".opencode-junc", targets / "opencode-junc")
    _make_junction(home / ".codex-junc", targets / "codex-junc")

    assert [c.key for c in detect("claude_local", home=home, environ={})] == ["claude-code-real"]
    assert [c.key for c in detect("opencode_local", home=home, environ={})] == ["opencode-go-real"]
    assert [c.key for c in detect("codex_local", home=home, environ={})] == ["codex-real"]


def test_real_windows_junction_inside_sample_dir_not_traversed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    targets = tmp_path / "targets"
    (targets / "secret_sessions").mkdir(parents=True)
    (targets / "secret_sessions" / "a.jsonl").write_text("{}\n", encoding="utf-8")

    _write_claude_state(home, "c-default")
    (home / ".claude" / "projects").mkdir(parents=True)
    _make_junction(home / ".claude" / "projects" / "linked", targets / "secret_sessions")

    _write_codex_auth(home / ".codex", account_id="cx-default")
    (home / ".codex" / "sessions").mkdir(parents=True)
    _make_junction(home / ".codex" / "sessions" / "linked", targets / "secret_sessions")

    assert detect("claude_local", home=home, environ={})[0].has_sample is False
    assert detect("codex_local", home=home, environ={})[0].has_sample is False


def test_monkeypatched_junction_skipped_on_candidate_folder_and_identity_file(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"

    # Claude: folder junction skipped; identity-file junction yields account_digest=None
    _write_claude_state(home / ".claude-keep", "c-keep")
    _write_claude_state(home / ".claude-jdir", "c-jdir")
    c_jfile = _write_claude_state(home / ".claude-jfile", "c-jfile")

    # OpenCode: folder junction skipped; DB file junction yields no extra candidate
    _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-default")
    _write_opencode_db(home / ".opencode-keep" / "opencode.db", "oc-keep")
    _write_opencode_db(home / ".opencode-jdir" / "opencode.db", "oc-jdir")
    oc_jfile = _write_opencode_db(home / ".opencode-jfile" / "opencode.db", "oc-jfile")

    # Codex: folder junction skipped; auth.json junction yields account_digest=None
    _write_codex_auth(home / ".codex-keep", account_id="cx-keep")
    _write_codex_auth(home / ".codex-jdir", account_id="cx-jdir")
    cx_jfile = _write_codex_auth(home / ".codex-jfile", account_id="cx-jfile")

    blocked = {
        str(home / ".claude-jdir"),
        str(c_jfile),
        str(home / ".opencode-jdir"),
        str(oc_jfile),
        str(home / ".codex-jdir"),
        str(cx_jfile),
    }
    monkeypatch.setattr(profile_detect, "is_junction", lambda path: str(path) in blocked)

    claude_found = {c.key: c for c in detect("claude_local", home=home, environ={})}
    assert set(claude_found) == {"claude-code-keep", "claude-code-jfile"}
    assert claude_found["claude-code-keep"].account_digest == _sha256("c-keep")
    assert claude_found["claude-code-jfile"].account_digest is None

    oc_found = {c.key: c for c in detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})}
    assert set(oc_found) == {"default", "opencode-go-keep"}

    cx_found = {c.key: c for c in detect("codex_local", home=home, environ={})}
    assert set(cx_found) == {"codex-keep", "codex-jfile"}
    assert cx_found["codex-keep"].account_digest == _sha256("cx-keep")
    assert cx_found["codex-jfile"].account_digest is None


def test_symlinks_skipped_on_candidate_folder_and_identity_file(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    _write_claude_state(home / ".claude-real", "c-real")
    _write_claude_state(home / ".claude-symdir", "c-symdir")
    sym_state = _write_claude_state(home / ".claude-symfile", "c-symfile")

    _write_codex_auth(home / ".codex-real", account_id="cx-real")
    _write_codex_auth(home / ".codex-symdir", account_id="cx-symdir")
    sym_auth = _write_codex_auth(home / ".codex-symfile", account_id="cx-symfile")

    symlinked = {
        str(home / ".claude-symdir"),
        str(sym_state),
        str(home / ".codex-symdir"),
        str(sym_auth),
    }
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda self: True if str(self) in symlinked else real_is_symlink(self),
    )

    claude_found = {c.key: c for c in detect("claude_local", home=home, environ={})}
    assert set(claude_found) == {"claude-code-real", "claude-code-symfile"}
    assert claude_found["claude-code-symfile"].account_digest is None

    cx_found = {c.key: c for c in detect("codex_local", home=home, environ={})}
    assert set(cx_found) == {"codex-real", "codex-symfile"}
    assert cx_found["codex-symfile"].account_digest is None


# ---------------------------------------------------------------------------
# 6. Codex auth.json token hygiene
# ---------------------------------------------------------------------------


def test_codex_auth_tokens_never_appear_in_candidates_or_logs(tmp_path: Path, caplog) -> None:
    home = tmp_path / "home"
    secrets = (
        "syn-id-token-never-leak",
        "syn-access-token-never-leak",
        "syn-refresh-token-never-leak",
        "syn-openai-api-key-never-leak",
        "syn-raw-codex-account-id",
    )
    _write_codex_auth(
        home / ".codex",
        account_id=secrets[4],
        api_key=secrets[3],
        extra_tokens={
            "id_token": secrets[0],
            "access_token": secrets[1],
            "refresh_token": secrets[2],
        },
    )

    with caplog.at_level(logging.DEBUG):
        found = detect("codex_local", home=home, environ={})

    assert len(found) == 1
    candidate = found[0]
    assert candidate.account_digest == _sha256(secrets[4])
    serialized = " | ".join(
        [
            repr(candidate),
            str(candidate.provider),
            str(candidate.folder),
            str(candidate.location),
            str(candidate.key),
            str(candidate.account_digest),
            str(candidate.plan),
            str(candidate.candidate_id),
        ]
    )
    for secret in secrets:
        assert secret not in serialized
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 7. No transcript reading
# ---------------------------------------------------------------------------


def test_has_sample_does_not_open_or_read_jsonl_files(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    claude_jsonl = home / ".claude" / "projects" / "p1" / "chat.jsonl"
    claude_jsonl.parent.mkdir(parents=True)
    claude_jsonl.write_bytes(b"\xff\xfe\x00NOT_READABLE_TRANSCRIPT")
    _write_claude_state(home, "uuid-claude")

    codex_jsonl = home / ".codex" / "sessions" / "2026" / "11" / "rollout.jsonl"
    codex_jsonl.parent.mkdir(parents=True)
    codex_jsonl.write_bytes(b"\xff\xfe\x00NOT_READABLE_TRANSCRIPT")
    _write_codex_auth(home / ".codex", account_id="acct-codex")

    opened_jsonl: list[str] = []
    real_builtin_open = builtins.open
    real_path_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def guarded_builtin_open(file, *args, **kwargs):
        if str(file).lower().endswith(".jsonl"):
            opened_jsonl.append(str(file))
            raise AssertionError(f"Unexpected open of transcript: {file}")
        return real_builtin_open(file, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        if str(self).lower().endswith(".jsonl"):
            opened_jsonl.append(str(self))
            raise AssertionError(f"Unexpected Path.open of transcript: {self}")
        return real_path_open(self, *args, **kwargs)

    def guarded_read_text(self, *args, **kwargs):
        if str(self).lower().endswith(".jsonl"):
            opened_jsonl.append(str(self))
            raise AssertionError(f"Unexpected read_text of transcript: {self}")
        return real_read_text(self, *args, **kwargs)

    def guarded_read_bytes(self, *args, **kwargs):
        if str(self).lower().endswith(".jsonl"):
            opened_jsonl.append(str(self))
            raise AssertionError(f"Unexpected read_bytes of transcript: {self}")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_builtin_open)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    found = detect_all(home=home, environ={})
    by_provider = {c.provider: c for c in found}
    assert by_provider["claude_local"].has_sample is True
    assert by_provider["codex_local"].has_sample is True
    assert opened_jsonl == []


# ---------------------------------------------------------------------------
# 8. Bounds: max 20 candidates per provider & no unconfigured folders outside home
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["claude_local", "opencode_local", "codex_local"])
def test_max_20_candidates_bound_per_provider(tmp_path: Path, provider: str) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for idx in range(25):
        tag = f"{idx:02d}"
        if provider == "claude_local":
            _write_claude_state(home / f".claude-p{tag}", f"uuid-{tag}")
        elif provider == "opencode_local":
            _write_opencode_db(home / f".opencode-p{tag}" / "opencode.db", f"oc-{tag}")
        else:
            _write_codex_auth(home / f".codex-p{tag}", account_id=f"cx-{tag}")

    found = detect(provider, home=home, environ={})
    assert len(found) == 20


def test_folders_outside_home_not_listed_in_env_are_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    _write_claude_state(outside / ".claude-stray", "stray-c")
    _write_opencode_db(outside / ".opencode-stray" / "opencode.db", "stray-oc")
    _write_codex_auth(outside / ".codex-stray", account_id="stray-cx")

    assert detect_all(home=home, environ={}) == []


# ---------------------------------------------------------------------------
# 9. detect_all(), 60s caching, and reset_cache()
# ---------------------------------------------------------------------------


def test_detect_all_returns_all_providers_caches_60s_and_resets(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    (home / ".claude").mkdir(parents=True)
    _write_claude_state(home, "c-def")
    _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-def")
    _write_codex_auth(home / ".codex", account_id="cx-def")

    now = [1000.0]
    monkeypatch.setattr(profile_detect.time, "monotonic", lambda: now[0])

    env = {"XDG_DATA_HOME": str(xdg)}
    first = detect_all(home=home, environ=env)
    assert {c.provider for c in first} == {"claude_local", "opencode_local", "codex_local"}
    assert len(first) == 3

    # Add a new extra profile on disk; within TTL it stays cached
    _write_codex_auth(home / ".codex-new", account_id="cx-new")
    now[0] = 1059.0
    cached = detect_all(home=home, environ=env)
    assert len(cached) == 3

    # Advancing past 60s triggers a re-scan
    now[0] = 1060.5
    refreshed = detect_all(home=home, environ=env)
    assert len(refreshed) == 4
    assert any(c.provider == "codex_local" and c.key == "codex-new" for c in refreshed)

    # Add another profile; reset_cache() forces immediate re-scan
    _write_claude_state(home / ".claude-second", "c-second")
    assert len(detect_all(home=home, environ=env)) == 4
    reset_cache()
    after_reset = detect_all(home=home, environ=env)
    assert len(after_reset) == 5
    assert any(c.provider == "claude_local" and c.key == "claude-code-second" for c in after_reset)


# ---------------------------------------------------------------------------
# 10. Unsupported provider raises ValueError
# ---------------------------------------------------------------------------


def test_unsupported_provider_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported provider"):
        detect("grok_local", home=tmp_path, environ={})


# ---------------------------------------------------------------------------
# 11. Existing claude_accounts.discover / opencode_profiles.discover compatibility
# ---------------------------------------------------------------------------


def test_existing_claude_accounts_and_opencode_profiles_discover_skip_junctions(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"

    _write_claude_state(home, "c-default")
    _write_claude_state(home / ".claude-work", "c-work")
    _write_claude_state(home / ".claude-junc", "c-junc")

    default_db = _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-default")
    _write_opencode_db(home / ".opencode-work" / "opencode" / "opencode.db", "oc-work")
    _write_opencode_db(home / ".opencode-junc" / "opencode" / "opencode.db", "oc-junc")

    # Monkeypatched is_junction check
    blocked = {str(home / ".claude-junc"), str(home / ".opencode-junc")}
    monkeypatch.setattr(connection_paths, "is_junction", lambda path: str(path) in blocked)

    c_accounts = claude_accounts.discover(home=home, environ={})
    assert [a.key for a in c_accounts] == ["claude-code-work"]

    oc_profiles = opencode_profiles.discover(home=home, environ={}, primary_database=default_db)
    assert [p.key for p in oc_profiles] == ["opencode-go-work"]


def test_existing_discover_skips_real_windows_junction(tmp_path: Path) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    targets = tmp_path / "targets"

    _write_claude_state(home, "c-default")
    _write_claude_state(home / ".claude-work", "c-work")
    _write_claude_state(targets / "c-junc", "c-junc")
    _make_junction(home / ".claude-junc", targets / "c-junc")

    default_db = _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-default")
    _write_opencode_db(home / ".opencode-work" / "opencode.db", "oc-work")
    _write_opencode_db(targets / "oc-junc" / "opencode.db", "oc-junc")
    _make_junction(home / ".opencode-junc", targets / "oc-junc")

    assert [a.key for a in claude_accounts.discover(home=home, environ={})] == ["claude-code-work"]
    assert [p.key for p in opencode_profiles.discover(home=home, environ={}, primary_database=default_db)] == [
        "opencode-go-work"
    ]


# ---------------------------------------------------------------------------
# 12. current_account_digest() helper and 60s cache
# ---------------------------------------------------------------------------


def test_current_account_digest_reads_live_identity_caches_60s_and_checks_safety(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    targets = tmp_path / "targets"
    (home / ".claude" / "projects").mkdir(parents=True)
    _write_claude_state(home, "claude-def-uuid")
    claude_work = tmp_path / ".claude-work"
    (claude_work / "projects").mkdir(parents=True)
    _write_claude_state(claude_work, "claude-work-uuid")

    codex_work = tmp_path / ".codex-work"
    (codex_work / "sessions").mkdir(parents=True)
    _write_codex_auth(codex_work, account_id="codex-acct-A")

    oc_work_db = _write_opencode_db(tmp_path / ".opencode-work" / "opencode" / "opencode.db", "oc-key-A")
    oc_def_db = _write_opencode_db(xdg / "opencode" / "opencode.db", "oc-def-key")

    now = [2000.0]
    monkeypatch.setattr(profile_detect.time, "monotonic", lambda: now[0])
    env = {"XDG_DATA_HOME": str(xdg)}

    assert profile_detect.current_account_digest("claude_local", home / ".claude" / "projects", home=home, environ=env) == profile_detect._sha256_hex("claude-def-uuid")
    assert profile_detect.current_account_digest("claude_local", claude_work / "projects", home=home, environ=env) == profile_detect._sha256_hex("claude-work-uuid")
    assert profile_detect.current_account_digest("codex_local", str(codex_work / "sessions") + "/**/*.jsonl", home=home, environ=env) == profile_detect._sha256_hex("codex-acct-A")
    assert profile_detect.current_account_digest("opencode_local", oc_work_db, home=home, environ=env) == profile_detect._sha256_hex("oc-key-A")
    assert profile_detect.current_account_digest("opencode_local", oc_def_db.parent, is_default=True, home=home, environ=env) == profile_detect._sha256_hex("oc-def-key")
    assert profile_detect.current_account_digest("grok_local", tmp_path, home=home, environ=env) is None
    assert profile_detect.current_account_digest("codex_local", None, home=home, environ=env) is None

    # Switch Codex auth on disk: cached for 60s unless use_cache=False or reset_cache()
    _write_codex_auth(codex_work, account_id="codex-acct-B")
    now[0] = 2059.0
    assert profile_detect.current_account_digest("codex_local", codex_work / "sessions", home=home, environ=env) == profile_detect._sha256_hex("codex-acct-A")
    assert profile_detect.current_account_digest("codex_local", codex_work / "sessions", home=home, environ=env, use_cache=False) == profile_detect._sha256_hex("codex-acct-B")
    assert profile_detect.current_account_digest("codex_local", codex_work / "sessions", home=home, environ=env) == profile_detect._sha256_hex("codex-acct-B")

    # After 60s TTL expires, reads new digest
    _write_codex_auth(codex_work, account_id="codex-acct-C")
    now[0] = 2120.0
    assert profile_detect.current_account_digest("codex_local", codex_work / "sessions", home=home, environ=env) == profile_detect._sha256_hex("codex-acct-C")

    # reset_cache() clears digest cache immediately
    _write_codex_auth(codex_work, account_id="codex-acct-D")
    reset_cache()
    assert profile_detect.current_account_digest("codex_local", codex_work / "sessions", home=home, environ=env) == profile_detect._sha256_hex("codex-acct-D")

    # Junction folder is rejected
    _write_codex_auth(targets / "codex-junc", account_id="codex-junc")
    _make_junction(home / ".codex-junc", targets / "codex-junc")
    assert profile_detect.current_account_digest("codex_local", home / ".codex-junc", home=home, environ=env) is None
