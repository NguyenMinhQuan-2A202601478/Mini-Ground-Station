"""Pydantic request/response models for the HTTP surface."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Passes ---------------------------------------------------------------


class PassOpen(BaseModel):
    satellite_id: str = Field(max_length=64)
    ground_station_id: str = Field(max_length=64)
    aos_at: datetime | None = None
    max_elevation_deg: float | None = Field(default=None, ge=0, le=90)


class PassClose(BaseModel):
    los_at: datetime | None = None
    max_elevation_deg: float | None = Field(default=None, ge=0, le=90)
    status: Literal["completed", "aborted"] = "completed"


class PassOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    satellite_id: str
    ground_station_id: str
    aos_at: datetime
    los_at: datetime | None
    max_elevation_deg: float | None
    status: str
    frame_count: int


# --- Telemetry ------------------------------------------------------------


class TelemetryIn(BaseModel):
    """One downlinked frame.

    `pass_id` is optional: when omitted the API attaches the frame to the
    satellite's currently open pass, opening one if `auto_open_pass` is set.
    """

    satellite_id: str = Field(max_length=64)
    seq: int = Field(ge=0)
    recorded_at: datetime
    battery_voltage_v: float
    temperature_c: float
    lat_deg: float = Field(ge=-90, le=90)
    lon_deg: float = Field(ge=-180, le=180)
    alt_km: float
    battery_current_a: float | None = None
    signal_strength_dbm: float | None = None
    mode: Literal["NOMINAL", "SAFE", "PAYLOAD"] = "NOMINAL"
    raw: dict[str, Any] | None = None

    pass_id: int | None = None
    ground_station_id: str | None = Field(default=None, max_length=64)
    auto_open_pass: bool = True


class TelemetryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    pass_id: int | None
    satellite_id: str
    seq: int
    recorded_at: datetime
    received_at: datetime
    battery_voltage_v: float
    battery_current_a: float | None
    temperature_c: float
    lat_deg: float
    lon_deg: float
    alt_km: float
    signal_strength_dbm: float | None
    mode: str
    screened_at: datetime | None


class IngestResult(BaseModel):
    telemetry_id: int
    pass_id: int | None
    duplicate: bool = False


class BatchIngestResult(BaseModel):
    accepted: int
    duplicates: int
    results: list[IngestResult]


# --- Alerts ---------------------------------------------------------------


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telemetry_id: int | None
    pass_id: int | None
    satellite_id: str
    rule: str
    severity: str
    metric: str | None
    value: float | None
    threshold: float | None
    score: float | None
    message: str
    detected_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None


class AlertAck(BaseModel):
    resolve: bool = False


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool
    version: str


# --- Dashboard ------------------------------------------------------------


class SeriesPoint(BaseModel):
    """One time bucket of telemetry, aggregated in the database."""

    t: datetime
    n: int
    battery_avg: float | None = None
    battery_min: float | None = None
    battery_max: float | None = None
    temperature_avg: float | None = None
    temperature_min: float | None = None
    temperature_max: float | None = None
    signal_avg: float | None = None


class TelemetrySeries(BaseModel):
    satellite_id: str | None
    start: datetime | None
    end: datetime | None
    bucket_seconds: float
    frame_count: int
    points: list[SeriesPoint]


class AlertCounts(BaseModel):
    critical: int = 0
    warning: int = 0
    info: int = 0

    @property
    def total(self) -> int:
        return self.critical + self.warning + self.info


class Satellite(BaseModel):
    satellite_id: str
    frame_count: int
    last_contact_at: datetime | None


class Limits(BaseModel):
    """The operating limits the worker actually screens against.

    The dashboard draws its threshold lines from these rather than hard-coding
    them, so a line on a chart can never disagree with the rule that pages
    someone.
    """

    battery_min_v: float
    battery_critical_v: float
    temp_max_c: float
    temp_min_c: float


class Summary(BaseModel):
    """Everything the dashboard header needs, in one round trip."""

    satellite_id: str | None
    latest: TelemetryOut | None
    # The highest frame number received, which is not the same thing as the
    # latest frame: a backfilled downlink lands with an old sequence number and
    # a new arrival time.
    max_seq: int | None
    frame_count: int
    pass_count: int
    unscreened: int
    current_pass: PassOut | None
    last_pass: PassOut | None
    open_alerts: AlertCounts
    limits: Limits
