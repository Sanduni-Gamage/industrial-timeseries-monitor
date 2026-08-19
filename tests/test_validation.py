"""Validation and quality-coding tests.

Priority here is the unhappy paths. The MetroPT-3 file is clean - zero nulls, zero
duplicate timestamps, zero out-of-range values - so the quarantine and rejection code in
`ingestion/validator.py` never executed once during the real load. Code that only runs
when something goes wrong is exactly the code that is broken when something goes wrong,
so it is proven here with deliberately malformed input.
"""

from __future__ import annotations

import numpy as np
import pytest

from ingestion.quality import IssueType, QualityCode, RejectReason
from ingestion.validator import (
    IssueAccumulator,
    ValidationState,
    validate_chunk,
)


def quality_for(result, sensors, sensor_code: str) -> np.ndarray:
    """Pull one sensor's column out of the quality matrix by code."""
    index = next(i for i, s in enumerate(sensors) if s.sensor_code == sensor_code)
    return result.quality[:, index]


def run(chunk, sensors, settings, *, state=None, issues=None, is_final=True):
    state = state or ValidationState()
    issues = issues or IssueAccumulator()
    result = validate_chunk(chunk, sensors, settings, state, issues, is_final=is_final)
    return result, issues, state


# ---------------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------------

def test_clean_chunk_is_all_good(chunks, sensors, settings):
    result, issues, _ = run(chunks.steady(20), sensors, settings)

    assert len(result.frame) == 20
    assert result.quality.shape == (20, 15)
    assert (result.quality == QualityCode.GOOD).all()
    assert result.rejected == []
    for issue_type in (IssueType.GAP, IssueType.FLATLINE, IssueType.OUT_OF_RANGE,
                       IssueType.DUPLICATE_TIMESTAMP, IssueType.MISSING_VALUE):
        assert issues.total(issue_type) == 0


# ---------------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------------

def test_unparseable_timestamp_is_quarantined_not_dropped(chunks, sensors, settings):
    """A row with no usable timestamp cannot be keyed, so it cannot be stored - but it
    must survive verbatim in the quarantine with a reason."""
    chunk = chunks.build([
        "2020-02-01 00:00:00",
        None,                      # the source had something unparseable here
        "2020-02-01 00:00:20",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 2, "the bad row must not reach the fact table"
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert rejected.reason_code == RejectReason.UNPARSEABLE_TIMESTAMP
    assert rejected.source_line_no == 3, "line number must point at the real source line"
    assert "TP2=" in rejected.raw_payload, "the original values must be preserved"
    assert issues.total(IssueType.UNPARSEABLE_TIMESTAMP) == 1


def test_duplicate_timestamp_is_quarantined(chunks, sensors, settings):
    """Two readings at the same instant would collide on (SensorId, ReadingTs)."""
    chunk = chunks.build([
        "2020-02-01 00:00:00",
        "2020-02-01 00:00:10",
        "2020-02-01 00:00:10",     # duplicate
        "2020-02-01 00:00:20",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 3
    assert len(result.rejected) == 1
    assert result.rejected[0].reason_code == RejectReason.DUPLICATE_TIMESTAMP
    assert issues.total(IssueType.DUPLICATE_TIMESTAMP) == 1
    assert result.frame["timestamp"].is_unique


def test_backward_timestamp_is_quarantined(chunks, sensors, settings):
    chunk = chunks.build([
        "2020-02-01 00:00:00",
        "2020-02-01 00:00:20",
        "2020-02-01 00:00:10",     # goes backwards
        "2020-02-01 00:00:30",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 3
    assert result.rejected[0].reason_code == RejectReason.NON_MONOTONIC_TIMESTAMP
    assert issues.total(IssueType.NON_MONOTONIC_TS) == 1
    assert result.frame["timestamp"].is_monotonic_increasing


def test_gap_recorded_but_data_kept(chunks, sensors, settings):
    """A gap is information, not an error. 17.6% of the real archive is absent."""
    chunk = chunks.build([
        "2020-02-01 00:00:00",
        "2020-02-01 00:00:10",
        "2020-02-01 02:00:00",     # ~2 hour gap
        "2020-02-01 02:00:10",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 4, "gaps must not cause rows to be discarded"
    assert result.rejected == []
    assert issues.total(IssueType.GAP) == 1


def test_jitter_below_threshold_is_not_a_gap(chunks, sensors, settings):
    """The real file has 128,277 steps of 9 s and 38,321 of 12 s against a 10 s mode.
    A threshold that flagged those would report a million false gaps."""
    chunk = chunks.build([
        "2020-02-01 00:00:00",
        "2020-02-01 00:00:09",     # 9 s
        "2020-02-01 00:00:21",     # 12 s
        "2020-02-01 00:00:34",     # 13 s
        "2020-02-01 00:00:55",     # 21 s - still under the 30 s threshold
    ])
    _, issues, _ = run(chunk, sensors, settings)
    assert issues.total(IssueType.GAP) == 0


def test_gap_threshold_boundary_is_exclusive(chunks, sensors, settings):
    """Exactly at the threshold is not a gap; one second past it is."""
    at_threshold = chunks.build(["2020-02-01 00:00:00", "2020-02-01 00:00:30"])
    _, issues, _ = run(at_threshold, sensors, settings)
    assert issues.total(IssueType.GAP) == 0

    past_threshold = chunks.build(["2020-02-01 00:00:00", "2020-02-01 00:00:31"])
    _, issues, _ = run(past_threshold, sensors, settings)
    assert issues.total(IssueType.GAP) == 1


# ---------------------------------------------------------------------------------
# Range and type rules
# ---------------------------------------------------------------------------------

def test_negative_gauge_pressure_is_good_not_bad(chunks, sensors, settings):
    """Regression guard for the trap that would have silently destroyed the dataset.

    A naive `Value >= 0` constraint on a pressure tag rejects 1,275,474 of 1,516,948 TP2
    readings - 84% of them. They are gauge pressures with a small zero offset that the
    sensor reports whenever the compressor is unloaded, and they are correct.
    """
    chunk = chunks.steady(3, overrides={"TP2": [-0.012, -0.014, -0.032]})
    result, issues, _ = run(chunk, sensors, settings)

    assert (quality_for(result, sensors, "TP2") == QualityCode.GOOD).all()
    assert issues.total(IssueType.OUT_OF_RANGE) == 0


def test_impossible_value_marked_bad_range_but_retained(chunks, sensors, settings):
    """Out-of-range values are labelled, never deleted."""
    chunk = chunks.steady(3, overrides={"TP2": [5.0, 999.0, 5.2]})
    result, issues, _ = run(chunk, sensors, settings)

    codes = quality_for(result, sensors, "TP2")
    assert codes[1] == QualityCode.BAD_RANGE
    assert codes[0] == QualityCode.GOOD
    assert len(result.frame) == 3, "the row stays in the archive"
    assert result.frame["TP2"].iloc[1] == 999.0, "the original value is unchanged"
    assert issues.total(IssueType.OUT_OF_RANGE) == 1


def test_range_check_is_inclusive_at_the_limits(chunks, sensors, settings):
    """PhysicalMin/Max are plausibility bounds; a value exactly on one is still valid."""
    chunk = chunks.steady(2, overrides={"TP2": [-1.0, 16.0]})
    result, _, _ = run(chunk, sensors, settings)
    assert (quality_for(result, sensors, "TP2") == QualityCode.GOOD).all()


def test_non_binary_digital_marked_bad(chunks, sensors, settings):
    """All eight digital tags are strictly 0/1 in the real file, so this rule never
    fires there. It exists because the next file might not be so clean."""
    chunk = chunks.steady(3, overrides={"COMP": [0.0, 0.5, 1.0]})
    result, issues, _ = run(chunk, sensors, settings)

    codes = quality_for(result, sensors, "COMP")
    assert codes[1] == QualityCode.BAD_DIGITAL
    assert codes[0] == QualityCode.GOOD and codes[2] == QualityCode.GOOD
    assert issues.total(IssueType.NON_BINARY_DIGITAL) == 1


def test_missing_value_marked_bad_missing(chunks, sensors, settings):
    chunk = chunks.steady(3, overrides={"TP3": [8.0, np.nan, 8.2]})
    result, issues, _ = run(chunk, sensors, settings)

    assert quality_for(result, sensors, "TP3")[1] == QualityCode.BAD_MISSING
    assert issues.total(IssueType.MISSING_VALUE) == 1


def test_bad_range_takes_precedence_over_stale(chunks, sensors, settings):
    """A value that is both frozen and impossible is reported as impossible, because
    that is the more actionable fault."""
    frozen_impossible = [999.0] * 10
    chunk = chunks.build(
        [f"2020-02-01 00:{i:02d}:00" for i in range(10)],
        overrides={column: frozen_impossible if column == "TP2" else [7.0] * 10
                   for column in chunks.columns if column in
                   {"TP2", "TP3", "H1", "DV_pressure", "Reservoirs",
                    "Oil_temperature", "Motor_current"}},
    )
    result, _, _ = run(chunk, sensors, settings)
    assert (quality_for(result, sensors, "TP2") == QualityCode.BAD_RANGE).all()


# ---------------------------------------------------------------------------------
# Flatline detection - the finding the whole project turns on
# ---------------------------------------------------------------------------------

def test_flatline_marked_uncertain_stale(chunks, sensors, settings):
    """Every analogue signal holding an identical value is a logger freeze, not a
    steady process. Ten identical scans is well past the six-sample threshold."""
    chunk = chunks.build([f"2020-02-01 00:{i:02d}:00" for i in range(10)])
    result, issues, _ = run(chunk, sensors, settings)

    codes = quality_for(result, sensors, "TP2")
    assert codes[0] == QualityCode.GOOD, "the first scan is the last genuine reading"
    assert (codes[1:] == QualityCode.UNCERTAIN_STALE).all()
    assert issues.total(IssueType.FLATLINE) == 9
    assert len(result.frame) == 10, "held rows are stored, never deleted"


def test_short_plateau_is_not_a_flatline(chunks, sensors, settings):
    """Three identical scans is a plausible steady moment, not a freeze. The threshold
    is six samples (60 s at the measured 10 s cadence)."""
    chunk = chunks.build([f"2020-02-01 00:00:{i * 10:02d}" for i in range(3)])
    _, issues, _ = run(chunk, sensors, settings)
    assert issues.total(IssueType.FLATLINE) == 0


def test_one_sensor_holding_still_is_not_a_flatline(chunks, sensors, settings):
    """A single steady tag is normal. The check requires ALL analogue signals to freeze
    together, which is what makes it evidence of a logger fault rather than a quiet
    process."""
    count = 12
    overrides = {"TP2": [5.0] * count}       # TP2 frozen
    overrides["TP3"] = [8.0 + i * 0.01 for i in range(count)]  # others still moving
    chunk = chunks.steady(count, overrides=overrides)
    _, issues, _ = run(chunk, sensors, settings)
    assert issues.total(IssueType.FLATLINE) == 0


def test_flatline_spanning_a_chunk_boundary_is_measured_whole(chunks, sensors, settings):
    """The trickiest logic in the pipeline, and the reason it exists.

    A freeze straddling a chunk boundary must not be cut into two shorter runs that each
    fall under the threshold. Here 4 frozen scans end chunk one and 4 begin chunk two;
    neither half reaches the six-sample threshold alone.

    N identical scans produce N-1 flagged rows: the first is the last genuine reading and
    stays GOOD.
    """
    state = ValidationState()
    issues = IssueAccumulator()

    first = chunks.build(
        ["2020-02-01 00:00:00", "2020-02-01 00:00:10",
         "2020-02-01 00:00:20", "2020-02-01 00:00:30", "2020-02-01 00:00:40"],
        overrides={"TP2": [1.0, 5.0, 5.0, 5.0, 5.0]},
    )
    result_a = validate_chunk(first, sensors, settings, state, issues, is_final=False)

    assert result_a.held_back > 0, "an unfinished freeze must be held back"
    assert issues.total(IssueType.FLATLINE) == 0, "nothing decided yet"

    second = chunks.build(
        ["2020-02-01 00:00:50", "2020-02-01 00:01:00",
         "2020-02-01 00:01:10", "2020-02-01 00:01:20"],
        overrides={"TP2": [5.0, 5.0, 5.0, 5.0]},
        start_line=7,
    )
    result_b = validate_chunk(second, sensors, settings, issues=issues,
                              state=state, is_final=True)

    total_rows = len(result_a.frame) + len(result_b.frame)
    assert total_rows == 9, "every row must be emitted exactly once across the two chunks"
    assert issues.total(IssueType.FLATLINE) == 7, (
        "8 identical scans across the boundary must be flagged as 7 held rows; "
        "splitting the run at the chunk edge would report 0"
    )


def test_carried_rows_do_not_double_count_gaps(chunks, sensors, settings):
    """Rows held back for a freeze are re-processed in the next chunk. Their timestamp
    checks must not run twice, or one gap would be reported as two."""
    state = ValidationState()
    issues = IssueAccumulator()

    first = chunks.build(
        ["2020-02-01 00:00:00", "2020-02-01 03:00:00", "2020-02-01 03:00:10",
         "2020-02-01 03:00:20", "2020-02-01 03:00:30"],
        overrides={"TP2": [1.0, 5.0, 5.0, 5.0, 5.0]},
    )
    validate_chunk(first, sensors, settings, state, issues, is_final=False)
    assert issues.total(IssueType.GAP) == 1

    second = chunks.build(["2020-02-01 03:00:40"],
                          overrides={"TP2": [5.0]}, start_line=7)
    validate_chunk(second, sensors, settings, state, issues, is_final=True)

    assert issues.total(IssueType.GAP) == 1, "the same gap must not be counted twice"


# ---------------------------------------------------------------------------------
# Cross-sensor invariant
# ---------------------------------------------------------------------------------

def test_reservoirs_tp3_divergence_recorded(chunks, sensors, settings):
    """The source states Reservoirs should track TP3. Largest divergence ever observed
    in the real file is 0.182 bar, so 0.5 bar flags a failed instrument, not noise."""
    chunk = chunks.steady(
        4,
        overrides={"TP3": [8.0, 8.0, 8.0, 8.0], "Reservoirs": [8.0, 8.0, 9.5, 8.0]},
    )
    result, issues, _ = run(chunk, sensors, settings)

    assert issues.total(IssueType.XSENSOR_DIVERGENCE) == 1
    assert (quality_for(result, sensors, "TP3") != QualityCode.BAD_RANGE).all(), (
        "divergence is a relationship between two tags; blaming one with a quality code "
        "would be guessing which sensor drifted"
    )


def test_small_divergence_ignored(chunks, sensors, settings):
    chunk = chunks.steady(
        3, overrides={"TP3": [8.0, 8.0, 8.0], "Reservoirs": [8.006, 8.0, 7.995]}
    )
    _, issues, _ = run(chunk, sensors, settings)
    assert issues.total(IssueType.XSENSOR_DIVERGENCE) == 0


# ---------------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------------

def test_empty_chunk(chunks, sensors, settings):
    result, _, _ = run(chunks.build([]), sensors, settings)
    assert len(result.frame) == 0
    assert result.quality.shape == (0, 15)
    assert result.rejected == []


def test_single_row_chunk(chunks, sensors, settings):
    """One row has no predecessor, so no delta, no gap and no freeze can be computed."""
    result, issues, _ = run(chunks.build(["2020-02-01 00:00:00"]), sensors, settings)
    assert len(result.frame) == 1
    assert (result.quality == QualityCode.GOOD).all()
    assert issues.total(IssueType.GAP) == 0
    assert issues.total(IssueType.FLATLINE) == 0


def test_every_row_unparseable(chunks, sensors, settings):
    """Total rejection must not crash, and must leave an empty but well-shaped result."""
    result, issues, _ = run(chunks.build([None, None, None]), sensors, settings)
    assert len(result.frame) == 0
    assert len(result.rejected) == 3
    assert issues.total(IssueType.UNPARSEABLE_TIMESTAMP) == 3


def test_issue_detail_rows_are_capped_but_counts_stay_exact(chunks, sensors, settings):
    """A badly broken file must not write millions of rows into ops.DataQualityIssue.
    Per-event detail is capped; the aggregate count is always exact."""
    from ingestion.validator import MAX_DETAIL_ROWS_PER_TYPE, DataQualityIssue

    issues = IssueAccumulator()
    for _ in range(MAX_DETAIL_ROWS_PER_TYPE + 500):
        issues.detail(DataQualityIssue(issue_type=IssueType.GAP, severity="INFO",
                                       details="x"))
        issues.count(IssueType.GAP, 1)

    written = issues.finalise({})
    gap_details = [i for i in written if i.issue_type == IssueType.GAP
                   and i.details == "x"]
    assert len(gap_details) == MAX_DETAIL_ROWS_PER_TYPE
    assert issues.total(IssueType.GAP) == MAX_DETAIL_ROWS_PER_TYPE + 500
    assert any("capped" in i.details for i in written), (
        "truncation must be disclosed, not silent"
    )


@pytest.mark.parametrize("value", [np.inf, -np.inf])
def test_infinite_values_are_caught_as_out_of_range(chunks, sensors, settings, value):
    """A malformed float must not slip through as plausible."""
    chunk = chunks.steady(3, overrides={"TP2": [5.0, value, 5.2]})
    result, issues, _ = run(chunk, sensors, settings)
    assert quality_for(result, sensors, "TP2")[1] == QualityCode.BAD_RANGE
    assert issues.total(IssueType.OUT_OF_RANGE) == 1


# ---------------------------------------------------------------------------------
# Timezone and encoding edge cases
# ---------------------------------------------------------------------------------

def test_dst_repeated_local_time_is_quarantined_not_merged(chunks, sensors, settings):
    """A documented limitation of a timezone-less source, pinned by a test.

    During a daylight-saving fall-back the same local clock time occurs twice, and a naive
    archive cannot tell the two instants apart. The second occurrence collides on the
    primary key and is quarantined verbatim rather than overwriting the first. Inventing a
    timezone to disambiguate would fabricate information the source does not contain.
    """
    chunk = chunks.build([
        "2020-10-25 01:59:50",
        "2020-10-25 02:30:00",   # first pass, summer time
        "2020-10-25 02:30:00",   # second pass after the clocks go back
        "2020-10-25 03:00:00",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 3
    assert len(result.rejected) == 1
    assert result.rejected[0].reason_code == RejectReason.DUPLICATE_TIMESTAMP
    assert issues.total(IssueType.DUPLICATE_TIMESTAMP) == 1
    assert result.frame["timestamp"].is_unique


def test_dst_spring_forward_gap_is_reported_as_a_gap(chunks, sensors, settings):
    """The mirror case: local time jumps forward, so an hour of clock time never exists.

    In a naive archive that is indistinguishable from an hour of missing data, and it is
    reported as a gap. That is honest - the archive genuinely holds no readings for that
    clock hour - and it is why the gap count is described as 'periods with no data'
    rather than 'logger outages'.
    """
    chunk = chunks.build([
        "2020-03-29 00:59:50",
        "2020-03-29 01:00:00",
        "2020-03-29 02:00:00",   # clocks jumped; one hour of local time does not exist
        "2020-03-29 02:00:10",
    ])
    result, issues, _ = run(chunk, sensors, settings)

    assert len(result.frame) == 4, "no data is discarded for a clock change"
    assert issues.total(IssueType.GAP) == 1


def test_leap_day_is_parsed(chunks, sensors, settings):
    """2020 is a leap year and the archive spans February. A date library that rejects
    29 February would lose a whole day."""
    chunk = chunks.build(["2020-02-29 12:00:00", "2020-02-29 12:00:10"])
    result, _, _ = run(chunk, sensors, settings)
    assert len(result.frame) == 2
    assert result.frame["timestamp"].iloc[0].day == 29
