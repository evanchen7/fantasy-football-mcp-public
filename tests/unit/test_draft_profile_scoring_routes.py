"""Focused route tests for explicit local-profile scoring selection."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.requests import Request

import fastmcp_server


def _request(
    path: str,
    *,
    body: bytes,
    content_type: str,
    headers: dict[str, str] | None = None,
) -> Request:
    raw_headers = [
        (b"host", b"127.0.0.1:8765"),
        (b"origin", b"http://127.0.0.1:8765"),
        (b"x-fantasy-draft-ui", b"1"),
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("ascii"), value.encode("ascii")))
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
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        },
        receive,
    )


def _context(league_id: str = "498589") -> dict[str, Any]:
    return {
        "draft": {
            "sport": "f1",
            "leagueId": league_id,
            "teamId": "6",
            "sessionKey": f"f1:{league_id}",
        }
    }


def _profile(league_id: str = "498589", scoring: str = "HALF") -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "source": "local-draft-profile",
        "season": 2026,
        "importedAt": "2026-09-04T12:00:00Z",
        "draft": _context(league_id)["draft"],
        "rankings": [{"name": "Player One", "position": "RB", "rank": 1}],
        "leagueSettings": {
            "teams": 12,
            "rosterPositions": [{"position": "RB", "count": 2}],
            "scoringFormat": scoring,
        },
        "provenance": {"kind": "user-import", "format": "json"},
    }


def test_profile_response_omits_absent_legacy_scoring_instead_of_emitting_null() -> None:
    legacy = _profile()
    del legacy["leagueSettings"]["scoringFormat"]

    response = fastmcp_server._profile_response(legacy)

    assert "scoringFormat" not in response


@pytest.mark.asyncio
async def test_bind_route_passes_only_exact_scoring_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, dict[str, Any], str | None]] = []
    bound = _profile()
    monkeypatch.setattr(fastmcp_server, "_load_bound_live_draft", lambda _league: _context())

    def bind(
        source_league_id: str,
        draft: dict[str, Any],
        *,
        scoring_format: str | None = None,
    ) -> dict[str, Any]:
        captured.append((source_league_id, draft, scoring_format))
        return bound

    monkeypatch.setattr(fastmcp_server, "bind_local_draft_profile", bind)
    monkeypatch.setattr(fastmcp_server, "load_local_draft_profile", lambda _draft: bound)
    body = json.dumps(
        {
            "schemaVersion": 1,
            "sourceLeagueId": "10557704",
            "leagueId": "498589",
            "scoringFormat": "HALF",
        }
    ).encode()

    response = await fastmcp_server.bind_draft_profile(
        _request("/draft-profile-bind", body=body, content_type="application/json")
    )

    assert response.status_code == 200
    assert captured == [("10557704", _context()["draft"], "HALF")]
    assert json.loads(response.body)["scoringFormat"] == "HALF"

    rejected_body = body.replace(b'"HALF"', b'"half"')
    rejected = await fastmcp_server.bind_draft_profile(
        _request(
            "/draft-profile-bind",
            body=rejected_body,
            content_type="application/json",
        )
    )
    assert rejected.status_code == 400
    assert captured == [("10557704", _context()["draft"], "HALF")]


@pytest.mark.asyncio
async def test_default_route_persists_exact_scoring_without_touching_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str, str | None]] = []

    def select(sport: str, source: str, *, scoring_format: str | None = None) -> None:
        captured.append((sport, source, scoring_format))

    monkeypatch.setattr(fastmcp_server, "set_default_local_draft_profile", select)
    body = json.dumps(
        {
            "schemaVersion": 1,
            "sport": "f1",
            "sourceLeagueId": "10557704",
            "scoringFormat": "HALF",
        }
    ).encode()

    response = await fastmcp_server.set_draft_profile_default(
        _request("/draft-profile-default", body=body, content_type="application/json")
    )

    assert response.status_code == 200
    assert captured == [("f1", "10557704", "HALF")]
    assert json.loads(response.body) == {
        "status": "success",
        "sport": "f1",
        "sourceLeagueId": "10557704",
        "scoringFormat": "HALF",
    }

    rejected_body = body.replace(b'"HALF"', b'"half"')
    rejected = await fastmcp_server.set_draft_profile_default(
        _request(
            "/draft-profile-default",
            body=rejected_body,
            content_type="application/json",
        )
    )
    assert rejected.status_code == 400
    assert captured == [("f1", "10557704", "HALF")]


@pytest.mark.asyncio
async def test_xlsx_route_accepts_only_canonical_scoring_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def parse(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return _profile(kwargs["draft"]["leagueId"])

    monkeypatch.setattr(fastmcp_server, "profile_from_draftsheets_xlsx", parse)
    monkeypatch.setattr(fastmcp_server, "_load_bound_live_draft", lambda _league: _context())
    monkeypatch.setattr(fastmcp_server, "save_local_draft_profile", lambda value: value)
    common_headers = {
        "x-fantasy-league-id": "498589",
        "x-fantasy-team-count": "12",
        "x-fantasy-roster-positions": "QB=1,RB=2,WR=2,TE=1,FLEX=1,K=1,DST=1,BN=6,IR=1",
    }
    media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    rejected = await fastmcp_server.receive_draft_profile_xlsx(
        _request(
            "/draft-profile-xlsx",
            body=b"PK\x03\x04",
            content_type=media_type,
            headers={**common_headers, "x-fantasy-scoring-format": "half"},
        )
    )
    assert rejected.status_code == 400
    assert called is False

    accepted = await fastmcp_server.receive_draft_profile_xlsx(
        _request(
            "/draft-profile-xlsx",
            body=b"PK\x03\x04",
            content_type=media_type,
            headers={**common_headers, "x-fantasy-scoring-format": "HALF"},
        )
    )
    assert accepted.status_code == 200
    assert called is True
    assert json.loads(accepted.body)["scoringFormat"] == "HALF"
