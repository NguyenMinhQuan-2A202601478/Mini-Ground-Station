"""FastAPI application: the ground station's ingestion and query surface."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from mgs import __version__
from mgs.api.routes import alerts, health, passes, telemetry
from mgs.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("mgs").info("ground station API up, db=%s", _redact(settings.database_url))
    yield


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


app = FastAPI(
    title="mini-ground-station",
    version=__version__,
    summary="Telemetry ingestion, pass tracking, and anomaly alerts for a small satellite.",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(telemetry.router, prefix="/api/v1")
app.include_router(passes.router, prefix="/api/v1")
app.include_router(alerts.router, prefix="/api/v1")


def run() -> None:
    """Console-script entry point: `mgs-api`."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "mgs.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
