"""Threshold rules: the limits an operator can point at."""

from __future__ import annotations

from mgs.config import Settings
from mgs.worker.rules import evaluate_frame, evaluate_gap
from tests.conftest import make_frame

SETTINGS = Settings(battery_min_v=6.8, battery_critical_v=6.4, temp_max_c=60.0, temp_min_c=-30.0)


def rules_for(**overrides) -> set[str]:
    return {f.rule for f in evaluate_frame(make_frame(1, **overrides), SETTINGS)}


def test_nominal_frame_raises_nothing():
    assert rules_for() == set()


def test_battery_below_minimum_is_a_warning():
    findings = evaluate_frame(make_frame(1, battery_voltage_v=6.7), SETTINGS)
    assert [(f.rule, f.severity) for f in findings] == [("BATTERY_LOW", "warning")]


def test_battery_at_the_critical_floor_is_critical():
    # The floor is inclusive: 6.4 V is already the critical condition.
    findings = evaluate_frame(make_frame(1, battery_voltage_v=6.4), SETTINGS)
    assert [(f.rule, f.severity) for f in findings] == [("BATTERY_LOW", "critical")]


def test_battery_exactly_at_minimum_is_not_an_alert():
    assert rules_for(battery_voltage_v=6.8) == set()


def test_temperature_limits_both_ways():
    assert rules_for(temperature_c=61.0) == {"TEMP_HIGH"}
    assert rules_for(temperature_c=-31.0) == {"TEMP_LOW"}
    assert rules_for(temperature_c=20.0) == set()


def test_far_past_the_temperature_limit_escalates():
    hot = evaluate_frame(make_frame(1, temperature_c=80.0), SETTINGS)
    assert hot[0].severity == "critical"


def test_safe_mode_is_reported_on_its_own():
    assert rules_for(mode="SAFE") == {"SAFE_MODE"}


def test_one_frame_can_trip_several_rules():
    assert rules_for(battery_voltage_v=6.3, temperature_c=90.0, mode="SAFE") == {
        "BATTERY_LOW",
        "TEMP_HIGH",
        "SAFE_MODE",
    }


def test_sequence_gap_is_detected():
    finding = evaluate_gap(make_frame(10), previous_seq=7, settings=SETTINGS)
    assert finding is not None
    assert finding.rule == "DATA_GAP"
    assert "2 frame(s) missing" in finding.message


def test_consecutive_sequence_numbers_are_not_a_gap():
    assert evaluate_gap(make_frame(8), previous_seq=7, settings=SETTINGS) is None


def test_first_frame_ever_has_no_gap():
    assert evaluate_gap(make_frame(1), previous_seq=None, settings=SETTINGS) is None


def test_a_large_gap_is_critical():
    finding = evaluate_gap(make_frame(50), previous_seq=10, settings=SETTINGS)
    assert finding is not None and finding.severity == "critical"


def test_dedupe_key_is_stable_per_frame_and_rule():
    a, b = (evaluate_frame(make_frame(1, battery_voltage_v=6.0), SETTINGS)[0] for _ in range(2))
    assert a.dedupe_key == b.dedupe_key
