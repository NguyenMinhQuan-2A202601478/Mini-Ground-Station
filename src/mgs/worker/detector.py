"""Statistical anomaly detection over a rolling window of recent telemetry.

Two tiers, chosen by how much history exists and what is installed:

1. IsolationForest (needs the `ml` extra) once there are enough samples — it
   catches combinations that no single threshold would flag, e.g. a voltage
   that is legal on its own but wrong for that temperature.
2. Per-feature z-score otherwise — pure Python, always available, and the only
   honest thing to do during a cold start.

The threshold rules in `rules.py` run regardless; this layer only adds
findings, it never suppresses them.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass

from mgs.config import Settings
from mgs.models import Telemetry
from mgs.worker.rules import Finding

log = logging.getLogger("mgs.worker.detector")

FEATURES = ("battery_voltage_v", "temperature_c", "signal_strength_dbm")

try:  # optional: `pip install -e '.[ml]'`
    import numpy as np
    from sklearn.ensemble import IsolationForest

    ML_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the install profile
    np = None  # type: ignore[assignment]
    IsolationForest = None  # type: ignore[assignment]
    ML_AVAILABLE = False


def _vector(row: Telemetry) -> list[float]:
    # signal_strength_dbm is nullable; 0.0 is outside any real dBm range, so
    # substituting it would fabricate an outlier. Use the feature mean instead
    # by treating missing as NaN-free: fall back to a neutral -80 dBm.
    return [
        row.battery_voltage_v,
        row.temperature_c,
        row.signal_strength_dbm if row.signal_strength_dbm is not None else -80.0,
    ]


@dataclass
class _Baseline:
    mean: list[float]
    stdev: list[float]


class AnomalyDetector:
    """One detector per satellite. Refit cheaply from the rolling window."""

    def __init__(self, satellite_id: str, settings: Settings) -> None:
        self.satellite_id = satellite_id
        self.settings = settings
        self.baseline: _Baseline | None = None
        self.model = None

    @property
    def trained(self) -> bool:
        return self.baseline is not None

    def fit(self, history: list[Telemetry]) -> None:
        if not history:
            self.baseline = None
            self.model = None
            return

        columns = list(zip(*(_vector(r) for r in history), strict=True))
        self.baseline = _Baseline(
            mean=[statistics.fmean(c) for c in columns],
            # +1e-6 keeps a constant feature from dividing by zero.
            stdev=[(statistics.pstdev(c) if len(c) > 1 else 0.0) + 1e-6 for c in columns],
        )

        use_ml = (
            self.settings.enable_ml
            and ML_AVAILABLE
            and len(history) >= self.settings.ml_min_samples
        )
        if not use_ml:
            self.model = None
            if self.settings.enable_ml and not ML_AVAILABLE:
                log.debug("scikit-learn not installed; z-score only")
            return

        x = np.array([_vector(r) for r in history])
        self.model = IsolationForest(
            contamination=self.settings.ml_contamination, random_state=42
        ).fit(x)
        log.info("[%s] IsolationForest fitted on %d frames", self.satellite_id, len(history))

    def score(self, frame: Telemetry) -> Finding | None:
        if self.baseline is None:
            return None

        vec = _vector(frame)
        z = [
            abs((value - mean) / stdev)
            for value, mean, stdev in zip(vec, self.baseline.mean, self.baseline.stdev, strict=True)
        ]
        worst = max(range(len(z)), key=z.__getitem__)

        if self.model is not None:
            x = np.array([vec])
            if self.model.predict(x)[0] != -1:
                return None
            score = float(-self.model.decision_function(x)[0])
            return Finding(
                rule="ML_OUTLIER",
                severity="warning",
                message=(
                    f"IsolationForest flagged this frame (score {score:.3f}); the most "
                    f"unusual field is {FEATURES[worst]} at {z[worst]:.1f} sigma from the "
                    f"{self.settings.ml_training_window_hours}h baseline."
                ),
                satellite_id=frame.satellite_id,
                observed_at=frame.recorded_at,
                telemetry_id=frame.id,
                pass_id=frame.pass_id,
                metric=FEATURES[worst],
                value=vec[worst],
                score=score,
            )

        if z[worst] > self.settings.zscore_threshold:
            return Finding(
                rule="ML_OUTLIER",
                severity="warning",
                message=(
                    f"{FEATURES[worst]} is {z[worst]:.1f} standard deviations from its "
                    f"{self.settings.ml_training_window_hours}h baseline "
                    f"(no model yet — z-score fallback)."
                ),
                satellite_id=frame.satellite_id,
                observed_at=frame.recorded_at,
                telemetry_id=frame.id,
                pass_id=frame.pass_id,
                metric=FEATURES[worst],
                value=vec[worst],
                threshold=self.settings.zscore_threshold,
                score=z[worst],
            )
        return None
