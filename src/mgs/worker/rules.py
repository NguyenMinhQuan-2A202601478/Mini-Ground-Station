"""Deterministic threshold rules.

These run always, need no dependencies, and are the rules an operator can
argue with: a number crossed a documented limit. The statistical detector in
`detector.py` is the layer on top that catches what nobody wrote a limit for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mgs.config import Settings
from mgs.models import Telemetry
from mgs.schemas import Limits


@dataclass(frozen=True)
class Finding:
    """One thing worth telling an operator about."""

    rule: str
    severity: str
    message: str
    satellite_id: str
    # When the condition happened on board, not when the worker noticed it.
    # Screening can run minutes or days after a downlink; an alert timestamped
    # with the screening time would misplace the event in every chart.
    observed_at: datetime | None = None
    telemetry_id: int | None = None
    pass_id: int | None = None
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    score: float | None = None

    @property
    def dedupe_key(self) -> str:
        anchor = self.telemetry_id if self.telemetry_id is not None else f"pass:{self.pass_id}"
        return f"{self.satellite_id}:{self.rule}:{anchor}"


def evaluate_frame(frame: Telemetry, limits: Limits) -> list[Finding]:
    """Apply every threshold rule to one frame.

    `limits` are this spacecraft's, resolved from its own overrides falling
    back to the station defaults — see `mgs.limits`.
    """
    findings: list[Finding] = []

    def add(rule: str, severity: str, metric: str, value: float, threshold: float, msg: str):
        findings.append(
            Finding(
                rule=rule,
                severity=severity,
                message=msg,
                satellite_id=frame.satellite_id,
                observed_at=frame.recorded_at,
                telemetry_id=frame.id,
                pass_id=frame.pass_id,
                metric=metric,
                value=value,
                threshold=threshold,
            )
        )

    v = frame.battery_voltage_v
    if v <= limits.battery_critical_v:
        add(
            "BATTERY_LOW",
            "critical",
            "battery_voltage_v",
            v,
            limits.battery_critical_v,
            f"Battery at {v:.2f} V, at or below the critical floor of "
            f"{limits.battery_critical_v:.2f} V — the spacecraft is at risk of a brownout.",
        )
    elif v < limits.battery_min_v:
        add(
            "BATTERY_LOW",
            "warning",
            "battery_voltage_v",
            v,
            limits.battery_min_v,
            f"Battery at {v:.2f} V, below the nominal minimum of {limits.battery_min_v:.2f} V.",
        )

    t = frame.temperature_c
    if t > limits.temp_max_c:
        add(
            "TEMP_HIGH",
            "critical" if t > limits.temp_max_c + 15 else "warning",
            "temperature_c",
            t,
            limits.temp_max_c,
            f"Temperature {t:.1f} °C exceeds the {limits.temp_max_c:.1f} °C limit.",
        )
    elif t < limits.temp_min_c:
        add(
            "TEMP_LOW",
            "critical" if t < limits.temp_min_c - 15 else "warning",
            "temperature_c",
            t,
            limits.temp_min_c,
            f"Temperature {t:.1f} °C is below the {limits.temp_min_c:.1f} °C limit.",
        )

    if frame.mode == "SAFE":
        findings.append(
            Finding(
                rule="SAFE_MODE",
                severity="critical",
                message="Spacecraft reported SAFE mode — payload operations are suspended.",
                satellite_id=frame.satellite_id,
                observed_at=frame.recorded_at,
                telemetry_id=frame.id,
                pass_id=frame.pass_id,
                metric="mode",
            )
        )

    return findings


def evaluate_gap(frame: Telemetry, previous_seq: int | None, settings: Settings) -> Finding | None:
    """Frames are numbered on board; a jump means we lost data over the link."""
    if previous_seq is None:
        return None
    missing = frame.seq - previous_seq - 1
    if missing < settings.max_seq_gap:
        return None
    return Finding(
        rule="DATA_GAP",
        severity="warning" if missing < 10 else "critical",
        message=(
            f"{missing} frame(s) missing between seq {previous_seq} and {frame.seq} — "
            f"downlink loss or an on-board reset."
        ),
        satellite_id=frame.satellite_id,
        observed_at=frame.recorded_at,
        telemetry_id=frame.id,
        pass_id=frame.pass_id,
        metric="seq",
        value=float(frame.seq),
        threshold=float(previous_seq),
    )
