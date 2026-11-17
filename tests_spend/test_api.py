import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app import __version__
from spend_app.api import GZIP_MINIMUM_SIZE, create_app
import spend_app.api as api_module
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def make_settings(database: Path) -> Settings:
    return Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def add_fixture_event(database: Path) -> None:
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    occurred = datetime.now(UTC)
    computed = pricing.compute(
        model_key="gpt-5.6-sol",
        occurred_at=occurred,
        input_tokens=1000,
        cached_input_tokens=800,
        cache_write_tokens=0,
        output_tokens=100,
    )
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                occurred_at=occurred.isoformat().replace("+00:00", "Z"),
                session_id="api-session",
                project="api-project",
                input_tokens=1000,
                cached_input_tokens=800,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=100,
                reasoning_tokens=20,
                cost_usd=None,
                computed_cost_usd=float(computed),
                raw_id="api-event",
                ingested_at=occurred.isoformat().replace("+00:00", "Z"),
                is_exact=True,
            ),
        )


def test_api_contracts_and_generated_at(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    summary = client.get("/api/spend/summary")
    assert summary.status_code == 200
    body = summary.json()
    assert body["generatedAt"]
    assert body["window"]["from"]
    assert body["window"]["to"]
    assert body["window"]["buckets"] == 24
    assert body["window"]["key"] == "1d"
    assert "nav" not in body
    assert "total" not in body
    assert "navigation" in body
    assert "burnRatePerDay" in body["navigation"]
    assert "todayUsd" in body["navigation"]
    assert "capacity" in body
    assert "activity" in body
    assert "perDay" in body["waste"]
    assert body["totals"]["trackedValue"] is not None
    nav = client.get("/api/spend/nav")
    assert nav.status_code == 200
    assert set(("todayUsd", "burnRatePerDay", "lastRefreshAt", "cadenceSeconds", "cadenceMinutes", "status")) <= set(nav.json())
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    )
    assert entity.status_code == 200
    payload = entity.json()
    assert payload["sessions"]["shownTotal"] is not None
    assert payload["sessions"]["rows"][0]["id"] == "api-session"
    assert "opportunity" in payload
    health = client.get("/api/spend/health")
    assert health.status_code == 200
    assert "pricingGaps" in health.json()
    assert "quotas" in health.json()


def test_api_summary_json_over_threshold_is_gzip_compressed(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    frozen = datetime.now(UTC)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False, now=frozen))

    raw = client.get("/api/spend/summary?window=1d&tool=all", headers={"Accept-Encoding": "identity"})
    assert raw.status_code == 200
    assert raw.headers.get("content-encoding") is None
    assert len(raw.content) >= GZIP_MINIMUM_SIZE

    compressed = client.get("/api/spend/summary?window=1d&tool=all", headers={"Accept-Encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers.get("content-encoding") == "gzip"
    assert compressed.content == raw.content
    assert compressed.json() == raw.json()
    assert compressed.num_bytes_downloaded < len(raw.content)


def test_summary_ignores_removed_hud_referer_compat(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    for referer in ("http://127.0.0.1:17331/hud", "http://127.0.0.1:17331/?hud=1"):
        res = client.get("/api/spend/summary", headers={"Referer": referer})
        assert res.status_code == 200
        body = res.json()
        assert body["window"]["key"] == "1d"
        assert "series" in body and "totals" in body


def test_subscriptions_value_and_attention_api_roundtrip(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))

    value_res = client.get("/api/subscriptions/value", params={"period": "this_month"})
    assert value_res.status_code == 200
    assert value_res.headers.get("cache-control") == "no-store"
    value_body = value_res.json()
    assert value_body["period"] == "this_month"
    assert "groups" in value_body and "unassigned" in value_body

    bad_period = client.get("/api/subscriptions/value", params={"period": "bad"})
    assert bad_period.status_code == 422

    attention_res = client.get("/api/attention")
    assert attention_res.status_code == 200
    assert "current" in attention_res.json()


def test_api_rejects_unknown_window(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/summary", params={"window": "24h"})
    assert response.status_code == 400


def test_api_accepts_requested_windows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    for window in ("15m", "30m", "1h", "3h", "6h", "12h", "1d", "1w", "1mo", "mtd", "ytd", "all", "7d", "30d"):
        response = client.get("/api/spend/summary", params={"window": window})
        assert response.status_code == 200, window


def test_summary_does_not_call_collect_limits(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)

    def boom():
        raise AssertionError("collect_limits must not run for summary")

    monkeypatch.setattr(api_module, "snapshot_limits", boom)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/summary")
    assert response.status_code == 200
    assert "capacity" in response.json()


def test_api_frozen_payload_keys_and_injected_now(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    frozen = datetime(2026, 8, 30, 23, tzinfo=UTC)
    with connect(database) as connection:
        before = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    client = TestClient(create_app(make_settings(database), enable_scheduler=False, now=frozen))
    summary = client.get("/api/spend/summary").json()
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    health = client.get("/api/spend/health").json()
    for key in (
        "window",
        "generatedAt",
        "cadenceSeconds",
        "cadenceMinutes",
        "status",
        "navigation",
        "coverage",
        "totals",
        "capacity",
        "activity",
        "waste",
        "cacheSavings",
        "projected",
        "mix",
        "series",
        "tools",
        "models",
        "subscriptions",
        "heatmap",
    ):
        assert key in summary
    for key in (
        "trackedValue",
        "priced",
        "publishedRate",
        "tokens",
        "records",
        "sessions",
        "meanSessionValue",
        "cacheReusePct",
        "effectiveCostPerMillionTokens",
        "knownEffectiveCostPerMillionTokens",
        "effectiveCostPricedTokens",
        "effectiveCostCoveragePct",
        "effectiveCostComplete",
    ):
        assert key in summary["totals"]
    assert "subscriptionUsd" in summary["totals"]
    for key in (
        "generatedAt",
        "name",
        "kind",
        "value",
        "shareOfTrackedValue",
        "tokens",
        "mix",
        "opportunity",
        "providerLimits",
        "sessions",
    ):
        assert key in entity
    for key in ("ingest", "quotas", "pricingGaps", "providerVsComputedVariancePct", "generatedAt"):
        assert key in health
    assert {"nav", "total", "knownSpend", "referenceCost"}.isdisjoint(summary)
    assert summary["generatedAt"].startswith("2026-08-30T23:00:00")
    assert entity["generatedAt"].startswith("2026-08-30T23:00:00")
    assert health["generatedAt"].startswith("2026-08-30T23:00:00")
    with connect(database) as connection:
        after = connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0]
    assert after == before


def test_api_window_bucket_counts_are_frozen(tmp_path: Path) -> None:
    from spend_app.aggregate import WINDOW_SPECS

    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = TestClient(
        create_app(
            make_settings(database),
            enable_scheduler=False,
            now=datetime(2026, 8, 30, 23, tzinfo=UTC),
        )
    )
    expected = {
        "15m": 15,
        "30m": 15,
        "1h": 20,
        "3h": 18,
        "6h": 24,
        "12h": 24,
        "1d": 24,
        "1w": 28,
        "1mo": 30,
        "mtd": 28,
        "ytd": 32,
        "all": 32,
    }
    for window, buckets in expected.items():
        payload = client.get("/api/spend/summary", params={"window": window}).json()
        assert payload["window"]["buckets"] == buckets == WINDOW_SPECS[window][1]
        assert len(payload["series"]) == buckets


def test_limits_endpoint_contract(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    monkeypatch.setattr(
        api_module,
        "snapshot_limits",
        lambda _database_path=None: {
            "generatedAt": "2026-08-31T12:00:00Z",
            "providers": [
                {
                    "key": "codex",
                    "name": "Codex",
                    "status": "exact",
                    "windows": [{"key": "primary", "usedPct": 11}],
                }
            ],
        },
    )
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/api/spend/limits")
    assert response.status_code == 200
    assert response.json()["providers"][0]["windows"][0]["usedPct"] == 11


def test_api_title_is_burnrate(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    app = create_app(make_settings(database), enable_scheduler=False)
    assert app.title == "BURNRATE"
    assert app.version == __version__
    client = TestClient(app)
    payload = client.get("/api/spend").json()
    assert payload["service"] == "BURNRATE"
    assert payload["version"] == __version__


def _assert_api_only_csp(csp: str) -> None:
    directives = {
        part.strip().split()[0]: part.strip()
        for part in csp.split(";")
        if part.strip()
    }
    assert directives["default-src"] == "default-src 'none'"
    assert directives["base-uri"] == "base-uri 'none'"
    assert directives["form-action"] == "form-action 'none'"
    assert directives["object-src"] == "object-src 'none'"
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"
    assert "'unsafe-inline'" not in csp
    assert "fonts.googleapis.com" not in csp
    assert "fonts.gstatic.com" not in csp


def test_api_security_headers_include_csp(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))
    response = client.get("/healthz")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    _assert_api_only_csp(response.headers["Content-Security-Policy"])


def test_api_boundary_error_response_includes_hardened_csp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("BURNRATE_TEST_HOSTS", "0")
    database = tmp_path / "spend.db"
    initialize(database)
    app = create_app(make_settings(database), enable_scheduler=False)
    with TestClient(app, base_url="http://attacker.invalid:17331") as client:
        response = client.get("/healthz")
    assert response.status_code == 403
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    _assert_api_only_csp(response.headers["Content-Security-Policy"])


def test_api_csp_variate_dev_gating(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "spend_app").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".variate").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".variate" / "server.json").write_text(
        json.dumps({"port": 43210}), encoding="utf-8"
    )
    monkeypatch.setattr(api_module, "__file__", str(tmp_path / "spend_app" / "api.py"))

    database = tmp_path / "spend.db"
    initialize(database)
    client = TestClient(create_app(make_settings(database), enable_scheduler=False))

    for dev_val in (None, "0", "1"):
        if dev_val is None:
            monkeypatch.delenv("BURNRATE_DEV", raising=False)
        else:
            monkeypatch.setenv("BURNRATE_DEV", dev_val)
        csp = client.get("/healthz").headers["Content-Security-Policy"]
        _assert_api_only_csp(csp)
        assert "http://127.0.0.1:43210" not in csp
        assert "script-src" not in csp
        assert "connect-src" not in csp


def _iter_routes(routes, prefix: str = ""):
    for route in routes:
        if hasattr(route, "original_router"):
            ctx_prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            yield from _iter_routes(route.original_router.routes, prefix + ctx_prefix)
        else:
            yield prefix + (getattr(route, "path", "") or ""), route


def test_no_html_routes_registered_in_local_or_hub_mode(tmp_path: Path, monkeypatch) -> None:
    from fastapi.responses import HTMLResponse
    from starlette.responses import HTMLResponse as StarletteHTMLResponse

    for central in ("0", "1"):
        monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", central)
        if central == "1":
            monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
        else:
            monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
        database = tmp_path / f"routes_{central}.db"
        initialize(database)
        app = create_app(make_settings(database), enable_scheduler=False)
        discovered = list(_iter_routes(app.routes))
        assert discovered, "expected registered routes"
        paths = [path for path, _ in discovered]
        assert "/healthz" in paths
        assert "/api/spend" in paths
        for page in ("/", "/hud", "/setup", "/auth", "/connections", "/spend.css", "/spend.js"):
            assert page not in paths, (central, page)
        for path, route in discovered:
            assert path == "/healthz" or path.startswith("/api/"), (central, path)
            response_class = getattr(route, "response_class", None)
            actual = getattr(response_class, "value", response_class)
            assert actual not in (HTMLResponse, StarletteHTMLResponse), (central, path, actual)
            assert getattr(actual, "media_type", None) != "text/html", (central, path, actual)
            methods = set(getattr(route, "methods", None) or ())
            if "GET" in methods:
                assert path == "/healthz" or path.startswith("/api/"), (central, path)


def test_removed_web_pages_return_req_404_json_in_local_and_hub_modes(
    tmp_path: Path, monkeypatch
) -> None:
    removed_paths = (
        "/",
        "/hud",
        "/setup",
        "/auth",
        "/connections",
        "/spend.css",
        "/spend.js",
        "/request-state.js",
        "/dialogs.js",
        "/product.js",
        "/favicon.svg",
        "/logos.svg",
        "/favicon.ico",
        "/nonexistent-page",
    )
    for central in ("0", "1"):
        monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", central)
        if central == "1":
            monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
            base_url = "http://hub.test"
        else:
            monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
            base_url = "http://127.0.0.1:17331"
        database = tmp_path / f"pages404_{central}.db"
        initialize(database)
        app = create_app(make_settings(database), enable_scheduler=False)
        with TestClient(app, base_url=base_url) as client:
            for path in removed_paths:
                res = client.get(path, follow_redirects=False)
                assert res.status_code == 404, (central, path, res.status_code)
                assert res.headers["content-type"].startswith("application/json"), (central, path)
                assert res.headers["X-Content-Type-Options"] == "nosniff"
                assert res.headers["Referrer-Policy"] == "no-referrer"
                assert res.headers["X-Frame-Options"] == "DENY"
                _assert_api_only_csp(res.headers["Content-Security-Policy"])
                body = res.json()
                assert body["error"]["code"] == "REQ-404"
                assert isinstance(body["error"].get("message"), str)
                assert body["error"]["message"].strip()
                assert "desktop app" in body["error"]["message"].lower()
