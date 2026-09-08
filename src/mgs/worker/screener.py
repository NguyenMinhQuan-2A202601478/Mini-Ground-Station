"""One screening pass over unscreened telemetry. Pure function of the DB state.

Split out from the loop in `__main__.py` so it can be called once from a test
or a cron job without starting a daemon.

The important behaviour here is **episode-based alerting**. A battery that
stays under its floor for three hundred frames is one problem, not three
hundred alerts: the first frame of the condition opens an alert, later frames
only escalate it, and the frame where the condition clears resolves it. Rules
that describe a moment rather than a state — a downlink gap, a statistical
outlier — stay one alert per frame.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mgs.config import Settings
from mgs.models import Alert, Telemetry
from mgs.worker.detector import AnomalyDetector
from mgs.worker.rules import Finding, evaluate_frame, evaluate_gap

log = logging.getLogger("mgs.worker")

# Rules that describe a *state* the spacecraft is in, and therefore an episode
# with a beginning and an end. Everything else is a point-in-time event.
#
# ML_OUTLIER belongs here too: "operating outside its learned envelope" is a
# condition that lasts. Screening three orbits produced 142 separate outlier
# alerts as point events, nearly all of them consecutive frames from the same
# two excursions.
EPISODIC_RULES = frozenset({"BATTERY_LOW", "TEMP_HIGH", "TEMP_LOW", "SAFE_MODE", "ML_OUTLIER"})

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def fetch_unscreened(session: Session, limit: int) -> list[Telemetry]:
    """Claim the oldest unscreened frames.

    `FOR UPDATE SKIP LOCKED` means a second worker process can be started
    without the two of them fighting over the same rows.
    """
    stmt = (
        select(Telemetry)
        .where(Telemetry.screened_at.is_(None))
        .order_by(Telemetry.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def training_history(session: Session, satellite_id: str, settings: Settings) -> list[Telemetry]:
    cutoff = datetime.now(UTC) - timedelta(hours=settings.ml_training_window_hours)
    stmt = (
        select(Telemetry)
        .where(
            Telemetry.satellite_id == satellite_id,
            Telemetry.recorded_at >= cutoff,
            Telemetry.screened_at.is_not(None),
        )
        .order_by(Telemetry.recorded_at)
    )
    return list(session.scalars(stmt))


def previous_seq(session: Session, satellite_id: str, seq: int) -> int | None:
    return session.scalar(
        select(func.max(Telemetry.seq)).where(
            Telemetry.satellite_id == satellite_id, Telemetry.seq < seq
        )
    )


def open_episodes(session: Session, satellite_ids: list[str]) -> dict[tuple[str, str], Alert]:
    """Unresolved episodic alerts, keyed by (satellite_id, rule)."""
    stmt = select(Alert).where(
        Alert.satellite_id.in_(satellite_ids),
        Alert.rule.in_(EPISODIC_RULES),
        Alert.resolved_at.is_(None),
    )
    return {(a.satellite_id, a.rule): a for a in session.scalars(stmt)}


def _insert_point_alerts(session: Session, findings: list[Finding]) -> int:
    """Insert one alert per finding, skipping any already recorded."""
    if not findings:
        return 0
    rows = [
        {
            "telemetry_id": f.telemetry_id,
            "pass_id": f.pass_id,
            "satellite_id": f.satellite_id,
            "rule": f.rule,
            "severity": f.severity,
            "metric": f.metric,
            "value": f.value,
            "threshold": f.threshold,
            "score": f.score,
            "message": f.message,
            "detected_at": f.observed_at,
            "dedupe_key": f.dedupe_key,
        }
        for f in findings
    ]
    stmt = (
        insert(Alert)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_alerts_dedupe")
        .returning(Alert.id)
    )
    return len(session.execute(stmt).scalars().all())


def screen_once(session: Session, settings: Settings) -> tuple[int, int]:
    """Screen one batch. Returns (frames screened, alerts opened)."""
    frames = fetch_unscreened(session, settings.worker_batch_size)
    if not frames:
        return 0, 0

    by_satellite: dict[str, list[Telemetry]] = defaultdict(list)
    for frame in frames:
        by_satellite[frame.satellite_id].append(frame)

    episodes = open_episodes(session, list(by_satellite))
    point_findings: list[Finding] = []
    opened = 0

    for satellite_id, satellite_frames in by_satellite.items():
        detector = AnomalyDetector(satellite_id, settings)
        detector.fit(training_history(session, satellite_id, settings))

        satellite_frames.sort(key=lambda f: f.seq)
        prev = previous_seq(session, satellite_id, satellite_frames[0].seq)

        for frame in satellite_frames:
            findings = evaluate_frame(frame, settings)

            gap = evaluate_gap(frame, prev, settings)
            if gap:
                point_findings.append(gap)
            outlier = detector.score(frame)
            if outlier:
                findings.append(outlier)
            prev = frame.seq

            active: dict[str, Finding] = {}
            for finding in findings:
                if finding.rule in EPISODIC_RULES:
                    active[finding.rule] = finding
                else:
                    point_findings.append(finding)

            opened += _reconcile_episodes(session, frame, active, episodes)

    created = _insert_point_alerts(session, point_findings) + opened

    now = datetime.now(UTC)
    session.execute(
        update(Telemetry).where(Telemetry.id.in_([f.id for f in frames])).values(screened_at=now)
    )
    session.commit()

    log.info("screened %d frame(s), raised %d alert(s)", len(frames), created)
    return len(frames), created


def _reconcile_episodes(
    session: Session,
    frame: Telemetry,
    active: dict[str, Finding],
    episodes: dict[tuple[str, str], Alert],
) -> int:
    """Open, escalate, or resolve episodic alerts in light of one frame."""
    opened = 0

    for rule, finding in active.items():
        key = (frame.satellite_id, rule)
        existing = episodes.get(key)
        if existing is None:
            alert = Alert(
                telemetry_id=finding.telemetry_id,
                pass_id=finding.pass_id,
                satellite_id=finding.satellite_id,
                rule=finding.rule,
                severity=finding.severity,
                metric=finding.metric,
                value=finding.value,
                threshold=finding.threshold,
                score=finding.score,
                message=finding.message,
                detected_at=finding.observed_at,
                dedupe_key=f"{finding.satellite_id}:{rule}:episode:{finding.telemetry_id}",
            )
            session.add(alert)
            session.flush()
            episodes[key] = alert
            opened += 1
            log.warning("[%s] %s opened — %s", frame.satellite_id, rule, finding.message)
        elif SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[existing.severity]:
            # The same episode got worse: escalate in place rather than
            # opening a second alert for one continuing condition.
            existing.severity = finding.severity
            existing.value = finding.value
            existing.threshold = finding.threshold
            existing.message = finding.message
            session.flush()
            log.warning("[%s] %s escalated to %s", frame.satellite_id, rule, finding.severity)

    for (satellite_id, rule), alert in list(episodes.items()):
        if satellite_id != frame.satellite_id or rule in active:
            continue
        alert.resolved_at = frame.recorded_at
        session.flush()
        del episodes[(satellite_id, rule)]
        log.info("[%s] %s cleared at seq %d", satellite_id, rule, frame.seq)

    return opened
