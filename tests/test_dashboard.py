"""The read-only endpoints the dashboard is built on."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mgs.api.routes.dashboard import list_satellites, summary, telemetry_series
from mgs.ingest import close_pass, current_pass, ingest_frame
from mgs.models import Alert
from mgs.schemas import TelemetryIn

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def send(session, seq: int, **overrides) -> None:
    payload = dict(
        satellite_id="TEST-1",
        ground_station_id="TEST-GS",
        seq=seq,
        recorded_at=BASE + timedelta(seconds=seq),
        battery_voltage_v=7.0 + seq / 100,
        temperature_c=20.0 + seq,
        lat_deg=10.0,
        lon_deg=100.0,
        alt_km=420.0,
        signal_strength_dbm=-100.0,
    )
    ingest_frame(session, TelemetryIn(**{**payload, **overrides}))


def test_satellites_are_listed_with_their_frame_counts(session):
    for seq in range(1, 6):
        send(session, seq)
    send(session, 99, satellite_id="TEST-2", auto_open_pass=False)
    session.commit()

    rows = list_satellites(session)
    assert [(r.satellite_id, r.frame_count) for r in rows] == [("TEST-1", 5), ("TEST-2", 1)]


def test_summary_of_an_empty_database_is_still_answerable(session, settings):
    result = summary(session, satellite_id=None, settings=settings)

    assert result.latest is None
    assert result.frame_count == 0
    assert result.open_alerts.critical == 0
    assert result.current_pass is None


def test_summary_reports_the_open_pass_and_latest_frame(session, settings):
    for seq in range(1, 11):
        send(session, seq)
    session.commit()

    result = summary(session, satellite_id="TEST-1", settings=settings)
    assert result.frame_count == 10
    assert result.unscreened == 10
    assert result.latest.seq == 10
    assert result.current_pass is not None
    assert result.last_pass is None  # nothing has closed yet


def test_summary_counts_only_unresolved_alerts(session, settings):
    send(session, 1)
    session.add_all(
        [
            Alert(
                satellite_id="TEST-1",
                rule="BATTERY_LOW",
                severity="critical",
                message="x",
                dedupe_key="a",
            ),
            Alert(
                satellite_id="TEST-1",
                rule="TEMP_HIGH",
                severity="warning",
                message="y",
                dedupe_key="b",
            ),
            Alert(
                satellite_id="TEST-1",
                rule="DATA_GAP",
                severity="warning",
                message="z",
                dedupe_key="c",
                resolved_at=BASE,
            ),
        ]
    )
    session.commit()

    counts = summary(session, satellite_id="TEST-1", settings=settings).open_alerts
    assert (counts.critical, counts.warning) == (1, 1)


def test_summary_publishes_the_worker_limits(session, settings):
    """The dashboard draws its threshold lines from these; they must be the real ones."""
    result = summary(session, satellite_id=None, settings=settings)
    assert result.limits.battery_min_v == settings.battery_min_v
    assert result.limits.temp_max_c == settings.temp_max_c


def test_summary_scopes_to_one_satellite(session, settings):
    for seq in range(1, 4):
        send(session, seq)
    close_pass(session, current_pass(session, "TEST-1"))
    send(session, 50, satellite_id="TEST-2")
    session.commit()

    assert summary(session, satellite_id="TEST-1", settings=settings).frame_count == 3
    assert summary(session, satellite_id="TEST-2", settings=settings).frame_count == 1
    assert summary(session, satellite_id=None, settings=settings).frame_count == 4


def test_series_of_no_data_is_empty_not_an_error(session):
    result = telemetry_series(session, satellite_id="NOBODY")
    assert result.points == []
    assert result.frame_count == 0


def test_series_buckets_cover_every_frame(session):
    for seq in range(1, 101):
        send(session, seq)
    session.commit()

    result = telemetry_series(session, satellite_id="TEST-1", buckets=10)
    assert result.frame_count == 100
    assert sum(p.n for p in result.points) == 100, "downsampling must not drop frames"
    assert len(result.points) <= 10


def test_series_aggregates_are_the_real_min_avg_max(session):
    for seq in range(1, 11):
        send(session, seq)
    session.commit()

    only = telemetry_series(session, satellite_id="TEST-1", buckets=1).points[0]
    assert only.n == 10
    assert only.battery_min == pytest.approx(7.01)
    assert only.battery_max == pytest.approx(7.10)
    assert only.battery_avg == pytest.approx(7.055)
    assert only.temperature_max == pytest.approx(30.0)


def test_series_points_are_ordered_in_time(session):
    for seq in range(1, 51):
        send(session, seq)
    session.commit()

    points = telemetry_series(session, satellite_id="TEST-1", buckets=8).points
    assert [p.t for p in points] == sorted(p.t for p in points)


def test_series_respects_the_time_window(session):
    for seq in range(1, 21):
        send(session, seq)
    session.commit()

    result = telemetry_series(session, satellite_id="TEST-1", since=BASE + timedelta(seconds=11))
    assert result.frame_count == 10


def test_a_single_frame_yields_one_bucket(session):
    """Start equals end, so the bucket width would be zero if it were not guarded."""
    send(session, 1)
    session.commit()

    result = telemetry_series(session, satellite_id="TEST-1", buckets=50)
    assert len(result.points) == 1
    assert result.points[0].n == 1


def test_summary_reports_the_highest_frame_number_not_the_latest_one(session, settings):
    """A backfilled frame arrives late carrying an old sequence number.

    The simulator resumes its numbering from `max_seq`; resuming from the most
    recent frame's `seq` would replay everything that came after it.
    """
    for seq in (1, 2, 3, 400):
        send(session, seq)
    # Late arrival of a frame recorded long before the others.
    send(session, 7, recorded_at=BASE + timedelta(days=1))
    session.commit()

    result = summary(session, satellite_id="TEST-1", settings=settings)
    assert result.latest.seq == 7
    assert result.max_seq == 400
