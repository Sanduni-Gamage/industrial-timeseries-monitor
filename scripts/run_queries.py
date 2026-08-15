"""Execute every demonstration query and record its result count and timing.

A .sql file that has never been run is a liability, not documentation. This runs all of
them against the live database, so the numbers quoted in docs/SQL_DESIGN.md are measured
rather than claimed, and a schema change that breaks a query is caught immediately.

Usage
-----
    python scripts/run_queries.py                # run all, print a summary table
    python scripts/run_queries.py --show 5       # also print the first 5 rows of each
    python scripts/run_queries.py --only 08      # run one query by filename prefix

Exit codes: 0 all queries succeeded · 1 at least one failed · 2 usage error
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.config import PROJECT_ROOT, get_settings  # noqa: E402
from ingestion.db import connect, split_batches  # noqa: E402
from ingestion.logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger("run_queries")

QUERY_DIR = PROJECT_ROOT / "database" / "queries"
EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2

#: Queries whose cost is unbounded without a narrow window, or that are included to
#: demonstrate a technique rather than to be run routinely.
SLOW_QUERIES = {"08_gap_detection"}


def run_one(conn, path: Path, show_rows: int) -> tuple[bool, int, float, str]:
    """Execute one .sql file. Returns (ok, row_count, elapsed_ms, note)."""
    script = path.read_text(encoding="utf-8-sig")
    batches = split_batches(script)

    cursor = conn.cursor()
    started = time.perf_counter()
    rows: list = []
    try:
        for batch in batches:
            cursor.execute(batch)
            # A file may contain several statements; report on the last result set that
            # actually returned columns.
            while True:
                if cursor.description is not None:
                    rows = cursor.fetchall()
                if not cursor.nextset():
                    break
        elapsed_ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        return False, 0, (time.perf_counter() - started) * 1000, str(exc)[:160]
    finally:
        cursor.close()

    if show_rows and rows:
        columns = [d[0] for d in cursor.description] if cursor.description else []
        print(f"\n    {' | '.join(columns)}")
        for row in rows[:show_rows]:
            print(f"    {' | '.join(str(v) for v in row)}")
        print()

    return True, len(rows), elapsed_ms, ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", type=int, default=0, metavar="N",
                        help="print the first N rows of each result")
    parser.add_argument("--only", default=None, metavar="PREFIX",
                        help="run only queries whose filename starts with PREFIX")
    parser.add_argument("--include-slow", action="store_true",
                        help="include queries excluded by default for cost")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings, logger_name="run_queries")

    if not QUERY_DIR.is_dir():
        logger.error("Query directory not found: %s", QUERY_DIR)
        return EXIT_USAGE

    paths = sorted(QUERY_DIR.glob("*.sql"))
    if args.only:
        paths = [p for p in paths if p.stem.startswith(args.only)]
        if not paths:
            logger.error("No query matches prefix %r", args.only)
            return EXIT_USAGE

    logger.info("Running %d quer%s against %s",
                len(paths), "y" if len(paths) == 1 else "ies",
                settings.safe_connection_summary())

    failures = 0
    results: list[tuple[str, str, int, float, str]] = []

    with connect(settings=settings) as conn:
        for path in paths:
            if path.stem in SLOW_QUERIES and not args.include_slow and not args.only:
                results.append((path.stem, "SKIPPED", 0, 0.0, "use --include-slow"))
                continue
            ok, rows, ms, note = run_one(conn, path, args.show)
            results.append((path.stem, "OK" if ok else "FAILED", rows, ms, note))
            if not ok:
                failures += 1

    print()
    print(f"{'query':<38} {'status':<8} {'rows':>9} {'ms':>9}  note")
    print("-" * 96)
    for name, status, rows, ms, note in results:
        rows_text = f"{rows:,}" if status == "OK" else "-"
        ms_text = f"{ms:,.0f}" if status != "SKIPPED" else "-"
        print(f"{name:<38} {status:<8} {rows_text:>9} {ms_text:>9}  {note}")
    print("-" * 96)

    if failures:
        logger.error("%d quer%s failed.", failures, "y" if failures == 1 else "ies")
        return EXIT_FAILURE
    logger.info("All queries executed successfully.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
