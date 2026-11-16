from __future__ import annotations

import ast
import json
import struct
from pathlib import Path

import httpx
import pytest

from spend_app.limits import _antigravity_limits_uncached, _antigravity_loopback_client
from spend_app.quotas import antigravity_quota_samples


PAYLOAD = {
    "response": {
        "groups": [
            {
                "displayName": "Gemini Models",
                "buckets": [
                    {
                        "bucketId": "gemini-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remainingFraction": 0.88,
                        "resetTime": "2026-09-09T17:30:10Z",
                    },
                    {
                        "bucketId": "gemini-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remainingFraction": 0.35,
                        "resetTime": "2026-09-02T22:30:10Z",
                    },
                ],
            },
            {
                "displayName": "Claude and GPT models",
                "buckets": [
                    {
                        "bucketId": "3p-weekly",
                        "displayName": "Weekly Limit Remaining",
                        "remainingFraction": 1,
                        "resetTime": "2026-09-09T18:55:04Z",
                    },
                    {
                        "bucketId": "3p-5h",
                        "displayName": "Five Hour Limit Remaining",
                        "remainingFraction": 1,
                        "resetTime": "2026-09-02T23:55:04Z",
                    },
                ],
            },
        ]
    }
}


def _mock_loopback_client(monkeypatch, framed: bytes, check_post) -> None:
    def fake_client(**client_kwargs):
        assert client_kwargs["verify"] is False
        assert client_kwargs["trust_env"] is False
        assert client_kwargs["follow_redirects"] is False

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, *, content, headers, **kwargs):
                check_post(url, content=content, headers=headers, **kwargs)
                request = httpx.Request("POST", url)
                return httpx.Response(200, request=request, content=framed)

        return FakeClient()

    monkeypatch.setattr("spend_app.limits.httpx.Client", fake_client)


def test_antigravity_local_rpc_reads_grouped_quota_without_exposing_token(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "Antigravity" / "logs" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "Spawning: language_server.exe --csrf_token fixture-local-secret\n"
        "Local: https://127.0.0.1:64566/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.app_support_dir", lambda: tmp_path)
    encoded = json.dumps(PAYLOAD).encode()
    framed = b"\x00" + struct.pack(">I", len(encoded)) + encoded

    def check_post(url, *, content, headers, **kwargs):
        assert url.endswith("/RetrieveUserQuotaSummary")
        assert content == b"\x00\x00\x00\x00\x02{}"
        assert headers["x-codeium-csrf-token"] == "fixture-local-secret"

    _mock_loopback_client(monkeypatch, framed, check_post)

    def unexpected_process_lookup():
        raise AssertionError("process command line is only for a startup that omitted the token")

    monkeypatch.setattr("spend_app.limits._antigravity_process_csrf_tokens", unexpected_process_lookup)
    result = _antigravity_limits_uncached()
    assert result["status"] == "exact"
    assert result["key"] == "antigravity"
    assert {row["key"] for row in result["windows"]} == {
        "gemini-weekly",
        "gemini-5h",
        "3p-weekly",
        "3p-5h",
    }
    assert "fixture-local-secret" not in json.dumps(result)


def test_antigravity_uses_the_live_process_token_when_the_current_startup_omits_it(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "Antigravity" / "logs" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        "Starting app (v2.11.0)\n"
        "Spawning: language_server.exe --csrf_token stale-log-secret\n"
        "Local: https://127.0.0.1:1111/\n"
        "Starting app (v2.19.1)\n"
        "Local: https://127.0.0.1:62491/\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("spend_app.limits.app_support_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "spend_app.limits._antigravity_process_csrf_tokens",
        lambda: ["live-process-secret"],
    )
    encoded = json.dumps(PAYLOAD).encode()
    framed = b"\x00" + struct.pack(">I", len(encoded)) + encoded

    def check_post(url, *, content, headers, **kwargs):
        assert url.endswith("/RetrieveUserQuotaSummary")
        assert "127.0.0.1:62491/" in url
        assert headers["x-codeium-csrf-token"] == "live-process-secret"

    _mock_loopback_client(monkeypatch, framed, check_post)
    result = _antigravity_limits_uncached()
    assert result["status"] == "exact"
    assert "live-process-secret" not in json.dumps(result)
    assert "stale-log-secret" not in json.dumps(result)


def test_antigravity_quota_samples_store_used_percent_and_real_resets() -> None:
    payload = {
        "status": "exact",
        "windows": [
            {
                "key": bucket["bucketId"],
                "usedPct": (1 - bucket["remainingFraction"]) * 100,
                "resetAt": bucket["resetTime"],
            }
            for group in PAYLOAD["response"]["groups"]
            for bucket in group["buckets"]
        ],
    }
    rows = antigravity_quota_samples(payload, source="antigravity_local_rpc")
    by_key = {row.limit_key: row for row in rows}
    assert by_key["gemini-weekly"].pct == 12
    assert by_key["gemini-5h"].pct == 65
    assert by_key["3p-weekly"].pct == 0
    assert by_key["3p-5h"].pct == 0
    assert by_key["gemini-weekly"].resets_at == "2026-09-09T17:30:10Z"
    assert all(row.source == "antigravity_local_rpc" for row in rows)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1234",
        "https://localhost:1234",
        "https://127.0.0.2:1234",
        "https://[::1]:1234",
        "https://example.com:1234",
        "https://127.0.0.1.evil.example:1234",
    ],
)
def test_antigravity_loopback_client_rejects_non_loopback_or_plaintext_urls(url: str) -> None:
    with pytest.raises(RuntimeError):
        _antigravity_loopback_client(url)


def test_antigravity_loopback_client_configures_isolated_client(monkeypatch) -> None:
    captured: dict = {}
    sentinel = object()

    def fake_client(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("spend_app.limits.httpx.Client", fake_client)
    assert _antigravity_loopback_client("https://127.0.0.1:1234", timeout=15) is sentinel
    assert captured == {
        "verify": False,
        "timeout": 15,
        "trust_env": False,
        "follow_redirects": False,
    }


def test_verify_false_is_restricted_to_antigravity_loopback_client() -> None:
    spend_app_dir = Path(__file__).resolve().parents[1] / "spend_app"
    found: list[tuple[str, str | None, int]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, rel_path: str) -> None:
            self.rel_path = rel_path
            self.fn_stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.fn_stack.append(node.name)
            self.generic_visit(node)
            self.fn_stack.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.fn_stack.append(node.name)
            self.generic_visit(node)
            self.fn_stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            for kw in node.keywords:
                if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    found.append((self.rel_path, self.fn_stack[-1] if self.fn_stack else None, node.lineno))
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:
            for key, val in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "verify"
                    and isinstance(val, ast.Constant)
                    and val.value is False
                ):
                    found.append((self.rel_path, self.fn_stack[-1] if self.fn_stack else None, node.lineno))
            self.generic_visit(node)

    for py_file in sorted(spend_app_dir.rglob("*.py")):
        rel = py_file.relative_to(spend_app_dir).as_posix()
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        Visitor(rel).visit(tree)

    assert found, "Expected verify=False in _antigravity_loopback_client"
    disallowed = [
        entry for entry in found if (entry[0], entry[1]) != ("limits.py", "_antigravity_loopback_client")
    ]
    assert not disallowed, f"Disallowed verify=False occurrences: {disallowed}"
