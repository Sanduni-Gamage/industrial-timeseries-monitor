"""Database access helpers.

Thin, deliberate wrappers around pyodbc. There is no ORM here: the pipeline's hot path
is a bulk insert of 22.7 million rows, and an ORM would add object overhead per row for
no benefit. SQLAlchemy is still used for the API layer's read queries, where composable
query building does earn its keep.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyodbc

from ingestion.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Splits a script on batch separators. ``GO`` is a client-side directive understood by
#: sqlcmd and SSMS, not T-SQL, so pyodbc rejects it - scripts must be split before
#: execution. Matched only when it is alone on a line, so the word inside a string
#: literal or comment is left untouched.
_GO_SEPARATOR = re.compile(r"^\s*GO\s*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)


class DatabaseError(RuntimeError):
    """Raised when a database operation fails in a way the caller should handle."""


@contextmanager
def connect(
    *,
    database: str | None = None,
    autocommit: bool = False,
    settings: Settings | None = None,
) -> Iterator[pyodbc.Connection]:
    """Yield a pyodbc connection, committing on success and rolling back on failure.

    Parameters
    ----------
    database:
        Override the configured database. Pass ``'master'`` for operations that must
        run before the application database exists, such as ``CREATE DATABASE``.
    autocommit:
        Required for statements SQL Server refuses to run inside an explicit
        transaction (``CREATE DATABASE``, ``ALTER DATABASE``).
    """
    cfg = settings or get_settings()
    try:
        conn = pyodbc.connect(cfg.odbc_connection_string(database=database), autocommit=autocommit)
    except pyodbc.Error as exc:
        # Deliberately does not include the connection string: it may carry a password.
        raise DatabaseError(
            f"Could not connect to {cfg.safe_connection_summary()}: {exc}"
        ) from exc

    conn.timeout = cfg.db_command_timeout_seconds
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def split_batches(script: str) -> list[str]:
    """Split a T-SQL script on ``GO`` separators, dropping empty batches."""
    return [batch for batch in (b.strip() for b in _GO_SEPARATOR.split(script)) if batch]


def execute_script(conn: pyodbc.Connection, script: str, *, label: str = "script") -> int:
    """Execute a multi-batch T-SQL script. Returns the number of batches run.

    Any ``PRINT`` output is surfaced through the logger rather than swallowed, so an
    idempotent script can report what it decided to skip.
    """
    batches = split_batches(script)
    cursor = conn.cursor()
    for number, batch in enumerate(batches, start=1):
        try:
            cursor.execute(batch)
            while cursor.nextset():
                pass
        except pyodbc.Error as exc:
            first_line = batch.strip().splitlines()[0][:120]
            raise DatabaseError(
                f"{label}: batch {number}/{len(batches)} failed near {first_line!r}: {exc}"
            ) from exc
    cursor.close()
    logger.debug("%s: executed %d batch(es)", label, len(batches))
    return len(batches)


def execute_script_file(conn: pyodbc.Connection, path: Path) -> int:
    """Execute a ``.sql`` file. Encoding is explicit so a BOM cannot break batch one."""
    script = path.read_text(encoding="utf-8-sig")
    return execute_script(conn, script, label=path.name)


def scalar(conn: pyodbc.Connection, sql: str, params: Sequence[Any] = ()) -> Any:
    """Run a query and return the first column of the first row, or ``None``."""
    cursor = conn.cursor()
    try:
        row = cursor.execute(sql, *params).fetchone()
        return None if row is None else row[0]
    finally:
        cursor.close()


def fetch_all(conn: pyodbc.Connection, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
    """Run a query and return every row as a plain tuple."""
    cursor = conn.cursor()
    try:
        return [tuple(row) for row in cursor.execute(sql, *params).fetchall()]
    finally:
        cursor.close()


def database_exists(name: str, *, settings: Settings | None = None) -> bool:
    """Check for a database by name, connecting through ``master``."""
    with connect(database="master", autocommit=True, settings=settings) as conn:
        return scalar(conn, "SELECT DB_ID(?)", (name,)) is not None


def server_info(*, settings: Settings | None = None) -> dict[str, str]:
    """Return version and edition, for health checks and startup banners.

    ``SERVERPROPERTY`` returns ``sql_variant``; the explicit ``CAST`` avoids the
    implicit-conversion error documented in DEV_LOG DL-001.
    """
    with connect(database="master", settings=settings) as conn:
        row = fetch_all(
            conn,
            """
            SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)),
                   CAST(SERVERPROPERTY('Edition')        AS nvarchar(128)),
                   CAST(SERVERPROPERTY('MachineName')    AS nvarchar(128)),
                   @@SERVICENAME
            """,
        )[0]
    return {
        "product_version": row[0],
        "edition": row[1],
        "machine_name": row[2],
        "instance": row[3],
    }
