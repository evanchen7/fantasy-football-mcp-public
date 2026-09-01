"""Tests for wiring live state and Yahoo context into the specialist suite."""

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from src.agents.live_draft_recommender import LiveDraftRecommendationEngine
from src.services.live_draft_recommendation_service import get_live_draft_recommendation
from src.services.live_draft_store import save_live_draft
from tests.unit.test_live_draft_recommender import live_context, rankings


@pytest.mark.asyncio
async def test_service_combines_local_state_with_yahoo_rankings(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)
    calls = []

    async def call_tool(name: str, **arguments):
        calls.append((name, arguments))
        if name == "ff_get_draft_rankings":
            return {"rankings": rankings()}
        if name == "ff_get_league_info":
            return {
                "teams": 4,
                "scoring_type": "head",
                "roster_positions": [{"position": "QB", "count": 1}],
            }
        raise AssertionError(name)

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="nfl.l.10462193",
        strategy="balanced",
        count=3,
        ranking_count=200,
        simulations=32,
        store_path=path,
    )

    assert result["status"] == "success"
    assert result["leagueId"] == "10462193"
    assert len(result["recommendations"]) == 3
    assert {name for name, _ in calls} == {"ff_get_draft_rankings", "ff_get_league_info"}
    ranking_call = next(arguments for name, arguments in calls if name == "ff_get_draft_rankings")
    assert ranking_call["count"] == 200


@pytest.mark.asyncio
async def test_service_rejects_mismatched_league_identifiers(tmp_path: Path) -> None:
    async def call_tool(name: str, **arguments):
        raise AssertionError("Yahoo should not be called for mismatched league identifiers")

    with pytest.raises(ValueError, match="match"):
        await get_live_draft_recommendation(
            call_tool,
            league_key="nfl.l.10462193",
            league_id="999",
            store_path=tmp_path / "drafts.json",
        )


@pytest.mark.asyncio
async def test_service_returns_actionable_error_without_synced_state(tmp_path: Path) -> None:
    async def call_tool(name: str, **arguments):
        raise AssertionError("Yahoo should not be called without local state")

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="nfl.l.10462193",
        league_id="10462193",
        store_path=tmp_path / "missing.json",
    )

    assert result["status"] == "error"
    assert "extension" in result["message"].lower()


@pytest.mark.asyncio
async def test_service_preserves_yahoo_ranking_error(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {"teams": 4}
        return {"status": "error", "message": "Yahoo ranking quota exceeded"}

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="nfl.l.10462193",
        league_id="10462193",
        store_path=path,
    )

    assert result["status"] == "degraded"
    assert any("quota exceeded" in warning.lower() for warning in result["warnings"])


@pytest.mark.asyncio
async def test_service_degrades_when_roster_slots_are_missing(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {"teams": 4}
        return {"rankings": rankings()}

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="nfl.l.10462193",
        store_path=path,
    )

    assert result["status"] == "degraded"
    assert any("roster positions" in warning.lower() for warning in result["warnings"])


@pytest.mark.asyncio
async def test_service_preserves_league_info_error(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {"error": "League settings unavailable"}
        return {"rankings": rankings()}

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="nfl.l.10462193",
        league_id="10462193",
        store_path=path,
    )

    assert result["status"] == "degraded"
    assert any("settings unavailable" in warning.lower() for warning in result["warnings"])


@pytest.mark.asyncio
async def test_service_resolves_exact_yahoo_league_key_from_private_state(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)
    calls = []

    async def call_tool(name: str, **arguments):
        calls.append((name, arguments))
        if name == "ff_get_leagues":
            return {
                "leagues": [
                    {"key": "461.l.999", "name": "Other"},
                    {"key": "461.l.10462193", "name": "Draft league"},
                ]
            }
        if name == "ff_get_league_info":
            return {
                "teams": 4,
                "roster_positions": [{"position": "QB", "count": 1}],
                "your_team": {"key": "461.l.10462193.t.6"},
            }
        return {"rankings": rankings()}

    result = await get_live_draft_recommendation(
        call_tool,
        league_key=None,
        league_id="10462193",
        store_path=path,
        simulations=0,
    )

    assert result["status"] == "success"
    assert result["leagueKey"] == "461.l.10462193"
    assert [name for name, _ in calls] == [
        "ff_get_leagues",
        "ff_get_league_info",
        "ff_get_draft_rankings",
    ]
    assert calls[1][1]["league_key"] == "461.l.10462193"
    assert calls[2][1]["league_key"] == "461.l.10462193"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "league_keys, message",
    [
        (["461.l.999"], "could not be resolved"),
        (["461.l.10462193", "462.l.10462193"], "ambiguous"),
    ],
)
async def test_service_rejects_unresolved_or_ambiguous_yahoo_league_identity(
    tmp_path: Path, league_keys: list[str], message: str
) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)

    async def call_tool(name: str, **arguments):
        assert name == "ff_get_leagues"
        return {"leagues": [{"key": key} for key in league_keys]}

    with pytest.raises(ValueError, match=message):
        await get_live_draft_recommendation(
            call_tool,
            league_key=None,
            league_id="10462193",
            store_path=path,
        )


@pytest.mark.asyncio
async def test_service_rejects_authenticated_team_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {
                "teams": 4,
                "roster_positions": [{"position": "QB", "count": 1}],
                "your_team": {"key": "461.l.10462193.t.999"},
            }
        return {"rankings": rankings()}

    with pytest.raises(ValueError, match="team identity"):
        await get_live_draft_recommendation(
            call_tool,
            league_key="461.l.10462193",
            league_id="10462193",
            store_path=path,
            require_authenticated_team=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "league_info",
    [
        {"teams": 4, "roster_positions": [{"position": "QB", "count": 1}]},
        {
            "teams": 4,
            "roster_positions": [{"position": "QB", "count": 1}],
            "your_team": {"key": "malformed"},
        },
    ],
)
async def test_ui_service_requires_verified_authenticated_team(
    tmp_path: Path, league_info: dict
) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)
    calls = []

    async def call_tool(name: str, **arguments):
        calls.append(name)
        if name == "ff_get_league_info":
            return league_info
        raise AssertionError("rankings must not be called before team identity is verified")

    with pytest.raises(ValueError, match="team identity"):
        await get_live_draft_recommendation(
            call_tool,
            league_key="461.l.10462193",
            league_id="10462193",
            store_path=path,
            require_authenticated_team=True,
        )

    assert calls == ["ff_get_league_info"]


@pytest.mark.asyncio
async def test_service_serializes_all_yahoo_calls_across_ui_requests(tmp_path: Path) -> None:
    path = tmp_path / "drafts.json"
    save_live_draft(live_context(), path)
    events: list[str] = []

    def caller(label: str):
        async def call_tool(name: str, **arguments):
            events.append(f"{label}:{name}:start")
            await asyncio.sleep(0.01)
            events.append(f"{label}:{name}:end")
            if name == "ff_get_league_info":
                return {
                    "teams": 4,
                    "roster_positions": [{"position": "QB", "count": 1}],
                }
            return {"rankings": rankings()}

        return call_tool

    await asyncio.gather(
        get_live_draft_recommendation(
            caller("a"),
            league_key="461.l.10462193",
            store_path=path,
            simulations=0,
        ),
        get_live_draft_recommendation(
            caller("b"),
            league_key="461.l.10462193",
            store_path=path,
            simulations=0,
        ),
    )

    assert events in (
        [
            "a:ff_get_league_info:start",
            "a:ff_get_league_info:end",
            "a:ff_get_draft_rankings:start",
            "a:ff_get_draft_rankings:end",
            "b:ff_get_league_info:start",
            "b:ff_get_league_info:end",
            "b:ff_get_draft_rankings:start",
            "b:ff_get_draft_rankings:end",
        ],
        [
            "b:ff_get_league_info:start",
            "b:ff_get_league_info:end",
            "b:ff_get_draft_rankings:start",
            "b:ff_get_draft_rankings:end",
            "a:ff_get_league_info:start",
            "a:ff_get_league_info:end",
            "a:ff_get_draft_rankings:start",
            "a:ff_get_draft_rankings:end",
        ],
    )


@pytest.mark.asyncio
async def test_service_discards_result_when_synced_snapshot_advances_during_scoring(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "drafts.json"
    initial = live_context()
    initial["generatedAt"] = "2026-09-01T12:00:00+00:00"
    save_live_draft(initial, path)
    advanced = deepcopy(initial)
    advanced["generatedAt"] = "2026-09-01T12:00:01+00:00"
    advanced["picks"].append(
        {
            "pickNumber": 7,
            "player": "A. St. Brown",
            "position": "WR",
            "nflTeam": "DET",
            "fantasyTeam": "Alpha",
            "isUserPick": False,
        }
    )

    original_recommend = LiveDraftRecommendationEngine.recommend

    def recommend_then_sync(self, *args, **kwargs):
        result = original_recommend(self, *args, **kwargs)
        save_live_draft(advanced, path)
        return result

    monkeypatch.setattr(
        LiveDraftRecommendationEngine,
        "recommend",
        recommend_then_sync,
    )

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {
                "teams": 4,
                "roster_positions": [{"position": "QB", "count": 1}],
            }
        return {"rankings": rankings()}

    result = await get_live_draft_recommendation(
        call_tool,
        league_key="461.l.10462193",
        league_id="10462193",
        store_path=path,
        simulations=0,
    )

    assert result == {
        "status": "error",
        "errorCode": "draft_state_changed",
        "refreshRequired": True,
        "message": (
            "The synced draft changed while recommendations were being computed. "
            "Refresh recommendations to analyze the latest picks."
        ),
        "leagueId": "10462193",
        "primaryRecommendation": None,
        "alternatives": [],
        "recommendations": [],
        "contingency": None,
    }


@pytest.mark.asyncio
async def test_service_revalidation_rejects_new_same_league_session_ambiguity(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "drafts.json"
    initial = live_context()
    initial["generatedAt"] = "2026-09-01T12:00:00+00:00"
    save_live_draft(initial, path)
    ambiguous = deepcopy(initial)
    ambiguous["generatedAt"] = "2026-09-01T12:00:01+00:00"
    ambiguous["draft"]["sport"] = "football"
    ambiguous["draft"]["sessionKey"] = "football:10462193"

    original_recommend = LiveDraftRecommendationEngine.recommend

    def recommend_then_add_ambiguous_session(self, *args, **kwargs):
        result = original_recommend(self, *args, **kwargs)
        save_live_draft(ambiguous, path)
        return result

    monkeypatch.setattr(
        LiveDraftRecommendationEngine,
        "recommend",
        recommend_then_add_ambiguous_session,
    )

    async def call_tool(name: str, **arguments):
        if name == "ff_get_league_info":
            return {
                "teams": 4,
                "roster_positions": [{"position": "QB", "count": 1}],
            }
        return {"rankings": rankings()}

    with pytest.raises(ValueError, match="ambiguous across stored sessions"):
        await get_live_draft_recommendation(
            call_tool,
            league_key="461.l.10462193",
            league_id="10462193",
            store_path=path,
            simulations=0,
        )
