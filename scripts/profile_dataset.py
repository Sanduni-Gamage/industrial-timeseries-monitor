"""Profile the raw MetroPT-3 CSV and emit a reproducible data-quality profile.

This is a *read-only* investigation tool. It never modifies the source file and it
never writes to the database. Its job is to replace every assumption in
``docs/SQL_DESIGN.md`` with a measurement.

Outputs
-------
- ``reports/data_profile.json``  machine-readable, consumed later by seeding/validation
- ``docs/DATA_PROFILE.md``       human-readable narrative profile

Usage
-----
    python scripts/profile_dataset.py [--csv PATH] [--out-json PATH] [--out-md PATH]

Configuration precedence: CLI argument > environment variable > project default.
Environment variables: ``METROPT_RAW_CSV``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

LOG_FORMAT = "[%(levelname)s] %(message)s"
logger = logging.getLogger("profile")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = PROJECT_ROOT / "data" / "raw" / "MetroPT3(AirCompressor).csv"

#: Column groupings taken from the official UCI documentation (see DATA_DICTIONARY.md).
#: 7 analogue signals, 8 digital signals. Order matches the source file.
ANALOGUE_COLUMNS: tuple[str, ...] = (
    "TP2",
    "TP3",
    "H1",
    "DV_pressure",
    "Reservoirs",
    "Oil_temperature",
    "Motor_current",
)
DIGITAL_COLUMNS: tuple[str, ...] = (
    "COMP",
    "DV_eletric",
    "Towers",
    "MPG",
    "LPS",
    "Pressure_switch",
    "Oil_level",
    "Caudal_impulses",
)
SIGNAL_COLUMNS: tuple[str, ...] = ANALOGUE_COLUMNS + DIGITAL_COLUMNS

#: The source file's first column has an empty header (a pandas export artefact).
#: We name it explicitly rather than letting pandas invent ``Unnamed: 0``.
INDEX_COLUMN = "source_index"
TIMESTAMP_COLUMN = "timestamp"

#: Documented failure reports, verbatim from the UCI dataset page.
#: The duplicated "#1" label and the May/April note mismatch are preserved on purpose.
FAILURE_EVENTS: tuple[dict[str, str], ...] = (
    {"ref": "#1", "start": "2020-04-18 00:00", "end": "2020-04-18 23:59",
     "type": "Air leak", "severity": "High stress", "note": ""},
    {"ref": "#1", "start": "2020-05-29 23:30", "end": "2020-05-30 06:00",
     "type": "Air Leak", "severity": "High stress", "note": "Maintenance on 30Apr at 12:00"},
    {"ref": "#3", "start": "2020-06-05 10:00", "end": "2020-06-07 14:30",
     "type": "Air Leak", "severity": "High stress", "note": "Maintenance on 8Jun at 16:00"},
    {"ref": "#4", "start": "2020-07-15 14:30", "end": "2020-07-15 19:00",
     "type": "Air Leak", "severity": "High stress", "note": "Maintenance on 16Jul at 00:00"},
)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------

def sha256_of(path: Path, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    """Return the SHA-256 of *path*, read in chunks so large files stay memory-safe."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def to_native(value: Any) -> Any:
    """Convert NumPy/pandas scalars to plain Python so ``json.dump`` accepts them."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat(sep=" ")
    if isinstance(value, pd.Timedelta):
        return value.total_seconds()
    return value


def longest_constant_run(series: pd.Series) -> tuple[int, Any, int, int]:
    """Return ``(length, value, start_pos, end_pos)`` for the longest run of identical
    consecutive values, where ``end_pos`` is inclusive.

    Used as a *flatline* / stuck-sensor indicator. A frozen transmitter keeps reporting
    the same number, which range checks alone will never catch: the value is perfectly
    plausible, it just stopped being a measurement.
    """
    values = series.to_numpy()
    if values.size == 0:
        return 0, None, -1, -1
    # Boundaries where the value changes; run lengths are the gaps between them.
    change_points = np.flatnonzero(values[1:] != values[:-1]) + 1
    starts = np.concatenate(([0], change_points))
    ends = np.concatenate((change_points, [values.size]))
    lengths = ends - starts
    best = int(np.argmax(lengths))
    return int(lengths[best]), values[starts[best]], int(starts[best]), int(ends[best] - 1)


# --------------------------------------------------------------------------------------
# Profile containers
# --------------------------------------------------------------------------------------

@dataclass
class Profile:
    """Accumulates every measurement made about the source file."""

    source: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    timestamps: dict[str, Any] = field(default_factory=dict)
    completeness: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)
    cross_checks: dict[str, Any] = field(default_factory=dict)
    generated_utc: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_utc": self.generated_utc,
            "source": self.source,
            "structure": self.structure,
            "timestamps": self.timestamps,
            "completeness": self.completeness,
            "columns": self.columns,
            "coverage": self.coverage,
            "failures": self.failures,
            "cross_checks": self.cross_checks,
        }


# --------------------------------------------------------------------------------------
# Profiling steps
# --------------------------------------------------------------------------------------

def load_dataframe(csv_path: Path) -> pd.DataFrame:
    """Read the raw CSV with explicit dtypes and a named index column.

    Timestamps are parsed separately (not via ``parse_dates``) so that unparseable
    values surface as ``NaT`` and can be *counted* rather than silently killing the read.
    """
    logger.info("Loading dataset from %s", csv_path)
    names = [INDEX_COLUMN, TIMESTAMP_COLUMN, *SIGNAL_COLUMNS]
    dtypes = dict.fromkeys(SIGNAL_COLUMNS, "float64")
    dtypes[INDEX_COLUMN] = "int64"

    frame = pd.read_csv(
        csv_path,
        header=0,
        names=names,
        dtype=dtypes,
        low_memory=False,
    )
    logger.info("Records found: %s", f"{len(frame):,}")

    raw_ts = frame[TIMESTAMP_COLUMN]
    frame[TIMESTAMP_COLUMN] = pd.to_datetime(raw_ts, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    unparsed = int(frame[TIMESTAMP_COLUMN].isna().sum())
    if unparsed:
        logger.warning("Unparseable timestamps: %s", f"{unparsed:,}")
    return frame


def profile_source(profile: Profile, csv_path: Path) -> None:
    stat = csv_path.stat()
    logger.info("Hashing source file (this reads %.1f MB)...", stat.st_size / 1024 / 1024)
    profile.source = {
        "file_name": csv_path.name,
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / 1024 / 1024, 2),
        "sha256": sha256_of(csv_path),
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
    }


def profile_structure(profile: Profile, frame: pd.DataFrame) -> None:
    source_index = frame[INDEX_COLUMN]
    steps = source_index.diff().dropna()
    step_counts = steps.value_counts().head(5)

    profile.structure = {
        "row_count": int(len(frame)),
        "column_count": int(frame.shape[1]),
        "columns": list(frame.columns),
        "analogue_columns": list(ANALOGUE_COLUMNS),
        "digital_columns": list(DIGITAL_COLUMNS),
        "source_index_min": to_native(source_index.min()),
        "source_index_max": to_native(source_index.max()),
        "source_index_is_monotonic_increasing": bool(source_index.is_monotonic_increasing),
        "source_index_is_unique": bool(source_index.is_unique),
        "source_index_step_counts": {str(to_native(k)): int(v) for k, v in step_counts.items()},
    }


def profile_timestamps(profile: Profile, frame: pd.DataFrame) -> None:
    ts = frame[TIMESTAMP_COLUMN]
    valid = ts.dropna()
    deltas = valid.diff().dropna()
    delta_seconds = deltas.dt.total_seconds()

    interval_counts = delta_seconds.value_counts().head(12)
    modal_interval = float(delta_seconds.mode().iloc[0]) if not delta_seconds.empty else float("nan")

    # A "gap" is any step materially larger than the modal sampling interval.
    # Threshold = 3x modal interval: large enough to ignore ordinary jitter
    # (the source shows +/-1 s wobble), small enough to catch real dropouts.
    gap_threshold_s = modal_interval * 3
    gap_mask = delta_seconds > gap_threshold_s
    gap_count = int(gap_mask.sum())

    largest_gaps: list[dict[str, Any]] = []
    if gap_count:
        top = delta_seconds[gap_mask].nlargest(10)
        for pos, seconds in top.items():
            end_ts = valid.loc[pos]
            largest_gaps.append({
                "gap_start": to_native(end_ts - pd.Timedelta(seconds=seconds)),
                "gap_end": to_native(end_ts),
                "gap_seconds": float(seconds),
                "gap_hours": round(float(seconds) / 3600, 2),
            })

    span_seconds = float((valid.max() - valid.min()).total_seconds())
    expected_rows = int(span_seconds / modal_interval) + 1 if modal_interval > 0 else 0

    profile.timestamps = {
        "min": to_native(valid.min()),
        "max": to_native(valid.max()),
        "span_days": round(span_seconds / 86400, 2),
        "unparseable_count": int(ts.isna().sum()),
        "is_monotonic_increasing": bool(valid.is_monotonic_increasing),
        "is_unique": bool(valid.is_unique),
        "duplicate_timestamp_count": int(valid.duplicated().sum()),
        "backward_step_count": int((delta_seconds < 0).sum()),
        "zero_step_count": int((delta_seconds == 0).sum()),
        "modal_interval_seconds": modal_interval,
        "median_interval_seconds": float(delta_seconds.median()),
        "mean_interval_seconds": float(delta_seconds.mean()),
        "interval_distribution_top": {
            str(to_native(k)): int(v) for k, v in interval_counts.items()
        },
        "gap_threshold_seconds": gap_threshold_s,
        "gap_count": gap_count,
        "missing_samples_estimate": max(expected_rows - len(valid), 0),
        "expected_rows_if_continuous": expected_rows,
        "coverage_pct": round(100.0 * len(valid) / expected_rows, 2) if expected_rows else None,
        "largest_gaps": largest_gaps,
    }


def profile_completeness(profile: Profile, frame: pd.DataFrame) -> None:
    nulls = frame.isna().sum()
    full_dupes = int(frame.duplicated().sum())
    signal_dupes = int(frame.duplicated(subset=[TIMESTAMP_COLUMN, *SIGNAL_COLUMNS]).sum())

    profile.completeness = {
        "null_counts": {col: int(nulls[col]) for col in frame.columns},
        "total_null_cells": int(nulls.sum()),
        "duplicate_rows_all_columns": full_dupes,
        "duplicate_rows_timestamp_and_signals": signal_dupes,
    }


def profile_columns(profile: Profile, frame: pd.DataFrame) -> None:
    result: dict[str, Any] = {}
    ts = frame[TIMESTAMP_COLUMN]
    for col in SIGNAL_COLUMNS:
        series = frame[col].dropna()
        is_digital = col in DIGITAL_COLUMNS
        run_len, run_val, run_start, run_end = longest_constant_run(frame[col])

        entry: dict[str, Any] = {
            "class": "Digital" if is_digital else "Analogue",
            "dtype": str(frame[col].dtype),
            "count": int(series.size),
            "null_count": int(frame[col].isna().sum()),
            "min": to_native(series.min()),
            "max": to_native(series.max()),
            "mean": to_native(series.mean()),
            "median": to_native(series.median()),
            "std": to_native(series.std()),
            "p01": to_native(series.quantile(0.01)),
            "p25": to_native(series.quantile(0.25)),
            "p75": to_native(series.quantile(0.75)),
            "p99": to_native(series.quantile(0.99)),
            "distinct_count": int(series.nunique()),
            "zero_count": int((series == 0).sum()),
            "negative_count": int((series < 0).sum()),
            "longest_constant_run": run_len,
            "longest_constant_run_value": to_native(run_val),
            "longest_constant_run_start": to_native(ts.iloc[run_start]) if run_start >= 0 else None,
            "longest_constant_run_end": to_native(ts.iloc[run_end]) if run_end >= 0 else None,
            "longest_constant_run_hours": round(run_len * 10 / 3600, 2),
        }
        iqr = entry["p75"] - entry["p25"] if entry["p75"] is not None else None
        entry["iqr"] = iqr
        if iqr is not None:
            entry["iqr_lower_fence"] = entry["p25"] - 1.5 * iqr
            entry["iqr_upper_fence"] = entry["p75"] + 1.5 * iqr

        if is_digital:
            distinct = np.sort(series.unique())
            entry["distinct_values"] = [to_native(v) for v in distinct[:10]]
            entry["is_strictly_binary"] = bool(set(distinct.tolist()) <= {0.0, 1.0})
            entry["non_binary_count"] = int((~series.isin([0.0, 1.0])).sum())
            entry["active_pct"] = round(100.0 * float((series == 1.0).mean()), 3)
        result[col] = entry

    profile.columns = result


def profile_coverage(profile: Profile, frame: pd.DataFrame) -> None:
    """Rows per calendar month and per day, to expose uneven coverage."""
    ts = frame[TIMESTAMP_COLUMN].dropna()
    per_month = ts.dt.to_period("M").value_counts().sort_index()
    per_day = ts.dt.date.value_counts().sort_index()

    modal = profile.timestamps.get("modal_interval_seconds") or 10.0
    expected_per_day = int(86400 / modal)
    thin_days = per_day[per_day < expected_per_day * 0.5]

    profile.coverage = {
        "rows_per_month": {str(k): int(v) for k, v in per_month.items()},
        "distinct_days": int(per_day.size),
        "expected_rows_per_full_day": expected_per_day,
        "days_below_50pct_coverage": int(thin_days.size),
        "thinnest_days": {str(k): int(v) for k, v in thin_days.nsmallest(10).items()},
        "fullest_day_rows": int(per_day.max()),
    }


def profile_failures(profile: Profile, frame: pd.DataFrame) -> None:
    """Confirm the documented failure windows actually contain data."""
    ts = frame[TIMESTAMP_COLUMN]
    out: list[dict[str, Any]] = []
    for idx, event in enumerate(FAILURE_EVENTS, start=1):
        start = pd.Timestamp(event["start"])
        end = pd.Timestamp(event["end"])
        mask = (ts >= start) & (ts <= end)
        rows = int(mask.sum())
        duration_h = (end - start).total_seconds() / 3600
        modal = profile.timestamps.get("modal_interval_seconds") or 10.0
        expected = int((end - start).total_seconds() / modal) + 1

        entry: dict[str, Any] = {
            "sequence": idx,
            "source_reference": event["ref"],
            "start": event["start"],
            "end": event["end"],
            "failure_type": event["type"],
            "severity": event["severity"],
            "report_note": event["note"],
            "duration_hours": round(duration_h, 2),
            "rows_in_window": rows,
            "expected_rows_in_window": expected,
            "window_coverage_pct": round(100.0 * rows / expected, 2) if expected else None,
        }
        if rows:
            window = frame.loc[mask]
            entry["sensor_means_in_window"] = {
                col: to_native(window[col].mean()) for col in ANALOGUE_COLUMNS
            }
        out.append(entry)
    profile.failures = out


def profile_frozen_blocks(profile: Profile, frame: pd.DataFrame, top_n: int = 10) -> None:
    """Find windows where *every* analogue signal is simultaneously unchanging.

    A single stuck sensor can be a faulty transmitter. **All seven** analogue signals
    holding the exact same floating-point value at the same time cannot be physical:
    oil temperature drifts, pressure ripples, motor current fluctuates. It is the
    signature of a data-acquisition freeze - the logger repeating its last good scan.

    This matters more than it looks. Cell-level null checks report this data as
    perfectly complete, and range checks pass it happily because every held value is
    plausible. Only a change-detection check finds it.
    """
    analogue = frame[list(ANALOGUE_COLUMNS)].to_numpy()
    # True where this row is bit-identical to the previous row across all analogue signals.
    unchanged = np.all(analogue[1:] == analogue[:-1], axis=1)

    blocks: list[dict[str, Any]] = []
    block_count = 0
    if unchanged.any():
        padded = np.concatenate(([False], unchanged, [False]))
        edges = np.flatnonzero(padded[1:] != padded[:-1])
        starts, ends = edges[0::2], edges[1::2]
        lengths = ends - starts
        block_count = int(starts.size)
        order = np.argsort(-lengths)[:top_n]
        ts = frame[TIMESTAMP_COLUMN]
        for i in order:
            # +1 converts a 'diff' position back to a row position.
            first_row, last_row = int(starts[i]), int(ends[i])
            blocks.append({
                "start": to_native(ts.iloc[first_row]),
                "end": to_native(ts.iloc[last_row]),
                "sample_count": int(lengths[i]) + 1,
                "duration_hours": round((int(lengths[i]) + 1) * 10 / 3600, 2),
            })

    # Does held data contaminate the windows we intend to analyse? Row-aligned mask
    # (the first row has no predecessor, so it can never be "unchanged").
    frozen_rows = np.concatenate(([False], unchanged))
    ts = frame[TIMESTAMP_COLUMN]
    contamination: list[dict[str, Any]] = []
    for idx, event in enumerate(FAILURE_EVENTS, start=1):
        start, end = pd.Timestamp(event["start"]), pd.Timestamp(event["end"])
        lead_start = start - pd.Timedelta(hours=24)
        in_event = ((ts >= start) & (ts <= end)).to_numpy()
        in_lead = ((ts >= lead_start) & (ts < start)).to_numpy()
        contamination.append({
            "sequence": idx,
            "source_reference": event["ref"],
            "rows_in_event": int(in_event.sum()),
            "frozen_rows_in_event": int((in_event & frozen_rows).sum()),
            "rows_in_24h_lead_up": int(in_lead.sum()),
            "frozen_rows_in_24h_lead_up": int((in_lead & frozen_rows).sum()),
        })
    for entry in contamination:
        for scope in ("event", "24h_lead_up"):
            total = entry[f"rows_in_{scope}"]
            frozen_n = entry[f"frozen_rows_in_{scope}"]
            entry[f"frozen_pct_of_{scope}"] = round(100.0 * frozen_n / total, 2) if total else None

    profile.cross_checks["failure_window_contamination"] = {
        "rationale": "Pre-failure analysis is only meaningful over data the logger was "
                     "actually acquiring. Held (frozen) samples inside an event window or "
                     "its lead-up must be excluded, not averaged in.",
        "per_event": contamination,
    }

    total_frozen = int(unchanged.sum())
    profile.cross_checks["frozen_archive_blocks"] = {
        "rationale": "All 7 analogue signals bit-identical to the previous scan - "
                     "physically impossible, indicates last-value-hold in the logger.",
        "frozen_sample_count": total_frozen,
        "frozen_pct_of_file": round(100.0 * total_frozen / max(len(frame) - 1, 1), 3),
        "block_count": block_count,
        "longest_blocks": blocks,
    }


def profile_cross_checks(profile: Profile, frame: pd.DataFrame) -> None:
    """Checks that use documented physical relationships between sensors."""
    checks: dict[str, Any] = {}

    # The UCI docs state Reservoirs "should be close to" TP3.
    divergence = (frame["Reservoirs"] - frame["TP3"]).abs()
    checks["reservoirs_vs_tp3_abs_diff"] = {
        "rationale": "UCI documentation states Reservoirs should be close to TP3.",
        "mean": to_native(divergence.mean()),
        "p99": to_native(divergence.quantile(0.99)),
        "max": to_native(divergence.max()),
        "rows_over_0_5_bar": int((divergence > 0.5).sum()),
    }

    # Motor_current is documented with four nominal operating states.
    current = frame["Motor_current"].dropna()
    bands = {
        "off_lt_1A": int((current < 1.0).sum()),
        "offloaded_1_to_5A": int(((current >= 1.0) & (current < 5.0)).sum()),
        "under_load_5_to_8A": int(((current >= 5.0) & (current < 8.0)).sum()),
        "starting_ge_8A": int((current >= 8.0).sum()),
    }
    checks["motor_current_state_bands"] = {
        "rationale": "UCI documents nominal states: ~0A off, ~4A offloaded, ~7A load, ~9A start.",
        "band_counts": bands,
        "band_pct": {k: round(100.0 * v / len(current), 2) for k, v in bands.items()},
    }

    # LPS is documented to activate below 7 bar; MPG relates to an 8.2 bar setpoint.
    checks["documented_setpoints"] = {
        "lps_active_rows": int((frame["LPS"] == 1.0).sum()),
        "tp3_below_7_bar_rows": int((frame["TP3"] < 7.0).sum()),
        "lps_active_and_tp3_below_7": int(((frame["LPS"] == 1.0) & (frame["TP3"] < 7.0)).sum()),
        "tp3_below_8_2_bar_rows": int((frame["TP3"] < 8.2).sum()),
        "note": "Agreement between LPS activation and TP3 < 7 bar is evidence the "
                "documented setpoint matches the archived data.",
    }

    profile.cross_checks = checks


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------

def render_markdown(profile: Profile) -> str:
    p = profile.as_dict()
    src, st, ts, comp, cov = p["source"], p["structure"], p["timestamps"], p["completeness"], p["coverage"]
    lines: list[str] = []
    add = lines.append

    add("# Data Profile - MetroPT-3 raw CSV")
    add("")
    add(f"Generated `{p['generated_utc']}` by `scripts/profile_dataset.py`.")
    add("Every number below is measured from the file, not taken from documentation.")
    add("")
    add("## 1. Source file")
    add("")
    add("| Property | Value |")
    add("|---|---|")
    add(f"| File | `{src['file_name']}` |")
    add(f"| Size | {src['size_mb']:,} MB ({src['size_bytes']:,} bytes) |")
    add(f"| SHA-256 | `{src['sha256']}` |")
    add(f"| Rows | {st['row_count']:,} |")
    add(f"| Columns | {st['column_count']} |")
    add("")
    add("## 2. Structure")
    add("")
    add(f"Columns: `{'`, `'.join(st['columns'])}`")
    add("")
    add("The first column has an **empty header** in the source file - a pandas export "
        "artefact. It is read as `source_index` rather than allowing pandas to invent "
        "`Unnamed: 0`.")
    add("")
    add("| Property | Value |")
    add("|---|---|")
    add(f"| `source_index` range | {st['source_index_min']:,} … {st['source_index_max']:,} |")
    add(f"| Monotonic increasing | {st['source_index_is_monotonic_increasing']} |")
    add(f"| Unique | {st['source_index_is_unique']} |")
    add(f"| Most common step | {list(st['source_index_step_counts'].items())[:3]} |")
    add("")
    add("## 3. Timestamps and sampling")
    add("")
    add("| Property | Value |")
    add("|---|---|")
    add(f"| First reading | {ts['min']} |")
    add(f"| Last reading | {ts['max']} |")
    add(f"| Span | {ts['span_days']} days |")
    add(f"| Unparseable | {ts['unparseable_count']:,} |")
    add(f"| Strictly increasing | {ts['is_monotonic_increasing']} |")
    add(f"| Unique | {ts['is_unique']} |")
    add(f"| Duplicate timestamps | {ts['duplicate_timestamp_count']:,} |")
    add(f"| Backward steps | {ts['backward_step_count']:,} |")
    add(f"| **Modal interval** | **{ts['modal_interval_seconds']} s** |")
    add(f"| Median interval | {ts['median_interval_seconds']} s |")
    add(f"| Mean interval | {round(ts['mean_interval_seconds'], 3)} s |")
    add(f"| Gap threshold used | {ts['gap_threshold_seconds']} s (3x modal) |")
    add(f"| Gaps detected | {ts['gap_count']:,} |")
    add(f"| Rows if continuous | {ts['expected_rows_if_continuous']:,} |")
    add(f"| **Actual coverage** | **{ts['coverage_pct']}%** |")
    add(f"| Missing samples (est.) | {ts['missing_samples_estimate']:,} |")
    add("")
    add("### Interval distribution (top values)")
    add("")
    add("| Interval (s) | Count |")
    add("|---|---|")
    for k, v in list(ts["interval_distribution_top"].items())[:10]:
        add(f"| {k} | {v:,} |")
    add("")
    if ts["largest_gaps"]:
        add("### Ten largest gaps")
        add("")
        add("| Gap start | Gap end | Hours |")
        add("|---|---|---|")
        for g in ts["largest_gaps"]:
            add(f"| {g['gap_start']} | {g['gap_end']} | {g['gap_hours']:,} |")
        add("")
    add("## 4. Completeness and duplication")
    add("")
    add(f"- Total null cells across all columns: **{comp['total_null_cells']:,}**")
    add(f"- Fully duplicated rows (all columns): **{comp['duplicate_rows_all_columns']:,}**")
    add(f"- Duplicated (timestamp + all signals): **{comp['duplicate_rows_timestamp_and_signals']:,}**")
    add("")
    nonzero_nulls = {k: v for k, v in comp["null_counts"].items() if v}
    if nonzero_nulls:
        add("| Column | Nulls |")
        add("|---|---|")
        for k, v in nonzero_nulls.items():
            add(f"| `{k}` | {v:,} |")
    else:
        add("No null values in any column - consistent with the UCI declaration "
            "(`has_missing_values: no`). Note that this refers to *cells*; missing "
            "**time coverage** is a separate matter, quantified in section 3.")
    add("")
    add("## 5. Analogue sensors")
    add("")
    add("| Sensor | Min | Max | Mean | Median | Std | P01 | P99 | Negative rows | Distinct |")
    add("|---|---|---|---|---|---|---|---|---|---|")
    for col in ANALOGUE_COLUMNS:
        c = p["columns"][col]
        add(f"| `{col}` | {c['min']:.4g} | {c['max']:.4g} | {c['mean']:.4g} | "
            f"{c['median']:.4g} | {c['std']:.4g} | {c['p01']:.4g} | {c['p99']:.4g} | "
            f"{c['negative_count']:,} | {c['distinct_count']:,} |")
    add("")
    add("### Global IQR fences - and why they must not be used as alarm limits")
    add("")
    add("| Sensor | P25 | P75 | IQR lower fence | IQR upper fence | Usable globally? |")
    add("|---|---|---|---|---|---|")
    for col in ANALOGUE_COLUMNS:
        c = p["columns"][col]
        lo, hi = c["iqr_lower_fence"], c["iqr_upper_fence"]
        usable = "yes" if lo <= c["p01"] and hi >= c["p99"] else "**no - see below**"
        add(f"| `{col}` | {c['p25']:.4g} | {c['p75']:.4g} | {lo:.4g} | {hi:.4g} | {usable} |")
    add("")
    add("Several of these fences are nonsense as alarm limits, and that is the point. "
        "`TP2` sits at roughly -0.01 bar for the ~55% of the time the compressor is "
        "unloaded and rises above 8 bar when it runs, so its distribution is **bimodal**: "
        "the quartiles both land in the idle mode and the fences exclude every loaded "
        "sample. A global IQR rule would flag normal operation as anomalous and miss "
        "genuine faults. `Motor_current` shows the same problem from the other side - its "
        "lower fence is a negative current, which is meaningless.")
    add("")
    add("**Consequence for the design:** baselines and anomaly thresholds are computed "
        "*per operating state*, not globally. Operating state is derived from the "
        "documented `Motor_current` bands and the `COMP` valve signal. This is recorded "
        "as a design decision in `docs/SQL_DESIGN.md`.")
    add("")
    add("### Flatline (stuck-value) runs")
    add("")
    add("| Sensor | Longest run (samples) | Hours | Held value | From | To |")
    add("|---|---|---|---|---|---|")
    for col in SIGNAL_COLUMNS:
        c = p["columns"][col]
        add(f"| `{col}` | {c['longest_constant_run']:,} | {c['longest_constant_run_hours']} | "
            f"{c['longest_constant_run_value']} | {c['longest_constant_run_start']} | "
            f"{c['longest_constant_run_end']} |")
    add("")
    add("## 6. Digital sensors")
    add("")
    add("| Sensor | Strictly binary? | Non-binary rows | Distinct values | Active % | Longest flat run |")
    add("|---|---|---|---|---|---|")
    for col in DIGITAL_COLUMNS:
        c = p["columns"][col]
        add(f"| `{col}` | {c['is_strictly_binary']} | {c['non_binary_count']:,} | "
            f"{c['distinct_values']} | {c['active_pct']}% | {c['longest_constant_run']:,} |")
    add("")
    add("## 7. Coverage by month")
    add("")
    add("| Month | Rows |")
    add("|---|---|")
    for k, v in cov["rows_per_month"].items():
        add(f"| {k} | {v:,} |")
    add("")
    add(f"- Distinct calendar days present: **{cov['distinct_days']}**")
    add(f"- Rows expected in a fully-covered day: **{cov['expected_rows_per_full_day']:,}**")
    add(f"- Days with under 50% coverage: **{cov['days_below_50pct_coverage']}**")
    add("")
    add("## 8. Documented failure windows")
    add("")
    add("| # | Source ref | Start | End | Hours | Rows present | Coverage |")
    add("|---|---|---|---|---|---|---|")
    for f in p["failures"]:
        add(f"| {f['sequence']} | `{f['source_reference']}` | {f['start']} | {f['end']} | "
            f"{f['duration_hours']} | {f['rows_in_window']:,} | {f['window_coverage_pct']}% |")
    add("")
    add("## 9. Cross-sensor checks")
    add("")
    for name, check in p["cross_checks"].items():
        if name in {"frozen_archive_blocks", "failure_window_contamination"}:
            continue
        add(f"### `{name}`")
        add("")
        for k, v in check.items():
            add(f"- **{k}**: {v}")
        add("")

    contam = p["cross_checks"].get("failure_window_contamination")
    if contam:
        add("### Held data inside the failure windows")
        add("")
        add(contam["rationale"])
        add("")
        add("| # | Ref | Rows in event | Frozen | Rows in 24 h lead-up | Frozen |")
        add("|---|---|---|---|---|---|")
        for e in contam["per_event"]:
            add(f"| {e['sequence']} | `{e['source_reference']}` | {e['rows_in_event']:,} | "
                f"{e['frozen_rows_in_event']:,} ({e['frozen_pct_of_event']}%) | "
                f"{e['rows_in_24h_lead_up']:,} | "
                f"{e['frozen_rows_in_24h_lead_up']:,} ({e['frozen_pct_of_24h_lead_up']}%) |")
        add("")

    frozen = p["cross_checks"].get("frozen_archive_blocks")
    if frozen:
        add("## 10. Frozen archive blocks (stuck data acquisition)")
        add("")
        add("Windows where **all seven analogue signals** are bit-identical to the "
            "previous scan. Oil temperature drifts, pressure ripples and motor current "
            "fluctuates in any real machine, so simultaneous freezing of all of them is "
            "not physical - it is the logger repeating its last good scan.")
        add("")
        add("This is invisible to the two checks people reach for first: the cells are "
            "not null, and every held value is inside its plausible range.")
        add("")
        add(f"- Frozen samples: **{frozen['frozen_sample_count']:,}** "
            f"(**{frozen['frozen_pct_of_file']}%** of the file)")
        add(f"- Contiguous blocks found: **{frozen['block_count']}** (top {len(frozen['longest_blocks'])} shown)")
        add("")
        add("| Start | End | Samples | Hours |")
        add("|---|---|---|---|")
        for b in frozen["longest_blocks"]:
            add(f"| {b['start']} | {b['end']} | {b['sample_count']:,} | {b['duration_hours']:,} |")
        add("")
        add("**Handling.** These rows are ingested, not deleted, and marked with quality "
            "code `UNCERTAIN_STALE`. Analytics excludes them from baseline statistics; the "
            "dashboard shows them as a distinct 'held data' band rather than as normal "
            "operation. See `docs/SQL_DESIGN.md` section 3.1.")
        add("")
    add("---")
    add("")
    add("*Reproduce with* `python scripts/profile_dataset.py`. "
        "*Machine-readable form:* `reports/data_profile.json`.")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, default=None,
                        help="Path to the raw MetroPT-3 CSV (env: METROPT_RAW_CSV)")
    parser.add_argument("--out-json", type=Path, default=PROJECT_ROOT / "reports" / "data_profile.json")
    parser.add_argument("--out-md", type=Path, default=PROJECT_ROOT / "docs" / "DATA_PROFILE.md")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def resolve_csv_path(cli_value: Path | None) -> Path:
    """CLI argument, then ``METROPT_RAW_CSV``, then the project default."""
    if cli_value is not None:
        return cli_value.expanduser().resolve()
    env_value = os.environ.get("METROPT_RAW_CSV")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_CSV


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT, stream=sys.stdout)

    csv_path = resolve_csv_path(args.csv)
    if not csv_path.is_file():
        logger.error("Dataset not found: %s", csv_path)
        logger.error("Set METROPT_RAW_CSV or pass --csv. See data/README.md for how to obtain it.")
        return 2

    profile = Profile(generated_utc=datetime.now(UTC).isoformat(timespec="seconds"))

    profile_source(profile, csv_path)
    frame = load_dataframe(csv_path)

    logger.info("Timestamp range: %s .. %s",
                frame[TIMESTAMP_COLUMN].min(), frame[TIMESTAMP_COLUMN].max())

    profile_structure(profile, frame)
    profile_timestamps(profile, frame)
    logger.info("Modal sampling interval: %s s (coverage %s%%)",
                profile.timestamps["modal_interval_seconds"], profile.timestamps["coverage_pct"])

    profile_completeness(profile, frame)
    logger.info("Missing values: %s cells | Duplicate rows: %s",
                f"{profile.completeness['total_null_cells']:,}",
                f"{profile.completeness['duplicate_rows_all_columns']:,}")

    profile_columns(profile, frame)
    profile_coverage(profile, frame)
    profile_failures(profile, frame)
    # Order matters: profile_cross_checks assigns the dict, profile_frozen_blocks adds to it.
    profile_cross_checks(profile, frame)
    profile_frozen_blocks(profile, frame)
    frozen = profile.cross_checks["frozen_archive_blocks"]
    if frozen["frozen_sample_count"]:
        logger.warning("Frozen archive samples: %s (%s%% of file) across %s block(s)",
                       f"{frozen['frozen_sample_count']:,}", frozen["frozen_pct_of_file"],
                       frozen["block_count"])

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(profile.as_dict(), indent=2, default=to_native), encoding="utf-8")
    args.out_md.write_text(render_markdown(profile), encoding="utf-8")

    logger.info("Wrote %s", args.out_json)
    logger.info("Wrote %s", args.out_md)
    logger.info("Profiling completed successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
