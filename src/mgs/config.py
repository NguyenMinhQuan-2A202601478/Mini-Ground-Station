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

    # Worker — deterministic threshold rules
    poll_interval_seconds: float = 10.0
    battery_min_v: float = 6.8
    battery_critical_v: float = 6.4
    temp_max_c: float = 60.0
    temp_min_c: float = -30.0
    max_seq_gap: int = 1  # frames may arrive out of order; a gap > this is a DATA_GAP

    # Worker — statistical detector (optional, needs the `ml` extra)
    enable_ml: bool = True
    ml_training_window_hours: int = 24
    ml_min_samples: int = 50
    ml_contamination: float = 0.02
    zscore_threshold: float = 3.0
    worker_batch_size: int = 500

    # Simulator
    sim_api_url: str = "http://localhost:8000"
    sim_satellite_id: str = "VNSAT-1"
    sim_ground_station_id: str = "HANOI-GS"
    sim_orbit_seconds: float = 120.0
    sim_frame_interval_seconds: float = 1.0
    sim_fault_rate: float = 1.5  # expected injected faults per orbit


@lru_cache
def get_settings() -> Settings:
    return Settings()
