"""Retention: what may be deleted, and what may never be."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from mgs.ingest import ingest_frame
from mgs.models import Alert, Telemetry
from mgs.retention import purge
from mgs.schemas import TelemetryIn
from mgs.worker.screener import screen_once

pytestmark = pytest.mark.integration

NOW = datetime.now(UTC)


def send(session, seq: int, *, age_days: float, **overrides) -> None:
    payload = dict(
        satellite_id="TEST-1",
        ground_station_id="TEST-GS",
        seq=seq,
        recorded_at=NOW - timedelta(days=age_days),
        battery_voltage_v=7.8,
        temperature_c=20.0,
        lat_deg=10.0,
        lon_deg=100.0,
        alt_km=420.0,
    )
    ingest_frame(session, TelemetryIn(**{**payload, **overrides}))


def keeping(settings, days: int):
    return settings.model_copy(update={"retention_days": days})


def count(session) -> int:
    return session.scalar(select(func.count()).select_from(Telemetry))


def test_retention_is_off_until_someone_chooses_a_policy(session, settings):
    """Silently deleting a mission's telemetry by default would be indefensible."""
    with pytest.raises(ValueError, match="disabled"):
        purge(session, settings)


def test_old_screened_frames_are_deleted(session, settings):
    for seq in range(1, 11):
        send(session, seq, age_days=100)
    session.commit()
    screen_once(session, settings)

    result = purge(session, keeping(settings, 30))
    assert result.deleted == 10
    assert count(session) == 0


def test_recent_frames_are_left_alone(session, settings):
    for seq in range(1, 11):
        send(session, seq, age_days=1)
    session.commit()
    screen_once(session, settings)

    assert purge(session, keeping(settings, 30)).deleted == 0
    assert count(session) == 10


def test_a_frame_an_alert_cites_is_evidence_and_survives(session, settings):
    """The whole point of the retention rule.

    An alert that says "battery at 6.2 V on frame 4200" is worthless if frame
    4200 has been swept away; the foreign key is `ON DELETE RESTRICT` for the
    same reason.
    """
    for seq in range(1, 11):
        send(session, seq, age_days=100, battery_voltage_v=6.2 if seq == 5 else 7.8)
    session.commit()
    screen_once(session, settings)

    result = purge(session, keeping(settings, 30))
    assert result.kept_as_evidence == 1
    cited = session.scalars(select(Alert.telemetry_id)).all()
    survivors = session.scalars(select(Telemetry.id)).all()
    assert set(cited) <= set(survivors)


def test_unscreened_frames_are_never_deleted(session, settings):
    """Deleting work in progress loses telemetry nobody ever looked at."""
    for seq in range(1, 11):
        send(session, seq, age_days=100)
    session.commit()

    assert purge(session, keeping(settings, 30)).deleted == 0
    assert count(session) == 10


def test_a_dry_run_reports_without_deleting(session, settings):
    for seq in range(1, 11):
        send(session, seq, age_days=100)
    session.commit()
    screen_once(session, settings)

    result = purge(session, keeping(settings, 30), dry_run=True)
    assert result.deleted == 10
    assert count(session) == 10, "a dry run must not touch anything"


def test_purging_is_safe_to_repeat(session, settings):
    for seq in range(1, 11):
        send(session, seq, age_days=100)
    session.commit()
    screen_once(session, settings)

    first = purge(session, keeping(settings, 30))
    second = purge(session, keeping(settings, 30))
    assert (first.deleted, second.deleted) == (10, 0)


def test_a_purge_larger_than_one_batch_completes(session, settings):
    small = keeping(settings, 30).model_copy(update={"retention_batch_size": 3})
    for seq in range(1, 21):
        send(session, seq, age_days=100)
    session.commit()
    screen_once(session, small)

    assert purge(session, small).deleted == 20
    assert count(session) == 0


def test_the_cutoff_follows_the_policy(session, settings):
    for seq, age in enumerate([1, 10, 40, 100], start=1):
        send(session, seq, age_days=age)
    session.commit()
    screen_once(session, settings)

    assert purge(session, keeping(settings, 30)).deleted == 2  # the 40- and 100-day frames
    assert count(session) == 2


def test_passes_and_alerts_are_not_swept_with_the_telemetry(session, settings):
    """The audit trail outlives the raw frames it summarises."""
    from mgs.models import Pass

    for seq in range(1, 11):
        send(session, seq, age_days=100, battery_voltage_v=6.2 if seq == 5 else 7.8)
    session.commit()
    screen_once(session, settings)
    alerts_before = session.scalar(select(func.count()).select_from(Alert))

    purge(session, keeping(settings, 30))

    assert session.scalar(select(func.count()).select_from(Alert)) == alerts_before
    assert session.scalar(select(func.count()).select_from(Pass)) == 1
