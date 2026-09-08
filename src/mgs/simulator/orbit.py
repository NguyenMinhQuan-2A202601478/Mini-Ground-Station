"""A deliberately small orbit + spacecraft-health model.

Not an SGP4 propagator. It produces plausible, *self-consistent* numbers: the
satellite goes round, is only in view of the station for part of each orbit,
its battery charges in sunlight and drains in eclipse, and its temperature
follows the same sun angle. That is enough to exercise the ingestion path,
the pass logic, and the anomaly rules.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

EARTH_RADIUS_KM = 6371.0
INCLINATION_DEG = 51.6  # ISS-like
ALTITUDE_KM = 420.0

# Phase at which the ground track reaches the northern latitude of the station.
# The simulator anchors the track here (see `anchor_offset_deg`) so the station
# actually gets a pass every orbit. A real propagator would not: with a true
# ground track, most orbits miss a given station entirely, and you would wait
# hours between passes. That is the wrong trade for a development feed.
CLOSEST_APPROACH_PHASE = 0.4245


@dataclass(frozen=True)
class State:
    """The spacecraft at one instant of simulated time."""

    phase: float  # 0..1 through the orbit
    lat_deg: float
    lon_deg: float
    alt_km: float
    elevation_deg: float  # as seen from the ground station; < 0 means below horizon
    in_sunlight: bool

    @property
    def in_view(self) -> bool:
        return self.elevation_deg > 0.0


@dataclass(frozen=True)
class GroundStation:
    identifier: str
    lat_deg: float = 21.03  # Hanoi
    lon_deg: float = 105.85


def raw_track(phase: float) -> tuple[float, float]:
    """Sub-satellite latitude/longitude for a circular inclined orbit."""
    theta = 2.0 * math.pi * phase
    inc = math.radians(INCLINATION_DEG)
    lat = math.asin(math.sin(inc) * math.sin(theta))
    lon = math.atan2(math.cos(inc) * math.sin(theta), math.cos(theta))
    return math.degrees(lat), math.degrees(lon)


def anchor_offset_deg(station: GroundStation, orbit_index: int = 0) -> float:
    """Longitude shift that brings this orbit's closest approach over the station.

    `orbit_index` wanders the track a little so successive passes differ in
    maximum elevation instead of being identical overhead passes.
    """
    _, track_lon = raw_track(CLOSEST_APPROACH_PHASE)
    wander = 11.0 * math.sin(orbit_index * 1.7)
    return station.lon_deg - track_lon + wander


def propagate(phase: float, station: GroundStation, lon_offset_deg: float = 0.0) -> State:
    """Position and visibility at a fractional point through the orbit."""
    lat_deg, track_lon_deg = raw_track(phase)
    lon_deg = (track_lon_deg + lon_offset_deg + 180.0) % 360.0 - 180.0

    elevation = _elevation(lat_deg, lon_deg, ALTITUDE_KM, station)
    # Eclipse for roughly a third of the orbit, offset from the pass window.
    in_sunlight = not (0.55 <= phase < 0.90)

    return State(
        phase=phase,
        lat_deg=lat_deg,
        lon_deg=lon_deg,
        alt_km=ALTITUDE_KM,
        elevation_deg=elevation,
        in_sunlight=in_sunlight,
    )


def _elevation(lat: float, lon: float, alt_km: float, station: GroundStation) -> float:
    """Elevation angle of the satellite above the station's horizon, in degrees."""
    central = _great_circle_rad(lat, lon, station.lat_deg, station.lon_deg)
    r = EARTH_RADIUS_KM
    # Standard look-angle geometry for a spherical Earth.
    denom = math.sin(central)
    if denom < 1e-9:
        return 90.0
    return math.degrees(math.atan((math.cos(central) - r / (r + alt_km)) / denom))


def _great_circle_rad(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    cos_c = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dlon)
    return math.acos(max(-1.0, min(1.0, cos_c)))


@dataclass
class Spacecraft:
    """Battery, thermal, and mode state, integrated frame by frame.

    Every rate below is **per orbit**, and `step` is driven by how much of an
    orbit elapsed rather than by wall-clock seconds. That keeps the physics
    identical whether an orbit is compressed into 30 seconds or played out at
    90 minutes, and whether frames arrive 20 times a second or once a second.
    """

    battery_voltage_v: float = 8.0
    temperature_c: float = 20.0
    mode: str = "NOMINAL"
    _fault_phase_left: float = 0.0
    _fault_kind: str | None = None
    _fault_gain: float = 1.0

    BATTERY_FULL_V = 8.4
    BATTERY_EMPTY_V = 6.0

    # Volts gained/lost over one full orbit spent entirely in that condition.
    CHARGE_V_PER_ORBIT = 2.5
    ECLIPSE_DRAIN_V_PER_ORBIT = 3.4
    TRANSMIT_DRAIN_V_PER_ORBIT = 0.5

    SUNLIT_TEMP_C = 28.0
    ECLIPSE_TEMP_C = -12.0
    THERMAL_RESPONSE_PER_ORBIT = 4.0  # time constant ≈ a quarter orbit

    def step(
        self, state: State, dphase: float, faults_per_orbit: float, rng: random.Random
    ) -> None:
        """Advance the health model by `dphase` (a fraction of one orbit)."""
        rate = self.CHARGE_V_PER_ORBIT if state.in_sunlight else -self.ECLIPSE_DRAIN_V_PER_ORBIT
        if state.in_view:
            rate -= self.TRANSMIT_DRAIN_V_PER_ORBIT  # transmitter on
        self.battery_voltage_v += rate * dphase + rng.gauss(0, 0.05 * math.sqrt(dphase))

        target = self.SUNLIT_TEMP_C if state.in_sunlight else self.ECLIPSE_TEMP_C
        self.temperature_c += (target - self.temperature_c) * (
            1.0 - math.exp(-self.THERMAL_RESPONSE_PER_ORBIT * dphase)
        )
        self.temperature_c += rng.gauss(0, 3.0 * math.sqrt(dphase))

        if self._fault_phase_left > 0.0:
            self._apply_fault(dphase)
        elif rng.random() < faults_per_orbit * dphase:
            self._start_fault(rng)

        self.battery_voltage_v = min(
            self.BATTERY_FULL_V, max(self.BATTERY_EMPTY_V, self.battery_voltage_v)
        )
        # A deeply drained battery trips the spacecraft into SAFE mode, and it
        # only recovers once charged again — the alerting side should see a
        # sustained condition, not a one-frame blip.
        if self.battery_voltage_v < 6.5:
            self.mode = "SAFE"
        elif self.mode == "SAFE" and self.battery_voltage_v > 7.4:
            self.mode = "NOMINAL"

    def _start_fault(self, rng: random.Random) -> None:
        self._fault_kind = rng.choice(["battery_sag", "thermal_spike", "cold_soak"])
        self._fault_phase_left = rng.uniform(0.02, 0.06)  # 2–6% of an orbit
        # Most faults are minor wobbles; a few are severe enough to break a
        # documented limit. A simulator that only ever produced alarming values
        # would tell you nothing about false-positive rates.
        self._fault_gain = rng.uniform(0.5, 2.4)

    def _apply_fault(self, dphase: float) -> None:
        self._fault_phase_left = max(0.0, self._fault_phase_left - dphase)
        if self._fault_kind == "battery_sag":
            self.battery_voltage_v -= 18.0 * self._fault_gain * dphase
        elif self._fault_kind == "thermal_spike":
            self.temperature_c += 700.0 * self._fault_gain * dphase
        elif self._fault_kind == "cold_soak":
            self.temperature_c -= 500.0 * self._fault_gain * dphase
        if self._fault_phase_left == 0.0:
            self._fault_kind = None
            self._fault_gain = 1.0

    def signal_strength_dbm(self, state: State, rng: random.Random) -> float:
        """Stronger the higher the satellite is in the sky."""
        base = -110.0 + 0.45 * max(0.0, state.elevation_deg)
        return base + rng.gauss(0, 1.5)
