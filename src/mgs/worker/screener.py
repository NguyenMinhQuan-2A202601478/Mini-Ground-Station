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
from mgs.limits import resolve as resolve_limits
from mgs.models import Alert, Satellite, Telemetry
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

# On reproducibility. The threshold and gap rules depend only on the frame in
# front of them, so they are the same however the work is divided. `ML_OUTLIER`
# depends on the history screened *before* it, so it is reproducible from the
# same starting state — re-screening a whole day gives the same alerts every
# time — but a worker that screened each pass as it landed had less history
# than one screening the day in bulk, and may reasonably disagree. That is a
# property of any online detector, not a defect.

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


# One arbitrary, stable key. Screening is a sequential fold over a satellite's
# frames — deciding whether *this* frame continues the episode the last one
# opened — so two workers splitting one stream between them would each see a
# fragment and open an episode for it. Ingestion stays fully parallel; only
# screening is single-flight.
SCREENING_LOCK_KEY = 0x6D67_7301


def claim_screening_lock(session: Session, *, wait: bool = False) -> bool:
    """Take the advisory lock for this batch.

    The lock is held for the transaction, so a worker that crashes mid-batch
    releases it when its connection dies — there is nothing to clean up.

    A polling worker does not wait: it will look again in a few seconds. A
    one-shot run does, because "someone else is screening" and "there is
    nothing left to screen" are different answers, and exiting on the first as
    if it were the second leaves the whole backlog sitting there.
    """
    if wait:
        session.execute(select(func.pg_advisory_xact_lock(SCREENING_LOCK_KEY)))
        return True
    return bool(session.scalar(select(func.pg_try_advisory_xact_lock(SCREENING_LOCK_KEY))))


def fetch_unscreened(session: Session, limit: int) -> list[Telemetry]:
    """Claim the oldest unscreened frames.

    `FOR UPDATE SKIP LOCKED` keeps a second process from processing a row this
    one already holds, which matters if the advisory lock above is ever
    relaxed to per-satellite.
    """
    stmt = (
        select(Telemetry)
        .where(Telemetry.screened_at.is_(None))
        .order_by(Telemetry.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(session.scalars(stmt))


def training_history(
    session: Session, satellite_id: str, settings: Settings, before: datetime
) -> list[Telemetry]:
    """The window of already-screened telemetry the detector learns from.

    The window is anchored to the *telemetry*, not to the wall clock. Anchoring
    it to `now()` breaks the moment screening is not live: replaying a week-old
    dump would find an empty training set and silently score nothing, and two
    screenings of the same data minutes apart would train on slightly different
    sets and disagree. Ending the window at the batch also keeps it causal — no
    frame is scored against history recorded after it.
    """
    cutoff = before - timedelta(hours=settings.ml_training_window_hours)
    stmt = (
        select(Telemetry)
        .where(
            Telemetry.satellite_id == satellite_id,
            Telemetry.recorded_at >= cutoff,
            Telemetry.recorded_at < before,
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


def screen_once(
    session: Session, settings: Settings, *, wait_for_lock: bool = False
) -> tuple[int, int]:
    """Screen one batch. Returns (frames screened, alerts opened)."""
    if not claim_screening_lock(session, wait=wait_for_lock):
        log.debug("another worker is screening; standing by")
        return 0, 0

    frames = fetch_unscreened(session, settings.worker_batch_size)
    if not frames:
        session.rollback()  # release the lock rather than idle inside a transaction
        return 0, 0

    by_satellite: dict[str, list[Telemetry]] = defaultdict(list)
    for frame in frames:
        by_satellite[frame.satellite_id].append(frame)

    episodes = open_episodes(session, list(by_satellite))
    point_findings: list[Finding] = []
    opened = 0

    for satellite_id, satellite_frames in by_satellite.items():
        satellite_frames.sort(key=lambda f: f.seq)
        anchor = min(frame.recorded_at for frame in satellite_frames)

        limits = resolve_limits(session.get(Satellite, satellite_id), settings)

        detector = AnomalyDetector(satellite_id, settings)
        detector.fit(training_history(session, satellite_id, settings, anchor))

        prev = previous_seq(session, satellite_id, satellite_frames[0].seq)

        for frame in satellite_frames:
            findings = evaluate_frame(frame, limits)

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

            opened += _reconcile_episodes(session, frame, active, episodes, settings)

    created = _insert_point_alerts(session, point_findings) + opened

    now = datetime.now(UTC)
    session.execute(
        update(Telemetry).where(Telemetry.id.in_([f.id for f in frames])).values(screened_at=now)
    )
    session.commit()

    log.info("screened %d frame(s), raised %d alert(s)", len(frames), created)
    return len(frames), created


def _open_episode(session: Session, finding: Finding, rule: str) -> tuple[Alert, bool]:
    """Open an episode, or adopt the one this frame already opened before.

    Re-screening replays frames that have been screened once already. The
    episode they opened is closed by then, so `open_episodes` does not find it
    and a plain insert collides with `uq_alerts_dedupe`. Let the database
    decide, and take back whichever row won.

    An adopted row is re-opened. It has to be: the next batch looks for open
    episodes in the database, and an adopted-but-still-resolved row is invisible
    there, so a condition spanning a batch boundary would open a second alert on
    the first frame of the next batch. Re-screening recomputes alerts from
    telemetry, so the recomputed state wins over an earlier resolution — the
    frame that clears the condition sets `resolved_at` again on the way through.
    """
    key = f"{finding.satellite_id}:{rule}:episode:{finding.telemetry_id}"
    stmt = (
        insert(Alert)
        .values(
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
            dedupe_key=key,
        )
        .on_conflict_do_nothing(constraint="uq_alerts_dedupe")
        .returning(Alert.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()
    if new_id is not None:
        return session.get(Alert, new_id), True

    existing = session.scalars(select(Alert).where(Alert.dedupe_key == key)).one()
    existing.resolved_at = None
    existing.clearing_since = None
    session.flush()
    return existing, False


def _reconcile_episodes(
    session: Session,
    frame: Telemetry,
    active: dict[str, Finding],
    episodes: dict[tuple[str, str], Alert],
    settings: Settings,
) -> int:
    """Open, escalate, or resolve episodic alerts in light of one frame."""
    opened = 0

    for rule, finding in active.items():
        key = (frame.satellite_id, rule)
        existing = episodes.get(key)
        if existing is None:
            alert, created = _open_episode(session, finding, rule)
            episodes[key] = alert
            if created:
                opened += 1
                log.warning("[%s] %s opened — %s", frame.satellite_id, rule, finding.message)
            continue
        if SEVERITY_ORDER[finding.severity] > SEVERITY_ORDER[existing.severity]:
            # The same episode got worse: escalate in place rather than
            # opening a second alert for one continuing condition.
            existing.severity = finding.severity
            existing.value = finding.value
            existing.threshold = finding.threshold
            existing.message = finding.message
            session.flush()
            log.warning("[%s] %s escalated to %s", frame.satellite_id, rule, finding.severity)

    hysteresis = timedelta(seconds=settings.alert_clear_after_seconds)
    for (satellite_id, rule), alert in list(episodes.items()):
        if satellite_id != frame.satellite_id:
            continue
        if rule in active:
            # The condition came back before the quiet period was up: this is
            # the same episode, not a new one.
            if alert.clearing_since is not None:
                alert.clearing_since = None
                session.flush()
            continue

        if alert.clearing_since is None:
            alert.clearing_since = frame.recorded_at
            session.flush()

        if frame.recorded_at - alert.clearing_since >= hysteresis:
            # Resolved *when it actually stopped*, not when we became sure.
            alert.resolved_at = alert.clearing_since
            session.flush()
            del episodes[(satellite_id, rule)]
            log.info(
                "[%s] %s cleared at %s, confirmed at seq %d",
                satellite_id,
                rule,
                alert.clearing_since.isoformat(timespec="seconds"),
                frame.seq,
            )

    return opened
