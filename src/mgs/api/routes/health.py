from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from mgs import __version__
from mgs.db import get_session
from mgs.schemas import Health

router = APIRouter(tags=["health"])


@router.get("/health", response_model=Health)
def health(session: Session = Depends(get_session)) -> Health:
    try:
        session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return Health(status="ok" if db_ok else "degraded", database=db_ok, version=__version__)
