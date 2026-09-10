"""SQLAlchemy 2.0 ORM models.

The authoritative description of this schema — including the reasoning behind
the indexes and uniqueness constraints — lives in `docs/product/schema.md`.
Keep the two in sync; the constraints below are what actually enforce
idempotent ingestion and duplicate-free alerting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Enumerated string values ---------------------------------------------
# Kept as CHECK-constrained text rather than PG enums: adding a new rule or
# status must not require a migration that rewrites a type.

PASS_STATUSES = ("active", "completed", "aborted")
SEVERITIES = ("info", "warning", "critical")
SPACECRAFT_MODES = ("NOMINAL", "SAFE", "PAYLOAD")


class Satellite(Base):
    """A spacecraft this station tracks.

    The three operational tables carry `satellite_id` as text and always did;
    this table gives that identifier somewhere to point, and somewhere to hang
    the things that differ between spacecraft. The most important of those is
    the operating limits: a battery that has aged is not a battery that is
    failing, and telling them apart is a per-satellite judgement, not a station
    one.

    Rows appear on their own. A frame from an unknown spacecraft registers it
    rather than being rejected: telemetry that has already been received is not
    something to throw away over a missing configuration row.
    """

    __tablename__ = "satellites"

    satellite_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    catalog_number: Mapped[int | None] = mapped_column(Integer)
    operator: Mapped[str | None] = mapped_column(String(128))

    # Limit overrides. NULL means "use the station default from Settings", so a
    # row that nobody has edited behaves exactly as before this table existed.
    battery_min_v: Mapped[float | None] = mapped_column(Float)
    battery_critical_v: Mapped[float | None] = mapped_column(Float)
    temp_max_c: Mapped[float | None] = mapped_column(Float)
    temp_min_c: Mapped[float | None] = mapped_column(Float)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "catalog_number IS NULL OR catalog_number > 0", name="ck_satellites_catalog"
        ),
        CheckConstraint(
            "battery_critical_v IS NULL OR battery_min_v IS NULL "
            "OR battery_critical_v <= battery_min_v",
            name="ck_satellites_battery_order",
        ),
        CheckConstraint(
            "temp_min_c IS NULL OR temp_max_c IS NULL OR temp_min_c <= temp_max_c",
            name="ck_satellites_temp_order",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Satellite {self.satellite_id}>"


class Pass(Base):
    """A contact window between one satellite and one ground station."""

    __tablename__ = "passes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    satellite_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("satellites.satellite_id", ondelete="RESTRICT"), nullable=False
    )
    ground_station_id: Mapped[str] = mapped_column(String(64), nullable=False)

    aos_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    los_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_elevation_deg: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    telemetry: Mapped[list[Telemetry]] = relationship(back_populates="pass_", lazy="noload")

    __table_args__ = (
        CheckConstraint(f"status IN {PASS_STATUSES}", name="ck_passes_status"),
        CheckConstraint(
            "max_elevation_deg IS NULL OR (max_elevation_deg BETWEEN 0 AND 90)",
            name="ck_passes_elevation",
        ),
        CheckConstraint("los_at IS NULL OR los_at >= aos_at", name="ck_passes_window"),
        Index("ix_passes_satellite_aos", "satellite_id", aos_at.desc()),
        # A satellite is over the station once at a time: at most one open pass.
        Index(
            "uq_passes_open",
            "satellite_id",
            unique=True,
            postgresql_where=los_at.is_(None),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Pass {self.id} {self.satellite_id} aos={self.aos_at} status={self.status}>"


class Telemetry(Base):
    """One received telemetry frame. Append-only apart from `screened_at`."""

    __tablename__ = "telemetry"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    pass_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("passes.id", ondelete="RESTRICT")
    )
    satellite_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("satellites.satellite_id", ondelete="RESTRICT"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    battery_voltage_v: Mapped[float] = mapped_column(Float, nullable=False)
    battery_current_a: Mapped[float | None] = mapped_column(Float)
    temperature_c: Mapped[float] = mapped_column(Float, nullable=False)

    lat_deg: Mapped[float] = mapped_column(Float, nullable=False)
    lon_deg: Mapped[float] = mapped_column(Float, nullable=False)
    alt_km: Mapped[float] = mapped_column(Float, nullable=False)

    signal_strength_dbm: Mapped[float | None] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="NOMINAL")
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # The worker's cursor lives here rather than in a separate state table, so a
    # restart resumes exactly where it stopped. See docs/product/schema.md.
    screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pass_: Mapped[Pass | None] = relationship(back_populates="telemetry", lazy="noload")
    alerts: Mapped[list[Alert]] = relationship(back_populates="telemetry", lazy="noload")

    __table_args__ = (
        # Idempotent ingestion: a retried POST hits this instead of duplicating.
        UniqueConstraint("satellite_id", "seq", name="uq_telemetry_sat_seq"),
        CheckConstraint("lat_deg BETWEEN -90 AND 90", name="ck_telemetry_lat"),
        CheckConstraint("lon_deg BETWEEN -180 AND 180", name="ck_telemetry_lon"),
        CheckConstraint(f"mode IN {SPACECRAFT_MODES}", name="ck_telemetry_mode"),
        Index("ix_telemetry_sat_recorded", "satellite_id", recorded_at.desc()),
        # Retention sweeps by age across every satellite, which the composite
        # index above cannot serve.
        Index("ix_telemetry_recorded", recorded_at),
        Index("ix_telemetry_pass", "pass_id"),
        # The worker's queue.
        Index("ix_telemetry_unscreened", "id", postgresql_where=screened_at.is_(None)),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Telemetry {self.id} {self.satellite_id} seq={self.seq}>"


class Alert(Base):
    """A problem the worker found in the telemetry stream."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telemetry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("telemetry.id", ondelete="RESTRICT")
    )
    pass_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("passes.id", ondelete="RESTRICT")
    )
    satellite_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("satellites.satellite_id", ondelete="RESTRICT"), nullable=False
    )

    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str | None] = mapped_column(String(64))
    value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the condition first went quiet. An episode is not resolved the
    # instant one frame looks normal — it has to stay normal — so this is the
    # candidate end time, promoted to `resolved_at` once it has held.
    clearing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Re-running the worker over the same frames must not create duplicates.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    telemetry: Mapped[Telemetry | None] = relationship(back_populates="alerts", lazy="noload")

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_alerts_dedupe"),
        CheckConstraint(f"severity IN {SEVERITIES}", name="ck_alerts_severity"),
        Index(
            "ix_alerts_open",
            "satellite_id",
            detected_at.desc(),
            postgresql_where=resolved_at.is_(None),
        ),
        Index("ix_alerts_telemetry", "telemetry_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Alert {self.id} {self.rule} {self.severity} {self.satellite_id}>"
