"""Bulk writes into staging, then a set-based merge into the fact table.

Row-by-row inserts of 22.7 M rows over ODBC would take hours. Chunks go to a staging table
with fast_executemany, then INSERT ... WHERE NOT EXISTS makes the load idempotent.
"""

from __future__ import annotations

import logging
import platform
from collections.abc import Sequence
from datetime import datetime

import pyodbc

from ingestion import __version__
from ingestion.loader import SourceFile
from ingestion.quality import RunStatus
from ingestion.transformer import ReadingRow
from ingestion.validator import DataQualityIssue, RejectedRow

logger = logging.getLogger(__name__)


def start_run(conn: pyodbc.Connection, source: SourceFile) -> int:
    """Open a ledger row and return its id.

    Written and committed immediately, before any data moves. A run that dies mid-load
    then leaves a ``RUNNING`` row behind - which is the point: an interrupted load should
    be visible in the ledger rather than leaving the database silently half-populated
    with no record of why.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO ops.IngestionRun
            (SourceFile, SourceSha256, SourceBytes, Status, ToolVersion, HostName)
        OUTPUT INSERTED.IngestionRunId
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        str(source.path),
        source.sha256,
        source.size_bytes,
        RunStatus.RUNNING,
        __version__,
        platform.node()[:128],
    )
    run_id = int(cursor.fetchone()[0])
    cursor.close()
    conn.commit()
    logger.info("Ingestion run %d started for %s", run_id, source.path.name)
    return run_id


def find_completed_run(conn: pyodbc.Connection, source: SourceFile) -> int | None:
    """Return the id of an earlier successful run of this exact file, if any.

    Identity is the SHA-256, not the filename: a renamed copy of the same bytes is the
    same data, and a changed file at the same path is not.
    """
    cursor = conn.cursor()
    row = cursor.execute(
        """
        SELECT TOP (1) IngestionRunId
        FROM ops.IngestionRun
        WHERE SourceSha256 = ? AND Status = ?
        ORDER BY IngestionRunId DESC
        """,
        source.sha256,
        RunStatus.SUCCEEDED,
    ).fetchone()
    cursor.close()
    return None if row is None else int(row[0])


def finish_run(
    conn: pyodbc.Connection,
    run_id: int,
    *,
    status: str,
    rows_read: int,
    rows_inserted: int,
    rows_skipped_dup: int,
    rows_rejected: int,
    min_ts: datetime | None,
    max_ts: datetime | None,
    notes: str | None = None,
) -> None:
    """Close the ledger row with the outcome and the counts."""
    conn.cursor().execute(
        """
        UPDATE ops.IngestionRun
        SET Status = ?, CompletedUtc = SYSUTCDATETIME(), RowsRead = ?, RowsInserted = ?,
            RowsSkippedDup = ?, RowsRejected = ?, MinReadingTs = ?, MaxReadingTs = ?,
            Notes = ?
        WHERE IngestionRunId = ?
        """,
        status, rows_read, rows_inserted, rows_skipped_dup, rows_rejected,
        min_ts, max_ts, notes, run_id,
    )
    conn.commit()
    logger.info("Ingestion run %d closed as %s", run_id, status)


def truncate_staging(conn: pyodbc.Connection) -> None:
    """Empty the staging heap. ``TRUNCATE`` is minimally logged; ``DELETE`` is not."""
    conn.cursor().execute("TRUNCATE TABLE stg.SensorReadingStage")
    conn.commit()


def load_readings(
    conn: pyodbc.Connection,
    rows: Sequence[ReadingRow],
    *,
    batch_rows: int,
) -> tuple[int, int]:
    """Bulk-load one chunk and merge it into the fact table.

    Returns ``(inserted, skipped_as_duplicate)``.

    The staging table is a heap with no constraints or indexes on purpose: constraints
    there would be evaluated once per row during the load, whereas the set-based INSERT
    out of it validates everything in a single pass.
    """
    if not rows:
        return 0, 0

    truncate_staging(conn)

    cursor = conn.cursor()
    # Turn per-row round trips into per-batch parameter arrays. This single flag is the
    # difference between a load measured in minutes and one measured in hours.
    cursor.fast_executemany = True
    insert_sql = (
        "INSERT INTO stg.SensorReadingStage (SensorId, ReadingTs, Value, QualityCodeId) "
        "VALUES (?, ?, ?, ?)"
    )
    for start in range(0, len(rows), batch_rows):
        cursor.executemany(insert_sql, rows[start : start + batch_rows])
    conn.commit()

    # Idempotent merge. Expressed as INSERT ... WHERE NOT EXISTS rather than MERGE:
    # for an insert-only path it produces the same result with a simpler plan, and it
    # sidesteps the well-documented concurrency caveats of MERGE.
    cursor.execute(
        """
        INSERT INTO ts.SensorReading (SensorId, ReadingTs, Value, QualityCodeId)
        SELECT s.SensorId, s.ReadingTs, s.Value, s.QualityCodeId
        FROM stg.SensorReadingStage AS s
        WHERE NOT EXISTS (
            SELECT 1 FROM ts.SensorReading AS r
            WHERE r.SensorId = s.SensorId AND r.ReadingTs = s.ReadingTs
        )
        """
    )
    inserted = cursor.rowcount
    conn.commit()
    cursor.close()

    skipped = len(rows) - inserted
    if skipped:
        logger.debug("Chunk: %s inserted, %s already present", f"{inserted:,}", f"{skipped:,}")
    return inserted, skipped


def write_issues(
    conn: pyodbc.Connection, run_id: int, issues: Sequence[DataQualityIssue]
) -> int:
    """Persist data-quality issues for a run."""
    if not issues:
        return 0
    cursor = conn.cursor()
    cursor.fast_executemany = True
    cursor.executemany(
        """
        INSERT INTO ops.DataQualityIssue
            (IngestionRunId, SensorId, IssueType, Severity, WindowStartTs, WindowEndTs,
             AffectedRows, ObservedValue, IsSummary, Details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                issue.sensor_id,
                issue.issue_type,
                issue.severity,
                issue.window_start.to_pydatetime() if issue.window_start is not None else None,
                issue.window_end.to_pydatetime() if issue.window_end is not None else None,
                issue.affected_rows,
                issue.observed_value,
                1 if issue.is_summary else 0,
                issue.details[:1000],
            )
            for issue in issues
        ],
    )
    conn.commit()
    cursor.close()
    logger.info("Recorded %d data-quality issue row(s)", len(issues))
    return len(issues)


def write_rejected(
    conn: pyodbc.Connection, run_id: int, rejected: Sequence[RejectedRow]
) -> int:
    """Persist quarantined rows verbatim.

    This table is what lets the project claim nothing is silently discarded and then
    prove it: every row the pipeline refused is here, with its original content and the
    reason it was refused.
    """
    if not rejected:
        return 0
    cursor = conn.cursor()
    cursor.fast_executemany = True
    cursor.executemany(
        """
        INSERT INTO ops.RejectedRow
            (IngestionRunId, SourceLineNo, RawPayload, ReasonCode, ReasonDetail)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (run_id, row.source_line_no, row.raw_payload, row.reason_code, row.reason_detail)
            for row in rejected
        ],
    )
    conn.commit()
    cursor.close()
    logger.warning("Quarantined %d row(s) in ops.RejectedRow", len(rejected))
    return len(rejected)


def reading_bounds(conn: pyodbc.Connection) -> tuple[datetime | None, datetime | None, int]:
    """Return ``(min timestamp, max timestamp, row count)`` for the fact table."""
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT MIN(ReadingTs), MAX(ReadingTs), COUNT_BIG(*) FROM ts.SensorReading"
    ).fetchone()
    cursor.close()
    return row[0], row[1], int(row[2])
