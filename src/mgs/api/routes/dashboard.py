"""Read-only endpoints the dashboard needs, and the dashboard page itself."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from mgs.config import Settings, get_settings
from mgs.db import get_session
from mgs.models import Alert, Pass, Telemetry
from mgs.schemas import (
    AlertCounts,
    Limits,
    Satellite,
    SeriesPoint,
    Summary,
    TelemetrySeries,
)

router = APIRouter(tags=["dashboard"])
# The page itself is served from the root, not from under /api/v1.
page = APIRouter(include_in_schema=False)

STATIC = Path(__file__).resolve().parent.parent / "static"


@router.get("/satellites", response_model=list[Satellite])
def list_satellites(session: Session = Depends(get_session)) -> list[Satellite]:
    rows = session.execute(
        select(
            Telemetry.satellite_id,
            func.count().label("frames"),
            func.max(Telemetry.received_at).label("last_contact"),
        )
        .group_by(Telemetry.satellite_id)
        .order_by(Telemetry.satellite_id)
    ).all()
    return [
        Satellite(satellite_id=r.satellite_id, frame_count=r.frames, last_contact_at=r.last_contact)
        for r in rows
    ]


@router.get("/summary", response_model=Summary)
def summary(
    session: Session = Depends(get_session),
    satellite_id: str | None = None,
    settings: Settings = Depends(get_settings),
) -> Summary:
    def scoped(stmt: Select, column) -> Select:
        return stmt.where(column == satellite_id) if satellite_id else stmt

    latest = session.scalars(
        scoped(select(Telemetry), Telemetry.satellite_id)
        .order_by(Telemetry.recorded_at.desc())
        .limit(1)
    ).first()

    frame_count = session.scalar(
        scoped(select(func.count()).select_from(Telemetry), Telemetry.satellite_id)
    )
    max_seq = session.scalar(scoped(select(func.max(Telemetry.seq)), Telemetry.satellite_id))
    unscreened = session.scalar(
        scoped(select(func.count()).select_from(Telemetry), Telemetry.satellite_id).where(
            Telemetry.screened_at.is_(None)
        )
    )
    pass_count = session.scalar(scoped(select(func.count()).select_from(Pass), Pass.satellite_id))

    current = session.scalars(
        scoped(select(Pass), Pass.satellite_id).where(Pass.los_at.is_(None)).limit(1)
    ).first()
    last = session.scalars(
        scoped(select(Pass), Pass.satellite_id)
        .where(Pass.los_at.is_not(None))
        .order_by(Pass.aos_at.desc())
        .limit(1)
    ).first()

    severities = session.execute(
        scoped(
            select(Alert.severity, func.count()).where(Alert.resolved_at.is_(None)),
            Alert.satellite_id,
        ).group_by(Alert.severity)
    ).all()

    return Summary(
        satellite_id=satellite_id,
        latest=latest,
        max_seq=max_seq,
        frame_count=frame_count or 0,
        pass_count=pass_count or 0,
        unscreened=unscreened or 0,
        current_pass=current,
        last_pass=last,
        open_alerts=AlertCounts(**{severity: count for severity, count in severities}),
        limits=Limits(
            battery_min_v=settings.battery_min_v,
            battery_critical_v=settings.battery_critical_v,
            temp_max_c=settings.temp_max_c,
            temp_min_c=settings.temp_min_c,
        ),
    )


@router.get("/telemetry/series", response_model=TelemetrySeries)
def telemetry_series(
    session: Session = Depends(get_session),
    satellite_id: str | None = None,
    pass_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    buckets: Annotated[int, Query(ge=1, le=2000)] = 240,
) -> TelemetrySeries:
    """Telemetry aggregated into time buckets, for charting.

    The aggregation happens in PostgreSQL: a chart 900 pixels wide has no use
    for 50,000 individual frames, and shipping them all would make the page
    slower the longer the mission runs.
    """
    filters = []
    if satellite_id:
        filters.append(Telemetry.satellite_id == satellite_id)
    if pass_id:
        filters.append(Telemetry.pass_id == pass_id)
    if since:
        filters.append(Telemetry.recorded_at >= since)
    if until:
        filters.append(Telemetry.recorded_at <= until)

    bounds = session.execute(
        select(
            func.min(Telemetry.recorded_at),
            func.max(Telemetry.recorded_at),
            func.count(),
        ).where(*filters)
    ).one()
    start, end, total = bounds

    empty = TelemetrySeries(
        satellite_id=satellite_id,
        start=start,
        end=end,
        bucket_seconds=0.0,
        frame_count=total or 0,
        points=[],
    )
    if not total or start is None or end is None:
        return empty

    span = (end - start).total_seconds()
    if span <= 0:
        # Every frame shares one timestamp: one bucket, no width to divide.
        buckets = 1
        span = 1.0

    epoch = func.extract("epoch", Telemetry.recorded_at)
    bucket = func.width_bucket(
        epoch, start.timestamp(), end.timestamp() + span / buckets, buckets
    ).label("bucket")

    rows = session.execute(
        select(
            bucket,
            func.min(Telemetry.recorded_at).label("bucket_start"),
            func.count().label("n"),
            func.avg(Telemetry.battery_voltage_v),
            func.min(Telemetry.battery_voltage_v),
            func.max(Telemetry.battery_voltage_v),
            func.avg(Telemetry.temperature_c),
            func.min(Telemetry.temperature_c),
            func.max(Telemetry.temperature_c),
            func.avg(Telemetry.signal_strength_dbm),
        )
        .where(*filters)
        .group_by(bucket)
        .order_by(bucket)
    ).all()

    return TelemetrySeries(
        satellite_id=satellite_id,
        start=start,
        end=end,
        bucket_seconds=span / buckets,
        frame_count=total,
        points=[
            SeriesPoint(
                t=r.bucket_start,
                n=r.n,
                battery_avg=_f(r[3]),
                battery_min=_f(r[4]),
                battery_max=_f(r[5]),
                temperature_avg=_f(r[6]),
                temperature_min=_f(r[7]),
                temperature_max=_f(r[8]),
                signal_avg=_f(r[9]),
            )
            for r in rows
        ],
    )


def _f(value) -> float | None:
    """`avg()` returns Decimal; the JSON schema wants a float."""
    return None if value is None else float(value)


@page.get("/")
def dashboard() -> FileResponse:
    return FileResponse(STATIC / "index.html", media_type="text/html")
