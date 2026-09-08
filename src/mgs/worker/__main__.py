"""The anomaly worker: poll, screen, alert, repeat.

Runs as its own process, not inside the API — screening must not add latency
to ingestion, and a slow model must never drop a frame.

    mgs-worker              # daemon: poll forever
    mgs-worker --once       # drain the backlog and exit (cron, CI, tests)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from types import FrameType

from mgs.config import get_settings
from mgs.db import get_sessionmaker
from mgs.worker.detector import ML_AVAILABLE
from mgs.worker.screener import screen_once

log = logging.getLogger("mgs.worker")

_stop = False


def _handle_signal(signum: int, _frame: FrameType | None) -> None:
    global _stop
    log.info("signal %s received, finishing the current batch then exiting", signum)
    _stop = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mgs-worker", description="Telemetry anomaly screener.")
    p.add_argument(
        "--once",
        action="store_true",
        help="screen everything currently unscreened, then exit instead of polling",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "anomaly worker up | poll=%.1fs | battery<%.2fV | temp %.0f..%.0f°C | ML=%s",
        settings.poll_interval_seconds,
        settings.battery_min_v,
        settings.temp_min_c,
        settings.temp_max_c,
        "on" if (settings.enable_ml and ML_AVAILABLE) else "off (z-score only)",
    )

    session_factory = get_sessionmaker()
    total_frames = total_alerts = 0
    while not _stop:
        try:
            with session_factory() as session:
                screened, raised = screen_once(session, settings)
            total_frames += screened
            total_alerts += raised
            if args.once and screened == 0:
                log.info("backlog drained: %d frame(s), %d alert(s)", total_frames, total_alerts)
                break
            # A full batch means there is a backlog: keep going without sleeping.
            if screened >= settings.worker_batch_size or (args.once and screened):
                continue
        except Exception:
            log.exception("screening batch failed; retrying after the poll interval")
            if args.once:
                return 1
        time.sleep(settings.poll_interval_seconds)

    log.info("anomaly worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
