"""Ingestion against a real PostgreSQL: the constraints are the point."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from mgs.ingest import close_pass, current_pass, ingest_frame, open_pass
from mgs.models import Pass, Telemetry
from mgs.schemas import TelemetryIn

pytestmark = pytest.mark.integration


def frame(seq: int, **overrides) -> TelemetryIn:
    payload = dict(
        satellite_id="TEST-1",
        ground_station_id="TEST-GS",
        seq=seq,
        recorded_at=datetime.now(UTC) + timedelta(seconds=seq),
        battery_voltage_v=7.8,
        temperature_c=20.0,
        lat_deg=10.0,
        lon_deg=100.0,
        alt_km=420.0,
    )
    return TelemetryIn(**{**payload, **overrides})


def test_first_frame_opens_a_pass(session):
    result = ingest_frame(session, frame(1))
    assert result.pass_id is not None
    assert not result.duplicate
    assert session.get(Pass, result.pass_id).status == "active"


def test_re_posting_the_same_frame_is_a_no_op(session):
    first = ingest_frame(session, frame(1))
    second = ingest_frame(session, frame(1))

    assert second.duplicate is True
    assert second.telemetry_id == first.telemetry_id
    assert session.scalar(select(func.count()).select_from(Telemetry)) == 1


def test_a_replayed_pass_does_not_duplicate_anything(session):
    for seq in range(1, 11):
        ingest_frame(session, frame(seq))
    for seq in range(1, 11):  # the whole pass sent again after a timeout
        ingest_frame(session, frame(seq))

    assert session.scalar(select(func.count()).select_from(Telemetry)) == 10


def test_frames_attach_to_the_open_pass(session):
    first = ingest_frame(session, frame(1))
    second = ingest_frame(session, frame(2))
    assert first.pass_id == second.pass_id


def test_a_new_pass_opens_after_the_previous_one_closes(session):
    first = ingest_frame(session, frame(1))
    close_pass(session, session.get(Pass, first.pass_id))
    second = ingest_frame(session, frame(2))

    assert second.pass_id != first.pass_id


def test_only_one_pass_is_open_per_satellite(session):
    a = open_pass(session, satellite_id="TEST-1", ground_station_id="TEST-GS")
    b = open_pass(session, satellite_id="TEST-1", ground_station_id="TEST-GS")
    assert a.id == b.id


def test_frame_count_tracks_ingestion(session):
    for seq in range(1, 6):
        ingest_frame(session, frame(seq))
    ingest_frame(session, frame(3))  # duplicate must not inflate the counter

    session.flush()
    assert current_pass(session, "TEST-1").frame_count == 5


def test_out_of_pass_frames_are_kept_without_a_pass(session):
    result = ingest_frame(session, frame(1, auto_open_pass=False))
    assert result.pass_id is None
    assert session.get(Telemetry, result.telemetry_id) is not None
