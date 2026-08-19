"""Validation and quality coding.

Nothing here deletes data. Every check assigns a quality code, records an issue, or moves
an unstorable row to quarantine, so "nothing was silently dropped" is a property the
database can prove.

The flatline check matters most on this dataset: 24 blocks, 3.35% of the file, invisible
to both null and range checks. See docs/DATA_PROFILE.md section 10.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ingestion.config import Settings
from ingestion.loader import TIMESTAMP_COLUMN, SensorDefinition
from ingestion.quality import IssueType, QualityCode, RejectReason, Severity

logger = logging.getLogger(__name__)

#: Cap on individually-recorded events of one type per run. Aggregate counts are always
#: exact; only the per-event detail rows are capped, so a pathological file cannot write
#: millions of rows into ops.DataQualityIssue.
MAX_DETAIL_ROWS_PER_TYPE = 1_000

#: The two tags the source documentation says "should be close" to one another.
CROSS_CHECK_PAIR = ("Reservoirs", "TP3")


@dataclass
class DataQualityIssue:
    """One row destined for ``ops.DataQualityIssue``."""

    issue_type: str
    severity: str
    details: str
    sensor_id: int | None = None
    window_start: pd.Timestamp | None = None
    window_end: pd.Timestamp | None = None
    affected_rows: int = 1
    observed_value: float | None = None
    is_summary: bool = False
    """True for the one aggregate row per issue type; False for a specific event.

    Detail and summary rows share this table, so anything that adds up
    ``affected_rows`` must filter on this flag or it double-counts.
    """


@dataclass
class RejectedRow:
    """One row destined for ``ops.RejectedRow`` - the dead-letter queue."""

    source_line_no: int | None
    raw_payload: str
    reason_code: str
    reason_detail: str | None = None


class IssueAccumulator:
    """Collects quality issues across chunks.

    Two kinds of record are kept. *Counters* are exact totals per issue type and sensor,
    emitted as one summary row each at the end of the run. *Details* are individually
    interesting events - a specific gap, a specific flatline block - capped so that a
    badly broken file cannot flood the table.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, int | None], int] = {}
        self._details: list[DataQualityIssue] = []
        self._detail_counts: dict[str, int] = {}
        self._truncated: set[str] = set()

    def count(self, issue_type: str, rows: int, sensor_id: int | None = None) -> None:
        """Add to the exact running total for an issue type."""
        if rows <= 0:
            return
        key = (issue_type, sensor_id)
        self._counters[key] = self._counters.get(key, 0) + rows

    def detail(self, issue: DataQualityIssue) -> None:
        """Record an individually interesting event, subject to the per-type cap."""
        seen = self._detail_counts.get(issue.issue_type, 0)
        if seen >= MAX_DETAIL_ROWS_PER_TYPE:
            self._truncated.add(issue.issue_type)
            return
        self._detail_counts[issue.issue_type] = seen + 1
        self._details.append(issue)

    def total(self, issue_type: str) -> int:
        return sum(rows for (kind, _), rows in self._counters.items() if kind == issue_type)

    def finalise(self, sensor_names: dict[int, str]) -> list[DataQualityIssue]:
        """Return every issue row to write: details, then one summary per counter."""
        issues = list(self._details)

        for (issue_type, sensor_id), rows in sorted(
            self._counters.items(), key=lambda item: (item[0][0], item[0][1] or 0)
        ):
            scope = sensor_names.get(sensor_id, "all sensors") if sensor_id else "all sensors"
            issues.append(
                DataQualityIssue(
                    issue_type=issue_type,
                    severity=_severity_for(issue_type),
                    sensor_id=sensor_id,
                    affected_rows=rows,
                    is_summary=True,
                    details=f"Run total: {rows:,} occurrence(s) of {issue_type} for {scope}.",
                )
            )

        for issue_type in sorted(self._truncated):
            issues.append(
                DataQualityIssue(
                    issue_type=issue_type,
                    severity=Severity.INFO,
                    affected_rows=0,
                    is_summary=True,
                    details=(
                        f"Per-event detail for {issue_type} was capped at "
                        f"{MAX_DETAIL_ROWS_PER_TYPE:,} rows. The summary row above carries "
                        f"the exact total."
                    ),
                )
            )
        return issues


def _severity_for(issue_type: str) -> str:
    """Map an issue type to a severity.

    Deliberately explicit rather than clever: someone reading a quality report should be
    able to see why a category is a warning and not a crisis.
    """
    critical = {
        IssueType.SCHEMA_MISMATCH,
        IssueType.UNPARSEABLE_TIMESTAMP,
        IssueType.DUPLICATE_TIMESTAMP,
        IssueType.NON_MONOTONIC_TS,
        IssueType.OUT_OF_RANGE,
        IssueType.NON_BINARY_DIGITAL,
    }
    warning = {
        IssueType.FLATLINE,
        IssueType.XSENSOR_DIVERGENCE,
        IssueType.MISSING_VALUE,
    }
    if issue_type in critical:
        return Severity.CRITICAL
    if issue_type in warning:
        return Severity.WARNING
    return Severity.INFO  # gaps are expected in this archive; 17.6% of it is absent


@dataclass
class ValidationState:
    """Carried between chunks so boundary-spanning checks stay exact."""

    prev_analogue: np.ndarray | None = None
    prev_timestamp: pd.Timestamp | None = None
    carry: pd.DataFrame | None = None
    rows_seen: int = 0


@dataclass
class ChunkResult:
    """What validation produced for one chunk."""

    frame: pd.DataFrame
    """Rows cleared for loading, in file order."""

    quality: np.ndarray
    """``uint8`` matrix, shape ``(len(frame), len(sensors))``, aligned to sensor order."""

    rejected: list[RejectedRow] = field(default_factory=list)
    held_back: int = 0
    """Rows deferred to the next chunk because they sit inside an unfinished flatline."""


def _runs_of_true(flags: np.ndarray) -> list[tuple[int, int]]:
    """Return ``(start, length)`` for each maximal run of ``True`` in *flags*."""
    if flags.size == 0 or not flags.any():
        return []
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(s), int(e - s)) for s, e in zip(edges[0::2], edges[1::2], strict=True)]


def validate_chunk(
    chunk: pd.DataFrame,
    sensors: list[SensorDefinition],
    settings: Settings,
    state: ValidationState,
    issues: IssueAccumulator,
    *,
    is_final: bool = False,
) -> ChunkResult:
    """Validate one chunk and assign a quality code to every cell.

    Chunk boundaries are handled exactly rather than approximately. A flatline that is
    still running when the chunk ends is *held back* and prepended to the next chunk, so
    a freeze spanning a boundary is measured at its true length instead of being cut in
    two and falling under the threshold. At end of file the held rows are flushed.
    """
    # Rows carried over from the previous chunk have already been through the
    # timestamp and cross-sensor checks. `carried` marks how many, so those checks are
    # applied to new rows only and cannot double-count.
    carried = 0 if state.carry is None else len(state.carry)
    frame = chunk if state.carry is None else pd.concat([state.carry, chunk], ignore_index=True)
    state.carry = None

    signal_columns = [sensor.source_column for sensor in sensors]
    analogue_columns = [s.source_column for s in sensors if not s.is_digital]

    empty_quality = np.empty((0, len(sensors)), dtype=np.uint8)

    frame, rejected = _quarantine_bad_timestamps(frame, signal_columns, issues, offset=carried)
    if frame.empty:
        return ChunkResult(frame=frame, quality=empty_quality, rejected=rejected)

    frame, ts_rejected = _check_timestamp_order(
        frame, signal_columns, state, settings, issues, offset=carried
    )
    rejected.extend(ts_rejected)
    if frame.empty:
        return ChunkResult(frame=frame, quality=empty_quality, rejected=rejected)

    # Timestamp continuity is now established for the whole buffer, so the marker
    # advances to its end even if part of the buffer is held back below.
    state.prev_timestamp = frame[TIMESTAMP_COLUMN].iloc[-1]

    unchanged = _unchanged_flags(frame, analogue_columns, state.prev_analogue)
    _check_cross_sensor(frame.iloc[carried:], sensors, settings, issues)

    # Hold back an unfinished flatline so the next chunk can measure its true length.
    # Without this, a freeze straddling a chunk boundary is cut into two shorter runs,
    # each of which may fall under the threshold and go unreported.
    held_back = 0
    if not is_final and unchanged.size and unchanged[-1]:
        # Distance from the end back to the last False; if every flag is True the whole
        # buffer is one continuing freeze and all of it is carried.
        trailing = int(np.argmin(unchanged[::-1])) if not unchanged.all() else unchanged.size
        emit_n = unchanged.size - trailing
        held_back = trailing
        state.carry = frame.iloc[emit_n:].reset_index(drop=True)
        frame = frame.iloc[:emit_n].reset_index(drop=True)
        unchanged = unchanged[:emit_n]

    if frame.empty:
        return ChunkResult(frame=frame, quality=empty_quality, rejected=rejected,
                           held_back=held_back)

    quality = _assign_quality(frame, sensors, settings, unchanged, issues)

    # The flatline anchor must be the last row actually emitted, so the first carried
    # row compares against its true predecessor next time round.
    if analogue_columns:
        state.prev_analogue = frame[analogue_columns].to_numpy()[-1]
    state.rows_seen += len(frame)

    return ChunkResult(frame=frame, quality=quality, rejected=rejected, held_back=held_back)


def _row_payload(row: pd.Series, columns: list[str]) -> str:
    """Render a source row for the quarantine table, preserving what was actually read."""
    parts = [f"{TIMESTAMP_COLUMN}={row.get(TIMESTAMP_COLUMN)!r}"]
    parts += [f"{column}={row.get(column)!r}" for column in columns]
    return ", ".join(parts)[:4000]


def _quarantine_bad_timestamps(
    frame: pd.DataFrame,
    signal_columns: list[str],
    issues: IssueAccumulator,
    *,
    offset: int = 0,
) -> tuple[pd.DataFrame, list[RejectedRow]]:
    """Move rows whose timestamp could not be parsed into quarantine.

    A reading with no timestamp cannot be keyed, so it cannot be stored - but it can be
    kept, verbatim, with a reason. ``offset`` skips rows carried over from the previous
    chunk, which have already been checked.
    """
    bad = frame[TIMESTAMP_COLUMN].isna()
    if offset:
        bad.iloc[:offset] = False
    if not bad.any():
        return frame, []

    count = int(bad.sum())
    issues.count(IssueType.UNPARSEABLE_TIMESTAMP, count)
    rejected = [
        RejectedRow(
            source_line_no=int(row.get("source_line_no", 0)) or None,
            raw_payload=_row_payload(row, signal_columns),
            reason_code=RejectReason.UNPARSEABLE_TIMESTAMP,
            reason_detail="Timestamp did not match the expected %Y-%m-%d %H:%M:%S format.",
        )
        for _, row in frame[bad].iterrows()
    ]
    logger.warning("Quarantined %s row(s) with unparseable timestamps", f"{count:,}")
    return frame[~bad].reset_index(drop=True), rejected


def _check_timestamp_order(
    frame: pd.DataFrame,
    signal_columns: list[str],
    state: ValidationState,
    settings: Settings,
    issues: IssueAccumulator,
    *,
    offset: int = 0,
) -> tuple[pd.DataFrame, list[RejectedRow]]:
    """Detect gaps, duplicates and backward steps; quarantine what cannot be stored.

    Gaps are recorded and kept, since an absent interval is information. Duplicates and
    backward steps are quarantined, because they collide on the primary key. ``offset``
    skips rows carried over from the previous chunk so a gap is not counted twice.
    """
    timestamps = frame[TIMESTAMP_COLUMN]
    previous = timestamps.shift(1)
    if state.prev_timestamp is not None and len(previous):
        previous.iloc[0] = state.prev_timestamp

    deltas = (timestamps - previous).dt.total_seconds()
    if offset:
        deltas.iloc[:offset] = np.nan  # neither a gap nor a duplicate: already assessed

    gaps = (deltas > settings.gap_threshold_seconds).fillna(False)
    if gaps.any():
        issues.count(IssueType.GAP, int(gaps.sum()))
        for position in np.flatnonzero(gaps.to_numpy()):
            seconds = float(deltas.iloc[position])
            end = timestamps.iloc[position]
            issues.detail(
                DataQualityIssue(
                    issue_type=IssueType.GAP,
                    severity=Severity.INFO,
                    window_start=end - pd.Timedelta(seconds=seconds),
                    window_end=end,
                    affected_rows=max(int(seconds // 10) - 1, 0),
                    observed_value=seconds,
                    details=(
                        f"No data for {seconds / 3600:.2f} h "
                        f"({seconds:,.0f} s), against a {settings.gap_threshold_seconds} s "
                        f"threshold."
                    ),
                )
            )

    duplicate = (deltas == 0).fillna(False)
    backward = (deltas < 0).fillna(False)
    drop = duplicate | backward
    if not drop.any():
        return frame, []

    issues.count(IssueType.DUPLICATE_TIMESTAMP, int(duplicate.sum()))
    issues.count(IssueType.NON_MONOTONIC_TS, int(backward.sum()))
    rejected = [
        RejectedRow(
            source_line_no=int(row.get("source_line_no", 0)) or None,
            raw_payload=_row_payload(row, signal_columns),
            reason_code=(
                RejectReason.DUPLICATE_TIMESTAMP
                if duplicate.iloc[position]
                else RejectReason.NON_MONOTONIC_TIMESTAMP
            ),
            reason_detail=f"Delta from previous reading was {deltas.iloc[position]:.0f} s.",
        )
        for position, (_, row) in enumerate(frame.iterrows())
        if drop.iloc[position]
    ]
    logger.warning("Quarantined %s row(s) with duplicate or backward timestamps",
                   f"{int(drop.sum()):,}")
    return frame[~drop].reset_index(drop=True), rejected


def _unchanged_flags(
    frame: pd.DataFrame, analogue_columns: list[str], prev_analogue: np.ndarray | None
) -> np.ndarray:
    """Flag rows whose analogue signals are all bit-identical to the previous scan.

    Comparing every analogue signal at once is what separates a genuinely steady process
    from a frozen logger. One sensor holding still is normal; oil temperature, five
    pressures and motor current holding the same float simultaneously is not.
    """
    if not analogue_columns:
        return np.zeros(len(frame), dtype=bool)

    values = frame[analogue_columns].to_numpy()
    if values.shape[0] == 0:
        return np.zeros(0, dtype=bool)

    reference = values if prev_analogue is None else np.vstack([prev_analogue, values])
    unchanged = np.all(reference[1:] == reference[:-1], axis=1)
    if prev_analogue is None:
        # The first row of the very first chunk has no predecessor to compare against.
        unchanged = np.concatenate(([False], unchanged))
    return unchanged


def _assign_quality(
    frame: pd.DataFrame,
    sensors: list[SensorDefinition],
    settings: Settings,
    unchanged: np.ndarray,
    issues: IssueAccumulator,
) -> np.ndarray:
    """Build the per-cell quality matrix.

    Precedence, strongest first: BAD_MISSING, BAD_DIGITAL, BAD_RANGE, UNCERTAIN_STALE,
    GOOD. A value that is both frozen and impossible is reported as impossible, because
    that is the more actionable fault.
    """
    row_count = len(frame)
    quality = np.full((row_count, len(sensors)), QualityCode.GOOD, dtype=np.uint8)

    # Flatline: only runs at or beyond the configured length count as a freeze. N
    # identical scans produce N-1 "unchanged" flags, hence the -1.
    min_unchanged = max(settings.flatline_min_samples - 1, 1)
    stale = np.zeros(row_count, dtype=bool)
    for start, length in _runs_of_true(unchanged):
        if length < min_unchanged:
            continue
        stale[start : start + length] = True
        window = frame[TIMESTAMP_COLUMN]
        issues.detail(
            DataQualityIssue(
                issue_type=IssueType.FLATLINE,
                severity=Severity.WARNING,
                window_start=window.iloc[max(start - 1, 0)],
                window_end=window.iloc[start + length - 1],
                affected_rows=length,
                details=(
                    f"All analogue signals held identical values for {length + 1:,} "
                    f"consecutive scans ({(length + 1) * 10 / 3600:.2f} h). Marked "
                    f"UNCERTAIN_STALE; excluded from baselines."
                ),
            )
        )
    if stale.any():
        issues.count(IssueType.FLATLINE, int(stale.sum()))

    for column_index, sensor in enumerate(sensors):
        values = frame[sensor.source_column].to_numpy()

        quality[stale, column_index] = QualityCode.UNCERTAIN_STALE

        if sensor.physical_min is not None and sensor.physical_max is not None:
            out_of_range = (values < sensor.physical_min) | (values > sensor.physical_max)
            out_of_range &= ~np.isnan(values)
            if out_of_range.any():
                quality[out_of_range, column_index] = QualityCode.BAD_RANGE
                issues.count(IssueType.OUT_OF_RANGE, int(out_of_range.sum()), sensor.sensor_id)

        if sensor.is_digital:
            non_binary = ~np.isin(values, (0.0, 1.0)) & ~np.isnan(values)
            if non_binary.any():
                quality[non_binary, column_index] = QualityCode.BAD_DIGITAL
                issues.count(IssueType.NON_BINARY_DIGITAL, int(non_binary.sum()), sensor.sensor_id)

        missing = np.isnan(values)
        if missing.any():
            quality[missing, column_index] = QualityCode.BAD_MISSING
            issues.count(IssueType.MISSING_VALUE, int(missing.sum()), sensor.sensor_id)

    return quality


def _check_cross_sensor(
    frame: pd.DataFrame,
    sensors: list[SensorDefinition],
    settings: Settings,
    issues: IssueAccumulator,
) -> None:
    """Check the one physical invariant the source documentation gives us.

    The dataset states Reservoirs "should be close to" TP3, so the threshold is
    documented rather than chosen. Measured divergence never exceeds 0.182 bar, so the
    0.5 bar default flags instrument failure. Recorded as an issue only, because blaming
    one of two tags would be guesswork.
    """
    left, right = CROSS_CHECK_PAIR
    available = {sensor.source_column for sensor in sensors}
    if left not in available or right not in available:
        return

    divergence = (frame[left] - frame[right]).abs()
    breaches = divergence > settings.xsensor_divergence_bar
    if not breaches.any():
        return

    count = int(breaches.sum())
    issues.count(IssueType.XSENSOR_DIVERGENCE, count)
    worst = divergence[breaches].idxmax()
    issues.detail(
        DataQualityIssue(
            issue_type=IssueType.XSENSOR_DIVERGENCE,
            severity=Severity.WARNING,
            window_start=frame[TIMESTAMP_COLUMN][breaches].min(),
            window_end=frame[TIMESTAMP_COLUMN][breaches].max(),
            affected_rows=count,
            observed_value=float(divergence.loc[worst]),
            details=(
                f"|{left} - {right}| exceeded {settings.xsensor_divergence_bar} bar on "
                f"{count:,} scan(s); worst was {divergence.loc[worst]:.3f} bar. The source "
                f"documentation states these two pressures should track one another."
            ),
        )
    )


def summarise(issues: IssueAccumulator) -> dict[str, Any]:
    """Totals for the console summary and the run ledger."""
    return {
        issue_type: issues.total(issue_type)
        for issue_type in (
            IssueType.UNPARSEABLE_TIMESTAMP,
            IssueType.DUPLICATE_TIMESTAMP,
            IssueType.NON_MONOTONIC_TS,
            IssueType.GAP,
            IssueType.MISSING_VALUE,
            IssueType.OUT_OF_RANGE,
            IssueType.NON_BINARY_DIGITAL,
            IssueType.FLATLINE,
            IssueType.XSENSOR_DIVERGENCE,
        )
    }
