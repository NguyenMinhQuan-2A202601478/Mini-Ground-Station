from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgs.db import get_session
from mgs.ingest import close_pass, current_pass, open_pass
from mgs.models import Pass
from mgs.schemas import PassClose, PassOpen, PassOut

router = APIRouter(prefix="/passes", tags=["passes"])


@router.post("", response_model=PassOut, status_code=201)
def post_pass(body: PassOpen, session: Session = Depends(get_session)) -> Pass:
    """Open a contact window (AOS). Idempotent per satellite."""
    return open_pass(
        session,
        satellite_id=body.satellite_id,
        ground_station_id=body.ground_station_id,
        aos_at=body.aos_at,
        max_elevation_deg=body.max_elevation_deg,
    )


@router.post("/{pass_id}/close", response_model=PassOut)
def post_close_pass(pass_id: int, body: PassClose, session: Session = Depends(get_session)) -> Pass:
    """Close a contact window (LOS)."""
    row = session.get(Pass, pass_id)
    if row is None:
        raise HTTPException(status_code=404, detail="pass not found")
    if row.los_at is not None:
        raise HTTPException(status_code=409, detail="pass already closed")
    return close_pass(
        session,
        row,
        los_at=body.los_at,
        max_elevation_deg=body.max_elevation_deg,
        status=body.status,
    )


@router.get("/current", response_model=PassOut)
def get_current_pass(satellite_id: str, session: Session = Depends(get_session)) -> Pass:
    row = current_pass(session, satellite_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no open pass for this satellite")
    return row


@router.get("", response_model=list[PassOut])
def list_passes(
    session: Session = Depends(get_session),
    satellite_id: str | None = None,
    limit: int = Query(default=50, le=500),
) -> list[Pass]:
    stmt = select(Pass).order_by(Pass.aos_at.desc()).limit(limit)
    if satellite_id:
        stmt = stmt.where(Pass.satellite_id == satellite_id)
    return list(session.scalars(stmt))
