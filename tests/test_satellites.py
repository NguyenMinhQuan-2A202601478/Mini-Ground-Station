"""Per-satellite operating limits.

The table earns its place here. A battery that has aged is not a battery that
is failing, and the floor that separates the two belongs to the spacecraft
rather than to the station listening to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mgs.api.routes.dashboard import list_satellites, summary, update_satellite
from mgs.ingest import ingest_frame
from mgs.limits import resolve, station_defaults
from mgs.models import Satellite
from mgs.schemas import SatelliteUpdate, TelemetryIn
from mgs.worker.screener import screen_once

pytestmark = pytest.mark.integration

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def send(session, seq: int, satellite_id: str = "TEST-1", **overrides) -> None:
    payload = dict(
        satellite_id=satellite_id,
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


def alerts_for(session, satellite_id: str):
    from sqlalchemy import select

    from mgs.models import Alert

    return list(session.scalars(select(Alert).where(Alert.satellite_id == satellite_id)))


# --- resolving --------------------------------------------------------------


def test_no_satellite_at_all_falls_back_to_the_station(settings):
    assert resolve(None, settings) == station_defaults(settings)


def test_an_unedited_satellite_behaves_exactly_as_before(settings):
    """The migration must not change how anything is screened."""
    assert resolve(Satellite(satellite_id="X"), settings) == station_defaults(settings)


def test_one_override_leaves_the_others_at_the_station_default(settings):
    limits = resolve(Satellite(satellite_id="X", battery_min_v=6.0), settings)

    assert limits.battery_min_v == 6.0
    assert limits.battery_critical_v == settings.battery_critical_v
    assert limits.temp_max_c == settings.temp_max_c


# --- screening --------------------------------------------------------------


def test_a_satellite_is_screened_against_its_own_floor(session, settings):
    """The same voltage, two spacecraft, two verdicts."""
    for seq in range(1, 6):
        send(session, seq, "TEST-1", battery_voltage_v=6.5)
        send(session, 100 + seq, "TEST-2", battery_voltage_v=6.5)
    session.flush()
    # TEST-2 is an old spacecraft; 6.5 V is normal for it now.
    session.get(Satellite, "TEST-2").battery_min_v = 6.2
    session.get(Satellite, "TEST-2").battery_critical_v = 6.0
    session.commit()

    screen_once(session, settings)

    assert [a.rule for a in alerts_for(session, "TEST-1")] == ["BATTERY_LOW"]
    assert alerts_for(session, "TEST-2") == [], "6.5 V is within this spacecraft's limits"


def test_raising_a_limit_makes_previously_normal_telemetry_alert(session, settings):
    for seq in range(1, 6):
        send(session, seq, battery_voltage_v=7.2)
    session.flush()
    session.get(Satellite, "TEST-1").battery_min_v = 7.5
    session.commit()

    screen_once(session, settings)
    assert [a.rule for a in alerts_for(session, "TEST-1")] == ["BATTERY_LOW"]


def test_temperature_overrides_are_honoured_too(session, settings):
    for seq in range(1, 6):
        send(session, seq, temperature_c=45.0)
    session.flush()
    session.get(Satellite, "TEST-1").temp_max_c = 40.0
    session.commit()

    screen_once(session, settings)
    assert [a.rule for a in alerts_for(session, "TEST-1")] == ["TEMP_HIGH"]


# --- the API ----------------------------------------------------------------


def test_summary_reports_the_limits_that_apply_to_that_satellite(session, settings):
    """The dashboard draws its threshold lines from this."""
    send(session, 1)
    session.flush()
    session.get(Satellite, "TEST-1").battery_min_v = 6.1
    session.commit()

    scoped = summary(session, satellite_id="TEST-1", settings=settings)
    station_wide = summary(session, satellite_id=None, settings=settings)

    assert scoped.limits.battery_min_v == 6.1
    assert station_wide.limits.battery_min_v == settings.battery_min_v


def test_an_operator_can_set_and_clear_an_override(session, settings):
    send(session, 1)
    session.commit()

    updated = update_satellite(
        "TEST-1",
        SatelliteUpdate(name="Test Bird", operator="VinSpace", battery_min_v=6.1),
        session=session,
        settings=settings,
    )
    assert (updated.name, updated.operator) == ("Test Bird", "VinSpace")
    assert updated.limits.battery_min_v == 6.1

    reverted = update_satellite(
        "TEST-1", SatelliteUpdate(clear=["battery_min_v"]), session=session, settings=settings
    )
    assert reverted.limits.battery_min_v == settings.battery_min_v
    assert reverted.name == "Test Bird", "clearing a limit must not wipe the identity"


def test_editing_an_unknown_satellite_is_a_404(session, settings):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        update_satellite("NOBODY", SatelliteUpdate(), session=session, settings=settings)
    assert raised.value.status_code == 404


def test_an_inverted_limit_pair_is_refused(session, settings):
    """A critical floor above the nominal minimum is a rule nothing can satisfy."""
    from fastapi import HTTPException

    send(session, 1)
    session.commit()

    with pytest.raises(HTTPException) as raised:
        update_satellite(
            "TEST-1",
            SatelliteUpdate(battery_min_v=6.0, battery_critical_v=7.0),
            session=session,
            settings=settings,
        )
    assert raised.value.status_code == 422


def test_the_listing_carries_counters_and_effective_limits(session, settings):
    for seq in range(1, 4):
        send(session, seq, battery_voltage_v=6.0)
    session.commit()
    screen_once(session, settings)

    row = list_satellites(session, settings)[0]
    assert row.frame_count == 3
    assert row.pass_count == 1
    assert row.open_alerts >= 1
    assert row.limits.battery_min_v == settings.battery_min_v
