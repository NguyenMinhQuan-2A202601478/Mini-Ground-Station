from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from mgs.config import Settings
from mgs.models import Base, Telemetry

TEST_DATABASE_URL = os.environ.get(
    "MGS_TEST_DATABASE_URL", "postgresql+psycopg://mgs:mgs@localhost:5433/mgs_test"
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Deliberately no hysteresis: tests that are about the episode state
    machine should not also be about the quiet period. The hysteresis tests set
    their own value."""
    return Settings(database_url=TEST_DATABASE_URL, enable_ml=False, alert_clear_after_seconds=0.0)


@pytest.fixture(scope="session")
def engine():
    """A throwaway schema. Skips the whole integration suite if no database."""
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/mgs"
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    try:
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        admin.dispose()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no PostgreSQL at {admin_url}: {exc}")

    eng = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        yield s
        s.rollback()
    with engine.begin() as conn:
        conn.execute(
            text("TRUNCATE alerts, telemetry, passes, satellites RESTART IDENTITY CASCADE")
        )


def make_frame(seq: int, **overrides) -> Telemetry:
    """An in-memory frame, for rules that do not need the database."""
    defaults = dict(
        id=seq,
        pass_id=1,
        satellite_id="TEST-1",
        seq=seq,
        recorded_at=datetime.now(UTC) + timedelta(seconds=seq),
        battery_voltage_v=7.8,
        temperature_c=20.0,
        lat_deg=10.0,
        lon_deg=100.0,
        alt_km=420.0,
        signal_strength_dbm=-90.0,
        mode="NOMINAL",
    )
    return Telemetry(**{**defaults, **overrides})
