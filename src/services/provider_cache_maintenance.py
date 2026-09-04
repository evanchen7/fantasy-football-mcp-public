"""Bounded, metadata-only inspection and warming for provider snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.services.fantasypros_request_budget import (
    DEFAULT_DAILY_REQUEST_LIMIT,
    FantasyProsDailyRequestBudget,
)
from src.services.fantasypros_snapshot_cache import (
    FantasyProsSnapshotCache,
)

_MAX_DATABASE_BYTES = 16_777_216
_MAX_SNAPSHOTS = 16
_MAX_TOTAL_RECORDS = 100_000
_RUN_TIMEOUT_SECONDS = 45.0
_SCORING = frozenset({"STD", "HALF", "PPR"})
_ENDPOINT_VARIANTS = {
    "players": frozenset({"catalog", "catalog-season"}),
    "injuries": frozenset({"weekly"}),
    "news": frozenset({"recent"}),
    "projections": frozenset(
        {"preseason-std", "preseason-half", "preseason-ppr"}
    ),
    "adp": frozenset({"preseason-std", "preseason-half", "preseason-ppr"}),
    "sleeper_players": frozenset({"active"}),
}
_RECORD_LIMITS = {
    "players": 5_000,
    "injuries": 2_000,
    "news": 100,
    "projections": 5_000,
    "adp": 5_000,
    "sleeper_players": 10_000,
}
_TTL_SECONDS = {
    "players": 86_400.0,
    "injuries": 300.0,
    "news": 300.0,
    "projections": 86_400.0,
    "adp": 86_400.0,
    "sleeper_players": 86_400.0,
}
_FANTASYPROS_DATASETS = ("players", "injuries", "news", "projections", "adp")
_RUN_LOCK = asyncio.Lock()


class ProviderCacheMaintenanceBusy(RuntimeError):
    """Raised when one cache-maintenance run already owns the process lock."""

    def __init__(self) -> None:
        super().__init__("Provider cache maintenance is already running")


class ProviderCacheMaintenanceTimeout(RuntimeError):
    """Raised when the bounded cache-maintenance deadline expires."""

    def __init__(self) -> None:
        super().__init__("Provider cache maintenance timed out")


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not 20 <= len(value) <= 30 or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.astimezone(timezone.utc).year not in range(2000, 2101):
        return None
    return parsed.astimezone(timezone.utc)


def _empty_cache(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "sizeBytes": None,
        "snapshotCount": 0,
        "recordCount": 0,
        "latestFetchedAt": None,
        "snapshots": [],
    }


def _sanitize_cache_metadata(raw: Any, now: datetime) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _empty_cache("unavailable")
    status = raw.get("status")
    if status == "missing":
        return _empty_cache("missing")
    if status != "available":
        return _empty_cache("unavailable")
    size_bytes = raw.get("sizeBytes")
    rows = raw.get("snapshots")
    if (
        type(size_bytes) is not int
        or not 0 < size_bytes <= _MAX_DATABASE_BYTES
        or not isinstance(rows, list)
        or len(rows) > _MAX_SNAPSHOTS
    ):
        return _empty_cache("unavailable")

    snapshots: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return _empty_cache("unavailable")
        endpoint = row.get("endpoint")
        variant = row.get("variant")
        season = row.get("season")
        week = row.get("week")
        fetched_at = _parse_iso(row.get("fetchedAt"))
        record_count = row.get("recordCount")
        returned_count = row.get("returnedCount")
        reported_count = row.get("reportedCount")
        truncated = row.get("truncated")
        public_api_limited = row.get("publicApiLimited")
        scope_valid = False
        if endpoint == "players" and variant == "catalog":
            scope_valid = season is None and week is None
        elif endpoint == "players" and variant == "catalog-season":
            scope_valid = type(season) is int and week == 0
        elif endpoint == "injuries":
            scope_valid = type(season) is int and type(week) is int
        elif endpoint in {"projections", "adp"}:
            scope_valid = type(season) is int and week == 0
        elif endpoint in {"news", "sleeper_players"}:
            scope_valid = season is None and week is None
        if (
            endpoint not in _ENDPOINT_VARIANTS
            or variant not in _ENDPOINT_VARIANTS[endpoint]
            or fetched_at is None
            or type(record_count) is not int
            or not 0 <= record_count <= _RECORD_LIMITS[endpoint]
            or type(returned_count) is not int
            or not 0 <= returned_count <= 100_000
            or type(truncated) is not bool
            or type(public_api_limited) is not bool
            or (
                reported_count is not None
                and (
                    type(reported_count) is not int
                    or not 0 <= reported_count <= 10_000_000
                )
            )
            or (season is not None and (type(season) is not int or not 2012 <= season <= 2100))
            or (week is not None and (type(week) is not int or not 0 <= week <= 25))
            or not scope_valid
        ):
            return _empty_cache("unavailable")
        partial_catalog = (
            endpoint == "players"
            and reported_count is not None
            and reported_count > returned_count
        )
        ttl_seconds = min(_TTL_SECONDS[endpoint], 300.0) if partial_catalog else _TTL_SECONDS[endpoint]
        age = (now - fetched_at).total_seconds()
        snapshots.append(
            {
                "provider": "Sleeper" if endpoint == "sleeper_players" else "FantasyPros",
                "dataset": endpoint,
                "variant": variant,
                "season": season,
                "week": week,
                "fetchedAt": _iso(fetched_at),
                "recordCount": record_count,
                "stale": age < 0.0 or age >= ttl_seconds,
            }
        )
    snapshots.sort(
        key=lambda item: _parse_iso(item["fetchedAt"])
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    total_records = sum(item["recordCount"] for item in snapshots)
    if total_records > _MAX_TOTAL_RECORDS:
        return _empty_cache("unavailable")
    return {
        "status": "available",
        "sizeBytes": size_bytes,
        "snapshotCount": len(snapshots),
        "recordCount": total_records,
        "latestFetchedAt": snapshots[0]["fetchedAt"] if snapshots else None,
        "snapshots": snapshots,
    }


def _sanitize_budget_metadata(raw: Any, today: datetime) -> dict[str, Any]:
    fallback = {
        "status": "unavailable",
        "utcDate": today.date().isoformat(),
        "used": None,
        "remaining": None,
        "limit": DEFAULT_DAILY_REQUEST_LIMIT,
    }
    if not isinstance(raw, Mapping):
        return fallback
    status = raw.get("status")
    utc_date = raw.get("utcDate")
    used = raw.get("used")
    remaining = raw.get("remaining")
    limit = raw.get("limit")
    if (
        status not in {"available", "missing"}
        or utc_date != today.date().isoformat()
        or type(limit) is not int
        or not 1 <= limit <= DEFAULT_DAILY_REQUEST_LIMIT
        or type(used) is not int
        or type(remaining) is not int
        or not 0 <= used <= limit
        or remaining != limit - used
        or (status == "missing" and used != 0)
    ):
        return fallback
    return {
        "status": status,
        "utcDate": utc_date,
        "used": used,
        "remaining": remaining,
        "limit": limit,
    }


def get_provider_cache_stats(
    *,
    snapshot_cache: Any | None = None,
    request_budget: Any | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Return metadata-only cache and request-budget statistics."""

    now = _utc_now(clock or (lambda: datetime.now(timezone.utc)))
    cache = snapshot_cache if snapshot_cache is not None else FantasyProsSnapshotCache()
    budget = request_budget if request_budget is not None else FantasyProsDailyRequestBudget()
    try:
        cache_metadata = cache.metadata(now=now)
    except Exception:
        cache_metadata = None
    try:
        budget_metadata = budget.metadata(now=now)
    except Exception:
        budget_metadata = None
    safe_cache = _sanitize_cache_metadata(cache_metadata, now)
    safe_budget = _sanitize_budget_metadata(budget_metadata, now)
    return {
        "schemaVersion": 1,
        "status": (
            "success"
            if safe_cache["status"] == "available"
            and safe_budget["status"] in {"available", "missing"}
            else "degraded"
        ),
        "cache": safe_cache,
        "fantasyProsBudget": safe_budget,
    }


def _unavailable_dataset() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "recordCount": 0,
        "fetchedAt": None,
        "stale": False,
        "refreshFailed": False,
        "publicApiLimited": False,
    }


def _sanitize_dataset(
    raw: Any,
    maximum: int,
    *,
    require_records: bool,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _unavailable_dataset()
    status = raw.get("status")
    record_count = raw.get("recordCount")
    fetched_at = _parse_iso(raw.get("fetchedAt"))
    stale = raw.get("stale")
    refresh_failed = raw.get("refreshFailed")
    public_limited = raw.get("publicApiLimited")
    if (
        status not in {"available", "unavailable"}
        or type(record_count) is not int
        or not 0 <= record_count <= maximum
        or type(stale) is not bool
        or type(refresh_failed) is not bool
        or type(public_limited) is not bool
        or (status == "available") != (fetched_at is not None)
        or (status == "unavailable" and record_count != 0)
        or (status == "unavailable" and stale is True)
        or (require_records and status == "available" and record_count == 0)
    ):
        return _unavailable_dataset()
    return {
        "status": status,
        "recordCount": record_count,
        "fetchedAt": _iso(fetched_at) if fetched_at is not None else None,
        "stale": stale,
        "refreshFailed": refresh_failed,
        "publicApiLimited": public_limited,
    }


def _sanitize_fantasypros_result(raw: Any) -> dict[str, Any]:
    raw_datasets = raw.get("datasets") if isinstance(raw, Mapping) else None
    datasets = {
        name: _sanitize_dataset(
            raw_datasets.get(name) if isinstance(raw_datasets, Mapping) else None,
            _RECORD_LIMITS[name],
            require_records=name in {"players", "projections", "adp"},
        )
        for name in _FANTASYPROS_DATASETS
    }
    available = sum(item["status"] == "available" for item in datasets.values())
    degraded = any(
        item["status"] != "available"
        or item["stale"] is True
        or item["refreshFailed"] is True
        for item in datasets.values()
    )
    return {
        "status": (
            "unavailable" if available == 0 else "degraded" if degraded else "success"
        ),
        "datasets": datasets,
    }


def _sanitize_sleeper_result(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raw = {}
    status = raw.get("status")
    record_count = raw.get("catalogPlayers")
    fetched_at = _parse_iso(raw.get("catalogFetchedAt"))
    stale = raw.get("cacheStale")
    refresh_failed = raw.get("refreshFailed")
    if (
        status not in {"success", "degraded", "unavailable"}
        or type(record_count) is not int
        or not 0 <= record_count <= _RECORD_LIMITS["sleeper_players"]
        or type(stale) is not bool
        or type(refresh_failed) is not bool
        or (record_count > 0) != (fetched_at is not None)
    ):
        return {
            "status": "unavailable",
            "recordCount": 0,
            "fetchedAt": None,
            "stale": False,
            "refreshFailed": False,
        }
    safe_status = (
        "unavailable"
        if record_count == 0
        else "degraded"
        if stale or refresh_failed
        else "success"
    )
    return {
        "status": safe_status,
        "recordCount": record_count,
        "fetchedAt": _iso(fetched_at) if fetched_at is not None else None,
        "stale": stale,
        "refreshFailed": refresh_failed,
    }


def _same_snapshot_second(left: Any, right: Any) -> bool:
    left_time = _parse_iso(left)
    right_time = _parse_iso(right)
    return (
        left_time is not None
        and right_time is not None
        and left_time.replace(microsecond=0) == right_time.replace(microsecond=0)
    )


def _selected_snapshots_persisted(
    stats: Mapping[str, Any],
    *,
    scoring: str,
    season: int,
    fantasypros_result: Mapping[str, Any],
    sleeper_result: Mapping[str, Any],
) -> bool:
    cache = stats.get("cache")
    if not isinstance(cache, Mapping) or cache.get("status") != "available":
        return False
    raw_snapshots = cache.get("snapshots")
    if not isinstance(raw_snapshots, list):
        return False

    def persisted(
        *,
        provider: str,
        dataset: str,
        variant: str,
        snapshot_season: int | None,
        week: int | None,
        result: Mapping[str, Any],
    ) -> bool:
        return any(
            isinstance(row, Mapping)
            and row.get("provider") == provider
            and row.get("dataset") == dataset
            and row.get("variant") == variant
            and row.get("season") == snapshot_season
            and row.get("week") == week
            and row.get("recordCount") == result.get("recordCount")
            and row.get("stale") is False
            and _same_snapshot_second(row.get("fetchedAt"), result.get("fetchedAt"))
            for row in raw_snapshots
        )

    raw_datasets = fantasypros_result.get("datasets")
    if not isinstance(raw_datasets, Mapping):
        return False
    datasets = {
        name: raw_datasets.get(name)
        for name in _FANTASYPROS_DATASETS
    }
    if not all(isinstance(item, Mapping) for item in datasets.values()):
        return False

    players = datasets["players"]
    injuries = datasets["injuries"]
    news = datasets["news"]
    projections = datasets["projections"]
    adp = datasets["adp"]
    if not all(
        (
            persisted(
                provider="FantasyPros",
                dataset="players",
                variant="catalog" if scoring == "HALF" else "catalog-season",
                snapshot_season=None if scoring == "HALF" else season,
                week=None if scoring == "HALF" else 0,
                result=players,
            ),
            persisted(
                provider="FantasyPros",
                dataset="injuries",
                variant="weekly",
                snapshot_season=season,
                week=0,
                result=injuries,
            ),
            persisted(
                provider="FantasyPros",
                dataset="news",
                variant="recent",
                snapshot_season=None,
                week=None,
                result=news,
            ),
            persisted(
                provider="FantasyPros",
                dataset="projections",
                variant=f"preseason-{scoring.lower()}",
                snapshot_season=season,
                week=0,
                result=projections,
            ),
            persisted(
                provider="Sleeper",
                dataset="sleeper_players",
                variant="active",
                snapshot_season=None,
                week=None,
                result=sleeper_result,
            ),
        )
    ):
        return False
    if scoring == "HALF":
        return persisted(
            provider="FantasyPros",
            dataset="adp",
            variant="preseason-half",
            snapshot_season=season,
            week=0,
            result=adp,
        )
    return (
        _same_snapshot_second(adp.get("fetchedAt"), players.get("fetchedAt"))
        and type(adp.get("recordCount")) is int
        and type(players.get("recordCount")) is int
        and 0 < adp["recordCount"] <= players["recordCount"]
    )


async def run_provider_cache_job(
    scoring: str = "HALF",
    *,
    fantasypros_provider: Any | None = None,
    sleeper_provider: Any | None = None,
    snapshot_cache: Any | None = None,
    request_budget: Any | None = None,
    clock: Callable[[], datetime] | None = None,
    run_lock: asyncio.Lock | None = None,
    timeout_seconds: float = _RUN_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Warm FantasyPros then Sleeper through their shared live-service providers."""

    normalized_scoring = scoring.strip().upper() if isinstance(scoring, str) else ""
    if normalized_scoring not in _SCORING:
        raise ValueError("scoring must be STD, HALF, or PPR")
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = _utc_now(clock)
    lock = run_lock or _RUN_LOCK
    if lock.locked():
        raise ProviderCacheMaintenanceBusy
    await lock.acquire()
    try:
        if fantasypros_provider is None or sleeper_provider is None:
            from src.services import live_draft_recommendation_service

            if fantasypros_provider is None:
                fantasypros_provider = (
                    live_draft_recommendation_service._FANTASYPROS_PROVIDER
                )
            if sleeper_provider is None:
                sleeper_provider = (
                    live_draft_recommendation_service._SLEEPER_PLAYER_PROVIDER
                )

        async def execute() -> dict[str, Any]:
            try:
                fantasypros_raw = await fantasypros_provider.warm_cache(
                    year=now.year,
                    scoring=normalized_scoring,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                fantasypros_raw = None
            try:
                sleeper_raw = await sleeper_provider.warm_player_cache()
            except asyncio.CancelledError:
                raise
            except Exception:
                sleeper_raw = None
            completed_at = _utc_now(clock)
            if completed_at < now:
                completed_at = now
            stats = await asyncio.to_thread(
                get_provider_cache_stats,
                snapshot_cache=snapshot_cache,
                request_budget=request_budget,
                clock=lambda: completed_at,
            )
            fantasypros_result = _sanitize_fantasypros_result(fantasypros_raw)
            sleeper_result = _sanitize_sleeper_result(sleeper_raw)
            persisted = _selected_snapshots_persisted(
                stats,
                scoring=normalized_scoring,
                season=now.year,
                fantasypros_result=fantasypros_result,
                sleeper_result=sleeper_result,
            )
            return {
                "schemaVersion": 1,
                "status": (
                    "success"
                    if fantasypros_result["status"]
                    == sleeper_result["status"]
                    == "success"
                    and persisted
                    else "degraded"
                ),
                "scoring": normalized_scoring,
                "season": now.year,
                "startedAt": _iso(now),
                "completedAt": _iso(completed_at),
                "providers": {
                    "fantasyPros": fantasypros_result,
                    "sleeper": sleeper_result,
                },
                "stats": stats,
            }

        timeout = max(0.01, min(float(timeout_seconds), _RUN_TIMEOUT_SECONDS))
        try:
            return await asyncio.wait_for(execute(), timeout=timeout)
        except asyncio.TimeoutError as error:
            raise ProviderCacheMaintenanceTimeout from error
    finally:
        lock.release()


__all__ = [
    "ProviderCacheMaintenanceBusy",
    "ProviderCacheMaintenanceTimeout",
    "get_provider_cache_stats",
    "run_provider_cache_job",
]
