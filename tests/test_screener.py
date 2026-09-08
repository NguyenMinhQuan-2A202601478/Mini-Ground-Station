"""Screening behaviour: episodes, not one alert per frame."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from mgs.ingest import ingest_frame
from mgs.models import Alert
from mgs.schemas import TelemetryIn
from mgs.worker.screener import screen_once

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def send(session, seq: int, **overrides) -> None:
    payload = dict(
        satellite_id="TEST-1",
        ground_station_id="TEST-GS",
        seq=seq,
        recorded_at=BASE + timedelta(seconds=seq),
        battery_voltage_v=7.8,
        temperature_c=20.0,
        lat_deg=10.0,
        lon_deg=100.0,
        alt_km=420.0,
    )
    ingest_frame(session, TelemetryIn(**{**payload, **overrides}))


def alerts(session, rule: str | None = None) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.id)
    if rule:
        stmt = stmt.where(Alert.rule == rule)
    return list(session.scalars(stmt))


def test_healthy_telemetry_raises_nothing(session, settings):
    for seq in range(1, 21):
        send(session, seq)
    session.commit()

    screened, raised = screen_once(session, settings)
    assert (screened, raised) == (20, 0)


def test_a_sustained_low_battery_is_one_alert(session, settings):
    for seq in range(1, 51):
        send(session, seq, battery_voltage_v=6.5)
    session.commit()
    screen_once(session, settings)

    low = alerts(session, "BATTERY_LOW")
    assert len(low) == 1, "a continuing condition must not re-alert every frame"
    assert low[0].resolved_at is None


def test_an_episode_resolves_when_the_condition_clears(session, settings):
    for seq in range(1, 11):
        send(session, seq, battery_voltage_v=6.5)
    for seq in range(11, 21):
        send(session, seq, battery_voltage_v=7.9)
    session.commit()
    screen_once(session, settings)

    low = alerts(session, "BATTERY_LOW")
    assert len(low) == 1
    assert low[0].resolved_at == BASE + timedelta(seconds=11)


def test_a_condition_that_returns_opens_a_second_alert(session, settings):
    for seq in range(1, 6):
        send(session, seq, battery_voltage_v=6.5)
    for seq in range(6, 11):
        send(session, seq, battery_voltage_v=7.9)
    for seq in range(11, 16):
        send(session, seq, battery_voltage_v=6.5)
    session.commit()
    screen_once(session, settings)

    assert len(alerts(session, "BATTERY_LOW")) == 2


def test_an_episode_escalates_in_place(session, settings):
    for seq in range(1, 6):
        send(session, seq, battery_voltage_v=6.6)  # warning
    for seq in range(6, 11):
        send(session, seq, battery_voltage_v=6.2)  # critical
    session.commit()
    screen_once(session, settings)

    low = alerts(session, "BATTERY_LOW")
    assert len(low) == 1
    assert low[0].severity == "critical"


def test_screening_is_idempotent(session, settings):
    for seq in range(1, 21):
        send(session, seq, battery_voltage_v=6.5, temperature_c=95.0)
    session.commit()

    screen_once(session, settings)
    before = len(alerts(session))
    # Re-screen the same frames, as a restarted worker replaying a batch would.
    session.execute(select(Alert))
    from mgs.models import Telemetry

    session.query(Telemetry).update({"screened_at": None})
    session.commit()
    screen_once(session, settings)

    assert len(alerts(session)) == before


def test_alerts_are_timestamped_from_the_telemetry_not_the_clock(session, settings):
    send(session, 1, battery_voltage_v=6.0)
    session.commit()
    screen_once(session, settings)

    assert alerts(session)[0].detected_at == BASE + timedelta(seconds=1)


def test_a_downlink_gap_is_reported_once_per_gap(session, settings):
    send(session, 1)
    send(session, 2)
    send(session, 9)  # frames 3-8 lost
    session.commit()
    screen_once(session, settings)

    gaps = alerts(session, "DATA_GAP")
    assert len(gaps) == 1
    assert "6 frame(s) missing" in gaps[0].message


def test_frames_are_marked_screened(session, settings):
    for seq in range(1, 6):
        send(session, seq)
    session.commit()
    screen_once(session, settings)

    assert screen_once(session, settings) == (0, 0)
