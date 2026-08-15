"""Pipeline-level tests: reconciliation arithmetic and run-status decisions.

These are the rules that decide whether an operator sees a green tick or a red one, so
they are worth testing directly rather than only observing them on a real load.
"""

from __future__ import annotations

import pytest

from ingestion.pipeline import IngestionResult, _coverage_issue
from ingestion.quality import RunStatus, Severity

# ---------------------------------------------------------------------------------
# Reconciliation - the load checking its own arithmetic
# ---------------------------------------------------------------------------------

def test_reconciliation_balances_on_a_clean_load():
    issue = _coverage_issue(rows_read=1_516_948, inserted=22_754_220, skipped=0,
                            sensor_count=15)
    assert issue.severity == Severity.INFO
    assert issue.affected_rows == 0
    assert "22,754,220 expected" in issue.details
    assert issue.is_summary, "reconciliation is a summary row, not a per-event detail"


def test_reconciliation_balances_on_a_full_reingest():
    """Nothing inserted because everything was already present is still balanced."""
    issue = _coverage_issue(rows_read=1_516_948, inserted=0, skipped=22_754_220,
                            sensor_count=15)
    assert issue.severity == Severity.INFO
    assert issue.affected_rows == 0


def test_quarantined_rows_are_subtracted_not_treated_as_loss():
    """Regression guard. Quarantined rows are recorded in ops.RejectedRow and can be
    audited, so they must reduce the expectation rather than show up as an unexplained
    shortfall. Reporting a discrepancy every time one row was rejected would cry wolf."""
    issue = _coverage_issue(rows_read=16, inserted=195, skipped=0, sensor_count=15,
                            rejected=3)
    assert issue.severity == Severity.INFO
    assert issue.affected_rows == 0
    assert "3 quarantined" in issue.details
    assert "unaccounted 0" in issue.details


def test_missing_values_are_subtracted_too():
    """A cell with no value cannot be stored NOT NULL, so it is not an expected row."""
    issue = _coverage_issue(rows_read=10, inserted=149, skipped=0, sensor_count=15,
                            missing=1)
    assert issue.severity == Severity.INFO
    assert issue.affected_rows == 0


def test_reconciliation_flags_a_shortfall():
    """Rows that are neither inserted, already present, quarantined nor missing were
    lost by the pipeline. That is a code fault, not a data fault, and it is CRITICAL."""
    issue = _coverage_issue(rows_read=100, inserted=1_400, skipped=0, sensor_count=15)
    assert issue.severity == Severity.CRITICAL
    assert issue.affected_rows == 100
    assert "unaccounted 100" in issue.details


def test_reconciliation_flags_an_excess():
    """More readings than the source could produce means duplicates leaked past the
    idempotency guard."""
    issue = _coverage_issue(rows_read=100, inserted=1_600, skipped=0, sensor_count=15)
    assert issue.severity == Severity.CRITICAL
    assert issue.affected_rows == 100


# ---------------------------------------------------------------------------------
# Run status -> exit code
# ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status, expected",
    [
        (RunStatus.SUCCEEDED, True),
        (RunStatus.SKIPPED, True),
        (RunStatus.PARTIAL, True),    # only produced by an explicit --limit-days
        (RunStatus.FAILED, False),
        (RunStatus.RUNNING, False),   # an interrupted run must not look successful
    ],
)
def test_status_maps_to_the_right_verdict(status, expected):
    assert IngestionResult(status=status).succeeded is expected


def test_partial_is_a_success_but_stays_partial_in_the_ledger():
    """The operator asked for a subset and got one, so the command succeeds. The ledger
    still records PARTIAL so a truncated load can never later be mistaken for a full
    one - which is the mistake that would matter."""
    result = IngestionResult(status=RunStatus.PARTIAL, rows_read=7_144)
    assert result.succeeded
    assert result.status == RunStatus.PARTIAL
