"""Create the application database and apply schema, seed, indexes and views.

Idempotent end to end: every SQL file guards its own objects, so re-running is a no-op
against an already-provisioned database.

Usage
-----
    python scripts/init_database.py                   # create DB (if needed), schema + seed
    python scripts/init_database.py --with-indexes    # also apply indexes.sql
    python scripts/init_database.py --with-views      # also apply views.sql
    python scripts/init_database.py --with-historian  # also apply historian.sql
    python scripts/init_database.py --all             # everything
    python scripts/init_database.py --drop-first      # DESTRUCTIVE, requires --confirm-drop

Exit codes: 0 success · 1 failure · 2 usage error
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a plain script from anywhere, not only as a module.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.config import PROJECT_ROOT, get_settings  # noqa: E402
from ingestion.db import (  # noqa: E402
    DatabaseError,
    connect,
    database_exists,
    execute_script_file,
    scalar,
    server_info,
)
from ingestion.logging_setup import configure_logging  # noqa: E402

logger = logging.getLogger("init_database")

SQL_DIR = PROJECT_ROOT / "database"

EXIT_OK, EXIT_FAILURE, EXIT_USAGE = 0, 1, 2


def create_database(name: str) -> bool:
    """Create the database if absent. Returns True if it was created by this call.

    ``CREATE DATABASE`` cannot run inside an explicit transaction, hence autocommit.
    """
    if database_exists(name):
        logger.info("Database %s already exists.", name)
        return False

    logger.info("Creating database %s ...", name)
    with connect(database="master", autocommit=True) as conn:
        # The name is an identifier, not a parameter, so it cannot be bound. It comes
        # from validated configuration rather than user input; QUOTENAME neutralises
        # any bracket in it regardless.
        safe_name = scalar(conn, "SELECT QUOTENAME(?)", (name,))
        conn.cursor().execute(f"CREATE DATABASE {safe_name}")
    logger.info("Database %s created.", name)
    return True


def drop_database(name: str) -> None:
    """Drop the database, forcing other sessions off first. Destructive."""
    if not database_exists(name):
        logger.info("Database %s does not exist; nothing to drop.", name)
        return
    logger.warning("Dropping database %s ...", name)
    with connect(database="master", autocommit=True) as conn:
        safe_name = scalar(conn, "SELECT QUOTENAME(?)", (name,))
        cursor = conn.cursor()
        # SINGLE_USER WITH ROLLBACK IMMEDIATE evicts open connections; without it the
        # drop blocks indefinitely behind an idle session in SSMS.
        cursor.execute(f"ALTER DATABASE {safe_name} SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cursor.execute(f"DROP DATABASE {safe_name}")
    logger.warning("Database %s dropped.", name)


def apply_script(filename: str) -> None:
    path = SQL_DIR / filename
    if not path.is_file():
        raise DatabaseError(f"SQL file not found: {path}")
    logger.info("Applying %s ...", filename)
    with connect() as conn:
        batches = execute_script_file(conn, path)
    logger.info("Applied %s (%d batches).", filename, batches)


def report_state() -> None:
    """Log a short summary of what now exists, so the script proves its own work."""
    with connect() as conn:
        tables = scalar(conn, "SELECT COUNT(*) FROM sys.tables")
        views = scalar(conn, "SELECT COUNT(*) FROM sys.views")
        sensors = scalar(conn, "SELECT COUNT(*) FROM asset.Sensor")
        equipment = scalar(conn, "SELECT COUNT(*) FROM asset.Equipment")
        failures = scalar(conn, "SELECT COUNT(*) FROM ops.FailureEvent")
        quality = scalar(conn, "SELECT COUNT(*) FROM ref.QualityCode")
        readings = scalar(conn, "SELECT COUNT_BIG(*) FROM ts.SensorReading")
    logger.info("Tables: %s | Views: %s", tables, views)
    logger.info("Equipment: %s | Sensors: %s | Quality codes: %s | Failure events: %s",
                equipment, sensors, quality, failures)
    logger.info("Sensor readings: %s", f"{readings:,}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--with-indexes", action="store_true", help="also apply indexes.sql")
    parser.add_argument("--with-views", action="store_true", help="also apply views.sql")
    parser.add_argument("--with-historian", action="store_true",
                        help="also apply historian.sql (interpolated retrieval and the "
                             "compression/aggregate comparison views)")
    parser.add_argument("--with-aggregates", action="store_true",
                        help="also rebuild the hourly and daily aggregate archive")
    parser.add_argument("--all", action="store_true",
                        help="schema, seed, indexes, aggregates, views and historian "
                             "retrieval (post-ingestion)")
    parser.add_argument("--drop-first", action="store_true",
                        help="DESTRUCTIVE: drop the database before creating it")
    parser.add_argument("--confirm-drop", action="store_true",
                        help="required acknowledgement for --drop-first")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings, logger_name="init_database")

    if args.drop_first and not args.confirm_drop:
        logger.error("--drop-first destroys all ingested data. Re-run with --confirm-drop "
                     "if that is what you intend.")
        return EXIT_USAGE

    try:
        info = server_info()
        logger.info("Connected to %s", settings.safe_connection_summary())
        logger.info("SQL Server %s - %s", info["product_version"], info["edition"])

        if args.drop_first:
            drop_database(settings.db_database)

        create_database(settings.db_database)
        apply_script("schema.sql")
        apply_script("seed.sql")

        # Order matters after a load: indexes first so the aggregate rebuild can use the
        # columnstore, then aggregates, then views (several read the aggregate tables).
        if args.all or args.with_indexes:
            apply_script("indexes.sql")
        if args.all or args.with_aggregates:
            apply_script("aggregates.sql")
        if args.all or args.with_views:
            apply_script("views.sql")
        # Last: fn_ValueAt reads the compressed archive and vw_AggregateComparison reads
        # the hourly aggregates, so both want everything above already in place.
        if args.all or args.with_historian:
            apply_script("historian.sql")

        report_state()
        logger.info("Database initialisation completed successfully.")
        return EXIT_OK

    except DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_FAILURE
    except Exception:
        logger.exception("Unexpected failure during database initialisation.")
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
