"""Run the analytics pipeline: operating states, baselines, detection, reporting.

Order matters and each stage depends on the one before it:

    1. operating state   every scan classified from motor current
    2. aggregates        state-aware hourly rollups (SQL)
    3. baselines         per sensor, per state, over the reference window
    4. detection         adaptive / fixed-baseline / documented setpoint
    5. summary           alarm rates judged against EEMUA 191 guidance

Safe to re-run: every stage rebuilds its own output rather than appending.

Usage
-----
    python scripts/run_analytics.py                # everything
    python scripts/run_analytics.py --skip-states  # reuse existing ScanState
    python scripts/run_analytics.py --dry-run      # detect and report, write nothing

Exit codes: 0 success · 1 failure · 3 completed but the alarm rate is unusable
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analytics import anomaly_detection as ad  # noqa: E402
from analytics import operating_state, statistics  # noqa: E402
from ingestion.config import PROJECT_ROOT, get_settings  # noqa: E402
from ingestion.db import (  # noqa: E402
    DatabaseError,
    connect,
    execute_script_file,
    fetch_all,
    scalar,
)
from ingestion.logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger("analytics")

EXIT_OK, EXIT_FAILURE, EXIT_DEGRADED = 0, 1, 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-states", action="store_true",
                        help="reuse the existing analytics.ScanState")
    parser.add_argument("--skip-aggregates", action="store_true",
                        help="reuse the existing hourly aggregates")
    parser.add_argument("--dry-run", action="store_true",
                        help="run detection and report, but write nothing")
    parser.add_argument("--baseline-name", default=statistics.REFERENCE_BASELINE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings, logger_name="analytics", log_file="analytics.log")
    started = time.perf_counter()

    try:
        with connect(settings=settings) as conn:
            readings = int(scalar(conn, "SELECT COUNT_BIG(*) FROM ts.SensorReading") or 0)
            if readings == 0:
                logger.error("No readings in the archive. Run `python -m ingestion` first.")
                return EXIT_FAILURE
            logger.info("Archive holds %s readings", f"{readings:,}")

            # --- 1. operating state ------------------------------------------------
            if args.skip_states:
                logger.info("Skipping operating-state rebuild (--skip-states)")
            else:
                logger.info("Classifying operating state for every scan...")
                operating_state.rebuild(conn)

            # --- 2. aggregates -----------------------------------------------------
            if args.skip_aggregates:
                logger.info("Skipping aggregate rebuild (--skip-aggregates)")
            else:
                logger.info("Rebuilding the aggregate archive...")
                execute_script_file(conn, PROJECT_ROOT / "database" / "aggregates.sql")

            # --- 3. baselines ------------------------------------------------------
            logger.info("Computing baselines over %s to %s...",
                        statistics.REFERENCE_START, statistics.REFERENCE_END)
            baselines = statistics.compute(conn)
            usable = [b for b in baselines if not b.is_degenerate]
            logger.info("Computed %d baseline(s); %d usable, %d flat "
                        "(signals that never move in that state)",
                        len(baselines), len(usable), len(baselines) - len(usable))
            if not args.dry_run:
                statistics.store(conn, baselines, baseline_name=args.baseline_name)

            # --- 4. detection ------------------------------------------------------
            span_days = float(scalar(
                conn, "SELECT DATEDIFF(hour, MIN(ReadingTs), MAX(ReadingTs))/24.0 "
                      "FROM ts.SensorReading") or 1.0)

            found: list[ad.Anomaly] = []
            for label, detector in (("adaptive (MAD, trailing 7d, same state)",
                                     ad.detect_adaptive_mad),
                                    ("fixed baseline (IQR)", ad.detect_baseline_iqr),
                                    ("documented setpoint (7 bar)", ad.detect_setpoint)):
                t0 = time.perf_counter()
                results = detector(conn)
                found.extend(results)
                rate = ad.alarm_rate(results, span_days)
                logger.info("  %-38s %7s alarms  %6.1f/day  %s  (%.1fs)",
                            label, f"{rate['total']:,}", rate["per_day"],
                            rate["verdict"], time.perf_counter() - t0)

            if not args.dry_run:
                ad.store(conn, found)

            # --- 5. summary --------------------------------------------------------
            combined = ad.alarm_rate(found, span_days)
            severities = Counter(a.severity for a in found)

            logger.info("-" * 72)
            logger.info("Archive span        : %.1f days", span_days)
            logger.info("Total alarms        : %s (%.1f/day)",
                        f"{combined['total']:,}", combined["per_day"])
            logger.info("  CRITICAL          : %s", f"{severities['CRITICAL']:,}")
            logger.info("  WARNING           : %s", f"{severities['WARNING']:,}")
            logger.info("Alarm-rate verdict  : %s", combined["verdict"])
            logger.info("  EEMUA 191 target  : <= %d/day per operator position",
                        ad.ALARM_RATE_TARGET_PER_DAY)
            logger.info("-" * 72)

            if not args.dry_run:
                for row in fetch_all(conn, """
                        SELECT EquipmentCode, HealthStatus, CriticalAnomalies,
                               WarningAnomalies, LastReadingTs
                        FROM asset.vw_EquipmentHealth"""):
                    logger.info("Equipment %s: %s (last 24 h: %s critical, %s warning, "
                                "archive ends %s)",
                                row[0], row[1], row[2], row[3], row[4])

            logger.info("Analytics completed in %.1f s", time.perf_counter() - started)

            if combined["per_day"] > ad.ALARM_RATE_MAX_PER_DAY:
                logger.error("Alarm rate exceeds the manageable ceiling of %d/day. "
                             "An alarm list nobody can read is worse than none; review "
                             "the thresholds in analytics/anomaly_detection.py.",
                             ad.ALARM_RATE_MAX_PER_DAY)
                return EXIT_DEGRADED
            return EXIT_OK

    except DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except Exception:
        logger.exception("Analytics failed.")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
