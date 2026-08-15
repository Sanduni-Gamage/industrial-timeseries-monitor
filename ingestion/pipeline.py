"""Ingestion orchestration: read, validate, transform, load, record.

The pipeline is deliberately linear and boring. Each stage has one job, state carried
between chunks is explicit rather than implicit, and every outcome ends up in the run
ledger whether it succeeded or not.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyodbc

from ingestion.config import Settings
from ingestion.db import connect
from ingestion.loader import (
    TIMESTAMP_COLUMN,
    SensorDefinition,
    describe_source,
    iter_chunks,
    load_sensor_definitions,
    verify_header,
)
from ingestion.quality import IssueType, RunStatus, Severity
from ingestion.transformer import to_reading_rows
from ingestion.validator import (
    DataQualityIssue,
    IssueAccumulator,
    ValidationState,
    summarise,
    validate_chunk,
)
from ingestion.writer import (
    find_completed_run,
    finish_run,
    load_readings,
    reading_bounds,
    start_run,
    truncate_staging,
    write_issues,
    write_rejected,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    """Everything the caller - and the exit code - needs to know."""

    status: str
    run_id: int | None = None
    rows_read: int = 0
    readings_inserted: int = 0
    readings_skipped: int = 0
    rows_rejected: int = 0
    issues_written: int = 0
    min_ts: datetime | None = None
    max_ts: datetime | None = None
    elapsed_seconds: float = 0.0
    quality_summary: dict[str, int] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        """Whether the caller should treat this run as a success.

        ``PARTIAL`` counts. It is only ever produced by an explicit ``--limit-days``,
        so the operator asked for a subset and got one - the ledger still records it as
        partial so a truncated load can never be mistaken for a complete one later.
        """
        return self.status in {RunStatus.SUCCEEDED, RunStatus.SKIPPED, RunStatus.PARTIAL}


def run_ingestion(
    settings: Settings,
    *,
    csv_path: Path | None = None,
    limit_days: int | None = None,
    force: bool = False,
) -> IngestionResult:
    """Ingest the source file into SQL Server.

    Parameters
    ----------
    limit_days:
        Stop after this many days of readings. For fast iteration during development;
        the run is recorded as ``PARTIAL`` so a truncated load can never be mistaken for
        a complete one in the ledger.
    force:
        Re-process a file that a previous run already loaded successfully. The load is
        idempotent either way - this only skips the shortcut, it does not duplicate rows.
    """
    started = time.perf_counter()
    path = csv_path or settings.metropt_raw_csv

    logger.info("Loading dataset...")
    if not path.is_file():
        logger.error("Dataset not found: %s", path)
        logger.error("Set METROPT_RAW_CSV or pass --csv. See data/README.md.")
        return IngestionResult(status=RunStatus.FAILED)

    logger.info("Source file: %s (%.1f MB)", path.name, path.stat().st_size / 1024 / 1024)
    logger.info("Hashing source file for the run ledger...")
    source = describe_source(path)
    logger.info("SHA-256: %s", source.sha256)

    with connect(settings=settings) as conn:
        sensors = load_sensor_definitions(conn)
        logger.info("Sensors configured: %d (%d analogue, %d digital)",
                    len(sensors),
                    sum(1 for s in sensors if not s.is_digital),
                    sum(1 for s in sensors if s.is_digital))

        try:
            extra_columns = verify_header(path, sensors)
        except ValueError as exc:
            # A structurally unusable file is a clean failure, not a crash. No ledger
            # row is opened, because nothing was attempted.
            logger.error("%s", exc)
            return IngestionResult(status=RunStatus.FAILED,
                                   elapsed_seconds=time.perf_counter() - started)
        if extra_columns:
            logger.warning("Source file has %d column(s) not configured as sensors: %s",
                           len(extra_columns), extra_columns)

        previous = find_completed_run(conn, source)
        if previous is not None and not force:
            _, _, existing = reading_bounds(conn)
            logger.info("This exact file (by SHA-256) was already ingested by run %d.", previous)
            logger.info("Database holds %s readings. Nothing to do.", f"{existing:,}")
            logger.info("Pass --force to re-process it; the load is idempotent either way.")
            return IngestionResult(
                status=RunStatus.SKIPPED,
                run_id=previous,
                readings_inserted=0,
                elapsed_seconds=time.perf_counter() - started,
            )

        run_id = start_run(conn, source)
        try:
            result = _process(conn, settings, path, sensors, run_id, limit_days)
        except Exception as exc:
            logger.exception("Ingestion failed; closing run %d as FAILED.", run_id)
            finish_run(
                conn, run_id, status=RunStatus.FAILED, rows_read=0, rows_inserted=0,
                rows_skipped_dup=0, rows_rejected=0, min_ts=None, max_ts=None,
                notes=f"{type(exc).__name__}: {exc}"[:4000],
            )
            return IngestionResult(status=RunStatus.FAILED, run_id=run_id,
                                   elapsed_seconds=time.perf_counter() - started)

    result.elapsed_seconds = time.perf_counter() - started
    return result


def _process(
    conn: pyodbc.Connection,
    settings: Settings,
    path: Path,
    sensors: list[SensorDefinition],
    run_id: int,
    limit_days: int | None,
) -> IngestionResult:
    """Stream the file through validation and loading."""
    state = ValidationState()
    issues = IssueAccumulator()
    quarantine: list[Any] = []

    rows_read = 0
    inserted = 0
    skipped = 0
    min_ts: pd.Timestamp | None = None
    max_ts: pd.Timestamp | None = None
    cutoff: pd.Timestamp | None = None
    truncated = False

    chunk_start = time.perf_counter()
    for chunk_number, chunk in enumerate(
        iter_chunks(path, sensors, chunk_rows=settings.ingest_chunk_rows), start=1
    ):
        if limit_days is not None:
            first = chunk[TIMESTAMP_COLUMN].dropna()
            if cutoff is None and not first.empty:
                cutoff = first.iloc[0].normalize() + pd.Timedelta(days=limit_days)
                logger.info("Development limit active: stopping at %s", cutoff)
            if cutoff is not None:
                keep = chunk[TIMESTAMP_COLUMN] < cutoff
                if not keep.all():
                    chunk = chunk[keep].reset_index(drop=True)
                    truncated = True

        # Counted after the development limit is applied, so the reconciliation check
        # compares like with like: rows the pipeline actually processed, not rows the
        # CSV reader happened to hand over.
        rows_read += len(chunk)

        is_final = truncated
        result = validate_chunk(chunk, sensors, settings, state, issues, is_final=is_final)
        quarantine.extend(result.rejected)

        if len(result.frame):
            first_ts = result.frame[TIMESTAMP_COLUMN].iloc[0]
            last_ts = result.frame[TIMESTAMP_COLUMN].iloc[-1]
            min_ts = first_ts if min_ts is None else min(min_ts, first_ts)
            max_ts = last_ts if max_ts is None else max(max_ts, last_ts)

            rows = to_reading_rows(result.frame, result.quality, sensors)
            chunk_inserted, chunk_skipped = load_readings(
                conn, rows, batch_rows=settings.ingest_batch_rows
            )
            inserted += chunk_inserted
            skipped += chunk_skipped

        if chunk_number % 5 == 0 or truncated:
            rate = rows_read / max(time.perf_counter() - chunk_start, 1e-9)
            logger.info(
                "Chunk %3d | source rows %s | readings inserted %s | %s to %s | %.0f rows/s",
                chunk_number, f"{rows_read:,}", f"{inserted:,}",
                min_ts, max_ts, rate,
            )

        if truncated:
            break

    # Flush rows held back inside a flatline that was still running at end of file.
    # Passing an empty chunk with is_final=True makes validate_chunk drain state.carry
    # without holding anything else back.
    if state.carry is not None and len(state.carry):
        logger.info("Flushing %s row(s) held back in a trailing flatline",
                    f"{len(state.carry):,}")
        empty_like = state.carry.iloc[0:0]
        result = validate_chunk(empty_like, sensors, settings, state, issues, is_final=True)
        quarantine.extend(result.rejected)
        if len(result.frame):
            last_ts = result.frame[TIMESTAMP_COLUMN].iloc[-1]
            max_ts = last_ts if max_ts is None else max(max_ts, last_ts)
            first_ts = result.frame[TIMESTAMP_COLUMN].iloc[0]
            min_ts = first_ts if min_ts is None else min(min_ts, first_ts)
            rows = to_reading_rows(result.frame, result.quality, sensors)
            chunk_inserted, chunk_skipped = load_readings(
                conn, rows, batch_rows=settings.ingest_batch_rows
            )
            inserted += chunk_inserted
            skipped += chunk_skipped

    # Clear the staging heap on the way out. load_readings() truncates before each
    # chunk, so leaving it full is not a correctness problem - but it strands the last
    # chunk's rows in the database indefinitely, where anyone inspecting stg sees stale
    # data that looks current, and the space counts against the Express size cap.
    truncate_staging(conn)

    sensor_names = {s.sensor_id: s.sensor_code for s in sensors}
    issue_rows = issues.finalise(sensor_names)
    issue_rows.append(
        _coverage_issue(
            rows_read, inserted, skipped, len(sensors),
            rejected=len(quarantine),
            missing=issues.total(IssueType.MISSING_VALUE),
        )
    )

    issues_written = write_issues(conn, run_id, issue_rows)
    rejected_written = write_rejected(conn, run_id, quarantine)

    # An empty or truncated source must never report success. A zero-byte file reads
    # cleanly (column names are supplied, not parsed from a header), so without this
    # guard pointing the pipeline at a failed download would produce a green tick, an
    # empty database and a SUCCEEDED ledger row. DEV_LOG DL-006 is exactly that failure
    # mode: a truncated download that still looked like a valid file.
    if rows_read == 0:
        status = RunStatus.FAILED
        logger.error("Source file yielded no rows: %s", path)
        logger.error("The file is empty or truncated. Verify it against the hashes in "
                     "data/README.md before re-running.")
    else:
        status = RunStatus.PARTIAL if truncated else RunStatus.SUCCEEDED
    summary = summarise(issues)
    notes = "; ".join(f"{k}={v:,}" for k, v in summary.items() if v)
    if truncated:
        notes = f"Development limit of {limit_days} day(s) applied. {notes}"

    finish_run(
        conn, run_id, status=status, rows_read=rows_read, rows_inserted=inserted,
        rows_skipped_dup=skipped, rows_rejected=rejected_written,
        min_ts=min_ts.to_pydatetime() if min_ts is not None else None,
        max_ts=max_ts.to_pydatetime() if max_ts is not None else None,
        notes=notes[:4000] or None,
    )

    return IngestionResult(
        status=status,
        run_id=run_id,
        rows_read=rows_read,
        readings_inserted=inserted,
        readings_skipped=skipped,
        rows_rejected=rejected_written,
        issues_written=issues_written,
        min_ts=min_ts.to_pydatetime() if min_ts is not None else None,
        max_ts=max_ts.to_pydatetime() if max_ts is not None else None,
        quality_summary=summary,
    )


def _coverage_issue(
    rows_read: int,
    inserted: int,
    skipped: int,
    sensor_count: int,
    *,
    rejected: int = 0,
    missing: int = 0,
) -> DataQualityIssue:
    """Record the load's own arithmetic, so the ledger is self-checking.

    Every reading the source could produce must be accounted for exactly once. Rows the
    pipeline deliberately refused - quarantined rows, and cells with no value - are
    subtracted from the expectation rather than counted as losses, because they are
    recorded elsewhere and can be audited. What is left over is unexplained, and
    unexplained means the pipeline dropped something.

    That distinction is the whole point. A reconciliation that reported a shortfall
    every time a single row was quarantined would cry wolf, and nobody would read it.
    """
    accepted_rows = rows_read - rejected
    expected = accepted_rows * sensor_count - missing
    accounted = inserted + skipped
    shortfall = expected - accounted
    return DataQualityIssue(
        issue_type="LOAD_RECONCILIATION",
        is_summary=True,
        severity=Severity.INFO if shortfall == 0 else Severity.CRITICAL,
        affected_rows=abs(shortfall),
        details=(
            f"{rows_read:,} source rows read, {rejected:,} quarantined, "
            f"{accepted_rows:,} accepted x {sensor_count} sensors "
            f"minus {missing:,} missing values = {expected:,} expected readings. "
            f"Inserted {inserted:,}, already present {skipped:,}, "
            f"unaccounted {shortfall:,}."
        ),
    )


def log_summary(result: IngestionResult) -> None:
    """Print the operator-facing summary."""
    logger.info("-" * 68)
    logger.info("Status              : %s", result.status)
    logger.info("Ingestion run id    : %s", result.run_id)
    logger.info("Source rows         : %s", f"{result.rows_read:,}")
    logger.info("Readings inserted   : %s", f"{result.readings_inserted:,}")
    logger.info("Readings already in : %s", f"{result.readings_skipped:,}")
    logger.info("Rows quarantined    : %s", f"{result.rows_rejected:,}")
    logger.info("Timestamp range     : %s to %s", result.min_ts, result.max_ts)
    logger.info("Elapsed             : %.1f s", result.elapsed_seconds)
    if result.rows_read and result.elapsed_seconds:
        logger.info("Throughput          : %.0f readings/s",
                    result.readings_inserted / result.elapsed_seconds)
    logger.info("-" * 68)
    logger.info("Data quality:")
    for issue_type in sorted(result.quality_summary):
        count = result.quality_summary[issue_type]
        marker = " " if count == 0 else "!"
        logger.info("  %s %-24s %s", marker, issue_type, f"{count:,}")
    logger.info("-" * 68)
    if result.quality_summary.get(IssueType.FLATLINE):
        logger.warning(
            "Held (frozen) readings were found and marked UNCERTAIN_STALE. They are "
            "stored and queryable but excluded from baseline statistics."
        )
