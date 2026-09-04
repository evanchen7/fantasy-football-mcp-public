"""Tests for bounded provider cache inspection and warming."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.services import live_draft_recommendation_service
from src.services.provider_cache_maintenance import (
    ProviderCacheMaintenanceBusy,
    ProviderCacheMaintenanceTimeout,
    get_provider_cache_stats,
    run_provider_cache_job,
)

NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


class FakeSnapshotCache:
    def metadata(self, *, now: datetime) -> dict[str, Any]:
        assert now == NOW
        return {
            "status": "available",
            "sizeBytes": 4096,
            "snapshotCount": 2,
            "recordCount": 12,
            "latestFetchedAt": "2026-09-03T11:00:00Z",
            "snapshots": [
                {
                    "endpoint": "players",
                    "variant": "catalog-season",
                    "season": 2026,
                    "week": 0,
                    "fetchedAt": "2026-09-03T11:00:00Z",
                    "recordCount": 10,
                    "returnedCount": 10,
                    "reportedCount": 10,
                    "truncated": False,
                    "publicApiLimited": False,
                },
                {
                    "endpoint": "sleeper_players",
                    "variant": "active",
                    "season": None,
                    "week": None,
                    "fetchedAt": "2026-09-03T10:00:00Z",
                    "recordCount": 2,
                    "returnedCount": 2,
                    "reportedCount": 2,
                    "truncated": False,
                    "publicApiLimited": False,
                },
            ],
        }


class WarmSnapshotCache:
    def __init__(self, scoring: str = "HALF", *, include_old_ppr: bool = False) -> None:
        self.scoring = scoring
        self.include_old_ppr = include_old_ppr

    def metadata(self, *, now: datetime) -> dict[str, Any]:
        assert now == NOW
        scoring = self.scoring.lower()

        def row(
            endpoint: str,
            variant: str,
            *,
            season: int | None,
            week: int | None,
            count: int = 10,
            fetched_at: str = "2026-09-03T12:00:00Z",
        ) -> dict[str, Any]:
            return {
                "endpoint": endpoint,
                "variant": variant,
                "season": season,
                "week": week,
                "fetchedAt": fetched_at,
                "recordCount": count,
                "returnedCount": count,
                "reportedCount": count,
                "truncated": False,
                "publicApiLimited": False,
            }

        rows = [
            row(
                "players",
                "catalog" if self.scoring == "HALF" else "catalog-season",
                season=None if self.scoring == "HALF" else 2026,
                week=None if self.scoring == "HALF" else 0,
            ),
            row("injuries", "weekly", season=2026, week=0),
            row("news", "recent", season=None, week=None),
            row(
                "projections",
                f"preseason-{scoring}",
                season=2026,
                week=0,
            ),
            row(
                "sleeper_players",
                "active",
                season=None,
                week=None,
                count=2,
            ),
        ]
        if self.scoring == "HALF":
            rows.append(row("adp", "preseason-half", season=2026, week=0))
        if self.include_old_ppr:
            rows.append(
                row(
                    "projections",
                    "preseason-ppr",
                    season=2026,
                    week=0,
                    fetched_at="2026-08-01T12:00:00Z",
                )
            )
        return {
            "status": "available",
            "sizeBytes": 4096,
            "snapshotCount": len(rows),
            "recordCount": sum(item["recordCount"] for item in rows),
            "latestFetchedAt": "2026-09-03T12:00:00Z",
            "snapshots": rows,
        }


class FakeBudget:
    def metadata(self, *, now: datetime) -> dict[str, Any]:
        assert now == NOW
        return {
            "status": "available",
            "utcDate": "2026-09-03",
            "used": 7,
            "remaining": 88,
            "limit": 95,
        }


def test_stats_are_bounded_allowlisted_metadata_only() -> None:
    result = get_provider_cache_stats(
        snapshot_cache=FakeSnapshotCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
    )

    assert result == {
        "schemaVersion": 1,
        "status": "success",
        "cache": {
            "status": "available",
            "sizeBytes": 4096,
            "snapshotCount": 2,
            "recordCount": 12,
            "latestFetchedAt": "2026-09-03T11:00:00Z",
            "snapshots": [
                {
                    "provider": "FantasyPros",
                    "dataset": "players",
                    "variant": "catalog-season",
                        "season": 2026,
                        "week": 0,
                    "fetchedAt": "2026-09-03T11:00:00Z",
                    "recordCount": 10,
                    "stale": False,
                },
                {
                    "provider": "Sleeper",
                    "dataset": "sleeper_players",
                    "variant": "active",
                    "season": None,
                    "week": None,
                    "fetchedAt": "2026-09-03T10:00:00Z",
                    "recordCount": 2,
                    "stale": False,
                },
            ],
        },
        "fantasyProsBudget": {
            "status": "available",
            "utcDate": "2026-09-03",
            "used": 7,
            "remaining": 88,
            "limit": 95,
        },
    }
    encoded = json.dumps(result)
    for forbidden in (
        "path",
        "apiKey",
        "leagueId",
        "teamId",
        "sessionKey",
        "https://",
    ):
        assert forbidden not in encoded


def test_stats_apply_endpoint_and_partial_catalog_ttls() -> None:
    def row(
        endpoint: str,
        variant: str,
        age: timedelta,
        *,
        season: int | None,
        week: int | None,
        returned: int = 10,
        reported: int | None = 10,
    ) -> dict[str, Any]:
        return {
            "endpoint": endpoint,
            "variant": variant,
            "season": season,
            "week": week,
            "fetchedAt": (NOW - age).isoformat().replace("+00:00", "Z"),
            "recordCount": 10,
            "returnedCount": returned,
            "reportedCount": reported,
            "truncated": False,
            "publicApiLimited": False,
        }

    rows = [
        row("players", "catalog-season", timedelta(hours=23), season=2026, week=0),
        row(
            "players",
            "catalog",
            timedelta(minutes=5),
            season=None,
            week=None,
            returned=10,
            reported=20,
        ),
        row("injuries", "weekly", timedelta(minutes=5), season=2026, week=0),
        row("news", "recent", timedelta(minutes=4, seconds=59), season=None, week=None),
        row("projections", "preseason-half", timedelta(hours=23), season=2026, week=0),
        row("adp", "preseason-half", timedelta(hours=23), season=2026, week=0),
        row("sleeper_players", "active", timedelta(hours=23), season=None, week=None),
        row("projections", "preseason-ppr", timedelta(minutes=-1), season=2026, week=0),
    ]

    class MetadataCache:
        def metadata(self, *, now: datetime) -> dict[str, Any]:
            return {
                "status": "available",
                "sizeBytes": 4096,
                "snapshotCount": len(rows),
                "recordCount": 80,
                "latestFetchedAt": rows[-1]["fetchedAt"],
                "snapshots": rows,
            }

    result = get_provider_cache_stats(
        snapshot_cache=MetadataCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
    )
    stale = {
        (item["dataset"], item["variant"]): item["stale"]
        for item in result["cache"]["snapshots"]
    }
    assert stale == {
        ("players", "catalog-season"): False,
        ("players", "catalog"): True,
        ("injuries", "weekly"): True,
        ("news", "recent"): False,
        ("projections", "preseason-half"): False,
        ("adp", "preseason-half"): False,
        ("sleeper_players", "active"): False,
        ("projections", "preseason-ppr"): True,
    }
    assert result["status"] == "success"


def test_stats_degrade_malformed_metadata_without_echoing_it() -> None:
    private_detail = "token at https://private.invalid/league/123"

    class MalformedCache:
        def metadata(self, *, now: datetime) -> dict[str, Any]:
            return {
                "status": "available",
                "sizeBytes": private_detail,
                "snapshots": [{"player": private_detail}],
            }

    class MalformedBudget:
        def metadata(self, *, now: datetime) -> dict[str, Any]:
            return {"status": "available", "error": private_detail}

    result = get_provider_cache_stats(
        snapshot_cache=MalformedCache(),
        request_budget=MalformedBudget(),
        clock=lambda: NOW,
    )

    assert result["status"] == "degraded"
    assert result["cache"]["status"] == "unavailable"
    assert result["fantasyProsBudget"]["status"] == "unavailable"
    assert private_detail not in json.dumps(result)


def test_missing_budget_is_trustworthy_zero_usage() -> None:
    class MissingBudget:
        def metadata(self, *, now: datetime) -> dict[str, Any]:
            return {
                "status": "missing",
                "utcDate": "2026-09-03",
                "used": 0,
                "remaining": 95,
                "limit": 95,
            }

    result = get_provider_cache_stats(
        snapshot_cache=FakeSnapshotCache(),
        request_budget=MissingBudget(),
        clock=lambda: NOW,
    )

    assert result["status"] == "success"
    assert result["fantasyProsBudget"]["status"] == "missing"


def test_fantasypros_job_requires_records_only_for_essential_datasets() -> None:
    from src.services.provider_cache_maintenance import _sanitize_fantasypros_result

    def dataset(count: int) -> dict[str, Any]:
        return {
            "status": "available",
            "recordCount": count,
            "fetchedAt": "2026-09-03T12:00:00Z",
            "stale": False,
            "refreshFailed": False,
            "publicApiLimited": False,
        }

    result = _sanitize_fantasypros_result(
        {
            "datasets": {
                "players": dataset(0),
                "injuries": dataset(0),
                "news": dataset(0),
                "projections": dataset(10),
                "adp": dataset(10),
            }
        }
    )

    assert result["status"] == "degraded"
    assert result["datasets"]["players"] == {
        "status": "unavailable",
        "recordCount": 0,
        "fetchedAt": None,
        "stale": False,
        "refreshFailed": False,
        "publicApiLimited": False,
    }
    assert result["datasets"]["injuries"]["status"] == "available"
    assert result["datasets"]["news"]["status"] == "available"


class FakeFantasyProsProvider:
    def __init__(self, *, wait: asyncio.Event | None = None) -> None:
        self.calls: list[tuple[int, str]] = []
        self.wait = wait

    async def warm_cache(self, *, year: int, scoring: str) -> dict[str, Any]:
        self.calls.append((year, scoring))
        if self.wait is not None:
            await self.wait.wait()
        dataset = {
            "status": "available",
            "recordCount": 10,
            "fetchedAt": "2026-09-03T12:00:00Z",
            "stale": False,
            "refreshFailed": False,
            "publicApiLimited": False,
        }
        return {
            "status": "success",
            "provider": "FantasyPros",
            "datasets": {
                name: dict(dataset)
                for name in ("players", "injuries", "news", "projections", "adp")
            },
            "private": "token at https://private.invalid",
        }


class FakeSleeperProvider:
    def __init__(self, order: list[str] | None = None) -> None:
        self.calls = 0
        self.order = order

    async def warm_player_cache(self) -> dict[str, Any]:
        self.calls += 1
        if self.order is not None:
            self.order.append("sleeper")
        return {
            "status": "success",
            "provider": "Sleeper",
            "catalogFetchedAt": "2026-09-03T12:00:00Z",
            "cacheStale": False,
            "refreshFailed": False,
            "catalogPlayers": 2,
            "players": [{"name": "must not escape"}],
        }


@pytest.mark.asyncio
async def test_job_uses_current_utc_season_and_runs_providers_sequentially() -> None:
    order: list[str] = []

    class OrderedFantasyPros(FakeFantasyProsProvider):
        async def warm_cache(self, *, year: int, scoring: str) -> dict[str, Any]:
            order.append("fantasyPros")
            return await super().warm_cache(year=year, scoring=scoring)

    fantasypros = OrderedFantasyPros()
    sleeper = FakeSleeperProvider(order)

    result = await run_provider_cache_job(
        scoring="PPR",
        fantasypros_provider=fantasypros,
        sleeper_provider=sleeper,
        snapshot_cache=WarmSnapshotCache("PPR"),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert order == ["fantasyPros", "sleeper"]
    assert fantasypros.calls == [(2026, "PPR")]
    assert sleeper.calls == 1
    assert result["schemaVersion"] == 1
    assert result["status"] == "success"
    assert result["scoring"] == "PPR"
    assert result["season"] == 2026
    assert result["providers"]["sleeper"] == {
        "status": "success",
        "recordCount": 2,
        "fetchedAt": "2026-09-03T12:00:00Z",
        "stale": False,
        "refreshFailed": False,
    }
    assert result["stats"]["cache"]["recordCount"] == 42
    assert "private.invalid" not in repr(result)
    assert "must not escape" not in repr(result)


@pytest.mark.asyncio
async def test_unrelated_stale_scoring_snapshot_does_not_degrade_selected_job() -> None:
    result = await run_provider_cache_job(
        scoring="HALF",
        fantasypros_provider=FakeFantasyProsProvider(),
        sleeper_provider=FakeSleeperProvider(),
        snapshot_cache=WarmSnapshotCache("HALF", include_old_ppr=True),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert any(
        item["variant"] == "preseason-ppr" and item["stale"] is True
        for item in result["stats"]["cache"]["snapshots"]
    )
    assert result["providers"]["fantasyPros"]["status"] == "success"
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_unrelated_available_cache_does_not_verify_selected_persistence() -> None:
    result = await run_provider_cache_job(
        scoring="HALF",
        fantasypros_provider=FakeFantasyProsProvider(),
        sleeper_provider=FakeSleeperProvider(),
        snapshot_cache=FakeSnapshotCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert result["stats"]["cache"]["status"] == "available"
    assert result["providers"]["fantasyPros"]["status"] == "success"
    assert result["providers"]["sleeper"]["status"] == "success"
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_persistence_verification_matches_sqlite_second_precision() -> None:
    class FractionalFantasyPros(FakeFantasyProsProvider):
        async def warm_cache(self, *, year: int, scoring: str) -> dict[str, Any]:
            result = await super().warm_cache(year=year, scoring=scoring)
            for dataset in result["datasets"].values():
                dataset["fetchedAt"] = "2026-09-03T12:00:00.987654Z"
            return result

    class FractionalSleeper(FakeSleeperProvider):
        async def warm_player_cache(self) -> dict[str, Any]:
            result = await super().warm_player_cache()
            result["catalogFetchedAt"] = "2026-09-03T12:00:00.123456Z"
            return result

    result = await run_provider_cache_job(
        scoring="HALF",
        fantasypros_provider=FractionalFantasyPros(),
        sleeper_provider=FractionalSleeper(),
        snapshot_cache=WarmSnapshotCache("HALF"),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert result["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("cache_status", ["missing", "unavailable"])
async def test_successful_provider_results_require_verified_cache_persistence(
    cache_status: str,
) -> None:
    class UnreadableCache:
        def metadata(self, *, now: datetime) -> dict[str, Any]:
            return {
                "status": cache_status,
                "sizeBytes": None,
                "snapshotCount": 0,
                "recordCount": 0,
                "latestFetchedAt": None,
                "snapshots": [],
            }

    result = await run_provider_cache_job(
        fantasypros_provider=FakeFantasyProsProvider(),
        sleeper_provider=FakeSleeperProvider(),
        snapshot_cache=UnreadableCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert result["stats"]["status"] == "degraded"
    assert result["stats"]["cache"]["status"] == cache_status
    assert result["providers"]["fantasyPros"]["status"] == "success"
    assert result["providers"]["sleeper"]["status"] == "success"
    assert result["status"] == "degraded"


@pytest.mark.asyncio
async def test_job_reuses_recommendation_service_singletons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fantasypros = FakeFantasyProsProvider()
    sleeper = FakeSleeperProvider()
    monkeypatch.setattr(
        live_draft_recommendation_service, "_FANTASYPROS_PROVIDER", fantasypros
    )
    monkeypatch.setattr(
        live_draft_recommendation_service, "_SLEEPER_PLAYER_PROVIDER", sleeper
    )

    await run_provider_cache_job(
        snapshot_cache=FakeSnapshotCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=asyncio.Lock(),
    )

    assert fantasypros.calls == [(2026, "HALF")]
    assert sleeper.calls == 1


@pytest.mark.asyncio
async def test_concurrent_job_is_rejected_without_waiting() -> None:
    release = asyncio.Event()
    fantasypros = FakeFantasyProsProvider(wait=release)
    lock = asyncio.Lock()
    first = asyncio.create_task(
        run_provider_cache_job(
            fantasypros_provider=fantasypros,
            sleeper_provider=FakeSleeperProvider(),
            snapshot_cache=FakeSnapshotCache(),
            request_budget=FakeBudget(),
            clock=lambda: NOW,
            run_lock=lock,
        )
    )
    for _ in range(10):
        if lock.locked():
            break
        await asyncio.sleep(0)

    with pytest.raises(ProviderCacheMaintenanceBusy):
        await run_provider_cache_job(
            fantasypros_provider=FakeFantasyProsProvider(),
            sleeper_provider=FakeSleeperProvider(),
            snapshot_cache=FakeSnapshotCache(),
            request_budget=FakeBudget(),
            clock=lambda: NOW,
            run_lock=lock,
        )

    release.set()
    await first


@pytest.mark.asyncio
async def test_job_has_a_bounded_total_timeout() -> None:
    never = asyncio.Event()
    with pytest.raises(ProviderCacheMaintenanceTimeout):
        await run_provider_cache_job(
            fantasypros_provider=FakeFantasyProsProvider(wait=never),
            sleeper_provider=FakeSleeperProvider(),
            snapshot_cache=FakeSnapshotCache(),
            request_budget=FakeBudget(),
            clock=lambda: NOW,
            run_lock=asyncio.Lock(),
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_default_provider_resolution_failure_releases_run_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = asyncio.Lock()
    sleeper = FakeSleeperProvider()
    monkeypatch.delattr(
        live_draft_recommendation_service,
        "_FANTASYPROS_PROVIDER",
    )
    with pytest.raises(AttributeError):
        await run_provider_cache_job(
            sleeper_provider=sleeper,
            snapshot_cache=FakeSnapshotCache(),
            request_budget=FakeBudget(),
            clock=lambda: NOW,
            run_lock=lock,
        )
    assert lock.locked() is False

    fantasypros = FakeFantasyProsProvider()
    result = await run_provider_cache_job(
        fantasypros_provider=fantasypros,
        sleeper_provider=sleeper,
        snapshot_cache=WarmSnapshotCache(),
        request_budget=FakeBudget(),
        clock=lambda: NOW,
        run_lock=lock,
    )
    assert result["status"] == "success"
