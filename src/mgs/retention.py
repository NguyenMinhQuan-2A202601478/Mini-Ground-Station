"""Retention: delete telemetry old enough to be of no further use.

Telemetry is append-only and nothing here ever removed it, so the table grows
forever — measured at about 2.3 kB per frame including indexes, which is
roughly 2.3 GB a year per satellite at the default half-minute cadence, and
seventy at one frame a second.

Two rules shape what this is allowed to delete:

1. **A frame an alert cites is evidence, and evidence is not deleted.** The
   foreign key already says so (`ON DELETE RESTRICT`); this simply never
   selects such a frame, rather than discovering the constraint at run time.
2. **Unscreened frames are work in progress.** Deleting one loses telemetry
   that was never examined, which is worse than keeping it.

Not partitioning. Partitioning by `recorded_at` would be the obvious way to
make this cheap, and PostgreSQL forbids it here: every unique constraint on a
partitioned table must contain the partition key, so
`UNIQUE (satellite_id, seq)` would have to become
`UNIQUE (satellite_id, seq, recorded_at)` — and that is precisely the
constraint that makes ingestion idempotent. Partitioning would buy cheaper
deletes by giving up exactly-once ingestion. See `docs/decisions/0006`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from mgs.config import Settings
from mgs.models import Alert, Telemetry

log = logging.getLogger("mgs.retention")


@dataclass
class PurgeResult:
    cutoff: datetime
    deleted: int
    kept_as_evidence: int
    remaining: int

    def describe(self) -> str:
        return (
            f"deleted {self.deleted} frame(s) recorded before "
            f"{self.cutoff.isoformat(timespec='seconds')}, kept "
            f"{self.kept_as_evidence} cited by an alert, {self.remaining} remain"
        )


def _deletable(cutoff: datetime):
    """Screened, older than the cutoff, and not cited by any alert."""
    cited = select(Alert.telemetry_id).where(Alert.telemetry_id == Telemetry.id)
    return (
        Telemetry.recorded_at < cutoff,
        Telemetry.screened_at.is_not(None),
        ~cited.exists(),
    )


def purge(session: Session, settings: Settings, *, dry_run: bool = False) -> PurgeResult:
    """Apply the retention policy once.

    Deletes in batches, committing as it goes: a purge that has been running
    for a minute should have freed something, not be holding one transaction
    open over the whole table.
    """
    if settings.retention_days <= 0:
        raise ValueError("retention is disabled; set MGS_RETENTION_DAYS to enable it")

    cutoff = datetime.now(UTC) - timedelta(days=settings.retention_days)
    conditions = _deletable(cutoff)

    kept = session.scalar(
        select(func.count())
        .select_from(Telemetry)
        .where(
            Telemetry.recorded_at < cutoff,
            select(Alert.telemetry_id).where(Alert.telemetry_id == Telemetry.id).exists(),
        )
    )

    deleted = 0
    if dry_run:
        deleted = session.scalar(select(func.count()).select_from(Telemetry).where(*conditions))
    else:
        while True:
            batch = session.scalars(
                select(Telemetry.id).where(*conditions).limit(settings.retention_batch_size)
            ).all()
            if not batch:
                break
            session.execute(delete(Telemetry).where(Telemetry.id.in_(batch)))
            session.commit()
            deleted += len(batch)
            log.info("deleted %d frame(s) so far", deleted)

    remaining = session.scalar(select(func.count()).select_from(Telemetry))
    return PurgeResult(
        cutoff=cutoff, deleted=deleted or 0, kept_as_evidence=kept or 0, remaining=remaining or 0
    )
