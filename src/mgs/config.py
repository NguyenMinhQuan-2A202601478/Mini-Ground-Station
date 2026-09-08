"""Runtime configuration, read once from the environment / `.env`."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MGS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    database_url: str = "postgresql+psycopg://mgs:mgs@localhost:5433/mgs"
    sql_echo: bool = False

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    log_level: str = "INFO"
    # Comma-separated keys accepted on writing endpoints. Empty disables the
    # check, which the API reports at startup and on /health.
    api_keys: str = ""

    # Worker — deterministic threshold rules
    poll_interval_seconds: float = 10.0
    battery_min_v: float = 6.8
    battery_critical_v: float = 6.4
    temp_max_c: float = 60.0
    temp_min_c: float = -30.0
    max_seq_gap: int = 1  # frames may arrive out of order; a gap > this is a DATA_GAP
    # A condition has to stay quiet this long before its alert is resolved.
    # Without it a single normal frame in the middle of an excursion closes the
    # episode and the next frame opens another one.
    alert_clear_after_seconds: float = 120.0

    # Worker — statistical detector (optional, needs the `ml` extra)
    enable_ml: bool = True
    ml_training_window_hours: int = 24
    ml_min_samples: int = 50
    ml_contamination: float = 0.02
    zscore_threshold: float = 3.0
    worker_batch_size: int = 500

    # Retention. 0 keeps telemetry forever, which is the safe default and not a
    # sustainable one: see `mgs.retention`.
    retention_days: float = 0.0  # fractional days are allowed, for short policies
    retention_batch_size: int = 5000

    # Ground station — real coordinates, because the pass geometry is real.
    station_id: str = "HANOI-GS"
    station_lat_deg: float = 21.0278
    station_lon_deg: float = 105.8342
    station_elevation_m: float = 20.0
    # Below this the spacecraft is behind terrain and clutter, not merely low.
    station_min_elevation_deg: float = 5.0

    # Simulator
    sim_api_url: str = "http://localhost:8000"
    sim_api_key: str = ""  # sent as X-API-Key on every write
    # Empty means "use the name in the element set".
    sim_satellite_id: str = ""
    sim_catalog_number: int = 25544  # the ISS
    sim_duration: str = "24h"  # of mission time, per run
    sim_time_scale: float = 600.0  # simulated seconds per second of wall clock
    sim_record_interval_seconds: float = 30.0
    sim_downlink_rate: float = 2.0  # frames per simulated second while in contact
    sim_onboard_capacity: int = 4096
    sim_drop_rate: float = 0.02
    sim_fault_rate: float = 1.5  # expected injected faults per orbit


@lru_cache
def get_settings() -> Settings:
    return Settings()
