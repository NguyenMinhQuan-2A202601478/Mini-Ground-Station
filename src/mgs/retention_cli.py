"""`mgs-retention`: apply the retention policy once, then exit.

Meant for cron or a scheduled container, not a daemon — deleting a day's worth
of old telemetry once a day does not need a process sitting idle in between.

    mgs-retention --days 90 --dry-run
    mgs-retention                      # uses MGS_RETENTION_DAYS
"""

from __future__ import annotations

import argparse
import logging
import sys

from mgs.config import get_settings
from mgs.db import get_sessionmaker
from mgs.retention import purge

log = logging.getLogger("mgs.retention")


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="mgs-retention", description=__doc__)
    parser.add_argument(
        "--days",
        type=float,
        default=settings.retention_days,
        help="delete screened telemetry older than this many days (0 disables it)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would go, delete nothing"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    if args.days <= 0:
        log.error(
            "retention is disabled. Pass --days N or set MGS_RETENTION_DAYS to choose "
            "how long telemetry is kept."
        )
        return 2

    settings = settings.model_copy(update={"retention_days": args.days})
    with get_sessionmaker()() as session:
        result = purge(session, settings, dry_run=args.dry_run)
    log.info("%s%s", "[dry run] " if args.dry_run else "", result.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
