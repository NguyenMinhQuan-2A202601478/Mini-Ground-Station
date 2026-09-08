"""Satellite simulator: fly an orbit, downlink telemetry while in view.

Talks to the API over HTTP exactly like a real ground-station front end would,
so nothing in the ingestion path is bypassed.

    mgs-sim --orbits 3            # or: python -m mgs.simulator
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import UTC, datetime

import httpx

from mgs.config import get_settings
from mgs.simulator.orbit import GroundStation, Spacecraft, anchor_offset_deg, propagate

log = logging.getLogger("mgs.simulator")

# Frames pushed per downlink call while the station is in view.
DOWNLINK_BATCH = 12


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    p = argparse.ArgumentParser(prog="mgs-sim", description="Simulated satellite telemetry feed.")
    p.add_argument("--api-url", default=settings.sim_api_url)
    p.add_argument("--satellite-id", default=settings.sim_satellite_id)
    p.add_argument("--station-id", default=settings.sim_ground_station_id)
    p.add_argument("--orbits", type=int, default=0, help="0 = run until interrupted")
    p.add_argument(
        "--orbit-seconds",
        type=float,
        default=settings.sim_orbit_seconds,
        help="wall-clock seconds per simulated orbit",
    )
    p.add_argument("--frame-interval", type=float, default=settings.sim_frame_interval_seconds)
    p.add_argument(
        "--fault-rate",
        type=float,
        default=settings.sim_fault_rate,
        help="expected number of injected faults per orbit",
    )
    p.add_argument(
        "--drop-rate",
        type=float,
        default=0.02,
        help="probability of losing a frame on the downlink (creates DATA_GAP alerts)",
    )
    p.add_argument("--seed", type=int, default=None)
    return p.parse_args(argv)


class Downlink:
    """Thin HTTP client for the ingestion API."""

    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0)

    def open_pass(self, satellite_id: str, station_id: str, aos_at: datetime) -> int:
        r = self.client.post(
            "/api/v1/passes",
            json={
                "satellite_id": satellite_id,
                "ground_station_id": station_id,
                "aos_at": aos_at.isoformat(),
            },
        )
        r.raise_for_status()
        return r.json()["id"]

    def close_pass(self, pass_id: int, los_at: datetime, max_elevation_deg: float) -> None:
        r = self.client.post(
            f"/api/v1/passes/{pass_id}/close",
            json={"los_at": los_at.isoformat(), "max_elevation_deg": max_elevation_deg},
        )
        r.raise_for_status()

    def send(self, frames: list[dict]) -> dict:
        r = self.client.post("/api/v1/telemetry/batch", json=frames)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.client.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    rng = random.Random(args.seed)
    station = GroundStation(args.station_id)
    craft = Spacecraft()
    link = Downlink(args.api_url)

    seq = 0
    orbit = 0
    frames_per_orbit = max(1, int(args.orbit_seconds / args.frame_interval))

    log.info(
        "simulator up | %s -> %s | %.0fs/orbit, %d frames/orbit | api=%s",
        args.satellite_id,
        args.station_id,
        args.orbit_seconds,
        frames_per_orbit,
        args.api_url,
    )

    # Frames are recorded all the way round the orbit and stored on board; the
    # spacecraft can only downlink them while the station is in view. That is
    # what a store-and-forward CubeSat does, and it is why `telemetry` carries
    # both `recorded_at` (on-board clock) and `received_at` (ground clock).
    onboard: list[dict] = []

    try:
        while args.orbits == 0 or orbit < args.orbits:
            offset = anchor_offset_deg(station, orbit)
            pass_id: int | None = None
            max_elevation = 0.0

            for step in range(frames_per_orbit):
                phase = step / frames_per_orbit
                state = propagate(phase, station, offset)
                craft.step(state, 1.0 / frames_per_orbit, args.fault_rate, rng)
                now = datetime.now(UTC)

                seq += 1
                onboard.append(
                    {
                        "satellite_id": args.satellite_id,
                        "ground_station_id": args.station_id,
                        "seq": seq,
                        "recorded_at": now.isoformat(),
                        "battery_voltage_v": round(craft.battery_voltage_v, 3),
                        "battery_current_a": round(0.6 if state.in_sunlight else -0.4, 3),
                        "temperature_c": round(craft.temperature_c, 2),
                        "lat_deg": round(state.lat_deg, 4),
                        "lon_deg": round(state.lon_deg, 4),
                        "alt_km": round(state.alt_km, 2),
                        "signal_strength_dbm": round(craft.signal_strength_dbm(state, rng), 1),
                        "mode": craft.mode,
                        "raw": {"elevation_deg": round(state.elevation_deg, 2)},
                    }
                )

                if state.in_view and pass_id is None:
                    pass_id = link.open_pass(args.satellite_id, args.station_id, now)
                    max_elevation = 0.0
                    log.info(
                        "AOS  orbit %d — pass %d open, %d frame(s) waiting on board",
                        orbit,
                        pass_id,
                        len(onboard),
                    )

                if state.in_view:
                    max_elevation = max(max_elevation, state.elevation_deg)
                    batch, onboard = onboard[:DOWNLINK_BATCH], onboard[DOWNLINK_BATCH:]
                    # A frame lost on the downlink still burned its sequence
                    # number on board — that is what makes the gap detectable.
                    kept = [
                        f | {"pass_id": pass_id} for f in batch if rng.random() >= args.drop_rate
                    ]
                    lost = len(batch) - len(kept)
                    if kept:
                        result = link.send(kept)
                        log.info(
                            "downlinked %d frame(s)%s — %d still on board | %.2f V, %.1f °C, %s",
                            result["accepted"],
                            f" ({lost} lost on the link)" if lost else "",
                            len(onboard),
                            craft.battery_voltage_v,
                            craft.temperature_c,
                            craft.mode,
                        )

                if not state.in_view and pass_id is not None:
                    link.close_pass(pass_id, now, round(max_elevation, 2))
                    log.info(
                        "LOS  pass %d closed — max elevation %.1f°, %d frame(s) left on board",
                        pass_id,
                        max_elevation,
                        len(onboard),
                    )
                    pass_id = None

                time.sleep(args.frame_interval)

            if pass_id is not None:  # orbit ended mid-pass
                link.close_pass(pass_id, datetime.now(UTC), round(max_elevation, 2))
                log.info("LOS  pass %d closed at orbit boundary", pass_id)
            orbit += 1
    except KeyboardInterrupt:
        log.info("simulator stopped after %d orbit(s)", orbit)
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
