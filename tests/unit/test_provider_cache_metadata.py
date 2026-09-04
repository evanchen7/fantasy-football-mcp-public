"""Tests for privacy-safe, read-only provider cache metadata."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.services.fantasypros_request_budget import FantasyProsDailyRequestBudget
from src.services.fantasypros_snapshot_cache import (
    FantasyProsSnapshot,
    FantasyProsSnapshotCache,
    FantasyProsSnapshotKey,
)


def test_snapshot_metadata_counts_normalized_rows_without_exposing_them(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "provider-snapshots.sqlite3"
    fetched_at = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    cache = FantasyProsSnapshotCache(path=path)
    cache.save(
        FantasyProsSnapshotKey(
            "sleeper_players",
            "active",
            record_limit=10_000,
        ),
        FantasyProsSnapshot(
            records=(
                {
                    "sleeperId": "101",
                    "name": "Private Player Name",
                    "position": "RB",
                    "team": "SF",
                    "yearsExperience": 2,
                    "yahooId": "987654",
                },
            ),
            fetched_at=fetched_at,
            truncated=False,
            returned_count=5_000,
            reported_count=5_000,
            reported_limit=None,
            public_api_limited=False,
        ),
    )

    metadata = cache.metadata(now=fetched_at + timedelta(hours=1))

    assert metadata == {
        "status": "available",
        "sizeBytes": path.stat().st_size,
        "snapshotCount": 1,
        "recordCount": 1,
        "latestFetchedAt": "2026-09-03T12:00:00Z",
        "snapshots": [
            {
                "endpoint": "sleeper_players",
                "variant": "active",
                "season": None,
                "week": None,
                "fetchedAt": "2026-09-03T12:00:00Z",
                "recordCount": 1,
                "returnedCount": 5_000,
                "reportedCount": 5_000,
                "truncated": False,
                "publicApiLimited": False,
            }
        ],
    }
    encoded = json.dumps(metadata)
    assert "Private Player Name" not in encoded
    assert "987654" not in encoded
    assert str(path) not in encoded


def test_snapshot_metadata_missing_is_side_effect_free(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "provider-snapshots.sqlite3"

    metadata = FantasyProsSnapshotCache(path=path).metadata(
        now=datetime(2026, 9, 3, tzinfo=timezone.utc)
    )

    assert metadata == {
        "status": "missing",
        "sizeBytes": None,
        "snapshotCount": 0,
        "recordCount": 0,
        "latestFetchedAt": None,
        "snapshots": [],
    }
    assert not path.parent.exists()


def test_snapshot_metadata_corrupt_is_graceful_and_does_not_echo_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared" / "provider-snapshots.sqlite3"
    path.parent.mkdir()
    private_detail = b"not sqlite: token=private-secret /Users/private/path"
    path.write_bytes(private_detail)

    metadata = FantasyProsSnapshotCache(path=path).metadata(
        now=datetime(2026, 9, 3, tzinfo=timezone.utc)
    )

    assert metadata == {
        "status": "unavailable",
        "sizeBytes": None,
        "snapshotCount": 0,
        "recordCount": 0,
        "latestFetchedAt": None,
        "snapshots": [],
    }
    assert path.read_bytes() == private_detail
    assert "private-secret" not in repr(metadata)
    assert str(path) not in repr(metadata)


def test_snapshot_metadata_rejects_broken_symlink_hardlink_and_oversize_file(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    broken = tmp_path / "broken.sqlite3"
    broken.symlink_to(tmp_path / "missing-target.sqlite3")
    assert FantasyProsSnapshotCache(path=broken).metadata(now=now)["status"] == (
        "unavailable"
    )

    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"private")
    hardlink = tmp_path / "hardlink.sqlite3"
    os.link(source, hardlink)
    assert FantasyProsSnapshotCache(path=hardlink).metadata(now=now)["status"] == (
        "unavailable"
    )

    oversize = tmp_path / "oversize.sqlite3"
    with oversize.open("wb") as handle:
        handle.truncate(16_777_217)
    assert FantasyProsSnapshotCache(path=oversize).metadata(now=now)["status"] == (
        "unavailable"
    )


def test_snapshot_metadata_rejects_wrong_schema_and_locked_database(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    wrong_schema = tmp_path / "wrong-schema.sqlite3"
    with sqlite3.connect(wrong_schema) as connection:
        connection.execute("PRAGMA user_version = 99")
    assert FantasyProsSnapshotCache(path=wrong_schema).metadata(now=now)["status"] == (
        "unavailable"
    )

    locked_path = tmp_path / "locked.sqlite3"
    cache = FantasyProsSnapshotCache(path=locked_path)
    cache.save(
        FantasyProsSnapshotKey("players", "catalog", record_limit=5_000),
        FantasyProsSnapshot(
            records=(),
            fetched_at=now,
            truncated=False,
            returned_count=0,
            reported_count=0,
            reported_limit=None,
            public_api_limited=False,
        ),
    )
    with sqlite3.connect(locked_path) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        assert FantasyProsSnapshotCache(path=locked_path).metadata(now=now) == {
            "status": "unavailable",
            "sizeBytes": None,
            "snapshotCount": 0,
            "recordCount": 0,
            "latestFetchedAt": None,
            "snapshots": [],
        }


def test_budget_metadata_reports_today_usage_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "private" / "fantasypros-request-budget.json"
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    budget = FantasyProsDailyRequestBudget(path=path, daily_limit=10)
    budget.reserve(now)
    before = path.read_bytes()

    metadata = budget.metadata(now=now + timedelta(hours=1))

    assert metadata == {
        "status": "available",
        "utcDate": "2026-09-03",
        "used": 1,
        "remaining": 9,
        "limit": 10,
    }
    assert path.read_bytes() == before


def test_budget_metadata_treats_an_old_day_as_zero_without_rewriting(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "fantasypros-request-budget.json"
    old_day = datetime(2026, 9, 2, 23, tzinfo=timezone.utc)
    budget = FantasyProsDailyRequestBudget(path=path, daily_limit=10)
    budget.reserve(old_day)
    before = path.read_bytes()

    metadata = budget.metadata(now=old_day + timedelta(hours=2))

    assert metadata == {
        "status": "available",
        "utcDate": "2026-09-03",
        "used": 0,
        "remaining": 10,
        "limit": 10,
    }
    assert path.read_bytes() == before


def test_budget_metadata_missing_and_corrupt_are_graceful_and_side_effect_free(
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "absent" / "fantasypros-request-budget.json"
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    missing = FantasyProsDailyRequestBudget(path=missing_path, daily_limit=10).metadata(
        now=now
    )
    assert missing == {
        "status": "missing",
        "utcDate": "2026-09-03",
        "used": 0,
        "remaining": 10,
        "limit": 10,
    }
    assert not missing_path.parent.exists()

    corrupt_path = tmp_path / "shared" / "fantasypros-request-budget.json"
    corrupt_path.parent.mkdir()
    corrupt_path.write_text(
        '{"schemaVersion":1,"utcDate":"2026-09-03","requestCount":1,'
        '"token":"private-secret"}',
        encoding="utf-8",
    )
    before = corrupt_path.read_bytes()
    corrupt = FantasyProsDailyRequestBudget(path=corrupt_path, daily_limit=10).metadata(
        now=now
    )
    assert corrupt == {
        "status": "unavailable",
        "utcDate": "2026-09-03",
        "used": None,
        "remaining": None,
        "limit": 10,
    }
    assert corrupt_path.read_bytes() == before
    assert "private-secret" not in repr(corrupt)


def test_budget_metadata_rejects_broken_symlink_and_hardlink(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    broken = tmp_path / "broken-budget.json"
    broken.symlink_to(tmp_path / "missing-budget.json")
    broken_result = FantasyProsDailyRequestBudget(path=broken).metadata(now=now)
    assert broken_result["status"] == "unavailable"

    source = tmp_path / "source-budget.json"
    source.write_text(
        '{"schemaVersion":1,"utcDate":"2026-09-03","requestCount":1}',
        encoding="utf-8",
    )
    hardlink = tmp_path / "hardlink-budget.json"
    os.link(source, hardlink)
    hardlink_result = FantasyProsDailyRequestBudget(path=hardlink).metadata(now=now)
    assert hardlink_result["status"] == "unavailable"
