"""Security and validation tests for provider cache dashboard routes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

import fastmcp_server
from src.services.provider_cache_maintenance import (
    ProviderCacheMaintenanceBusy,
    ProviderCacheMaintenanceTimeout,
)


def _request(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    host: str = "127.0.0.1:8765",
    origin: str | None = None,
    ui_header: str | None = "1",
    content_type: str | None = None,
    client: str = "127.0.0.1",
    content_length: str | None = "auto",
    query_string: bytes = b"",
    transfer_encoding: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"host", host.encode("ascii"))]
    if origin is not None:
        headers.append((b"origin", origin.encode("ascii")))
    if ui_header is not None:
        headers.append((b"x-fantasy-draft-ui", ui_header.encode("ascii")))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode("ascii")))
    if content_length == "auto":
        headers.append((b"content-length", str(len(body)).encode("ascii")))
    elif content_length is not None:
        headers.append((b"content-length", content_length.encode("ascii")))
    if transfer_encoding is not None:
        headers.append((b"transfer-encoding", transfer_encoding.encode("ascii")))
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query_string,
            "headers": headers,
            "client": (client, 12345),
            "server": ("127.0.0.1", 8765),
        },
        receive,
    )


def _json(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


SAFE_STATS = {
    "schemaVersion": 1,
    "status": "success",
    "cache": {
        "status": "available",
        "sizeBytes": 4096,
        "snapshotCount": 0,
        "recordCount": 0,
        "latestFetchedAt": None,
        "snapshots": [],
    },
    "fantasyProsBudget": {
        "status": "missing",
        "utcDate": "2026-09-03",
        "used": 0,
        "remaining": 95,
        "limit": 95,
    },
}


@pytest.mark.asyncio
async def test_stats_route_is_loopback_host_and_ui_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fastmcp_server, "get_provider_cache_stats", lambda: SAFE_STATS)

    response = await fastmcp_server.receive_provider_cache_stats(
        _request("GET", "/provider-cache/stats")
    )

    assert response.status_code == 200
    assert _json(response) == SAFE_STATS
    assert response.headers["cache-control"] == "no-store"

    for request in (
        _request("GET", "/provider-cache/stats", client="192.168.1.5"),
        _request("GET", "/provider-cache/stats", host="example.com"),
        _request("GET", "/provider-cache/stats", ui_header=None),
        _request(
            "GET",
            "/provider-cache/stats",
            origin="https://attacker.invalid",
        ),
        _request(
            "GET",
            "/provider-cache/stats",
            origin="moz-extension://private-extension",
        ),
    ):
        rejected = await fastmcp_server.receive_provider_cache_stats(request)
        assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_provider_cache_dashboard_asset_is_loopback_only_and_no_store() -> None:
    response = await fastmcp_server.serve_provider_cache_client(
        _request("GET", "/draft-dashboard/provider-cache-client.js")
    )
    assert response.status_code == 200
    assert response.media_type == "text/javascript"
    assert response.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in response.headers["content-security-policy"]

    rejected = await fastmcp_server.serve_provider_cache_client(
        _request(
            "GET",
            "/draft-dashboard/provider-cache-client.js",
            client="192.168.1.5",
        )
    )
    assert rejected.status_code == 403


@pytest.mark.asyncio
async def test_run_route_accepts_only_exact_same_loopback_origin_and_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    async def run(*, scoring: str) -> dict[str, Any]:
        captured.append(scoring)
        return {"schemaVersion": 1, "status": "success", "scoring": scoring}

    monkeypatch.setattr(fastmcp_server, "run_provider_cache_job", run)
    body = b'{"schemaVersion":1,"scoring":"HALF"}'
    response = await fastmcp_server.receive_provider_cache_run(
        _request(
            "POST",
            "/provider-cache/run",
            body=body,
            origin="http://127.0.0.1:8765",
            content_type="application/json",
        )
    )

    assert response.status_code == 200
    assert captured == ["HALF"]
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_provider_cache_options_exposes_only_same_loopback_origin() -> None:
    allowed = await fastmcp_server.receive_provider_cache_run(
        _request(
            "OPTIONS",
            "/provider-cache/run",
            origin="http://127.0.0.1:8765",
        )
    )
    assert allowed.status_code == 204
    assert allowed.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:8765"
    )
    assert allowed.headers["cache-control"] == "no-store"

    rejected = await fastmcp_server.receive_provider_cache_run(
        _request(
            "OPTIONS",
            "/provider-cache/run",
            origin="moz-extension://private-extension",
        )
    )
    assert rejected.status_code == 403
    assert "access-control-allow-origin" not in rejected.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route_request", "status_code"),
    [
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                content_type="application/json",
            ),
            403,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
                client="192.168.1.5",
            ),
            403,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                host="localhost:8765",
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            403,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="moz-extension://private-extension",
                content_type="application/json",
            ),
            403,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                ui_header=None,
                content_type="application/json",
            ),
            403,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="text/plain",
            ),
            415,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":true,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1.0,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":[]}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF","url":"secret"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"CUSTOM"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
                query_string=b"force=true",
            ),
            400,
        ),
        (
            _request(
                "POST",
                "/provider-cache/run",
                body=b'{"schemaVersion":1,"scoring":"HALF"}',
                origin="http://127.0.0.1:8765",
                content_type="application/json",
                transfer_encoding="chunked",
            ),
            400,
        ),
    ],
)
async def test_run_route_rejects_remote_or_malformed_requests(
    route_request: Request,
    status_code: int,
) -> None:
    response = await fastmcp_server.receive_provider_cache_run(route_request)
    assert response.status_code == status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_length", "body", "status_code"),
    [
        (None, b'{"schemaVersion":1,"scoring":"HALF"}', 400),
        ("0", b"", 400),
        ("-1", b"", 400),
        ("01", b"{}", 400),
        ("129", b"{}", 413),
        ("2", b"{}x", 400),
        ("3", b"{}", 400),
    ],
)
async def test_run_route_rejects_unbounded_or_mismatched_bodies_before_job(
    monkeypatch: pytest.MonkeyPatch,
    content_length: str | None,
    body: bytes,
    status_code: int,
) -> None:
    calls = 0

    async def run(*, scoring: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "success"}

    monkeypatch.setattr(fastmcp_server, "run_provider_cache_job", run)
    response = await fastmcp_server.receive_provider_cache_run(
        _request(
            "POST",
            "/provider-cache/run",
            body=body,
            origin="http://127.0.0.1:8765",
            content_type="application/json",
            content_length=content_length,
        )
    )
    assert response.status_code == status_code
    assert calls == 0


@pytest.mark.asyncio
async def test_run_route_maps_busy_timeout_and_failures_to_sanitized_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"schemaVersion":1,"scoring":"STD"}'

    for error, expected_status in (
        (ProviderCacheMaintenanceBusy(), 409),
        (ProviderCacheMaintenanceTimeout(), 504),
        (RuntimeError("token at /Users/private/cache.sqlite3"), 503),
    ):
        async def fail(*, scoring: str, _error: Exception = error) -> dict[str, Any]:
            raise _error

        monkeypatch.setattr(fastmcp_server, "run_provider_cache_job", fail)
        response = await fastmcp_server.receive_provider_cache_run(
            _request(
                "POST",
                "/provider-cache/run",
                body=body,
                origin="http://127.0.0.1:8765",
                content_type="application/json",
            )
        )
        assert response.status_code == expected_status
        assert "private" not in repr(_json(response))
        assert "/Users" not in repr(_json(response))
