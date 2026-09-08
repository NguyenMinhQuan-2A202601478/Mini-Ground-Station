from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgs.api.security import require_api_key
from mgs.db import get_session
from mgs.ingest import ingest_frame
from mgs.models import Telemetry
from mgs.schemas import BatchIngestResult, IngestResult, TelemetryIn, TelemetryOut

router = APIRouter(prefix="/telemetry", tags=["telemetry"])


@router.post(
    "", response_model=IngestResult, status_code=201, dependencies=[Depends(require_api_key)]
)
def post_telemetry(frame: TelemetryIn, session: Session = Depends(get_session)) -> IngestResult:
    """Ingest one frame. Re-posting the same (satellite_id, seq) is a no-op."""
    return ingest_frame(session, frame)


@router.post(
    "/batch",
    response_model=BatchIngestResult,
    status_code=201,
    dependencies=[Depends(require_api_key)],
)
def post_telemetry_batch(
    frames: list[TelemetryIn], session: Session = Depends(get_session)
) -> BatchIngestResult:
    """Ingest a burst of frames — how a real pass is usually downlinked."""
    results = [ingest_frame(session, frame) for frame in frames]
    duplicates = sum(1 for r in results if r.duplicate)
    return BatchIngestResult(
        accepted=len(results) - duplicates, duplicates=duplicates, results=results
    )


@router.get("", response_model=list[TelemetryOut])
def list_telemetry(
    session: Session = Depends(get_session),
    satellite_id: str | None = None,
    pass_id: int | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(le=5000)] = 200,
) -> list[Telemetry]:
    stmt = select(Telemetry).order_by(Telemetry.recorded_at.desc()).limit(limit)
    if satellite_id:
        stmt = stmt.where(Telemetry.satellite_id == satellite_id)
    if pass_id:
        stmt = stmt.where(Telemetry.pass_id == pass_id)
    if since:
        stmt = stmt.where(Telemetry.recorded_at >= since)
    return list(session.scalars(stmt))
