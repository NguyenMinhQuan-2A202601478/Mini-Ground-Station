"""Who is allowed to change something.

Reading is open — the dashboard is a read-only view of a station's state.
Writing is not: an unauthenticated POST could inject telemetry (and so teach
the detector that an anomaly is normal), open a pass that blocks the real one,
or resolve every alert on the board.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from mgs.api.main import app
from mgs.api.security import HEADER
from mgs.config import Settings, get_settings
from mgs.db import get_session
from mgs.models import Alert

pytestmark = pytest.mark.integration

KEY = "station-key-under-test"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def frame(seq: int) -> dict:
    return {
        "satellite_id": "TEST-1",
        "ground_station_id": "TEST-GS",
        "seq": seq,
        "recorded_at": (BASE + timedelta(seconds=seq)).isoformat(),
        "battery_voltage_v": 7.8,
        "temperature_c": 20.0,
        "lat_deg": 10.0,
        "lon_deg": 100.0,
        "alt_km": 420.0,
    }


def make_client(session, settings: Settings) -> TestClient:
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture
def open_station(session, settings):
    client = make_client(session, settings.model_copy(update={"api_keys": ""}))
    yield client
    app.dependency_overrides.clear()


@pytest.fixture
def locked_station(session, settings):
    client = make_client(session, settings.model_copy(update={"api_keys": f"other,{KEY}"}))
    yield client
    app.dependency_overrides.clear()


# --- with no keys configured ---------------------------------------------


def test_without_keys_writes_are_open(open_station):
    assert open_station.post("/api/v1/telemetry", json=frame(1)).status_code == 201


def test_health_admits_when_the_station_is_open(open_station):
    assert open_station.get("/health").json()["auth"] == "disabled"


# --- with keys configured -------------------------------------------------


def test_ingest_without_a_key_is_refused(locked_station):
    response = locked_station.post("/api/v1/telemetry", json=frame(1))
    assert response.status_code == 401
    assert HEADER in response.json()["detail"]


def test_ingest_with_the_wrong_key_is_refused(locked_station):
    response = locked_station.post(
        "/api/v1/telemetry", json=frame(1), headers={HEADER: "not-the-key"}
    )
    assert response.status_code == 401


def test_ingest_with_a_valid_key_is_accepted(locked_station):
    response = locked_station.post("/api/v1/telemetry", json=frame(1), headers={HEADER: KEY})
    assert response.status_code == 201


def test_any_of_the_configured_keys_works(locked_station):
    """Several keys so a feed can be rotated without an outage."""
    assert (
        locked_station.post(
            "/api/v1/telemetry", json=frame(2), headers={HEADER: "other"}
        ).status_code
        == 201
    )


def test_opening_a_pass_needs_a_key(locked_station):
    body = {"satellite_id": "TEST-1", "ground_station_id": "TEST-GS"}
    assert locked_station.post("/api/v1/passes", json=body).status_code == 401
    assert (
        locked_station.post("/api/v1/passes", json=body, headers={HEADER: KEY}).status_code == 201
    )


def test_silencing_an_alert_needs_a_key(session, locked_station):
    """The write that matters most: an open alert nobody can see is worse than none."""
    session.add(
        Alert(
            satellite_id="TEST-1",
            rule="BATTERY_LOW",
            severity="critical",
            message="x",
            dedupe_key="k",
        )
    )
    session.flush()
    alert_id = session.scalars(session.query(Alert).statement).one().id

    assert locked_station.post(f"/api/v1/alerts/{alert_id}/ack", json={}).status_code == 401
    ok = locked_station.post(
        f"/api/v1/alerts/{alert_id}/ack", json={"resolve": True}, headers={HEADER: KEY}
    )
    assert ok.status_code == 200
    assert ok.json()["resolved_at"] is not None


def test_reading_never_needs_a_key(locked_station):
    for path in (
        "/health",
        "/api/v1/telemetry",
        "/api/v1/passes",
        "/api/v1/alerts",
        "/api/v1/summary",
        "/api/v1/satellites",
        "/api/v1/telemetry/series",
    ):
        assert locked_station.get(path).status_code == 200, path


def test_health_reports_that_the_station_is_locked(locked_station):
    assert locked_station.get("/health").json()["auth"] == "enabled"


def test_the_rejection_names_the_header_the_caller_is_missing(locked_station):
    response = locked_station.post("/api/v1/telemetry", json=frame(1))
    assert response.headers.get("WWW-Authenticate") == HEADER
