"""The statistical layer, including its behaviour with no history at all."""

from __future__ import annotations

from mgs.config import Settings
from mgs.worker.detector import ML_AVAILABLE, AnomalyDetector
from tests.conftest import make_frame

# Force the z-score path so this test says the same thing whether or not the
# `ml` extra is installed.
SETTINGS = Settings(enable_ml=False, zscore_threshold=3.0)


def baseline_history(n: int = 100):
    return [make_frame(i, battery_voltage_v=7.8, temperature_c=20.0) for i in range(1, n + 1)]


def test_a_detector_with_no_history_never_fires():
    detector = AnomalyDetector("TEST-1", SETTINGS)
    detector.fit([])
    assert not detector.trained
    assert detector.score(make_frame(1, battery_voltage_v=0.1)) is None


def test_a_typical_frame_is_not_an_outlier():
    detector = AnomalyDetector("TEST-1", SETTINGS)
    detector.fit(baseline_history())
    assert detector.score(make_frame(200, battery_voltage_v=7.8, temperature_c=20.0)) is None


def test_a_wild_frame_is_an_outlier():
    detector = AnomalyDetector("TEST-1", SETTINGS)
    detector.fit(baseline_history())
    finding = detector.score(make_frame(200, temperature_c=250.0))

    assert finding is not None
    assert finding.rule == "ML_OUTLIER"
    assert finding.metric == "temperature_c"
    assert finding.score is not None and finding.score > SETTINGS.zscore_threshold


def test_a_constant_feature_does_not_divide_by_zero():
    """Every training frame identical: stdev is 0 and must not explode."""
    detector = AnomalyDetector("TEST-1", SETTINGS)
    detector.fit(baseline_history(10))
    assert detector.score(make_frame(200, battery_voltage_v=7.8, temperature_c=20.0)) is None


def test_missing_signal_strength_is_not_treated_as_zero():
    """0 dBm is outside any real range; substituting it would fabricate an outlier."""
    detector = AnomalyDetector("TEST-1", SETTINGS)
    detector.fit([make_frame(i, signal_strength_dbm=None) for i in range(1, 101)])
    assert detector.score(make_frame(200, signal_strength_dbm=None)) is None


def test_the_model_is_used_when_available_and_there_is_enough_history():
    settings = Settings(enable_ml=True, ml_min_samples=50)
    detector = AnomalyDetector("TEST-1", settings)
    detector.fit(baseline_history(100))
    assert (detector.model is not None) is ML_AVAILABLE


def test_too_little_history_falls_back_to_z_score():
    settings = Settings(enable_ml=True, ml_min_samples=50)
    detector = AnomalyDetector("TEST-1", settings)
    detector.fit(baseline_history(10))
    assert detector.model is None
    assert detector.trained
