"""Satellite simulator: fly a real orbit, downlink while the station can hear it.

The spacecraft records housekeeping telemetry all the way round its orbit and
stores it on board; it can only downlink during a contact window. Windows come
from SGP4 against a published element set, so they are the real ones: a few
per day, minutes long, in clusters separated by hours of silence.

Simulated time runs faster than the clock (`--time-scale`), which is the only
way to watch a day of operations without waiting a day for it.

    mgs-sim                       # 24 h of operations, compressed
    mgs-sim --fetch-tle           # against a current element set
    mgs-sim --duration 3d --time-scale 5000
"""

from __future__ import annotations

import argparse
import logging
import random
import re
import sys
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from mgs.config import get_settings
from mgs.simulator import tle as tle_module
from mgs.simulator.orbit import GroundStation, Propagator, Spacecraft

log = logging.getLogger("mgs.simulator")

DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 3600}


def parse_duration(text: str) -> timedelta:
    match = DURATION.match(text)
    if not match:
        raise argparse.ArgumentTypeError(f"expected something like 90m, 24h or 3d, got {text!r}")
    return timedelta(seconds=float(match.group(1)) * UNIT_SECONDS[match.group(2).lower()])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    p = argparse.ArgumentParser(prog="mgs-sim", description="Simulated satellite telemetry feed.")

    p.add_argument("--api-url", default=settings.sim_api_url)
    p.add_argument(
        "--satellite-id",
        default=settings.sim_satellite_id,
        help="defaults to the name in the element set",
    )

    tle_group = p.add_argument_group("element set")
    tle_group.add_argument("--tle-file", type=Path, help="fly this element set")
    tle_group.add_argument(
        "--catalog-number",
        type=int,
        default=settings.sim_catalog_number,
        help="NORAD catalog number (default: %(default)s, the ISS)",
    )
    tle_group.add_argument(
        "--fetch-tle", action="store_true", help="download a current element set from Celestrak"
    )

    station = p.add_argument_group("ground station")
    station.add_argument("--station-id", default=settings.station_id)
    station.add_argument("--station-lat", type=float, default=settings.station_lat_deg)
    station.add_argument("--station-lon", type=float, default=settings.station_lon_deg)
    station.add_argument("--station-alt", type=float, default=settings.station_elevation_m)
    station.add_argument(
        "--min-elevation",
        type=float,
        default=settings.station_min_elevation_deg,
        help="degrees above the horizon before the station can hear it",
    )

    run = p.add_argument_group("run")
    run.add_argument(
        "--duration",
        type=parse_duration,
        default=parse_duration(settings.sim_duration),
        help="how much mission time to fly, e.g. 90m, 24h, 3d (default: %(default)s)",
    )
    run.add_argument(
        "--start",
        type=datetime.fromisoformat,
        help="mission start in UTC (default: far enough back to end about now)",
    )
    run.add_argument(
        "--time-scale",
        type=float,
        default=settings.sim_time_scale,
        help="simulated seconds per second of wall clock (default: %(default)s)",
    )
    run.add_argument(
        "--record-interval",
        type=float,
        default=settings.sim_record_interval_seconds,
        help="simulated seconds between recorded frames (default: %(default)s)",
    )
    run.add_argument(
        "--downlink-rate",
        type=float,
        default=settings.sim_downlink_rate,
        help="frames per simulated second while in contact (default: %(default)s)",
    )
    run.add_argument(
        "--onboard-capacity",
        type=int,
        default=settings.sim_onboard_capacity,
        help="frames the recorder holds before it overwrites the oldest",
    )
    run.add_argument("--drop-rate", type=float, default=settings.sim_drop_rate)
    run.add_argument(
        "--fault-rate",
        type=float,
        default=settings.sim_fault_rate,
        help="expected number of injected faults per orbit",
    )
    run.add_argument("--seed", type=int, default=None)
    run.add_argument(
        "--restart-seq",
        action="store_true",
        help="number frames from zero instead of continuing the station's stream",
    )
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

    def latest_seq(self, satellite_id: str) -> int:
        """The highest frame number the station has already received.

        Frame numbers come from the spacecraft, and `UNIQUE (satellite_id, seq)`
        makes re-sending one a no-op — which is exactly right for a retried
        downlink, and exactly wrong for a restarted simulator, whose whole
        second run would be silently discarded as a replay. So pick up the
        count where the station left off.

        This asks for the highest number, not the most recent frame: a
        backfilled downlink arrives late carrying an old sequence number, and
        resuming from *that* would replay everything after it.
        """
        r = self.client.get("/api/v1/summary", params={"satellite_id": satellite_id})
        r.raise_for_status()
        return r.json().get("max_seq") or 0

    def send(self, frames: list[dict]) -> dict:
        r = self.client.post("/api/v1/telemetry/batch", json=frames)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.client.close()


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - one linear flight
    args = parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    element_set = tle_module.load(
        path=args.tle_file, catalog_number=args.catalog_number, refresh=args.fetch_tle
    )
    element_set.warn_if_stale()

    station = GroundStation(
        identifier=args.station_id,
        lat_deg=args.station_lat,
        lon_deg=args.station_lon,
        elevation_m=args.station_alt,
        min_elevation_deg=args.min_elevation,
    )
    propagator = Propagator(element_set, station)
    satellite_id = args.satellite_id or element_set.name

    start = args.start or datetime.now(UTC) - args.duration
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + args.duration
    period_seconds = propagator.period.total_seconds()

    schedule = propagator.passes(start, end)
    log.info(
        "flying %s (catalog %d) over %s at %.4f, %.4f",
        element_set.name,
        element_set.catalog_number,
        station.identifier,
        station.lat_deg,
        station.lon_deg,
    )
    log.info(
        "%s of mission time from %s, period %.1f min, %d pass(es) above %.0f°",
        args.duration,
        start.strftime("%Y-%m-%d %H:%M UTC"),
        period_seconds / 60.0,
        len(schedule),
        station.min_elevation_deg,
    )
    for window in schedule:
        log.info(
            "  predicted pass  AOS %s  %4.1f min  max elevation %4.1f°",
            window.aos.strftime("%H:%M:%S"),
            window.duration.total_seconds() / 60.0,
            window.max_elevation_deg,
        )
    if not schedule:
        log.warning("no contact windows in this interval — nothing will be downlinked")

    rng = random.Random(args.seed)
    craft = Spacecraft()
    link = Downlink(args.api_url)

    try:
        seq = 0 if args.restart_seq else link.latest_seq(satellite_id)
        # The recorder is finite. When a long silence fills it, the oldest
        # frames are overwritten and are simply never seen on the ground — a
        # real data gap, not a dropped packet.
        onboard: deque[dict] = deque(maxlen=args.onboard_capacity)
        per_downlink = max(1, int(args.downlink_rate * args.record_interval))
        dphase = args.record_interval / period_seconds

        when = start
        pass_id: int | None = None
        max_elevation = 0.0
        overflowed = 0

        while when < end:
            state = propagator.at(when)
            craft.step(state, dphase, args.fault_rate, rng)

            seq += 1
            if len(onboard) == onboard.maxlen:
                overflowed += 1
            onboard.append(
                {
                    "satellite_id": satellite_id,
                    "ground_station_id": station.identifier,
                    "seq": seq,
                    "recorded_at": when.isoformat(),
                    "battery_voltage_v": round(craft.battery_voltage_v, 3),
                    "battery_current_a": round(0.6 if state.in_sunlight else -0.4, 3),
                    "temperature_c": round(craft.temperature_c, 2),
                    "lat_deg": round(state.lat_deg, 4),
                    "lon_deg": round(state.lon_deg, 4),
                    "alt_km": round(state.alt_km, 2),
                    # Signal strength is stamped at downlink, not here: it is
                    # something the *station* measures about the link, and while
                    # this frame is being recorded there is usually no link.
                    "mode": craft.mode,
                    "raw": {
                        "elevation_deg": round(state.look.elevation_deg, 2),
                        "azimuth_deg": round(state.look.azimuth_deg, 1),
                        "range_km": round(state.look.range_km, 1),
                        "range_rate_km_s": round(state.look.range_rate_km_s, 4),
                        "doppler_hz": round(state.look.doppler_hz),
                        "sunlit": state.in_sunlight,
                    },
                }
            )

            if state.in_view and pass_id is None:
                pass_id = link.open_pass(satellite_id, station.identifier, when)
                max_elevation = 0.0
                log.info(
                    "AOS  %s — pass %d open, %d frame(s) on board",
                    when.strftime("%H:%M:%S"),
                    pass_id,
                    len(onboard),
                )

            if state.in_view:
                max_elevation = max(max_elevation, state.look.elevation_deg)
                batch = [onboard.popleft() for _ in range(min(per_downlink, len(onboard)))]
                # A frame lost on the link still burned its sequence number on
                # board — that is what makes the gap detectable on the ground.
                received_dbm = round(propagator.signal_strength_dbm(state.look, rng), 1)
                kept = [
                    frame | {"pass_id": pass_id, "signal_strength_dbm": received_dbm}
                    for frame in batch
                    if rng.random() >= args.drop_rate
                ]
                if kept:
                    result = link.send(kept)
                    log.info(
                        "  downlink %3d frame(s)%s — %4d on board | el %4.1f° "
                        "range %5.0f km | %.2f V, %5.1f °C, %s",
                        result["accepted"],
                        f" ({len(batch) - len(kept)} lost)" if len(kept) < len(batch) else "",
                        len(onboard),
                        state.look.elevation_deg,
                        state.look.range_km,
                        craft.battery_voltage_v,
                        craft.temperature_c,
                        craft.mode,
                    )

            if not state.in_view and pass_id is not None:
                link.close_pass(pass_id, when, round(max_elevation, 2))
                log.info(
                    "LOS  %s — pass %d closed, max elevation %.1f°, %d frame(s) still on board",
                    when.strftime("%H:%M:%S"),
                    pass_id,
                    max_elevation,
                    len(onboard),
                )
                pass_id = None

            when += timedelta(seconds=args.record_interval)
            if args.time_scale > 0:
                time.sleep(args.record_interval / args.time_scale)

        if pass_id is not None:  # the run ended mid-pass
            link.close_pass(pass_id, when, round(max_elevation, 2))
            log.info("LOS  pass %d closed at the end of the run", pass_id)

        if overflowed:
            log.warning(
                "the recorder overwrote %d frame(s) during long silences — they were "
                "never downlinked",
                overflowed,
            )
        log.info("mission complete: %d frame(s) recorded, %d still on board", seq, len(onboard))
    except KeyboardInterrupt:
        log.info("simulator stopped")
    finally:
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
