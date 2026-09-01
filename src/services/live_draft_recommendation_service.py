"""Application service that joins private live state with Yahoo draft data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from src.agents.live_draft_recommender import LiveDraftRecommendationEngine
from src.services.live_draft_store import LiveDraftValidationError, load_live_draft

ToolCaller = Callable[..., Awaitable[dict[str, Any]]]
_YAHOO_RECOMMENDATION_LOCK = asyncio.Lock()
_DRAFT_IDENTITY_FIELDS = ("sport", "leagueId", "teamId", "sessionKey")


def _snapshot_binding(live_state: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    draft = live_state.get("draft")
    generated_at = live_state.get("generatedAt")
    if not isinstance(draft, Mapping) or not isinstance(generated_at, str) or not generated_at:
        raise LiveDraftValidationError(
            "synced draft snapshot identity and generatedAt are required"
        )
    identity = tuple(draft.get(field) for field in _DRAFT_IDENTITY_FIELDS)
    if any(not isinstance(value, str) or not value for value in identity):
        raise LiveDraftValidationError(
            "synced draft snapshot identity and generatedAt are required"
        )
    return (*identity, generated_at)


def _refresh_required_result(league_id: str) -> dict[str, Any]:
    return {
        "status": "error",
        "errorCode": "draft_state_changed",
        "refreshRequired": True,
        "message": (
            "The synced draft changed while recommendations were being computed. "
            "Refresh recommendations to analyze the latest picks."
        ),
        "leagueId": league_id,
        "primaryRecommendation": None,
        "alternatives": [],
        "recommendations": [],
        "contingency": None,
    }


def _resolve_league_key(result: Mapping[str, Any], league_id: str) -> str:
    leagues = result.get("leagues")
    if not isinstance(leagues, list):
        raise LiveDraftValidationError(
            "Yahoo league identity could not be resolved for the synced draft"
        )
    suffix = f".l.{league_id}"
    matches: list[str] = []
    for league in leagues:
        if not isinstance(league, Mapping):
            continue
        key = league.get("key") or league.get("league_key")
        if isinstance(key, str) and key.endswith(suffix):
            matches.append(key)
    unique_matches = sorted(set(matches))
    if not unique_matches:
        raise LiveDraftValidationError(
            "Yahoo league identity could not be resolved for the synced draft"
        )
    if len(unique_matches) != 1:
        raise LiveDraftValidationError(
            "Yahoo league identity is ambiguous for the synced draft"
        )
    return unique_matches[0]


def _validate_authenticated_team(
    league_result: Mapping[str, Any],
    live_state: Mapping[str, Any],
    league_key: str,
    *,
    required: bool,
) -> None:
    your_team = league_result.get("your_team")
    if not isinstance(your_team, Mapping):
        if required:
            raise LiveDraftValidationError(
                "authenticated Yahoo team identity is unavailable for the synced draft"
            )
        return
    team_key = your_team.get("key") or your_team.get("team_key")
    if team_key in (None, ""):
        if required:
            raise LiveDraftValidationError(
                "authenticated Yahoo team identity is unavailable for the synced draft"
            )
        return
    team_id = live_state.get("draft", {}).get("teamId")
    expected = f"{league_key}.t.{team_id}"
    if not isinstance(team_key, str) or team_key != expected:
        raise LiveDraftValidationError(
            "authenticated Yahoo team identity does not match the synced draft"
        )


async def get_live_draft_recommendation(
    call_tool: ToolCaller,
    *,
    league_key: str | None,
    league_id: str | None = None,
    strategy: str = "balanced",
    count: int = 5,
    ranking_count: int = 250,
    simulations: int = 256,
    store_path: str | Path | None = None,
    require_authenticated_team: bool = False,
) -> dict[str, Any]:
    """Return a recommendation while keeping network calls off the scoring path."""

    marker = ".l."
    if league_key is None:
        if not isinstance(league_id, str) or not league_id:
            raise LiveDraftValidationError(
                "league_id is required when resolving the Yahoo league key"
            )
        league_id = str(league_id)
    else:
        if marker not in league_key:
            raise LiveDraftValidationError(
                "league_key must contain a Yahoo .l. league identifier"
            )
        derived_league_id = league_key.rsplit(marker, 1)[1]
        if not derived_league_id:
            raise LiveDraftValidationError("league_key must include a league identifier")
        if league_id is not None and str(league_id) != derived_league_id:
            raise LiveDraftValidationError("league_id must match league_key")
        league_id = derived_league_id

    live_state = load_live_draft(
        league_id=league_id,
        path=store_path,
        reject_ambiguous=True,
    )
    if live_state is None:
        return {
            "status": "error",
            "message": (
                "No synced live draft state was found. Load the Yahoo Draft Recorder "
                "extension, open Results → Round by Round, and rescan the page."
            ),
            "leagueId": league_id,
        }
    analyzed_snapshot = _snapshot_binding(live_state)

    # Keep all Yahoo calls serialized: concurrent 401 responses can race rotating token
    # refreshes. CPU-only scoring starts after releasing the Yahoo boundary.
    async with _YAHOO_RECOMMENDATION_LOCK:
        if league_key is None:
            leagues_result = await call_tool("ff_get_leagues")
            if not isinstance(leagues_result, Mapping):
                raise LiveDraftValidationError(
                    "Yahoo league identity could not be resolved for the synced draft"
                )
            league_key = _resolve_league_key(leagues_result, league_id)
        league_result = await call_tool("ff_get_league_info", league_key=league_key)
        league_mapping = league_result if isinstance(league_result, Mapping) else {}
        _validate_authenticated_team(
            league_mapping,
            live_state,
            league_key,
            required=require_authenticated_team,
        )
        rankings_result = await call_tool(
            "ff_get_draft_rankings",
            league_key=league_key,
            position="all",
            count=max(25, min(int(ranking_count), 500)),
        )
    rankings = rankings_result.get("rankings", []) if isinstance(rankings_result, Mapping) else []
    league_info = dict(league_result) if isinstance(league_result, Mapping) else {}
    engine = LiveDraftRecommendationEngine(simulations=max(0, min(int(simulations), 512)))
    result = await asyncio.to_thread(
        engine.recommend,
        live_state,
        rankings if isinstance(rankings, list) else [],
        league_info,
        strategy=strategy,
        count=max(1, min(int(count), 20)),
    )
    if isinstance(rankings_result, Mapping) and not rankings:
        ranking_error = rankings_result.get("message") or rankings_result.get("error")
        if ranking_error:
            result["warnings"].append(f"Yahoo rankings: {ranking_error}")
    league_warning = None
    if isinstance(league_result, Mapping) and (
        "error" in league_result or league_result.get("status") == "error"
    ):
        league_warning = league_result.get("message") or league_result.get("error")
    elif not isinstance(league_info.get("roster_positions"), list) or not league_info.get(
        "roster_positions"
    ):
        league_warning = "Yahoo league roster positions are unavailable; using 1QB defaults"
    if league_warning:
        result["warnings"].append(f"Yahoo league info: {league_warning}")
        if result.get("status") == "success":
            result["status"] = "degraded"
    current_state = load_live_draft(
        league_id=league_id,
        path=store_path,
        reject_ambiguous=True,
    )
    if (
        current_state is None
        or _snapshot_binding(current_state) != analyzed_snapshot
        or current_state != live_state
    ):
        return _refresh_required_result(league_id)
    result["leagueKey"] = league_key
    result["leagueId"] = live_state.get("draft", {}).get("leagueId")
    result["dataSources"] = {
        "liveState": "local browser extension",
        "rankings": "Yahoo pre-draft rankings",
        "league": "Yahoo league info",
    }
    return result
