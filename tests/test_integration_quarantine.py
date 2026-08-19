"""End-to-end proof that bad data is quarantined rather than lost.

test_validation.py proves the validator decides correctly. It does not prove the
decision survives the trip to SQL Server: write_rejected had never executed once,
because the real file is clean enough that no row is ever rejected.

This drives the whole pipeline against a deliberately corrupted file, then queries the
database to confirm every problem landed somewhere findable. It builds and drops its own
database, and skips when no SQL Server is reachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ingestion.config import Settings
from ingestion.db import DatabaseError, connect, execute_script_file, fetch_all, scalar
from ingestion.quality import IssueType, QualityCode, RejectReason, RunStatus
from tests.conftest import make_row

TEST_DATABASE = "IndustrialMonitor_PytestTmp"
SQL_DIR = Path(__file__).resolve().parents[1] / "database"


def _base_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "db_server": r"localhost\SQLEXPRESS01",
        "db_database": TEST_DATABASE,
        "db_trusted_connection": True,
        "db_connect_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _server_reachable() -> bool:
    try:
        with connect(database="master", settings=_base_settings()) as conn:
            scalar(conn, "SELECT 1")
        return True
    except (DatabaseError, Exception):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _server_reachable(),
                       reason="no reachable SQL Server; integration tests skipped"),
]


@pytest.fixture(scope="module")
def test_db():
    """Create a throwaway database, apply schema and seed, drop it afterwards."""
    settings = _base_settings()

    with connect(database="master", autocommit=True, settings=settings) as conn:
        cur = conn.cursor()
        if scalar(conn, "SELECT DB_ID(?)", (TEST_DATABASE,)) is not None:
            cur.execute(f"ALTER DATABASE [{TEST_DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
            cur.execute(f"DROP DATABASE [{TEST_DATABASE}]")
        cur.execute(f"CREATE DATABASE [{TEST_DATABASE}]")

    with connect(settings=settings) as conn:
        execute_script_file(conn, SQL_DIR / "schema.sql")
        execute_script_file(conn, SQL_DIR / "seed.sql")

    yield settings

    with connect(database="master", autocommit=True, settings=settings) as conn:
        cur = conn.cursor()
        cur.execute(f"ALTER DATABASE [{TEST_DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE")
        cur.execute(f"DROP DATABASE [{TEST_DATABASE}]")


@pytest.fixture(scope="module")
def corrupt_csv(tmp_path_factory) -> Path:
    """A file containing one of every problem the pipeline claims to handle.

    Deliberately hostile, and deliberately small enough to reason about by hand.
    """
    header = (",timestamp,TP2,TP3,H1,DV_pressure,Reservoirs,Oil_temperature,Motor_current,"
              "COMP,DV_eletric,Towers,MPG,LPS,Pressure_switch,Oil_level,Caudal_impulses")
    rows = [
        # --- three ordinary scans, values changing --------------------------------
        make_row(0,  "2021-01-01 00:00:00", analogue=5.0),
        make_row(10, "2021-01-01 00:00:10", analogue=5.1),
        make_row(20, "2021-01-01 00:00:20", analogue=5.2),
        # --- an unparseable timestamp ---------------------------------------------
        make_row(30, "not-a-timestamp", analogue=5.3),
        # --- a duplicate timestamp ------------------------------------------------
        make_row(40, "2021-01-01 00:00:30", analogue=5.4),
        make_row(50, "2021-01-01 00:00:30", analogue=5.5),
        # --- a backward step ------------------------------------------------------
        make_row(60, "2021-01-01 00:00:25", analogue=5.6),
        # --- a physically impossible pressure and a non-binary digital ------------
        make_row(70, "2021-01-01 00:00:40", analogue=5.7, TP2=999.0, COMP=0.5),
        # --- a two-hour gap, then a seven-scan freeze -----------------------------
        make_row(80,  "2021-01-01 02:00:00", analogue=7.0),
        make_row(90,  "2021-01-01 02:00:10", analogue=7.0),
        make_row(100, "2021-01-01 02:00:20", analogue=7.0),
        make_row(110, "2021-01-01 02:00:30", analogue=7.0),
        make_row(120, "2021-01-01 02:00:40", analogue=7.0),
        make_row(130, "2021-01-01 02:00:50", analogue=7.0),
        make_row(140, "2021-01-01 02:01:00", analogue=7.0),
        # --- back to normal -------------------------------------------------------
        make_row(150, "2021-01-01 02:01:10", analogue=8.0),
    ]
    path = tmp_path_factory.mktemp("corrupt") / "corrupt.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def ingested(test_db, corrupt_csv):
    """Run the real pipeline against the corrupt file once, share the result."""
    from ingestion.pipeline import run_ingestion

    settings = test_db.model_copy(update={"ingest_chunk_rows": 1_000,
                                          "ingest_batch_rows": 1_000})
    return run_ingestion(settings, csv_path=corrupt_csv), settings


# ---------------------------------------------------------------------------------

def test_run_succeeds_despite_bad_rows(ingested):
    """Bad rows are a data problem, not a pipeline failure. The load completes."""
    result, _ = ingested
    assert result.status == RunStatus.SUCCEEDED
    assert result.rows_read == 16


def test_rejected_rows_reach_the_quarantine_table(ingested):
    """The code path that had never executed during any real load."""
    _, settings = ingested
    with connect(settings=settings) as conn:
        rows = fetch_all(conn, """
            SELECT ReasonCode, SourceLineNo, RawPayload
            FROM ops.RejectedRow ORDER BY SourceLineNo""")

    reasons = [r[0] for r in rows]
    assert len(rows) == 3, "one unparseable, one duplicate, one backward"
    assert RejectReason.UNPARSEABLE_TIMESTAMP in reasons
    assert RejectReason.DUPLICATE_TIMESTAMP in reasons
    assert RejectReason.NON_MONOTONIC_TIMESTAMP in reasons

    for _, line_no, payload in rows:
        assert line_no is not None, "a quarantined row must point back at its source line"
        assert "TP2=" in payload, "the original values must be recoverable"


def test_ledger_counts_the_rejections(ingested):
    """`RowsRejected` on the run is what an operator sees first."""
    result, settings = ingested
    assert result.rows_rejected == 3
    with connect(settings=settings) as conn:
        assert scalar(conn, "SELECT RowsRejected FROM ops.IngestionRun WHERE Status = ?",
                      (RunStatus.SUCCEEDED,)) == 3


def test_every_issue_type_is_recorded(ingested):
    _, settings = ingested
    with connect(settings=settings) as conn:
        # Summary rows only. Detail and summary rows coexist in this table, so
        # summing both double-counts every issue - the bug this test caught.
        totals = dict(fetch_all(conn, """
            SELECT IssueType, SUM(CAST(AffectedRows AS BIGINT))
            FROM ops.DataQualityIssue WHERE IsSummary = 1 GROUP BY IssueType"""))

    assert totals.get(IssueType.UNPARSEABLE_TIMESTAMP) == 1
    assert totals.get(IssueType.DUPLICATE_TIMESTAMP) == 1
    assert totals.get(IssueType.NON_MONOTONIC_TS) == 1
    assert totals.get(IssueType.OUT_OF_RANGE) == 1
    assert totals.get(IssueType.NON_BINARY_DIGITAL) == 1
    assert totals.get(IssueType.FLATLINE) == 6, "7 identical scans produce 6 held rows"
    assert IssueType.GAP in totals


def test_bad_values_are_stored_and_labelled_not_deleted(ingested):
    """The central claim of the whole design, verified against the database."""
    _, settings = ingested
    with connect(settings=settings) as conn:
        impossible = fetch_all(conn, """
            SELECT r.Value, q.Code
            FROM ts.SensorReading r
            JOIN asset.Sensor s ON s.SensorId = r.SensorId
            JOIN ref.QualityCode q ON q.QualityCodeId = r.QualityCodeId
            WHERE s.SensorCode = 'TP2' AND r.Value > 900""")

    assert len(impossible) == 1, "the impossible reading is still in the archive"
    assert impossible[0][0] == pytest.approx(999.0)
    assert impossible[0][1] == "BAD_RANGE", "stored, and labelled as untrustworthy"


def test_held_readings_are_marked_uncertain_stale(ingested):
    _, settings = ingested
    with connect(settings=settings) as conn:
        held = scalar(conn, """
            SELECT COUNT_BIG(*) FROM ts.SensorReading
            WHERE QualityCodeId = ?""", (int(QualityCode.UNCERTAIN_STALE),))
    assert held == 6 * 15, "the freeze affects the whole scan, all 15 tags"


def test_reconciliation_accounts_for_every_expected_reading(ingested):
    """13 accepted rows x 15 sensors = 195 readings, and the ledger says so."""
    _, settings = ingested
    with connect(settings=settings) as conn:
        stored = scalar(conn, "SELECT COUNT_BIG(*) FROM ts.SensorReading")
        detail = scalar(conn, """
            SELECT Details FROM ops.DataQualityIssue
            WHERE IssueType = 'LOAD_RECONCILIATION'""")

    assert stored == 13 * 15
    assert "3 quarantined" in detail
    assert "unaccounted 0" in detail, (
        "quarantined rows must be subtracted from the expectation, not reported as loss"
    )


def test_staging_is_left_empty(ingested):
    _, settings = ingested
    with connect(settings=settings) as conn:
        assert scalar(conn, "SELECT COUNT_BIG(*) FROM stg.SensorReadingStage") == 0


def test_empty_source_file_fails_rather_than_reporting_success(test_db, tmp_path):
    """A truncated download reads cleanly and would otherwise produce a green tick over
    an empty database. DEV_LOG DL-006 is exactly that failure mode."""
    from ingestion.pipeline import run_ingestion

    empty = tmp_path / "truncated.csv"
    empty.write_text("", encoding="utf-8")

    result = run_ingestion(test_db, csv_path=empty)

    assert result.status == RunStatus.FAILED
    assert result.succeeded is False
    assert result.readings_inserted == 0
