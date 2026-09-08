"""Real orbit propagation: SGP4 against a published element set.

This replaces a hand-rolled circular-orbit model that had to cheat — it
anchored the ground track over the station so that every orbit produced a
pass. Real geometry does not work that way, and the difference is the whole
point: a low-Earth satellite is in view of one station for about ten minutes,
a few times a day, in clusters separated by long silences. Scheduling around
that silence is what a ground station *is*.

Positions come from Skyfield's SGP4, the same propagator the published TLEs
are meant to be used with. Look angles, ranges, and range rates are the real
ones; the spacecraft's battery and thermal behaviour is still a model, but it
is now driven by real eclipse geometry and a real orbital period.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from skyfield.api import EarthSatellite, load, wgs84

from mgs.simulator.tle import TLE

EARTH_RADIUS_KM = 6378.137
SPEED_OF_LIGHT_KM_S = 299792.458

# A 70 cm amateur/cubesat downlink, which is what most of these spacecraft use.
DOWNLINK_FREQ_MHZ = 437.5
SPACECRAFT_EIRP_DBM = 30.0  # ~1 W into a modest antenna
STATION_GAIN_DBI = 15.0
ZENITH_ATMOSPHERIC_LOSS_DB = 0.5

# Skyfield builds its own timescale from bundled leap-second data, so nothing
# here touches the network.
_TIMESCALE = load.timescale()


@dataclass(frozen=True)
class GroundStation:
    """Where the dish is. Real coordinates, because the geometry is real."""

    identifier: str = "HANOI-GS"
    lat_deg: float = 21.0278
    lon_deg: float = 105.8342
    elevation_m: float = 20.0
    # Below this the spacecraft is behind terrain and clutter, not merely low.
    min_elevation_deg: float = 5.0


@dataclass(frozen=True)
class LookAngles:
    elevation_deg: float
    azimuth_deg: float
    range_km: float
    # Negative while the satellite is approaching, positive as it recedes.
    range_rate_km_s: float

    @property
    def doppler_hz(self) -> float:
        return -DOWNLINK_FREQ_MHZ * 1e6 * self.range_rate_km_s / SPEED_OF_LIGHT_KM_S


@dataclass(frozen=True)
class State:
    """The spacecraft, and how the station sees it, at one instant."""

    when: datetime
    lat_deg: float
    lon_deg: float
    alt_km: float
    look: LookAngles
    in_sunlight: bool
    in_view: bool


@dataclass(frozen=True)
class PassWindow:
    aos: datetime
    tca: datetime  # time of closest approach — the culmination
    los: datetime
    max_elevation_deg: float

    @property
    def duration(self) -> timedelta:
        return self.los - self.aos


def sun_direction(when: datetime) -> np.ndarray:
    """Unit vector to the Sun in the equatorial frame.

    The low-precision series from the Astronomical Almanac: good to about
    0.01°, which is four orders of magnitude better than an eclipse test needs,
    and it avoids shipping a 17 MB planetary ephemeris to answer "is it in the
    Earth's shadow".
    """
    t = _TIMESCALE.from_datetime(when)
    n = t.tt - 2451545.0
    mean_longitude = math.radians((280.460 + 0.9856474 * n) % 360.0)
    mean_anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_longitude = (
        mean_longitude
        + math.radians(1.915) * math.sin(mean_anomaly)
        + math.radians(0.020) * math.sin(2 * mean_anomaly)
    )
    obliquity = math.radians(23.439 - 4.0e-7 * n)
    return np.array(
        [
            math.cos(ecliptic_longitude),
            math.cos(obliquity) * math.sin(ecliptic_longitude),
            math.sin(obliquity) * math.sin(ecliptic_longitude),
        ]
    )


def is_sunlit(position_km: np.ndarray, sun: np.ndarray) -> bool:
    """Cylindrical shadow test: behind the Earth, and within its silhouette."""
    along_sun = float(np.dot(position_km, sun))
    if along_sun > 0.0:
        return True  # on the sunward side of the Earth
    perpendicular = float(np.linalg.norm(position_km - along_sun * sun))
    return perpendicular > EARTH_RADIUS_KM


class Propagator:
    """One satellite, one ground station, real geometry between them."""

    def __init__(self, tle: TLE, station: GroundStation) -> None:
        self.tle = tle
        self.station = station
        self.satellite = EarthSatellite(tle.line1, tle.line2, tle.name, _TIMESCALE)
        self._topos = wgs84.latlon(
            station.lat_deg, station.lon_deg, elevation_m=station.elevation_m
        )
        self._relative = self.satellite - self._topos

    @property
    def name(self) -> str:
        return self.tle.name

    @property
    def period(self) -> timedelta:
        """Orbital period, from the element set's own mean motion."""
        revs_per_day = self.satellite.model.no_kozai * 1440.0 / (2.0 * math.pi)
        return timedelta(days=1.0 / revs_per_day)

    def at(self, when: datetime) -> State:
        t = _TIMESCALE.from_datetime(_as_utc(when))
        geocentric = self.satellite.at(t)
        subpoint = wgs84.subpoint(geocentric)

        altitude, azimuth, distance, _, _, range_rate = self._relative.at(t).frame_latlon_and_rates(
            self._topos
        )
        look = LookAngles(
            elevation_deg=altitude.degrees,
            azimuth_deg=azimuth.degrees % 360.0,
            range_km=distance.km,
            range_rate_km_s=range_rate.km_per_s,
        )
        return State(
            when=when,
            lat_deg=subpoint.latitude.degrees,
            lon_deg=subpoint.longitude.degrees,
            alt_km=subpoint.elevation.km,
            look=look,
            in_sunlight=is_sunlit(geocentric.position.km, sun_direction(when)),
            in_view=look.elevation_deg >= self.station.min_elevation_deg,
        )

    def passes(self, start: datetime, end: datetime) -> list[PassWindow]:
        """Every contact window in the interval, the way a scheduler asks for it."""
        t0 = _TIMESCALE.from_datetime(_as_utc(start))
        t1 = _TIMESCALE.from_datetime(_as_utc(end))
        times, events = self.satellite.find_events(
            self._topos, t0, t1, altitude_degrees=self.station.min_elevation_deg
        )

        windows: list[PassWindow] = []
        aos: datetime | None = None
        tca: datetime | None = None
        peak = 0.0
        for time, event in zip(times, events, strict=True):
            when = time.utc_datetime()
            if event == 0:  # rise
                aos, tca, peak = when, None, 0.0
            elif event == 1 and aos is not None:  # culminate
                tca = when
                peak = self.at(when).look.elevation_deg
            elif event == 2 and aos is not None:  # set
                windows.append(
                    PassWindow(aos=aos, tca=tca or aos, los=when, max_elevation_deg=peak)
                )
                aos, tca, peak = None, None, 0.0
        return windows

    def signal_strength_dbm(self, look: LookAngles, rng: random.Random) -> float:
        """Received power from the link geometry, not from a fudge factor.

        Free-space path loss grows with range, which is why a pass that only
        reaches 15° is quieter throughout than one that goes overhead: at 5°
        the spacecraft is four times further away than at zenith.
        """
        fspl_db = (
            20.0 * math.log10(max(look.range_km, 1.0))
            + 20.0 * math.log10(DOWNLINK_FREQ_MHZ)
            + 32.44
        )
        elevation = max(look.elevation_deg, 1.0)
        atmospheric_db = ZENITH_ATMOSPHERIC_LOSS_DB / math.sin(math.radians(elevation))
        return (
            SPACECRAFT_EIRP_DBM + STATION_GAIN_DBI - fspl_db - atmospheric_db + rng.gauss(0.0, 1.2)
        )


def _as_utc(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


@dataclass
class Spacecraft:
    """Battery, thermal, and mode state, integrated along the orbit.

    Rates are per *orbit*, and `step` is driven by how much of an orbit
    elapsed. The physics is then the same whether the recorder samples every
    second or every minute, and whether the run is played at real speed or
    compressed a thousandfold.
    """

    battery_voltage_v: float = 8.0
    temperature_c: float = 20.0
    mode: str = "NOMINAL"
    _fault_phase_left: float = 0.0
    _fault_kind: str | None = None
    _fault_gain: float = 1.0

    BATTERY_FULL_V = 8.4
    BATTERY_EMPTY_V = 6.0

    # Volts gained or lost over one full orbit spent entirely in that condition.
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
            self.temperature_c += 300.0 * self._fault_gain * dphase
        elif self._fault_kind == "cold_soak":
            self.temperature_c -= 260.0 * self._fault_gain * dphase
        if self._fault_phase_left == 0.0:
            self._fault_kind = None
            self._fault_gain = 1.0
