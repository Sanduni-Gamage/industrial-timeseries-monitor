"""Command-line entry point for the ingestion pipeline.

    python -m ingestion                     # ingest the configured source file
    python -m ingestion --limit-days 3      # development subset, recorded as PARTIAL
    python -m ingestion --force             # re-process a file already loaded
    python -m ingestion --csv path/to.csv   # override the configured path

Exit codes (consumed by scripts/ingest.ps1 and by CI):
    0  success, or nothing to do
    1  ingestion failed
    2  usage or configuration error
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from ingestion.config import get_settings
from ingestion.db import DatabaseError
from ingestion.logging_setup import configure_logging
from ingestion.pipeline import log_summary, run_ingestion

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2

logger = logging.getLogger("ingest")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--csv", type=Path, default=None,
                        help="override METROPT_RAW_CSV for this run")
    parser.add_argument("--limit-days", type=int, default=None, metavar="N",
                        help="ingest only the first N days (run recorded as PARTIAL)")
    parser.add_argument("--force", action="store_true",
                        help="re-process a file an earlier run already completed")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="override LOG_LEVEL for this run")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.limit_days is not None and args.limit_days < 1:
        print("[ERROR] --limit-days must be 1 or greater.")
        return EXIT_USAGE

    try:
        settings = get_settings()
    except Exception as exc:  # configuration errors must not look like data errors
        print(f"[ERROR] Configuration is invalid: {exc}")
        return EXIT_USAGE

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})
    configure_logging(settings, logger_name="ingest", log_file="ingest.log")

    logger.info("Target database: %s", settings.safe_connection_summary())

    try:
        result = run_ingestion(
            settings, csv_path=args.csv, limit_days=args.limit_days, force=args.force
        )
    except DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        logger.warning("Interrupted by user. The run ledger will show this run as RUNNING.")
        return EXIT_FAILURE

    log_summary(result)

    if not result.succeeded:
        logger.error("Ingestion did not complete successfully.")
        return EXIT_FAILURE

    logger.info("Ingestion completed successfully.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
