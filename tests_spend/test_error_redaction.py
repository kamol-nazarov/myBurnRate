"""Comprehensive regression coverage for error/evidence redaction and sync error codes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
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
    connect,
    initialize,
    redact_legacy_ingest_errors,
)
from spend_app.ingest import IngestRun
from spend_app.pricing import PricingEngine
from spend_app.providers import ProviderSpec
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

    # Length cap applies AFTER redaction so partial secrets cannot leak near the cutoff.
    padded = ("x" * 95) + token + " trailing"
    capped = redact(padded, 105)
    assert len(capped) <= 105
    _assert_none_survive(capped, forbidden)


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
