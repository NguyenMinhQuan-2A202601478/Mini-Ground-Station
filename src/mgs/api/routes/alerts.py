from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from mgs.db import get_session
from mgs.models import Alert
from mgs.schemas import AlertAck, AlertOut

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", response_model=list[AlertOut])
def list_alerts(
    session: Session = Depends(get_session),
    satellite_id: str | None = None,
    severity: str | None = None,
    rule: str | None = None,
    open_only: bool = True,
    limit: int = Query(default=100, le=1000),
) -> list[Alert]:
    stmt = select(Alert).order_by(Alert.detected_at.desc()).limit(limit)
    if satellite_id:
        stmt = stmt.where(Alert.satellite_id == satellite_id)
    if severity:
        stmt = stmt.where(Alert.severity == severity)
    if rule:
        stmt = stmt.where(Alert.rule == rule)
    if open_only:
        stmt = stmt.where(Alert.resolved_at.is_(None))
    return list(session.scalars(stmt))


@router.post("/{alert_id}/ack", response_model=AlertOut)
def ack_alert(alert_id: int, body: AlertAck, session: Session = Depends(get_session)) -> Alert:
    """Operator acknowledges an alert, and optionally marks it resolved."""
    row = session.get(Alert, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    now = datetime.now(UTC)
    row.acknowledged_at = row.acknowledged_at or now
    if body.resolve:
        row.resolved_at = row.resolved_at or now
    session.flush()
    return row
