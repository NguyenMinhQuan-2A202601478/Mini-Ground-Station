"""Real orbit propagation, checked against physics rather than against itself."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from mgs.simulator.orbit import (
    EARTH_RADIUS_KM,
    GroundStation,
    Propagator,
    Spacecraft,
    is_sunlit,
    sun_direction,
)
from mgs.simulator.tle import BUNDLED_TLE

STATION = GroundStation()
EPOCH = BUNDLED_TLE.epoch


@pytest.fixture(scope="module")
def propagator() -> Propagator:
    return Propagator(BUNDLED_TLE, STATION)


# --- the orbit itself -----------------------------------------------------


def test_the_period_matches_a_low_earth_orbit(propagator):
    """The ISS goes round in about 93 minutes. Anything else means bad elements."""
    minutes = propagator.period.total_seconds() / 60.0
    assert 88.0 < minutes < 96.0


def test_altitude_stays_in_the_iss_band(propagator):
    for minute in range(0, 95, 5):
        state = propagator.at(EPOCH + timedelta(minutes=minute))
        assert 380.0 < state.alt_km < 460.0, f"{state.alt_km} km at +{minute} min"


def test_the_ground_track_respects_the_inclination(propagator):
    """A 51.6° orbit never passes over Hanoi's antipode at 70° north."""
    latitudes = [propagator.at(EPOCH + timedelta(minutes=m)).lat_deg for m in range(0, 95, 2)]
    assert max(latitudes) <= 52.0
    assert min(latitudes) >= -52.0
    assert max(latitudes) > 45.0, "an inclined orbit should reach high latitudes"


def test_orbital_speed_is_right_for_the_altitude(propagator):
    """v = sqrt(mu/r) for a near-circular orbit: about 7.66 km/s at 420 km."""
    mu = 398600.4418
    state = propagator.at(EPOCH)
    expected = math.sqrt(mu / (EARTH_RADIUS_KM + state.alt_km))
    # Derive the speed from the period instead of trusting the propagator twice.
    circumference = 2 * math.pi * (EARTH_RADIUS_KM + state.alt_km)
    from_period = circumference / propagator.period.total_seconds()
    assert abs(from_period - expected) < 0.15, f"{from_period} vs {expected} km/s"


# --- passes ---------------------------------------------------------------


def test_a_day_holds_a_handful_of_passes(propagator):
    """Not one per orbit. Most orbits miss the station entirely."""
    windows = propagator.passes(EPOCH, EPOCH + timedelta(hours=24))
    assert 2 <= len(windows) <= 8, f"{len(windows)} passes in 24 h"


def test_passes_are_minutes_long_not_hours(propagator):
    for window in propagator.passes(EPOCH, EPOCH + timedelta(hours=24)):
        minutes = window.duration.total_seconds() / 60.0
        assert 0.5 < minutes < 12.0, f"a {minutes:.1f} minute pass is not physical"


def test_a_pass_rises_culminates_and_sets_in_that_order(propagator):
    for window in propagator.passes(EPOCH, EPOCH + timedelta(hours=24)):
        assert window.aos < window.tca < window.los


def test_the_culmination_really_is_the_highest_point(propagator):
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    step = window.duration / 20
    sampled = [propagator.at(window.aos + step * i).look.elevation_deg for i in range(21)]
    assert max(sampled) <= window.max_elevation_deg + 0.5


def test_the_station_hears_nothing_below_its_horizon(propagator):
    """`in_view` is the station's mask, not merely "above zero degrees"."""
    grazing = propagator.at(EPOCH)
    assert grazing.in_view == (grazing.look.elevation_deg >= STATION.min_elevation_deg)


def test_range_shrinks_as_the_satellite_climbs(propagator):
    """Overhead is close; near the horizon is three or four times further."""
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    at_horizon = propagator.at(window.aos).look.range_km
    at_peak = propagator.at(window.tca).look.range_km
    assert at_peak < at_horizon
    assert 350.0 < at_peak < 2100.0


def test_range_rate_changes_sign_across_the_culmination(propagator):
    """Approaching, then receding — which is what makes Doppler tracking a thing."""
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    approaching = propagator.at(window.aos).look.range_rate_km_s
    receding = propagator.at(window.los).look.range_rate_km_s
    assert approaching < 0 < receding


def test_doppler_shift_is_kilohertz_scale(propagator):
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    shift = abs(propagator.at(window.aos).look.doppler_hz)
    assert 2_000 < shift < 15_000, f"{shift:.0f} Hz at 437 MHz is not plausible"


def test_the_station_is_silent_most_of_the_day(propagator):
    """The operational fact the whole design turns on."""
    windows = propagator.passes(EPOCH, EPOCH + timedelta(hours=24))
    contact = sum(w.duration.total_seconds() for w in windows)
    assert contact / 86400.0 < 0.05, "a single station sees a LEO satellite ~1% of the day"


# --- eclipse --------------------------------------------------------------


def test_the_sun_vector_is_a_unit_vector():
    for month in range(1, 13):
        vector = sun_direction(datetime(2026, month, 15, 12, tzinfo=UTC))
        assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-9


def test_the_sun_moves_a_degree_a_day():
    a = sun_direction(datetime(2026, 3, 20, tzinfo=UTC))
    b = sun_direction(datetime(2026, 3, 21, tzinfo=UTC))
    degrees = math.degrees(math.acos(float(np.dot(a, b))))
    assert 0.9 < degrees < 1.1


def test_a_satellite_between_earth_and_sun_is_lit():
    sun = np.array([1.0, 0.0, 0.0])
    assert is_sunlit(np.array([EARTH_RADIUS_KM + 400.0, 0.0, 0.0]), sun) is True


def test_a_satellite_directly_behind_the_earth_is_in_shadow():
    sun = np.array([1.0, 0.0, 0.0])
    assert is_sunlit(np.array([-(EARTH_RADIUS_KM + 400.0), 0.0, 0.0]), sun) is False


def test_a_satellite_behind_the_earth_but_outside_its_silhouette_is_lit():
    sun = np.array([1.0, 0.0, 0.0])
    beside = np.array([-7000.0, EARTH_RADIUS_KM + 100.0, 0.0])
    assert is_sunlit(beside, sun) is True


def test_eclipse_covers_a_realistic_share_of_the_orbit(propagator):
    """The ISS is in sunlight roughly 60-65% of each orbit."""
    samples = [propagator.at(EPOCH + timedelta(minutes=m)).in_sunlight for m in range(0, 93)]
    lit = sum(samples) / len(samples)
    assert 0.50 < lit < 0.75, f"{lit:.0%} sunlit"


# --- the link -------------------------------------------------------------


def test_received_power_falls_off_with_range(propagator):
    rng = random.Random(0)
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    at_peak = propagator.signal_strength_dbm(propagator.at(window.tca).look, rng)
    at_horizon = propagator.signal_strength_dbm(propagator.at(window.aos).look, rng)
    assert at_peak > at_horizon + 5.0, "an overhead pass should be markedly stronger"


def test_received_power_is_in_a_plausible_band(propagator):
    rng = random.Random(0)
    window = max(
        propagator.passes(EPOCH, EPOCH + timedelta(hours=24)),
        key=lambda w: w.max_elevation_deg,
    )
    step = window.duration / 10
    for i in range(11):
        look = propagator.at(window.aos + step * i).look
        dbm = propagator.signal_strength_dbm(look, rng)
        assert -125.0 < dbm < -80.0, f"{dbm:.1f} dBm at {look.elevation_deg:.1f}°"


# --- the spacecraft health model -----------------------------------------


def test_health_model_does_not_depend_on_the_sampling_rate():
    """Coarse and fine sampling must describe the same spacecraft.

    This is the property that broke first: with per-frame rates instead of
    per-orbit ones, a fine sampling interval random-walked the temperature to
    -180 °C while a coarse one looked fine.
    """
    summaries = []
    for steps in (240, 4800):
        rng = random.Random(11)
        craft = Spacecraft()
        temps, volts = [], []
        for orbit in range(3):
            for i in range(steps):
                sunlit = not (0.55 <= i / steps < 0.90)
                craft.step(_fake_state(sunlit), 1 / steps, 0.0, rng)
                temps.append(craft.temperature_c)
                volts.append(craft.battery_voltage_v)
            del orbit
        summaries.append((min(temps), max(temps), min(volts), max(volts)))

    coarse, fine = summaries
    for a, b in zip(coarse, fine, strict=True):
        assert abs(a - b) < 12.0, f"{coarse} vs {fine}"


def test_battery_stays_within_its_physical_limits():
    rng = random.Random(3)
    craft = Spacecraft()
    for i in range(2000):
        craft.step(_fake_state(i % 100 < 62), 1 / 400, 2.0, rng)
        assert Spacecraft.BATTERY_EMPTY_V <= craft.battery_voltage_v <= Spacecraft.BATTERY_FULL_V


def test_a_drained_battery_trips_safe_mode_and_recovers():
    rng = random.Random(0)
    craft = Spacecraft(battery_voltage_v=6.2)
    craft.step(_fake_state(True), 0.001, 0.0, rng)
    assert craft.mode == "SAFE"

    craft.battery_voltage_v = 7.6
    craft.step(_fake_state(True), 0.001, 0.0, rng)
    assert craft.mode == "NOMINAL"


def _fake_state(in_sunlight: bool):
    """The health model only reads two flags off the state."""
    from mgs.simulator.orbit import LookAngles, State

    return State(
        when=EPOCH,
        lat_deg=0.0,
        lon_deg=0.0,
        alt_km=420.0,
        look=LookAngles(elevation_deg=-10.0, azimuth_deg=0.0, range_km=2000.0, range_rate_km_s=0.0),
        in_sunlight=in_sunlight,
        in_view=False,
    )
