"""Ingestion service: turn a validated frame into rows, idempotently.

Kept out of the route handlers so the simulator, tests, and any future
non-HTTP feed (a real SDR pipeline, a file replay) share one code path.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mgs.models import Pass, Telemetry
from mgs.schemas import IngestResult, TelemetryIn


def utcnow() -> datetime:
    return datetime.now(UTC)


def open_pass(
    session: Session,
    *,
    satellite_id: str,
    ground_station_id: str,
    aos_at: datetime | None = None,
    max_elevation_deg: float | None = None,
) -> Pass:
    """Open a contact window, or return the one already open for this satellite.

    The `uq_passes_open` partial unique index is the real guard; this lookup
    only avoids paying for a failed insert in the common case.
    """
    existing = current_pass(session, satellite_id)
    if existing is not None:
        return existing

    row = Pass(
        satellite_id=satellite_id,
        ground_station_id=ground_station_id,
        aos_at=aos_at or utcnow(),
        max_elevation_deg=max_elevation_deg,
        status="active",
    )
    session.add(row)
    session.flush()
    return row


def current_pass(session: Session, satellite_id: str) -> Pass | None:
    return session.scalars(
        select(Pass).where(Pass.satellite_id == satellite_id, Pass.los_at.is_(None))
    ).first()


def close_pass(
    session: Session,
    pass_row: Pass,
    *,
    los_at: datetime | None = None,
    max_elevation_deg: float | None = None,
    status: str = "completed",
) -> Pass:
    pass_row.los_at = los_at or utcnow()
    pass_row.status = status
    if max_elevation_deg is not None:
        pass_row.max_elevation_deg = max_elevation_deg
    session.flush()
    return pass_row


def ingest_frame(session: Session, frame: TelemetryIn) -> IngestResult:
    """Persist one frame. Safe to call twice with the same (satellite_id, seq)."""
    pass_id = frame.pass_id
    if pass_id is None:
        existing_pass = current_pass(session, frame.satellite_id)
        if existing_pass is None and frame.auto_open_pass:
            # A pass is a ground-station event, so it is timed on the ground
            # clock. The frame's own `recorded_at` is the on-board clock and
            # can be hours older: a store-and-forward spacecraft dumps a whole
            # orbit of recorded frames during one contact window.
            existing_pass = open_pass(
                session,
                satellite_id=frame.satellite_id,
                ground_station_id=frame.ground_station_id or "UNKNOWN-GS",
            )
        pass_id = existing_pass.id if existing_pass else None

    values = {
        "pass_id": pass_id,
        "satellite_id": frame.satellite_id,
        "seq": frame.seq,
        "recorded_at": frame.recorded_at,
        "received_at": utcnow(),
        "battery_voltage_v": frame.battery_voltage_v,
        "battery_current_a": frame.battery_current_a,
        "temperature_c": frame.temperature_c,
        "lat_deg": frame.lat_deg,
        "lon_deg": frame.lon_deg,
        "alt_km": frame.alt_km,
        "signal_strength_dbm": frame.signal_strength_dbm,
        "mode": frame.mode,
        "raw": frame.raw,
    }

    stmt = (
        insert(Telemetry)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_telemetry_sat_seq")
        .returning(Telemetry.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()

    if new_id is None:
        # Already ingested — a retried POST or a re-sent frame. Report the
        # original row rather than erroring; the sender's intent is satisfied.
        existing = session.scalars(
            select(Telemetry).where(
                Telemetry.satellite_id == frame.satellite_id, Telemetry.seq == frame.seq
            )
        ).one()
        return IngestResult(telemetry_id=existing.id, pass_id=existing.pass_id, duplicate=True)

    if pass_id is not None:
        session.execute(
            update(Pass).where(Pass.id == pass_id).values(frame_count=Pass.frame_count + 1)
        )

    return IngestResult(telemetry_id=new_id, pass_id=pass_id, duplicate=False)
