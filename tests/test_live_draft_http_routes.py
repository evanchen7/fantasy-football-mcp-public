"""HTTP safety and dashboard contract tests for the local draft UI."""

import json

import pytest
from starlette.requests import Request

import fastmcp_server


def request_for(
    method: str,
    path: str,
    *,
    payload: object | None = None,
    body: bytes | None = None,
    origin: str | None = "moz-extension://draft-recorder",
    client: str = "127.0.0.1",
    content_type: str | None = "application/json",
    ui_header: str | None = "1",
    content_length: str | None = None,
    host: str = "127.0.0.1:8765",
) -> Request:
    encoded = body if body is not None else json.dumps(payload if payload is not None else {}).encode()
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    if ui_header is not None:
        headers.append((b"x-fantasy-draft-ui", ui_header.encode()))
    length = str(len(encoded)) if content_length is None else content_length
    headers.append((b"content-length", length.encode()))
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (client, 49152),
        "server": ("127.0.0.1", 8765),
    }
    return Request(scope, receive)


def response_json(response) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_recommendation_route_derives_identity_and_clamps_inputs(monkeypatch) -> None:
    calls = []

    async def recommend(call_tool, **arguments):
        calls.append(arguments)
        return {"status": "success", "recommendations": []}

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", recommend)
    request = request_for(
        "POST",
        "/draft-recommendation",
        payload={
            "schemaVersion": 1,
            "leagueId": "10462193",
            "strategy": "aggressive",
            "count": 999,
            "rankingCount": 9999,
            "simulations": 9999,
        },
    )

    response = await fastmcp_server.receive_live_draft_recommendation(request)

    assert response.status_code == 200
    assert response_json(response)["status"] == "success"
    assert calls == [
        {
            "league_key": None,
            "league_id": "10462193",
            "strategy": "aggressive",
            "count": 20,
            "ranking_count": 500,
            "simulations": 512,
            "require_authenticated_team": True,
        }
    ]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["access-control-allow-origin"] == "moz-extension://draft-recorder"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_recommendation_route_clamps_lower_input_bounds(monkeypatch) -> None:
    calls = []

    async def recommend(call_tool, **arguments):
        calls.append(arguments)
        return {"status": "blocked", "leagueId": "10462193"}

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", recommend)

    response = await fastmcp_server.receive_live_draft_recommendation(
        request_for(
            "POST",
            "/draft-recommendation",
            payload={
                "schemaVersion": 1,
                "leagueId": "10462193",
                "count": -10,
                "rankingCount": -10,
                "simulations": -10,
            },
        )
    )

    assert response.status_code == 200
    assert calls[0]["count"] == 1
    assert calls[0]["ranking_count"] == 25
    assert calls[0]["simulations"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "status", "message"),
    [
        ({"origin": None}, 403, "Origin required"),
        ({"origin": "https://evil.example"}, 403, "Origin not allowed"),
        ({"client": "192.168.1.20"}, 403, "Loopback access required"),
        ({"content_type": "text/plain"}, 415, "application/json"),
        ({"ui_header": None}, 403, "UI header required"),
        ({"content_length": "not-a-number"}, 400, "Invalid content length"),
        ({"content_length": "-1"}, 400, "Invalid content length"),
        ({"content_length": "4097"}, 413, "Payload too large"),
        (
            {"origin": "http://localhost:8765", "host": "127.0.0.1:8765"},
            403,
            "Origin not allowed",
        ),
        (
            {"origin": "http://127.0.0.1:9999", "host": "127.0.0.1:8765"},
            403,
            "Origin not allowed",
        ),
        ({"origin": "http://127.0.0.1:8765/"}, 403, "Origin not allowed"),
        ({"origin": "moz-extension://draft-recorder/path"}, 403, "Origin not allowed"),
    ],
)
async def test_recommendation_route_rejects_unsafe_requests(
    monkeypatch, changes, status, message
) -> None:
    async def unexpected(*args, **kwargs):
        raise AssertionError("recommendation service must not be called")

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", unexpected)
    request = request_for(
        "POST",
        "/draft-recommendation",
        payload={"schemaVersion": 1, "leagueId": "10462193"},
        **changes,
    )

    response = await fastmcp_server.receive_live_draft_recommendation(request)

    assert response.status_code == status
    assert message in response_json(response)["message"]
    assert response.headers["cache-control"] == "no-store"
    if changes.get("origin") == "https://evil.example":
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_recommendation_route_bounds_streamed_body(monkeypatch) -> None:
    async def unexpected(*args, **kwargs):
        raise AssertionError("recommendation service must not be called")

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", unexpected)
    request = request_for(
        "POST",
        "/draft-recommendation",
        body=b"{" + b" " * 4096 + b"}",
        content_length="0",
    )

    response = await fastmcp_server.receive_live_draft_recommendation(request)

    assert response.status_code == 413
    assert response_json(response)["message"] == "Payload too large"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"schemaVersion": 1, "leagueId": "10462193", "leagueKey": "nfl.l.999"}, "Unsupported field"),
        ({"schemaVersion": 1, "leagueId": "../10462193"}, "leagueId"),
        ({"schemaVersion": 1, "leagueId": "10462193", "strategy": "reckless"}, "strategy"),
        ({"schemaVersion": 1, "leagueId": "10462193", "count": True}, "count"),
        ({"schemaVersion": 1, "leagueId": "10462193", "rankingCount": True}, "rankingCount"),
        ({"schemaVersion": 1, "leagueId": "10462193", "simulations": False}, "simulations"),
        ({"schemaVersion": True, "leagueId": "10462193"}, "schemaVersion 1"),
        ({"leagueId": "10462193"}, "schemaVersion 1"),
        (["10462193"], "JSON object"),
    ],
)
async def test_recommendation_route_allowlists_json(monkeypatch, payload, message) -> None:
    async def unexpected(*args, **kwargs):
        raise AssertionError("recommendation service must not be called")

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", unexpected)

    response = await fastmcp_server.receive_live_draft_recommendation(
        request_for("POST", "/draft-recommendation", payload=payload)
    )

    assert response.status_code == 400
    assert message in response_json(response)["message"]


@pytest.mark.asyncio
async def test_recommendation_route_accepts_same_origin_dashboard(monkeypatch) -> None:
    async def recommend(call_tool, **arguments):
        return {"status": "blocked", "warnings": ["Missing pick 4"]}

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", recommend)
    request = request_for(
        "POST",
        "/draft-recommendation",
        payload={"schemaVersion": 1, "leagueId": "10462193"},
        origin="http://127.0.0.1:8765",
    )

    response = await fastmcp_server.receive_live_draft_recommendation(request)

    assert response.status_code == 200
    assert response_json(response) == {
        "status": "blocked",
        "warnings": ["Missing pick 4"],
    }


@pytest.mark.asyncio
async def test_recommendation_route_passes_through_refresh_required_without_candidates(
    monkeypatch,
) -> None:
    async def recommend(call_tool, **arguments):
        return {
            "status": "error",
            "errorCode": "draft_state_changed",
            "refreshRequired": True,
            "message": "The synced draft changed. Refresh recommendations.",
            "leagueId": "10462193",
            "primaryRecommendation": None,
            "alternatives": [],
            "recommendations": [],
            "contingency": None,
        }

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", recommend)

    response = await fastmcp_server.receive_live_draft_recommendation(
        request_for(
            "POST",
            "/draft-recommendation",
            payload={"schemaVersion": 1, "leagueId": "10462193"},
        )
    )

    assert response.status_code == 200
    result = response_json(response)
    assert result["refreshRequired"] is True
    assert result["recommendations"] == []
    assert result["alternatives"] == []
    assert result["primaryRecommendation"] is None
    assert result["contingency"] is None


@pytest.mark.asyncio
async def test_recommendation_route_sanitizes_unexpected_service_failures(monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise RuntimeError("secret Yahoo transport detail")

    monkeypatch.setattr(fastmcp_server, "get_live_draft_recommendation", fail)

    response = await fastmcp_server.receive_live_draft_recommendation(
        request_for(
            "POST",
            "/draft-recommendation",
            payload={"schemaVersion": 1, "leagueId": "10462193"},
        )
    )

    assert response.status_code == 502
    assert response_json(response) == {
        "status": "error",
        "message": "Recommendation service unavailable",
    }
    assert b"secret" not in response.body


@pytest.mark.asyncio
async def test_recommendation_preflight_is_strict_and_private_network_safe() -> None:
    request = request_for(
        "OPTIONS",
        "/draft-recommendation",
        origin="chrome-extension://abcdefghijklmnop",
        content_type=None,
        ui_header=None,
    )

    response = await fastmcp_server.receive_live_draft_recommendation(request)

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == (
        "chrome-extension://abcdefghijklmnop"
    )
    assert response.headers["access-control-allow-private-network"] == "true"
    assert "X-Fantasy-Draft-UI" in response.headers["access-control-allow-headers"]


@pytest.mark.asyncio
async def test_recommendation_preflight_rejects_hostile_origin_without_cors() -> None:
    response = await fastmcp_server.receive_live_draft_recommendation(
        request_for(
            "OPTIONS",
            "/draft-recommendation",
            origin="https://hostile.example",
            content_type=None,
            ui_header=None,
        )
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_dashboard_assets_are_loopback_only_and_no_store() -> None:
    page = await fastmcp_server.serve_draft_dashboard(
        request_for("GET", "/draft-dashboard", body=b"", content_type=None, ui_header=None)
    )
    script = await fastmcp_server.serve_draft_dashboard_script(
        request_for(
            "GET", "/draft-dashboard/app.js", body=b"", content_type=None, ui_header=None
        )
    )
    shared = await fastmcp_server.serve_draft_recommendation_client(
        request_for(
            "GET",
            "/draft-dashboard/shared/recommendation-client.js",
            body=b"",
            content_type=None,
            ui_header=None,
        )
    )
    rejected = await fastmcp_server.serve_draft_dashboard(
        request_for(
            "GET",
            "/draft-dashboard",
            body=b"",
            content_type=None,
            ui_header=None,
            client="10.0.0.8",
        )
    )
    shared_rejected = await fastmcp_server.serve_draft_recommendation_client(
        request_for(
            "GET",
            "/draft-dashboard/shared/recommendation-client.js",
            body=b"",
            content_type=None,
            ui_header=None,
            client="10.0.0.8",
        )
    )

    assert page.status_code == 200
    assert b"Live Draft Assistant" in page.body
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert shared.status_code == 200
    assert shared.body == (
        fastmcp_server._DRAFT_SHARED_UI_DIRECTORY / "recommendation-client.js"
    ).read_bytes()
    assert rejected.status_code == 403
    assert shared_rejected.status_code == 403


def test_http_server_defaults_to_loopback(monkeypatch) -> None:
    calls = []
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(fastmcp_server.server, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    fastmcp_server.run_http_server(show_banner=False)

    assert calls == [(("http",), {"host": "127.0.0.1", "port": 8000, "show_banner": False})]


def test_container_and_render_deployments_explicitly_bind_external_interface() -> None:
    root = fastmcp_server._DRAFT_DASHBOARD_DIRECTORY.parents[1]

    assert "ENV HOST=0.0.0.0" in (root / "Dockerfile").read_text(encoding="utf-8")
    render = (root / "render.yaml").read_text(encoding="utf-8")
    assert "- key: HOST\n        value: 0.0.0.0" in render


def test_dashboard_uses_shared_ui_contract_without_inline_or_remote_code() -> None:
    dashboard = fastmcp_server._DRAFT_DASHBOARD_DIRECTORY
    index = (dashboard / "index.html").read_text(encoding="utf-8")
    script = (dashboard / "app.js").read_text(encoding="utf-8")
    shared_scripts = [
        "/draft-dashboard/shared/recommendation-client.js",
        "/draft-dashboard/shared/recommendation-view-model.js",
        "/draft-dashboard/shared/recommendation-renderer.js",
    ]

    assert all(name in index for name in shared_scripts)
    assert max(index.index(name) for name in shared_scripts) < index.index(
        "/draft-dashboard/app.js"
    )
    assert "fetchDraftRecommendationsForLeagueId" in script
    assert "createRecommendationViewModel" in script
    assert "renderRecommendationView" in script
    assert "function renderScenarios(data, model)" in script
    assert "renderScenarios(data, model);" in script
    assert "const model = render(data, leagueId);" in script
    assert "model?.mode === 'success' || model?.mode === 'degraded'" in script
    assert "visibleRecommendationCount" in script
    assert "data.recommendations.slice(0, visibleRecommendationCount)" in script
    assert "model.mode === 'success'" in script
    assert "setStatus(...requestStatusForModel(model, leagueId));" in script
    assert "Recommendations are blocked for league" in script
    assert "data.status === 'success'" not in script
    assert "model.recommendations.length" in script
    assert "window.location.hash" in script
    assert "window.location.search" not in script
    assert "innerHTML" not in script
    assert "function resetAnalysisPanels()" in script
    assert script.count("resetAnalysisPanels();") >= 3
    assert "setControlsDisabled(true);" in script
    assert "leagueInput.value.trim() !== leagueId" in script
    assert "typeof value === 'number' && Number.isFinite(value)" in script
    assert "scoring latency unavailable" in script
    assert "unavailable-score" in script
    assert "<script>" not in index
    assert "http://" not in index and "https://" not in index

    csp = fastmcp_server._draft_dashboard_headers()["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "http:" not in csp and "https:" not in csp
