"""Comprehensive regression coverage for error/evidence redaction and sync error codes."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import quote
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from spend_app import (
    attention_store,
    connections,
    plans_value,
    plans_value_store,
    scheduler,
    setup_wizard,
    source_evidence,
    source_health,
    support_report,
)
from spend_app.adapters.common import UsageRow, failed_result, persist_rows, public_error
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import APPROVED_REVISION, LocationError, approved
from spend_app.db import (
    INGEST_ERROR_REDACT_KEY,
    SPOKE_SYNC_ERROR_REDACT_KEY,
    connect,
    initialize,
    redact_legacy_ingest_errors,
)
from spend_app.ingest import IngestRun
from spend_app.pricing import PricingEngine
from spend_app.providers import ProviderSpec
import spend_app.redact as redact_module
from spend_app.redact import redact

ROOT = Path(__file__).resolve().parents[1]

FIXED_DEFAULT_ERROR = (
    "Connected source could not be imported. Recheck its location, schema or provider access."
)

# (case_id, raw_secret_token, raw_message, forbidden_substrings)
TOKEN_CASES: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "sk_proj_underscore",
        "sk-proj_abc123def456",
        "Upstream error using sk-proj_abc123def456 during import",
        ("sk-proj_abc123def456", "abc123def456"),
    ),
    (
        "jwt_full_3part",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "Token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U rejected",
        (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        ),
    ),
    (
        "jwt_header_dot",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.",
        "Truncated eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9. token",
        ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",),
    ),
    (
        "jwt_header_bare",
        "eyJhbGciOiJIUzI1NiJ9",
        "Bare JWT header eyJhbGciOiJIUzI1NiJ9 rejected",
        ("eyJhbGciOiJIUzI1NiJ9",),
    ),
    (
        "pct_encoded_single",
        "%73%6B-proj_encoded123",
        "Encoded key %73%6B-proj_encoded123 failed",
        ("%73%6B-proj_encoded123", "sk-proj_encoded123", "encoded123"),
    ),
    (
        "pct_encoded_double",
        "%2573%256B-proj_double123",
        "Double-encoded key %2573%256B-proj_double123 failed",
        ("%2573%256B-proj_double123", "%73%6B-proj_double123", "sk-proj_double123", "double123"),
    ),
    (
        "pct_encoded_triple",
        "%252573%25256B-proj_triple123",
        "Triple-encoded key %252573%25256B-proj_triple123 failed",
        ("%252573%25256B-proj_triple123", "sk-proj_triple123", "triple123"),
    ),
    (
        "github_ghp",
        "ghp_1234567890abcdef1234",
        "GitHub ghp_1234567890abcdef1234 failed",
        ("ghp_1234567890abcdef1234", "1234567890abcdef1234"),
    ),
    (
        "github_pat",
        "github_pat_11AAAAAA_0123456789abcdef",
        "GitHub PAT github_pat_11AAAAAA_0123456789abcdef failed",
        ("github_pat_11AAAAAA_0123456789abcdef", "0123456789abcdef"),
    ),
    (
        "brpair",
        "brpair_eyJodWJVcmwiOiJodHRwczovL2h1Yi5leGFtcGxlIn0=",
        "Pairing brpair_eyJodWJVcmwiOiJodHRwczovL2h1Yi5leGFtcGxlIn0= rejected",
        (
            "brpair_eyJodWJVcmwiOiJodHRwczovL2h1Yi5leGFtcGxlIn0=",
            "eyJodWJVcmwiOiJodHRwczovL2h1Yi5leGFtcGxlIn0",
        ),
    ),
    (
        "bearer",
        "Bearer bearer_secret_value_999",
        "Header Bearer bearer_secret_value_999 rejected",
        ("bearer_secret_value_999",),
    ),
    (
        "basic",
        "Basic dXNlcjpwYXNzd29yZA==",
        "Header Basic dXNlcjpwYXNzd29yZA== rejected",
        ("dXNlcjpwYXNzd29yZA==",),
    ),
    (
        "zai",
        "zai_live_secret_token_987",
        "Z.AI zai_live_secret_token_987 rejected",
        ("zai_live_secret_token_987",),
    ),
    (
        "xai",
        "xai-secret-token-456",
        "xAI xai-secret-token-456 rejected",
        ("xai-secret-token-456",),
    ),
    (
        "or_v1",
        "or-v1-secret-token-789",
        "OpenRouter or-v1-secret-token-789 rejected",
        ("or-v1-secret-token-789",),
    ),
    (
        "br_m",
        "br_m_machine_secret_12345",
        "Machine token br_m_machine_secret_12345 rejected",
        ("br_m_machine_secret_12345",),
    ),
    (
        "hex32",
        "0123456789abcdef0123456789abcdef",
        "Raw hex 0123456789abcdef0123456789abcdef leaked",
        ("0123456789abcdef0123456789abcdef",),
    ),
    (
        "api_key_field",
        "api_key=my_field_secret_value123",
        "Query api_key=my_field_secret_value123 failed",
        ("my_field_secret_value123",),
    ),
    (
        "email",
        "alice.smith@example.com",
        "Account alice.smith@example.com denied",
        ("alice.smith@example.com", "alice.smith"),
    ),
    (
        "windows_home",
        r"C:\Users\alice\Documents\secret.json",
        r"Read error at C:\Users\alice\Documents\secret.json",
        ("alice",),
    ),
    (
        "unix_home",
        "/home/alice/.codex/auth.json",
        "Read error at /home/alice/.codex/auth.json",
        ("alice",),
    ),
]


def _settings(tmp_path: Path, name: str = "spend.db") -> Settings:
    return Settings(
        database_path=tmp_path / name,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _assert_none_survive(text: str | None, forbidden: tuple[str, ...]) -> None:
    assert text is not None
    for secret in forbidden:
        assert secret not in text, f"Secret {secret!r} survived in {text!r}"


class _DummyInspector:
    def __init__(self, *, fail_on_recheck: bool = False) -> None:
        self.fail_on_recheck = fail_on_recheck
        self.recheck_error = "Missing location"

    def normalize(self, raw, meta):
        return raw

    def inspect(self, source, path, meta):
        if self.fail_on_recheck:
            raise LocationError(self.recheck_error)
        return {"state": "readable", "usableSample": True, "detail": "Readable sample"}


class _DummyVault:
    def available(self) -> bool:
        return True

    def get(self, ref: str) -> str:
        return "unused"

    def put(self, secret: str, ref: str | None = None) -> str:
        return ref or "burnrate-ref"

    def delete(self, ref: str) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. Parameterized token suite across every redaction & persistence path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_redact_module_scrubs_all_tokens(case_id, token, message, forbidden) -> None:
    _assert_none_survive(redact(message), forbidden)
    _assert_none_survive(redact(token), forbidden)

    # Length cap applies AFTER redaction so no credential prefix or fragment
    # survives even when the cutoff lands inside where the secret sat.
    credential_prefixes = (
        "sk-",
        "ghp_",
        "github_pat_",
        "brpair_",
        "br_m_",
        "zai_",
        "xai-",
        "or-v1-",
        "eyJ",
        "%73%6B",
        "%2573",
    )
    padded = ("x" * 94) + " " + token + " trailing"
    for limit in (98, 99, 101, 105, 110):
        capped = redact(padded, limit)
        assert len(capped) <= limit
        _assert_none_survive(capped, forbidden)
        for prefix in credential_prefixes:
            assert prefix not in capped, f"Prefix {prefix!r} leaked at limit={limit} in {capped!r}"
        for secret in forbidden:
            if len(secret) >= 4:
                assert secret[:4] not in capped, f"Fragment {secret[:4]!r} leaked at limit={limit} in {capped!r}"
                assert secret[-4:] not in capped, f"Fragment {secret[-4:]!r} leaked at limit={limit} in {capped!r}"
    if token.startswith(credential_prefixes):
        for sep in ("_", "/", ".", "=", ":"):
            emb = ("x" * 94) + sep + token + " trailing"
            for limit in (98, 99, 101, 105, 110):
                capped_emb = redact(emb, limit)
                assert len(capped_emb) <= limit
                _assert_none_survive(capped_emb, forbidden)
                for prefix in credential_prefixes:
                    assert prefix not in capped_emb, f"Prefix {prefix!r} leaked with sep={sep!r} at limit={limit} in {capped_emb!r}"
                for secret in forbidden:
                    if len(secret) >= 4:
                        assert secret[:4] not in capped_emb, f"Fragment {secret[:4]!r} leaked with sep={sep!r} at limit={limit} in {capped_emb!r}"
                        assert secret[-4:] not in capped_emb, f"Fragment {secret[-4:]!r} leaked with sep={sep!r} at limit={limit} in {capped_emb!r}"


def test_redact_percent_decode_three_pass_boundary_and_home_and_limit() -> None:
    # 3 passes decode and redact
    triple = "%252573%25256B-proj_triple123"
    assert redact(triple) == "[redacted]"
    # 4 passes stop after 3 decode passes (stability boundary)
    quad = "%25252573%2525256B-proj_quad123"
    result_quad = redact(quad)
    assert result_quad == "%73%6B-proj_quad123"
    assert "sk-proj_quad123" not in result_quad

    # Home directory scrubbing
    home = Path("C:/Users/alice")
    assert redact(r"C:\Users\alice\.codex\auth.json", home=home) == r"~\.codex\auth.json"
    assert redact("C:/Users/alice/.codex/auth.json", home=home) == "~/.codex/auth.json"
    assert redact(r"C:\Users\bob\secret.txt", home=home) == r"C:\Users\[user]\secret.txt"
    assert redact("/home/bob/.config/secret", home=home) == "/home/[user]/.config/secret"
    assert redact("/Users/bob/.config/secret", home=home) == "/Users/[user]/.config/secret"
    assert redact("https://alice:topsecret@hub.example/sync") == "https://[redacted]@hub.example/sync"

    # Limit edge cases
    assert redact("abcdef", 3) == "abc"
    assert redact("abcdef", 0) == ""
    assert redact("abcdef", -5) == ""
    assert redact(None) == ""


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_public_error_and_scheduler_redact_across_modes(
    case_id, token, message, forbidden, tmp_path: Path, monkeypatch
) -> None:
    # Default mode with APPROVED_REVISION == 0
    monkeypatch.delenv("BURNRATE_DEBUG_ERRORS", raising=False)
    assert APPROVED_REVISION.get() == 0
    err_rev0 = public_error(RuntimeError(message))
    assert err_rev0 == FIXED_DEFAULT_ERROR
    _assert_none_survive(err_rev0, forbidden)

    # Default mode with APPROVED_REVISION > 0
    with approved("C:/fake", 2):
        assert APPROVED_REVISION.get() == 2
        err_rev2 = public_error(RuntimeError(message))
        assert err_rev2 == FIXED_DEFAULT_ERROR
        _assert_none_survive(err_rev2, forbidden)

    # Debug mode: redacted raw exception text
    monkeypatch.setenv("BURNRATE_DEBUG_ERRORS", "1")
    err_debug = public_error(RuntimeError(message))
    _assert_none_survive(err_debug, forbidden)

    # scheduler._run_ingest_specs exception path -> persisted into ingest_runs.error
    db_path = tmp_path / "sched.db"
    initialize(db_path)
    spec = ProviderSpec(
        key="codex_local",
        ingest_import="tests_spend.test_error_redaction:dummy",
        capabilities=frozenset({"usage"}),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=lambda **kwargs: (_ for _ in ()).throw(RuntimeError(message)),
        kind="local",
    )
    monkeypatch.delenv("BURNRATE_DEBUG_ERRORS", raising=False)
    with pytest.raises(RuntimeError, match="ingest job\\(s\\) failed"):
        scheduler._run_ingest_specs([(spec, {"database_path": db_path})])
    with connect(db_path) as conn:
        stored_default = conn.execute(
            "SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert stored_default == FIXED_DEFAULT_ERROR
    _assert_none_survive(stored_default, forbidden)

    monkeypatch.setenv("BURNRATE_DEBUG_ERRORS", "1")
    with pytest.raises(RuntimeError, match="ingest job\\(s\\) failed"):
        scheduler._run_ingest_specs([(spec, {"database_path": db_path})])
    with connect(db_path) as conn:
        stored_debug = conn.execute(
            "SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    _assert_none_survive(stored_debug, forbidden)


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_ingest_run_and_persist_rows_redact_sqlite_errors(
    case_id, token, message, forbidden, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("BURNRATE_DEBUG_ERRORS", raising=False)
    db_path = tmp_path / "ingest.db"
    initialize(db_path)
    pricing = PricingEngine.load(ROOT / "pricing")

    # Direct IngestRun.finish
    with connect(db_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=message)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    _assert_none_survive(stored, forbidden)

    # persist_rows: unpriced model + quarantined row + issues text -> ingest_runs.error
    unpriced_row = UsageRow(
        source="codex_local",
        tool_key="codex",
        model_key=f"unlisted/{token}",
        occurred_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        session_id="s1",
        project="proj",
        input_tokens=10,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=5,
        reasoning_tokens=None,
        cost_usd=None,
        raw_id=f"raw-unpriced-{case_id}",
    )
    bad_row = UsageRow(
        source="codex_local",
        tool_key="codex",
        model_key="gpt-5.6-sol",
        occurred_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        session_id="s2",
        project="proj",
        input_tokens=-1,  # triggers ValueError in pricing.compute -> quarantined
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=0,
        reasoning_tokens=None,
        cost_usd=None,
        raw_id=f"raw-bad-{case_id}",
    )
    persist_rows(
        database_path=db_path,
        pricing=pricing,
        source="codex_local",
        usage_rows=[unpriced_row, bad_row],
        issues=[message],
    )
    with connect(db_path) as conn:
        persist_error = conn.execute(
            "SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "quarantined malformed rows: 1" in persist_error.lower()
    assert "Unpriced models:" in persist_error
    _assert_none_survive(persist_error, forbidden)


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_connections_mutate_recheck_and_completion_redact_stored_details(
    case_id, token, message, forbidden, tmp_path: Path
) -> None:
    db_path = tmp_path / "conn.db"
    initialize(db_path)
    settings = _settings(tmp_path, "conn.db")
    inspector = _DummyInspector()
    svc = connections.Service(
        settings,
        store=connections.Store(db_path),
        inspector=inspector,
        vault=_DummyVault(),
        probe=lambda *args: None,
    )
    svc.mutate(
        {
            "source": "codex_local",
            "revision": 0,
            "operation": "connect",
            "location": "C:/fake/sessions",
            "requestId": str(uuid4()),
        }
    )

    # recheck LocationError -> stored in connections.v1 detail
    inspector.fail_on_recheck = True
    inspector.recheck_error = message
    with pytest.raises(LocationError):
        svc.mutate(
            {
                "source": "codex_local",
                "revision": 1,
                "operation": "recheck",
                "location": "C:/fake/sessions",
                "requestId": str(uuid4()),
            }
        )
    with connect(db_path) as conn:
        raw_v1 = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (connections.KEY,)
        ).fetchone()[0]
    detail_recheck = json.loads(raw_v1)["bindings"]["codex_local"]["detail"]
    _assert_none_survive(detail_recheck, forbidden)

    # completion(issues) with accepted == 0
    svc.completion("codex_local", 1, {"status": "partial", "eventsAccepted": 0, "issues": [message]})
    with connect(db_path) as conn:
        raw_v1_zero = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (connections.KEY,)
        ).fetchone()[0]
    detail_zero = json.loads(raw_v1_zero)["bindings"]["codex_local"]["detail"]
    _assert_none_survive(detail_zero, forbidden)

    # completion(issues) with accepted > 0
    svc.completion("codex_local", 1, {"status": "partial", "eventsAccepted": 2, "issues": [message]})
    with connect(db_path) as conn:
        raw_v1_pos = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (connections.KEY,)
        ).fetchone()[0]
    detail_pos = json.loads(raw_v1_pos)["bindings"]["codex_local"]["detail"]
    _assert_none_survive(detail_pos, forbidden)


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_attention_safe_label_rejects_credentials_and_paths(
    case_id, token, message, forbidden
) -> None:
    assert attention_store.safe_label(token) == "Unidentified evidence"
    assert attention_store.safe_label(message) == "Unidentified evidence"


def test_attention_safe_label_additional_structural_checks() -> None:
    assert attention_store.safe_label(r"D:\private\folder") == "Unidentified evidence"
    assert attention_store.safe_label("C:/Users/alice/file") == "Unidentified evidence"
    assert attention_store.safe_label("https://hub.example/path") == "Unidentified evidence"
    assert attention_store.safe_label("owner@host") == "Unidentified evidence"
    assert attention_store.safe_label("codex:5h:codex") == "codex:5h:codex"


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_evidence_and_health_helpers_scrub_all_tokens(
    case_id, token, message, forbidden, tmp_path: Path
) -> None:
    _assert_none_survive(plans_value_store._safe_health_reason(message, "codex_local"), forbidden)
    _assert_none_survive(plans_value._collection_safe_text(message), forbidden)
    _assert_none_survive(source_evidence.sanitize_reason(message), forbidden)
    reason_text = source_evidence.source_reason_text(message)
    _assert_none_survive(reason_text, forbidden)

    # source_health.quarantine / skipped
    q_outcome = source_health.quarantine(message, "file.jsonl:1")
    s_outcome = source_health.skipped(message, "file.jsonl:2")
    _assert_none_survive(q_outcome.reason, forbidden)
    _assert_none_survive(s_outcome.reason, forbidden)

    # source_health.source_attempt exception path
    db_path = tmp_path / "sh.db"
    with pytest.raises(RuntimeError):
        with source_health.source_attempt(db_path, "codex_local"):
            raise RuntimeError(message)
    with connect(db_path) as conn:
        err_row = conn.execute(
            "SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    _assert_none_survive(err_row, forbidden)

    # source_health.source_attempt partial close_attempt path
    health = source_health.SourceHealth()
    health.note(q_outcome)
    health.note(s_outcome)
    with source_health.source_attempt(db_path, "codex_local") as outcomes:
        outcomes.append(source_health.close_attempt(health, written=1))
    with connect(db_path) as conn:
        partial_row = conn.execute(
            "SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    _assert_none_survive(partial_row, forbidden)


@pytest.mark.parametrize("case_id,token,message,forbidden", TOKEN_CASES, ids=[c[0] for c in TOKEN_CASES])
def test_support_report_and_diagnostics_endpoint_scrub_db_and_long_log_lines(
    case_id, token, message, forbidden, tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=message)

    logs = tmp_path / "logs"
    logs.mkdir()
    long_line = ("L" * 390) + token + " trailing-context"
    assert len(long_line) > support_report.LINE_CHARS
    for log_name in support_report.LOG_FILES:
        (logs / log_name).write_text(f"{message}\n{long_line}\n", encoding="utf-8")

    report_text = support_report.build_report(settings, log_dir=logs)
    for log_name in support_report.LOG_FILES:
        assert f"## {log_name}" in report_text
    assert "## Recent failed runs" in report_text
    _assert_none_survive(report_text, forbidden)

    # POST /api/setup/diagnostics end-to-end
    monkeypatch.setattr("spend_app.service.log_dir", lambda: logs)
    app = create_app(settings, enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.post("/api/setup/diagnostics", headers={"X-BURNRATE-Request": "1"})
    assert resp.status_code == 200
    body_text = resp.json()["text"]
    assert "## dashboard.log" in body_text
    assert "## collector.log" in body_text
    assert "## watchdog.log" in body_text
    _assert_none_survive(body_text, forbidden)


# ---------------------------------------------------------------------------
# 2. Revision-0 default collection test
# ---------------------------------------------------------------------------


def test_revision_zero_default_stores_fixed_sentence_in_ingest_runs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("BURNRATE_DEBUG_ERRORS", raising=False)
    assert APPROVED_REVISION.get() == 0

    raw_secret = "sk-proj_rev0secret999"
    assert public_error(RuntimeError(f"boom {raw_secret}")) == FIXED_DEFAULT_ERROR
    assert (
        public_error(PermissionError(f"denied {raw_secret}"))
        == "Permission denied reading the connected source. Recheck access."
    )
    resp_401 = SimpleNamespace(response=SimpleNamespace(status_code=401))
    exc_401 = RuntimeError(f"http 401 {raw_secret}")
    exc_401.response = resp_401.response  # type: ignore[attr-defined]
    assert public_error(exc_401) == "Connected provider rejected the credential or its read scope."
    exc_429 = RuntimeError(f"http 429 {raw_secret}")
    exc_429.response = SimpleNamespace(status_code=429)  # type: ignore[attr-defined]
    assert (
        public_error(exc_429)
        == "Connected provider rate limited collection. Wait for the normal retry cadence."
    )

    db_path = tmp_path / "rev0.db"
    initialize(db_path)
    pricing = PricingEngine.load(ROOT / "pricing")

    # Failure inside persist_rows rolls back atomically; callers record failed_result(reason=public_error(exc)).
    with pytest.raises(RuntimeError):
        persist_rows(
            database_path=db_path,
            pricing=pricing,
            source="codex_local",
            usage_rows=[],
            finalize=lambda _conn: (_ for _ in ()).throw(RuntimeError(f"db fail {raw_secret}")),
        )
    failed_result(
        database_path=db_path,
        source="codex_local",
        reason=public_error(RuntimeError(f"db fail {raw_secret}")),
    )
    with connect(db_path) as conn:
        err = conn.execute("SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert err == FIXED_DEFAULT_ERROR
    assert raw_secret not in err

    # Failure via scheduler._run_ingest_specs at default revision 0
    spec = ProviderSpec(
        key="claude_local",
        ingest_import="tests_spend.test_error_redaction:dummy",
        capabilities=frozenset({"usage"}),
        stability="official",
        exactness="exact",
        enabled_by_default=True,
        ingest=lambda **kwargs: (_ for _ in ()).throw(RuntimeError(f"collect fail {raw_secret}")),
        kind="local",
    )
    with pytest.raises(RuntimeError, match="ingest job\\(s\\) failed"):
        scheduler._run_ingest_specs([(spec, {"database_path": db_path})])
    with connect(db_path) as conn:
        err2 = conn.execute("SELECT error FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert err2 == FIXED_DEFAULT_ERROR
    assert raw_secret not in err2


# ---------------------------------------------------------------------------
# 3. Spoke sync error codes & UI mappings
# ---------------------------------------------------------------------------


def _seed_spoke(db_path: Path) -> None:
    initialize(db_path)
    with connect(db_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            **state["spokeConfig"],
            "hubUrl": "https://hub.example",
            "token": "br_m_spoke_secret_token",
            "enabled": True,
        }
        setup_wizard.write_setup_state(conn, state)


@pytest.mark.parametrize(
    "exc_factory,expected_code",
    [
        (lambda: httpx.ConnectError("dial failed https://user:pw@hub.example"), "hub_unreachable"),
        (lambda: OSError("socket down sk-proj_leak123"), "hub_unreachable"),
        (lambda: RuntimeError("Sync push failed (401): invalid br_m_spoke_secret_token"), "hub_rejected"),
        (lambda: RuntimeError("Sync push failed (403): forbidden sk-proj_leak123"), "hub_rejected"),
        (lambda: RuntimeError("Sync push failed (500): trace sk-proj_leak123"), "sync_failed"),
        (lambda: RuntimeError("unexpected boom sk-proj_leak123"), "sync_failed"),
    ],
)
def test_spoke_sync_stores_and_returns_fixed_error_codes(
    exc_factory, expected_code, tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, "spoke.db")
    _seed_spoke(settings.database_path)
    pricing = PricingEngine.load(ROOT / "pricing")

    def boom(*args, **kwargs):
        raise exc_factory()

    monkeypatch.setattr(setup_wizard, "sync_once", boom)
    result = setup_wizard.maybe_run_configured_spoke_sync(settings, pricing)
    assert result == {"ok": False, "error": expected_code}

    with connect(settings.database_path) as conn:
        saved = setup_wizard.read_setup_state(conn)
    stored_err = saved["spokeConfig"]["lastSyncError"]
    assert stored_err == expected_code
    assert "sk-proj_leak123" not in stored_err
    assert "br_m_spoke_secret_token" not in stored_err


def test_ui_templates_contain_spoke_sync_error_code_mappings() -> None:
    wizard_html = setup_wizard.SETUP_WIZARD_HTML
    wealthfront_html = (ROOT / "spend_app" / "wealthfront_ui.html").read_text(encoding="utf-8")
    for code in ("hub_unreachable", "hub_rejected", "sync_failed"):
        assert code in wizard_html, f"{code} missing from SETUP_WIZARD_HTML"
        assert code in wealthfront_html, f"{code} missing from wealthfront_ui.html"
    assert 'SYNC_ERROR_TEXT[spoke.lastSyncError] || "sync failed"' in wizard_html
    assert 'syncErrMap[syncErrRaw] || "Sync failed"' in wealthfront_html
    assert "syncErrMap[syncErrRaw] || syncErrRaw" not in wealthfront_html


# ---------------------------------------------------------------------------
# 4. One-time startup migration (db.initialize / db.redact_legacy_ingest_errors)
# ---------------------------------------------------------------------------


def test_one_time_startup_migration_redacts_legacy_ingest_errors_and_becomes_noop(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy.db"
    initialize(db_path)

    legacy_rows = [
        ("codex_local", "failed", "Failed with sk-proj_abc123def456"),
        ("claude_local", "failed", "Encoded %73%6B-proj_encoded123 failed"),
        (
            "opencode_local",
            "failed",
            "JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.sig123",
        ),
        ("grok_local", "partial", "Unpriced models: synthetic-clean-model"),
    ]
    with connect(db_path) as conn:
        for source, status, error in legacy_rows:
            conn.execute(
                "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written, error) "
                "VALUES(?, '2026-09-01T00:00:00Z', '2026-09-01T00:00:01Z', ?, 0, ?)",
                (source, status, error),
            )
        conn.execute("DELETE FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,))

    initialize(db_path)

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT source, error FROM ingest_runs ORDER BY id"
        ).fetchall()
        marker = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,)
        ).fetchone()
    assert marker is not None
    assert json.loads(marker[0])["updatedRows"] == 3

    by_source = {row[0]: row[1] for row in rows}
    assert "sk-proj_abc123def456" not in by_source["codex_local"]
    assert "[redacted]" in by_source["codex_local"]
    assert "%73%6B-proj_encoded123" not in by_source["claude_local"]
    assert "sk-proj_encoded123" not in by_source["claude_local"]
    assert "[redacted]" in by_source["claude_local"]
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in by_source["opencode_local"]
    assert "[redacted]" in by_source["opencode_local"]
    assert by_source["grok_local"] == "Unpriced models: synthetic-clean-model"

    # Subsequent initialize() is a no-op while marker is present
    unredacted_sentinel = "bypassed sk-proj_should_remain_untouched"
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written, error) "
            "VALUES('muse_local', '2026-09-01T00:01:00Z', '2026-09-01T00:01:01Z', 'failed', 0, ?)",
            (unredacted_sentinel,),
        )
    assert redact_legacy_ingest_errors(db_path) == 0
    initialize(db_path)
    with connect(db_path) as conn:
        latest = conn.execute(
            "SELECT error FROM ingest_runs WHERE source='muse_local' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert latest == unredacted_sentinel


# ---------------------------------------------------------------------------
# 5. Security review regressions: quoted fields, headers, embedded prefixes,
#    prose preservation, and bounded-batch startup migration
# ---------------------------------------------------------------------------


def test_redact_quoted_fields_headers_embedded_prefixes_and_prose() -> None:
    # Finding 1: full quoted values (single, double, JSON, JSON-escaped)
    dq = redact('login failed password="synthetic correct horse battery staple" next=ok')
    assert dq == 'login failed password="[redacted]" next=ok'

    sq = redact("login failed password='synthetic correct horse battery staple' next=ok")
    assert sq == "login failed password='[redacted]' next=ok"

    js = redact('{"password": "synthetic correct horse battery staple", "keep": "visible"}')
    assert js == '{"password": "[redacted]", "keep": "visible"}'

    escaped_js = redact(r'{\"password\":\"synthetic correct horse battery staple\",\"keep\":\"visible\"}')
    assert escaped_js == r'{\"password\":\"[redacted]\",\"keep\":\"visible\"}'

    inner_escaped = redact(r'{"api_key": "first \"quoted\" word", "ok": 1}')
    assert inner_escaped == '{"api_key": "[redacted]", "ok": 1}'

    # Finding 1: Cookie / Set-Cookie / Authorization headers redacted to end of line
    cookie_hdr = redact("Cookie: sid=a; csrf=b\nGET /healthz")
    assert cookie_hdr == "Cookie: [redacted]\nGET /healthz"

    set_cookie_hdr = redact("Set-Cookie: sid=secret123; Path=/; HttpOnly; SameSite=Lax\nX-Ok: 1")
    assert set_cookie_hdr == "Set-Cookie: [redacted]\nX-Ok: 1"

    auth_hdr = redact("Authorization: Custom scheme part1 part2\nHost: localhost")
    assert auth_hdr == "Authorization: [redacted]\nHost: localhost"

    json_cookie = redact('{"Cookie": "sid=a; csrf=b", "keep": "yes"}')
    assert json_cookie == '{"Cookie": "[redacted]", "keep": "yes"}'

    # Finding 2: credential prefixes embedded after _, /, ., =, :, -
    embedded_cases = [
        ("connection_sk-proj_syntheticCredentialValue", "connection_[redacted]", "syntheticCredentialValue"),
        ("path/sk-proj_syntheticPathValue", "path/[redacted]", "syntheticPathValue"),
        ("host.ghp_1234567890abcdef1234", "host.[redacted]", "1234567890abcdef1234"),
        ("k=github_pat_11AAAAAA_0123456789abcdef", "k=[redacted]", "0123456789abcdef"),
        ("k:br_m_machine_secret_12345", "k:[redacted]", "machine_secret_12345"),
        ("prefix_brpair_eyJodWJVcmwiOiJodHRwczovL2h1Yi5leGFtcGxlIn0=", "prefix_[redacted]", "eyJodWJVcmwi"),
        ("prefix/xai-secret-token-456", "prefix/[redacted]", "xai-secret-token-456"),
        ("prefix_zai_live_secret_token_987", "prefix_[redacted]", "zai_live_secret_token_987"),
        ("prefix.or-v1-secret-token-789", "prefix.[redacted]", "or-v1-secret-token-789"),
        ("prefix-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.sig123", "prefix-[redacted]", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
    ]
    for raw_val, expected, leaked in embedded_cases:
        cleaned = redact(raw_val)
        assert cleaned == expected
        assert leaked not in cleaned

    # Finding 3: ordinary prose survives while real field values stay redacted
    for prose in (
        "token: file is missing",
        "token: missing",
        "secret: not configured",
        "password: empty",
        "api_key: none",
        "BURNRATE_ENABLE_ZAI_QUOTA",
        "via zai_quota_endpoint",
        "task-12345",
        "heyJoe123456",
    ):
        assert redact(prose) == prose

    assert redact("ERROR token=hunter2") == "ERROR token=[redacted]"
    assert redact("token: 12345678") == "token: [redacted]"
    assert redact('password="abc"') == 'password="[redacted]"'


def test_review_regressions_through_ingest_run_and_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, "review_diag.db")
    initialize(settings.database_path)

    payloads = [
        'login failed password="synthetic correct horse battery staple"',
        '{"password": "synthetic json horse battery", "keep": "yes"}',
        r'{\"secret\":\"synthetic escaped battery staple\",\"ok\":true}',
        "Cookie: sid=alpha; csrf=beta_token",
        "Set-Cookie: sess=gamma_secret; Path=/; HttpOnly",
        "Authorization: Digest username=alice, response=deadbeef00112233",
        "failed connection_sk-proj_syntheticCredentialValue and path/ghp_1234567890abcdef1234",
        "token: file is missing",
    ]
    forbidden = (
        "synthetic",
        "horse",
        "battery",
        "staple",
        "alpha",
        "beta_token",
        "gamma_secret",
        "HttpOnly",
        "deadbeef00112233",
        "syntheticCredentialValue",
        "sk-proj",
        "1234567890abcdef1234",
        "ghp_",
    )

    with connect(settings.database_path) as conn:
        for idx, msg in enumerate(payloads):
            run = IngestRun.start(conn, f"src_{idx}")
            run.finish(status="failed", error=msg)
        stored_rows = [
            row[0]
            for row in conn.execute("SELECT error FROM ingest_runs ORDER BY id").fetchall()
        ]

    joined_db = "\n".join(stored_rows)
    _assert_none_survive(joined_db, forbidden)
    assert "token: file is missing" in stored_rows
    assert 'keep": "yes"' in joined_db

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text("\n".join(payloads) + "\n", encoding="utf-8")

    report = support_report.build_report(settings, log_dir=logs)
    _assert_none_survive(report, forbidden)
    assert "token: file is missing" in report

    monkeypatch.setattr("spend_app.service.log_dir", lambda: logs)
    app = create_app(settings, enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.post("/api/setup/diagnostics", headers={"X-BURNRATE-Request": "1"})
    assert resp.status_code == 200
    diag_text = resp.json()["text"]
    _assert_none_survive(diag_text, forbidden)
    assert "token: file is missing" in diag_text


def test_bounded_batch_migration_redacts_over_500_rows_and_is_idempotent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "batched_legacy.db"
    initialize(db_path)

    dirty_indices = {
        0: 'password="synthetic correct horse battery staple"',
        499: "Cookie: sid=a; csrf=b",
        500: "error connection_sk-proj_syntheticCredentialValue",
        999: "Encoded %73%6B-proj_encoded123 failed",
        1000: r'{\"password\":\"synthetic escaped secret\"}',
        1204: "host/github_pat_11AAAAAA_0123456789abcdef",
    }
    total_rows = 1205
    with connect(db_path) as conn:
        rows = [
            (
                "codex_local",
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:01Z",
                "failed",
                0,
                dirty_indices.get(i, "token: file is missing"),
            )
            for i in range(total_rows)
        ]
        conn.executemany(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written, error) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("DELETE FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,))

    initialize(db_path)

    with connect(db_path) as conn:
        stored = [
            row[0]
            for row in conn.execute("SELECT error FROM ingest_runs ORDER BY id").fetchall()
        ]
        marker = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,)
        ).fetchone()

    assert len(stored) == total_rows
    assert marker is not None
    assert json.loads(marker[0])["updatedRows"] == len(dirty_indices)

    for idx in dirty_indices:
        assert "[redacted]" in stored[idx]
    for leaked in (
        "synthetic",
        "battery",
        "csrf=b",
        "sk-proj",
        "encoded123",
        "0123456789abcdef",
    ):
        assert all(leaked not in row for row in stored)
    assert stored[1] == "token: file is missing"
    assert stored[750] == "token: file is missing"

    # Marker guard makes subsequent calls a no-op
    assert redact_legacy_ingest_errors(db_path) == 0

    # Resume after an interrupted pass (missing marker) is also idempotent
    with connect(db_path) as conn:
        conn.execute("DELETE FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,))
    assert redact_legacy_ingest_errors(db_path) == 0
    with connect(db_path) as conn:
        marker_after = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (INGEST_ERROR_REDACT_KEY,)
        ).fetchone()
    assert marker_after is not None
    assert json.loads(marker_after[0])["updatedRows"] == 0


SHORT_FIELD_REDACT_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("password_eq_short", "password=abc12", ("abc12",)),
    ("password_eq_1char", "password=a", ("=a",)),
    ("password_colon_short", "password: abc12", ("abc12",)),
    ("password_eq_safe_word", "password=missing", ("missing",)),
    ("pwd_eq_short", "pwd=z9", ("z9",)),
    ("token_eq_short", "token=x9", ("x9",)),
    ("token_colon_short", "token: x9", ("x9",)),
    ("token_colon_1char", "token: x", (": x",)),
    ("token_colon_non_phrase_word", "token: file", ("file",)),
    ("token_colon_arbitrary_prose", "token: custom prose value", ("custom",)),
    ("token_colon_safe_word_plus_secret", "token: missing secret123", ("missing",)),
    ("token_colon_safe_underscore_secret", "token: missing_secret123", ("missing_secret123",)),
    ("token_colon_safe_dot_secret", "token: missing.secret123", ("missing.secret123",)),
    ("token_colon_safe_paren_secret", "token: missing)secret123", ("missing)secret123",)),
    ("secret_colon_not_prefix", "secret: not mySecret123", (": not",)),
    ("api_key_eq_short", "api_key=short", ("short",)),
    ("apikey_eq_short", "apikey=short1", ("short1",)),
    ("access_key_eq_short", "access_key=ak12", ("ak12",)),
    ("client_secret_colon_short", "client_secret: cs12", ("cs12",)),
    ("secret_quoted_spaces", 'secret="a b"', ("a b",)),
    ("auth_bearer_short", "Authorization: Bearer x", ("Bearer x",)),
    ("password_colon_short_before_words", "password: abc12 rejected", ("abc12",)),
]

PROSE_KEEP_CASES: list[str] = [
    "token: file is missing",
    "token: file is missing.",
    "ingest failed (token: missing)",
    "token: missing",
    "token: MISSING",
    "secret: not configured",
    "secret: NOT configured",
    "password: empty",
    "password: invalid",
    "api_key: none",
    "pwd: unset",
    "access_key: denied",
    "client_secret: revoked",
    "token: expired",
    "password is required",
    "invalid token",
]


@pytest.mark.parametrize(
    "case_id,raw_input,forbidden",
    SHORT_FIELD_REDACT_CASES,
    ids=[c[0] for c in SHORT_FIELD_REDACT_CASES],
)
def test_short_explicit_secrets_redacted_in_redact_and_ingest_run(
    case_id, raw_input, forbidden, tmp_path: Path
) -> None:
    cleaned = redact(raw_input)
    assert "[redacted]" in cleaned
    _assert_none_survive(cleaned, forbidden)

    db_path = tmp_path / "short.db"
    initialize(db_path)
    with connect(db_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw_input)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "[redacted]" in stored
    _assert_none_survive(stored, forbidden)


@pytest.mark.parametrize("prose", PROSE_KEEP_CASES)
def test_prose_preserved_in_redact_and_ingest_run(prose: str, tmp_path: Path) -> None:
    assert redact(prose) == prose

    settings = _settings(tmp_path, "prose.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=prose)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert stored == prose

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(prose + "\n", encoding="utf-8")
    report_text = support_report.build_report(settings, log_dir=logs)
    assert prose in report_text


SUFFIX_AND_MULTILINE_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("suffix_bracket_password", "token: missing]password=x", ("missing]", "password=x", "=x")),
    (
        "suffix_bracket_secret",
        "password: missing]syntheticSecret123",
        ("missing]", "syntheticSecret123"),
    ),
    (
        "suffix_paren_colon_password",
        "token: missing):password=x",
        ("missing):", "password=x", "=x"),
    ),
    (
        "suffix_bracket_password_pct",
        "token%3A%20missing%5Dpassword%3Dx",
        ("missing]", "password=x", "=x"),
    ),
    (
        "suffix_bracket_secret_pct",
        "password%3A%20missing%5DsyntheticSecret123",
        ("missing]", "syntheticSecret123"),
    ),
    (
        "suffix_paren_colon_password_pct",
        "token%3A%20missing%29%3Apassword%3Dx",
        ("missing):", "password=x", "=x"),
    ),
    (
        "suffix_bracket_space_then_password",
        "token: missing] password=x",
        ("password=x", "=x"),
    ),
    (
        "suffix_file_is_missing_bracket_password",
        "token: file is missing]password=x",
        ("password=x", "=x"),
    ),
    (
        "multiline_json_lf",
        '{"password":\n "Ab9SensitiveValue", "keep": "ok"}',
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_json_crlf",
        '{"password":\r\n  "Ab9SensitiveValue", "keep": "ok"}',
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_json_pct_decoded_lf",
        "%7B%22password%22%3A%0A%20%22Ab9SensitiveValue%22%7D",
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_json_escaped_lf",
        r'{\"password\":' + "\n" + r' \"Ab9SensitiveValue\"}',
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_unquoted_colon_lf",
        "password:\nAb9SensitiveValue",
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_unquoted_eq_crlf",
        "password=\r\nAb9SensitiveValue",
        ("Ab9SensitiveValue",),
    ),
    (
        "multiline_colon_newline_not_exempt_prose",
        "password:\nmissing",
        ("missing",),
    ),
]


@pytest.mark.parametrize(
    "case_id,raw_input,forbidden",
    SUFFIX_AND_MULTILINE_CASES,
    ids=[c[0] for c in SUFFIX_AND_MULTILINE_CASES],
)
def test_suffix_leak_and_multiline_json_regressions(
    case_id, raw_input, forbidden, tmp_path: Path, monkeypatch
) -> None:
    cleaned = redact(raw_input)
    assert "[redacted]" in cleaned
    _assert_none_survive(cleaned, forbidden)

    settings = _settings(tmp_path, "suffix_multiline.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw_input)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "[redacted]" in stored
    _assert_none_survive(stored, forbidden)

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(raw_input + "\n", encoding="utf-8")

    report = support_report.build_report(settings, log_dir=logs)
    assert "[redacted]" in report
    _assert_none_survive(report, forbidden)

    monkeypatch.setattr("spend_app.service.log_dir", lambda: logs)
    app = create_app(settings, enable_scheduler=False)
    with TestClient(app) as client:
        resp = client.post("/api/setup/diagnostics", headers={"X-BURNRATE-Request": "1"})
    assert resp.status_code == 200
    diag_text = resp.json()["text"]
    assert "[redacted]" in diag_text
    _assert_none_survive(diag_text, forbidden)


def test_header_to_eol_stays_bounded_to_line() -> None:
    sample = "Authorization: Bearer secret123\nX-Keep: visible\ntoken: missing\n"
    cleaned = redact(sample)
    assert cleaned == "Authorization: [redacted]\nX-Keep: visible\ntoken: missing\n"


CROSSLINE_SECRET_CASES: list[tuple[str, str, tuple[str, ...]]] = [
    ("auth_eol_then_password", "Authorization:\npassword: Ab9SensitiveValue", ("Ab9SensitiveValue",)),
    ("cookie_eol_then_password", "Cookie:\npassword: Ab9SensitiveValue", ("Ab9SensitiveValue",)),
    ("token_eol_then_password", "token:\npassword: Ab9SensitiveValue", ("Ab9SensitiveValue",)),
    ("token_crlf_then_password", "token:\r\npassword: Ab9SensitiveValue", ("Ab9SensitiveValue",)),
    (
        "token_pct_newline_then_password",
        "token%3A%0Apassword%3A%20Ab9SensitiveValue",
        ("Ab9SensitiveValue",),
    ),
    (
        "json_double_quoted_crossline_keys",
        '{"token":\n"password": "Ab9SensitiveValue"}',
        ("Ab9SensitiveValue",),
    ),
    (
        "json_escaped_crossline_keys",
        r'{\"token\":' + "\n" + r'\"password\": \"Ab9SensitiveValue\"}',
        ("Ab9SensitiveValue",),
    ),
    (
        "single_quoted_crossline_keys",
        "token:\n'password': 'Ab9SensitiveValue'",
        ("Ab9SensitiveValue",),
    ),
    (
        "json_pct_newline_crossline_keys",
        "%7B%22token%22%3A%0A%22password%22%3A%20%22Ab9SensitiveValue%22%7D",
        ("Ab9SensitiveValue",),
    ),
]


@pytest.mark.parametrize(
    "case_id,raw_input,forbidden",
    CROSSLINE_SECRET_CASES,
    ids=[c[0] for c in CROSSLINE_SECRET_CASES],
)
def test_crossline_empty_key_does_not_swallow_next_field(
    case_id, raw_input, forbidden, tmp_path: Path
) -> None:
    cleaned = redact(raw_input)
    assert "[redacted]" in cleaned
    _assert_none_survive(cleaned, forbidden)

    settings = _settings(tmp_path, "crossline.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw_input)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "[redacted]" in stored
    _assert_none_survive(stored, forbidden)

    report = support_report.build_report(settings)
    assert "[redacted]" in report
    _assert_none_survive(report, forbidden)


@pytest.mark.parametrize(
    "raw_input,expected",
    [
        ("Authorization:\nX-Keep: visible", "Authorization:\n[redacted]"),
        ("Cookie:\nX-Keep: visible", "Cookie:\n[redacted]"),
        ("Authorization:\nBearer: x", "Authorization:\n[redacted]"),
    ],
)
def test_empty_header_at_eol_redacts_next_line_with_no_header_exemption(
    raw_input: str, expected: str, tmp_path: Path
) -> None:
    assert redact(raw_input) == expected

    settings = _settings(tmp_path, "empty_hdr.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw_input)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert stored == expected

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(raw_input + "\n", encoding="utf-8")
    report = support_report.build_report(settings, log_dir=logs)
    assert expected in report
    assert "X-Keep: visible" not in report
    assert "Bearer: x" not in report


def test_redact_repeated_parens_is_linear_time(tmp_path: Path) -> None:
    n = 32768
    payload = "token: missing" + (")" * n) + "SyntheticSuffix"

    t0 = time.perf_counter()
    cleaned = redact(payload)
    elapsed = time.perf_counter() - t0

    assert elapsed < 0.5, f"redact() took {elapsed:.3f}s for n={n}"
    assert cleaned == "[line omitted]"
    assert "SyntheticSuffix" not in cleaned

    # Within the 8192-char line cap, safe-phrase suffix verification still runs linearly
    in_cap_n = 8000
    in_cap_payload = "token: missing" + (")" * in_cap_n) + "SyntheticSuffix"
    t1 = time.perf_counter()
    in_cap_cleaned = redact(in_cap_payload)
    in_cap_elapsed = time.perf_counter() - t1

    assert in_cap_elapsed < 0.5, f"redact() took {in_cap_elapsed:.3f}s for n={in_cap_n}"
    assert in_cap_cleaned == "token: [redacted]"
    assert "SyntheticSuffix" not in in_cap_cleaned

    # Also verify bounded work and redaction through IngestRun.finish and build_report
    within_cap_payload = "token: missing" + (")" * 2000) + "SyntheticSuffix"
    settings = _settings(tmp_path, "linear_time.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run_big = IngestRun.start(conn, "codex_local")
        run_big.finish(status="failed", error=payload)
        stored_big = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run_big.id,)
        ).fetchone()[0]
        run_cap = IngestRun.start(conn, "claude_local")
        run_cap.finish(status="failed", error=within_cap_payload)
        stored_cap = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run_cap.id,)
        ).fetchone()[0]

    assert "SyntheticSuffix" not in stored_big
    assert stored_cap == "token: [redacted]"
    assert "SyntheticSuffix" not in stored_cap

    report = support_report.build_report(settings)
    assert "SyntheticSuffix" not in report
    assert "token: [redacted]" in report


@pytest.mark.parametrize(
    "raw_value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("hub_unreachable", "hub_unreachable"),
        ("hub_rejected", "hub_rejected"),
        ("sync_failed", "sync_failed"),
        ("RuntimeError: password=syntheticLegacyPassword123", "sync_failed"),
        ("Pairing changed; retry sync", "sync_failed"),
    ],
)
def test_public_setup_state_normalizes_last_sync_error(raw_value, expected) -> None:
    state = setup_wizard.default_setup_state()
    state["role"] = "spoke"
    state["spokeConfig"]["lastSyncError"] = raw_value
    projected = setup_wizard.public_setup_state(state)
    assert projected["spokeConfig"]["lastSyncError"] == expected


def test_legacy_last_sync_error_scrubbed_on_initialize_and_hidden_everywhere(
    tmp_path: Path,
) -> None:
    legacy_secret_err = "RuntimeError: password=syntheticLegacyPassword123"
    settings = _settings(tmp_path, "legacy_sync_err.db")
    initialize(settings.database_path)

    # Seed a legacy raw lastSyncError into app_setup.v1 while keeping the v1
    # ingest-error redaction marker intact (simulating an upgraded database).
    with connect(settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            **state["spokeConfig"],
            "hubUrl": "https://hub.example",
            "token": "br_m_spoke_secret_token",
            "enabled": True,
            "lastSyncError": legacy_secret_err,
        }
        setup_wizard.write_setup_state(conn, state)

    # Public projection scrubs immediately even before migration runs.
    with connect(settings.database_path) as conn:
        projected_before = setup_wizard.public_setup_state(setup_wizard.read_setup_state(conn))
    assert projected_before["spokeConfig"]["lastSyncError"] == "sync_failed"
    assert "syntheticLegacyPassword123" not in json.dumps(projected_before)

    # Re-initializing rewrites the stored app_setup.v1 value once to "sync_failed".
    initialize(settings.database_path)

    with connect(settings.database_path) as conn:
        raw_setup_row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()[0]
        sync_marker = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (SPOKE_SYNC_ERROR_REDACT_KEY,)
        ).fetchone()
        saved = setup_wizard.read_setup_state(conn)

    assert sync_marker is not None
    assert "syntheticLegacyPassword123" not in raw_setup_row
    assert "RuntimeError" not in raw_setup_row
    assert saved["spokeConfig"]["lastSyncError"] == "sync_failed"

    # Endpoints and diagnostics report never expose the legacy string.
    app = create_app(settings, enable_scheduler=False)
    with TestClient(app) as client:
        setup_resp = client.get("/api/setup/state")
        settings_resp = client.get("/api/onboarding")
    assert setup_resp.status_code == 200
    assert settings_resp.status_code == 200
    assert setup_resp.json()["setup"]["spokeConfig"]["lastSyncError"] == "sync_failed"
    assert settings_resp.json()["setup"]["spokeConfig"]["lastSyncError"] == "sync_failed"
    assert "syntheticLegacyPassword123" not in setup_resp.text
    assert "syntheticLegacyPassword123" not in settings_resp.text

    report_text = support_report.build_report(settings)
    assert "syntheticLegacyPassword123" not in report_text

    # Subsequent initialize() is a no-op once SPOKE_SYNC_ERROR_REDACT_KEY is set.
    with connect(settings.database_path) as conn:
        raw_before_second_init = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()[0]
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        raw_after_second_init = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()[0]
    assert raw_after_second_init == raw_before_second_init


# ---------------------------------------------------------------------------
# 6. Line-bounded fail-closed redesign & cross-line state machine tests
# ---------------------------------------------------------------------------


MULTILINE_SCALAR_CASES: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "password_colon_base64_eq",
        "password:\nAb9SensitiveValue==",
        "password:\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "password_colon_opaque_suffix",
        "password:\nAb9SensitiveValue:opaque",
        "password:\n[redacted]",
        ("Ab9SensitiveValue", "opaque"),
    ),
    (
        "token_eq_base64",
        "token=\nYWJjZGVmZw==",
        "token=\n[redacted]",
        ("YWJjZGVmZw",),
    ),
    (
        "pretty_json_password",
        '{"password":\n "Ab9SensitiveValue", "keep": "ok"}',
        '{"password":\n[redacted]',
        ("Ab9SensitiveValue",),
    ),
    (
        "auth_colon_newline_bearer",
        "Authorization:\nBearer Ab9SensitiveValue",
        "Authorization:\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "auth_colon_newline_basic",
        "Authorization:\nBasic dXNlcjpwYXNzd29yZA==",
        "Authorization:\n[redacted]",
        ("dXNlcjpwYXNzd29yZA==",),
    ),
    (
        "password_colon_crlf_base64_eq",
        "password:\r\nAb9SensitiveValue==",
        "password:\r\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "password_colon_blank_line_then_secret",
        "password:\n\nAb9SensitiveValue==",
        "password:\n\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "pct_password_colon_base64_eq",
        "password%3A%0AAb9SensitiveValue%3D%3D",
        "password:\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "pct_password_colon_opaque_suffix",
        "password%3A%0AAb9SensitiveValue%3Aopaque",
        "password:\n[redacted]",
        ("Ab9SensitiveValue", "opaque"),
    ),
    (
        "pct_token_eq_base64",
        "token%3D%0AYWJjZGVmZw%3D%3D",
        "token=\n[redacted]",
        ("YWJjZGVmZw",),
    ),
    (
        "pct_pretty_json_password",
        "%7B%22password%22%3A%0A%20%22Ab9SensitiveValue%22%2C%20%22keep%22%3A%20%22ok%22%7D",
        '{"password":\n[redacted]',
        ("Ab9SensitiveValue",),
    ),
    (
        "quoted_safe_phrase_plus_secret_next_line",
        'password:\n"token: missing Ab9SensitiveValue"',
        "password:\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "json_key_only_line_colon_next_line",
        '{"password"\n: "Ab9SensitiveValue"}',
        '{"password"\n[redacted]',
        ("Ab9SensitiveValue",),
    ),
    (
        "quoted_key_only_line_colon_next_line",
        '"password"\n: "Ab9SensitiveValue"',
        '"password"\n[redacted]',
        ("Ab9SensitiveValue",),
    ),
    (
        "bare_key_only_line_colon_next_line",
        "password\n: Ab9SensitiveValue",
        "password\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "bare_key_only_line_eq_next_line",
        "password\n= Ab9SensitiveValue",
        "password\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "nbsp_next_line_value",
        "password:\n\u00a0Ab9SensitiveValue",
        "password:\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "nbsp_before_colon",
        "password\u00a0:Ab9SensitiveValue",
        "password\u00a0:[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "bearer_eol_continuation",
        "request rejected: Bearer\nAb9SensitiveValue",
        "request rejected: Bearer\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "basic_crlf_continuation",
        "request rejected: Basic\r\nYWJjZGVmZw==",
        "request rejected: Basic\r\n[redacted]",
        ("YWJjZGVmZw==",),
    ),
    (
        "bearer_lowercase_nbsp_continuation",
        "request rejected: bearer\u00a0\nAb9SensitiveValue",
        "request rejected: bearer\u00a0\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "overlong_line_ending_in_password_colon",
        ("x" * 8200) + " password:\nAb9SensitiveValue",
        "[line omitted]\n[redacted]",
        ("Ab9SensitiveValue",),
    ),
    (
        "overlong_line_ending_in_bearer",
        ("x" * 8200) + " Bearer\nAb9SensitiveValue",
        "[line omitted]\n[redacted]",
        ("Ab9SensitiveValue",),
    ),

]


@pytest.mark.parametrize(
    "case_id,raw_input,expected,forbidden",
    MULTILINE_SCALAR_CASES,
    ids=[c[0] for c in MULTILINE_SCALAR_CASES],
)
def test_crossline_multiline_scalars_across_all_sinks(
    case_id, raw_input, expected, forbidden, tmp_path: Path
) -> None:
    cleaned = redact(raw_input)
    assert cleaned == expected
    _assert_none_survive(cleaned, forbidden)

    sanitized = source_evidence.sanitize_reason(raw_input)
    _assert_none_survive(sanitized, forbidden)
    reason_text = source_evidence.source_reason_text(raw_input)
    _assert_none_survive(reason_text, forbidden)

    settings = _settings(tmp_path, "multiline_scalar.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw_input)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "[redacted]" in stored
    _assert_none_survive(stored, forbidden)

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(raw_input + "\n", encoding="utf-8")
    report = support_report.build_report(settings, log_dir=logs)
    assert "[redacted]" in report
    _assert_none_survive(report, forbidden)


def test_no_raw_pre_truncation_and_url_userinfo_redaction(tmp_path: Path) -> None:
    pct_ghp = ("%2520" * 38) + "ghp_Ab9SensitiveValue"
    cleaned_ghp = redact(pct_ghp, 50)
    assert len(cleaned_ghp) <= 50
    assert "[redacted]" in cleaned_ghp
    _assert_none_survive(cleaned_ghp, ("ghp_", "Ab9SensitiveValue"))

    sanitized_ghp = source_evidence.sanitize_reason(pct_ghp)
    assert "[redacted]" in sanitized_ghp
    _assert_none_survive(sanitized_ghp, ("ghp_", "Ab9SensitiveValue"))

    reason_ghp = source_evidence.source_reason_text(pct_ghp)
    assert reason_ghp is not None and "[redacted]" in reason_ghp
    _assert_none_survive(reason_ghp, ("ghp_", "Ab9SensitiveValue"))

    pct_url = ("%252520" * 567) + "https://alice:Ab9SensitiveValue@example.invalid"
    cleaned_url = redact(pct_url, 1000)
    assert len(cleaned_url) <= 1000
    assert "https://[redacted]@example.invalid" in cleaned_url
    _assert_none_survive(cleaned_url, ("alice", "Ab9SensitiveValue"))

    sanitized_pct_url = source_evidence.sanitize_reason(pct_url)
    _assert_none_survive(sanitized_pct_url, ("alice", "Ab9SensitiveValue"))
    reason_pct_url = source_evidence.source_reason_text(pct_url)
    if reason_pct_url is not None:
        _assert_none_survive(reason_pct_url, ("alice", "Ab9SensitiveValue"))

    direct_url = "https://alice:Ab9SensitiveValue@example.invalid"
    assert source_evidence.sanitize_reason(direct_url) == "https://[redacted]@example.invalid"
    assert source_evidence.source_reason_text(direct_url) == "https://[redacted]@example.invalid"
    padded_url = ("%2520" * 10) + direct_url
    assert source_evidence.sanitize_reason(padded_url) == "https://[redacted]@example.invalid"
    assert source_evidence.source_reason_text(padded_url) == "https://[redacted]@example.invalid"

    settings = _settings(tmp_path, "no_pretrunc.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        r1 = IngestRun.start(conn, "codex_local")
        r1.finish(status="failed", error=pct_ghp)
        e1 = conn.execute("SELECT error FROM ingest_runs WHERE id=?", (r1.id,)).fetchone()[0]
        r2 = IngestRun.start(conn, "claude_local")
        r2.finish(status="failed", error=pct_url)
        e2 = conn.execute("SELECT error FROM ingest_runs WHERE id=?", (r2.id,)).fetchone()[0]

    assert "[redacted]" in e1
    _assert_none_survive(e1, ("ghp_", "Ab9SensitiveValue"))
    assert "https://[redacted]@example.invalid" in e2
    _assert_none_survive(e2, ("alice", "Ab9SensitiveValue"))

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(f"{pct_ghp}\n{pct_url}\n", encoding="utf-8")
    report = support_report.build_report(settings, log_dir=logs)
    _assert_none_survive(report, ("ghp_", "Ab9SensitiveValue", "alice"))


def test_long_secret_line_omitted_and_subsequent_line_kept(tmp_path: Path) -> None:
    huge_secret_line = "password=" + ("Z" * 65518) + " ghp_Ab9SensitiveValue1234"
    assert redact(huge_secret_line) == "[line omitted]"
    assert source_evidence.sanitize_reason(huge_secret_line) == "[line omitted]"
    assert source_evidence.source_reason_text(huge_secret_line) == "[line omitted]"

    keep_after_secret = "token=" + ("Z" * 5000) + "\nConnection refused"
    kept = redact(keep_after_secret, 1000)
    assert kept == "token=[redacted]\nConnection refused"
    assert "Connection refused" in kept
    assert "Z" * 20 not in kept
    assert source_evidence.sanitize_reason(keep_after_secret) == "token=[redacted] Connection refused"
    assert source_evidence.source_reason_text(keep_after_secret) == "token=[redacted] Connection refused"

    settings = _settings(tmp_path, "line_omitted.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        r_huge = IngestRun.start(conn, "codex_local")
        r_huge.finish(status="failed", error=huge_secret_line)
        stored_huge = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (r_huge.id,)
        ).fetchone()[0]
        r_keep = IngestRun.start(conn, "claude_local")
        r_keep.finish(status="failed", error=keep_after_secret)
        stored_keep = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (r_keep.id,)
        ).fetchone()[0]

    assert stored_huge == "[line omitted]"
    _assert_none_survive(stored_huge, ("ghp_", "Ab9SensitiveValue1234", "Z" * 20))
    assert stored_keep == "token=[redacted]\nConnection refused"

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(
        f"{huge_secret_line}\n{keep_after_secret}\n", encoding="utf-8"
    )
    report = support_report.build_report(settings, log_dir=logs)
    assert "[line omitted]" in report
    assert "Connection refused" in report
    _assert_none_survive(report, ("ghp_", "Ab9SensitiveValue1234", "Z" * 20))


def test_line_and_input_caps_fail_closed() -> None:
    assert redact("x" * 8192) == "x" * 8192
    assert redact("x" * 8193) == "[line omitted]"

    # 256 KiB cap drops only whole trailing lines and emits a single [truncated] marker
    line = "line " + ("x" * 8000)
    many_lines = (line + "\n") * 40
    cleaned = redact(many_lines)
    assert cleaned.endswith("\n[truncated]")
    assert cleaned.count("[truncated]") == 1
    kept_lines = cleaned.split("\n")[:-1]
    assert len(kept_lines) == 32
    assert all(item == line for item in kept_lines)


def test_adversarial_long_inputs_finish_linearly() -> None:
    timing_cases = [
        ("dots_65k", "token: missing" + ("." * 65536) + "SyntheticSuffix", "[line omitted]"),
        ("z_16k", "Z" * 16384, "[line omitted]"),
        ("z_65k", "Z" * 65536, "[line omitted]"),
        ("dots_8k", "token: missing" + ("." * 8000) + "SyntheticSuffix", "token: [redacted]"),
    ]
    for label, payload, expected in timing_cases:
        t0 = time.perf_counter()
        cleaned = redact(payload)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"{label} took {elapsed:.3f}s"
        assert cleaned == expected
        assert "SyntheticSuffix" not in cleaned

    # Adversarial in-line-cap inputs with '@' / dots also stay fast
    for label, payload in (
        ("at_8k", ("a" * 8000) + "@"),
        ("email_dots_8k", "a@" + ("b." * 4000)),
        ("many_ats_8k", "a@" * 4000),
    ):
        t0 = time.perf_counter()
        redact(payload)
        elapsed = time.perf_counter() - t0
        assert elapsed < 0.5, f"{label} took {elapsed:.3f}s"

def test_whitespace_only_line_over_limit_preserves_crossline_pending(tmp_path: Path) -> None:
    spaces_line = " " * 8193
    payload = f"password:\n{spaces_line}\nAb9SensitiveValue"
    cleaned = redact(payload)
    assert cleaned == "password:\n\n[redacted]"
    assert "Ab9SensitiveValue" not in cleaned

    assert source_evidence.sanitize_reason(payload) == "password: [redacted]"
    assert source_evidence.source_reason_text(payload) == "password: [redacted]"

    settings = _settings(tmp_path, "blank_over_limit.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=payload)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "Ab9SensitiveValue" not in stored

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(payload + "\n", encoding="utf-8")
    report = support_report.build_report(settings, log_dir=logs)
    assert "Ab9SensitiveValue" not in report
    assert "password:\n" in report

    # Non-blank line over 8192 chars is omitted and clears pending state
    non_blank_omitted = "password:\n" + ("x" * 8193) + "\nAb9SensitiveValue"
    assert redact(non_blank_omitted) == "password:\n[line omitted]\nAb9SensitiveValue"

def test_lone_surrogates_never_raise_and_redact_across_sinks(tmp_path: Path) -> None:
    payload = "password=Ab9" + chr(0xDC80) + "SensitiveValue"
    assert redact(payload) == "password=[redacted]"

    # Lone surrogate outside secret and across 256 KiB byte-accounting boundary
    outside = "prefix " + chr(0xDC80) + " password=Ab9SensitiveValue"
    assert redact(outside) == "prefix " + chr(0xDC80) + " password=[redacted]"

    large_surrogate = ("line " + chr(0xDC80) + ("x" * 8000) + "\n") * 40
    large_cleaned = redact(large_surrogate)
    assert large_cleaned.endswith("\n[truncated]")

    settings = _settings(tmp_path, "surrogate.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=payload)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert stored == "password=[redacted]"

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_bytes(b"password=Ab9\x80SensitiveValue\n")
    report = support_report.build_report(settings, log_dir=logs)
    assert "password=[redacted]" in report
    assert "Ab9" not in report
    assert "SensitiveValue" not in report


_ALL_27_FIELD_NAMES = (
    "api_key",
    "api-key",
    "apikey",
    "access_key",
    "access-key",
    "accesskey",
    "access_token",
    "access-token",
    "accesstoken",
    "refresh_token",
    "refresh-token",
    "refreshtoken",
    "client_secret",
    "client-secret",
    "clientsecret",
    "session_key",
    "session-key",
    "sessionkey",
    "x-api-key",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "authorization",
    "set-cookie",
    "cookie",
)


def _pct_layers(text_value: str, layers: int) -> str:
    out = text_value
    for _ in range(layers):
        out = quote(out, safe="")
    return out


def test_escaped_single_quote_and_unified_key_grammar_matrix(tmp_path: Path) -> None:
    # Full 648-variant escaped-single-quote empty-key matrix:
    # 27 key spellings * 2 separators (:, =) * 3 line endings (LF, CRLF, CR) * 4 percent-encoding layers (0..3)
    for key in _ALL_27_FIELD_NAMES:
        escaped_sq = f"\\'{key}\\'"
        for sep in (":", "="):
            for nl in ("\n", "\r\n", "\r"):
                for layers in range(4):
                    raw = _pct_layers(f"{escaped_sq}{sep}{nl}Ab9SensitiveValue", layers)
                    cleaned = redact(raw)
                    assert "[redacted]" in cleaned, (key, sep, repr(nl), layers, cleaned)
                    assert "Ab9SensitiveValue" not in cleaned, (key, sep, repr(nl), layers, cleaned)

    # All 5 key forms (bare, double, single, escaped-double, escaped-single) stay aligned
    # across same-line quoted/unquoted, empty-key pending, and key-only pending.
    for key in _ALL_27_FIELD_NAMES:
        for key_form in (key, f'"{key}"', f"'{key}'", f'\\"{key}\\"', f"\\'{key}\\'"):
            for sample in (
                f"{key_form}:Ab9SensitiveValue",
                f"{key_form}=Ab9SensitiveValue",
                f'{key_form}: "Ab9SensitiveValue"',
                f"{key_form}: 'Ab9SensitiveValue'",
                f'{key_form}: \\"Ab9SensitiveValue\\"',
                f"{key_form}: \\'Ab9SensitiveValue\\'",
                f"{key_form}:\nAb9SensitiveValue",
                f"{key_form}\n:\nAb9SensitiveValue",
                f"{key_form}\n=\nAb9SensitiveValue",
            ):
                cleaned = redact(sample)
                assert "[redacted]" in cleaned, (sample, cleaned)
                assert "Ab9SensitiveValue" not in cleaned, (sample, cleaned)

    # Unclosed quotes ending in backslash also scrub without leaking
    for sample in (
        'password="Ab9SensitiveValue\\',
        "password='Ab9SensitiveValue\\",
        r'password=\"Ab9SensitiveValue' + "\\",
        r"password=\'Ab9SensitiveValue" + "\\",
    ):
        cleaned = redact(sample)
        assert "[redacted]" in cleaned, (sample, cleaned)
        assert "Ab9SensitiveValue" not in cleaned, (sample, cleaned)

    # Verify representative escaped-single-quote variants across LF/CRLF/CR & 0..3 layers
    # through redact, IngestRun.finish, and support_report.build_report.
    settings = _settings(tmp_path, "escaped_sq_sinks.db")
    initialize(settings.database_path)
    sink_cases = [
        ("same_line_escaped_sq", r"{\'password\': \'Ab9SensitiveValue\', \'keep\': \'ok\'}"),
        ("lf_layer0", r"\'password\':" + "\nAb9SensitiveValue"),
        ("crlf_layer1", _pct_layers(r"\'token\':" + "\r\nAb9SensitiveValue", 1)),
        ("cr_layer2", _pct_layers(r"\'api_key\':" + "\rAb9SensitiveValue", 2)),
        ("lf_layer3", _pct_layers(r"\'authorization\':" + "\nAb9SensitiveValue", 3)),
    ]
    with connect(settings.database_path) as conn:
        for label, payload in sink_cases:
            assert "Ab9SensitiveValue" not in redact(payload), label
            run = IngestRun.start(conn, label)
            run.finish(status="failed", error=payload)
            stored = conn.execute(
                "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
            ).fetchone()[0]
            assert "[redacted]" in stored, (label, stored)
            assert "Ab9SensitiveValue" not in stored, (label, stored)

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(
        "\n".join(payload for _, payload in sink_cases) + "\n",
        encoding="utf-8",
    )
    report = support_report.build_report(settings, log_dir=logs)
    assert "Ab9SensitiveValue" not in report
    assert "[redacted]" in report


THREE_LINE_SPLIT_TEMPLATES: list[tuple[str, str]] = [
    ("json_double_quoted_3line", '"password"{nl}:{nl}"Ab9SensitiveValue"'),
    ("json_obj_double_quoted_3line", '{{"password"{nl}:{nl}"Ab9SensitiveValue"}}'),
    ("json_escaped_sq_3line", r"\'password\'{nl}:{nl}\'Ab9SensitiveValue\'"),
    ("bare_eq_3line", "password{nl}={nl}Ab9SensitiveValue"),
    ("bare_colon_3line_trailing_comma", "password{nl}: ,{nl}Ab9SensitiveValue"),
    ("bare_eq_3line_trailing_brace", "password{nl}=}}{nl}Ab9SensitiveValue"),
    ("json_double_quoted_with_blank_lines", '"password"{nl}{nl}:{nl}  {nl}"Ab9SensitiveValue"'),
    ("bare_eq_with_blank_lines", "password{nl}   {nl}={nl}{nl}Ab9SensitiveValue"),
]


@pytest.mark.parametrize("case_id,template", THREE_LINE_SPLIT_TEMPLATES)
@pytest.mark.parametrize("nl", ["\n", "\r\n", "\r"], ids=["LF", "CRLF", "CR"])
@pytest.mark.parametrize("layers", [0, 1, 2, 3], ids=["pct0", "pct1", "pct2", "pct3"])
def test_three_line_key_separator_value_splits_across_sinks(
    case_id: str, template: str, nl: str, layers: int, tmp_path: Path
) -> None:
    raw = _pct_layers(template.format(nl=nl), layers)
    cleaned = redact(raw)
    assert "[redacted]" in cleaned, (case_id, repr(nl), layers, cleaned)
    assert "Ab9SensitiveValue" not in cleaned, (case_id, repr(nl), layers, cleaned)

    settings = _settings(tmp_path, "three_line.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=raw)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert "[redacted]" in stored
    assert "Ab9SensitiveValue" not in stored

    logs = tmp_path / "logs"
    logs.mkdir()
    # Write raw UTF-8 bytes so CRLF/CR line endings are preserved on disk as well
    (logs / "dashboard.log").write_bytes((raw + "\n").encode("utf-8"))
    report = support_report.build_report(settings, log_dir=logs)
    assert "[redacted]" in report
    assert "Ab9SensitiveValue" not in report


def test_blank_line_byte_budget_and_collapse(tmp_path: Path) -> None:
    huge_spaces = " " * 300000
    cleaned_spaces = redact(huge_spaces)
    assert len(cleaned_spaces) <= 256 * 1024
    assert cleaned_spaces == ""

    # Multiple blank lines within 8192 chars count toward the 256 KiB budget
    many_blank_lines = ((" " * 8000) + "\n") * 40 + "Ab9SensitiveValue"
    cleaned_many_blanks = redact(many_blank_lines)
    assert len(cleaned_many_blanks.encode("utf-8")) <= 256 * 1024
    assert cleaned_many_blanks.endswith("\n[truncated]")
    assert "Ab9SensitiveValue" not in cleaned_many_blanks

    # Blank line > 8192 chars collapses to empty line without clearing pending
    payload = "password:\n" + (" " * 9000) + "\nAb9SensitiveValue"
    assert redact(payload) == "password:\n\n[redacted]"

    settings = _settings(tmp_path, "blank_budget.db")
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        run = IngestRun.start(conn, "codex_local")
        run.finish(status="failed", error=payload)
        stored = conn.execute(
            "SELECT error FROM ingest_runs WHERE id=?", (run.id,)
        ).fetchone()[0]
    assert stored == "password:\n\n[redacted]"

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text(payload + "\n", encoding="utf-8")
    report = support_report.build_report(settings, log_dir=logs)
    assert "Ab9SensitiveValue" not in report
    assert "password:\n\n[redacted]" in report


def test_regex_pathological_inputs_and_reviewer_timing_guards() -> None:
    patterns = [
        (name, obj)
        for name, obj in vars(redact_module).items()
        if isinstance(obj, re.Pattern)
    ]
    assert len(patterns) >= 13

    # Reviewer exact regression cases
    t0 = time.perf_counter()
    escaped_dq_unclosed = redact('password=\\"' + ("Z" * 28) + '"')
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"Reviewer escaped-quote case took {dt:.3f}s"
    assert ("Z" * 28) not in escaped_dq_unclosed
    assert "[redacted]" in escaped_dq_unclosed

    t0 = time.perf_counter()
    long_sep_line = redact("password\n:" + (" " * 24000) + "x")
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"Reviewer separator-only case took {dt:.3f}s"
    assert long_sep_line == "password\n[line omitted]"

    t0 = time.perf_counter()
    long_blank_after_sep = redact("password\n:\n" + (" " * 24000) + "\nAb9SensitiveValue")
    dt = time.perf_counter() - t0
    assert dt < 0.5, f"Long blank after separator took {dt:.3f}s"
    assert long_blank_after_sep == "password\n:\n\n[redacted]"

    def _payloads(run: str) -> tuple[str, ...]:
        return (
            run,
            "password=" + run,
            "password:" + run,
            'password="' + run + "'",
            "password='" + run + '"',
            'password=\\"' + run + '"',
            "password=\\'" + run + "'",
            'password=\\"' + run + "'",
            "password=\\'" + run + '"',
            "password\n:" + run + "x",
            "a@" + run,
        )

    specials = ("\\", '"', "'", ":", "=", ",", "}", ")", ".", "@", " ", "Z")
    for ch in specials:
        run_30k = ch * 30_000
        run_8k = ch * 8_000
        payloads_30k = _payloads(run_30k)

        for name, pattern in patterns:
            for payload in payloads_30k:
                t0 = time.perf_counter()
                pattern.search(payload)
                dt = time.perf_counter() - t0
                assert dt < 0.5, f"{name} took {dt:.3f}s on ch={ch!r} sample={payload[:32]!r}"

        for payload in payloads_30k + _payloads(run_8k):
            t0 = time.perf_counter()
            redact(payload)
            dt = time.perf_counter() - t0
            assert dt < 0.5, f"redact() took {dt:.3f}s on len={len(payload)} ch={ch!r} sample={payload[:32]!r}"
