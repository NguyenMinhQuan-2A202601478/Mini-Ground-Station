"""The simulator's orbit and health model."""

from __future__ import annotations

import random

from mgs.simulator.orbit import (
    GroundStation,
    Spacecraft,
    anchor_offset_deg,
    propagate,
)

STATION = GroundStation("TEST-GS")


def sample_orbit(offset: float, n: int = 500):
    return [propagate(i / n, STATION, offset) for i in range(n)]


def test_every_orbit_produces_a_pass():
    for orbit in range(6):
        states = sample_orbit(anchor_offset_deg(STATION, orbit))
        assert any(s.in_view for s in states), f"orbit {orbit} never came into view"


def test_a_pass_is_a_small_part_of_the_orbit():
    states = sample_orbit(anchor_offset_deg(STATION, 0))
    fraction = sum(s.in_view for s in states) / len(states)
    assert 0.03 < fraction < 0.25, f"pass covers {fraction:.0%} of the orbit"


def test_successive_passes_differ_in_geometry():
    peaks = [
        max(s.elevation_deg for s in sample_orbit(anchor_offset_deg(STATION, o))) for o in range(4)
    ]
    assert len(set(round(p) for p in peaks)) > 1


def test_ground_track_stays_on_the_globe():
    for state in sample_orbit(anchor_offset_deg(STATION, 0)):
        assert -90 <= state.lat_deg <= 90
        assert -180 <= state.lon_deg <= 180


def test_health_model_does_not_depend_on_the_frame_rate():
    """Coarse and fine sampling must describe the same spacecraft.

    This is the property that broke first: with per-frame rates instead of
    per-orbit ones, a 0.05 s frame interval random-walked the temperature to
    -180 °C while a 1 s interval looked fine.
    """
    summaries = []
    for frames in (240, 4800):
        rng = random.Random(11)
        craft = Spacecraft()
        offset = anchor_offset_deg(STATION, 0)
        temps, volts = [], []
        for orbit in range(3):
            offset = anchor_offset_deg(STATION, orbit)
            for i in range(frames):
                craft.step(propagate(i / frames, STATION, offset), 1 / frames, 0.0, rng)
                temps.append(craft.temperature_c)
                volts.append(craft.battery_voltage_v)
        summaries.append((min(temps), max(temps), min(volts), max(volts)))

    coarse, fine = summaries
    for a, b in zip(coarse, fine, strict=True):
        assert abs(a - b) < 12.0, f"{coarse} vs {fine}"


def test_battery_stays_within_its_physical_limits():
    rng = random.Random(3)
    craft = Spacecraft()
    for orbit in range(5):
        offset = anchor_offset_deg(STATION, orbit)
        for i in range(400):
            craft.step(propagate(i / 400, STATION, offset), 1 / 400, 2.0, rng)
            assert (
                Spacecraft.BATTERY_EMPTY_V <= craft.battery_voltage_v <= Spacecraft.BATTERY_FULL_V
            )


def test_a_drained_battery_trips_safe_mode_and_recovers():
    rng = random.Random(0)
    craft = Spacecraft(battery_voltage_v=6.2)
    state = propagate(0.0, STATION, anchor_offset_deg(STATION, 0))
    craft.step(state, 0.001, 0.0, rng)
    assert craft.mode == "SAFE"

    craft.battery_voltage_v = 7.6
    craft.step(state, 0.001, 0.0, rng)
    assert craft.mode == "NOMINAL"
