"""Anomaly detection with the alarm discipline that makes it usable.

Naive per-state IQR fences flag 15.13% of readings, about 7,300 alarms a day against the
EEMUA 191 / ISA-18.2 ceiling of roughly 150. This module uses a trailing same-state
window (drift), median/MAD (robustness), hourly buckets, and an ISA-18.2 on-delay,
reaching 12.5 a day. The fixed-baseline detector is kept alongside for comparison.

Measurements and reasoning: docs/ANALYTICS_FINDINGS.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
import pyodbc

from analytics.statistics import REFERENCE_BASELINE
from ingestion.db import fetch_all
from ingestion.quality import QualityCode

logger = logging.getLogger(__name__)

# --- Method names, stored in analytics.Anomaly.Method ---------------------------------
METHOD_ADAPTIVE_MAD = "ADAPTIVE_MAD"
METHOD_BASELINE_IQR = "BASELINE_IQR"
METHOD_SETPOINT = "SETPOINT"
METHOD_ISOLATION_FOREST = "ISOLATION_FOREST"

SEVERITY_NORMAL = "NORMAL"
SEVERITY_WARNING = "WARNING"
SEVERITY_CRITICAL = "CRITICAL"

#: Trailing window for the adaptive detector, in hourly buckets of the same state.
#: 168 = one week. Long enough that a normal duty cycle and a weekday/weekend pattern are
#: inside the window; short enough to follow a seasonal drift instead of fighting it.
ADAPTIVE_WINDOW_BUCKETS = 168

#: Minimum history before the adaptive detector will say anything. Without this it
#: produces confident nonsense for the first few hours of the archive.
ADAPTIVE_MIN_HISTORY = 24

#: Robust z-score thresholds. 3.5 is the conventional cut-off for the modified z-score
#: (Iglewicz & Hoaglin); 5.0 is used here for CRITICAL as a deliberately wide margin.
ROBUST_Z_WARNING = 3.5
ROBUST_Z_CRITICAL = 5.0

#: 0.6745 is the 0.75 quantile of the standard normal. Scaling MAD by it makes the
#: modified z-score comparable to a conventional z-score for normally distributed data.
MAD_SCALE = 0.6745

#: ISA-18.2 / EEMUA 191 style on-delay. A condition must hold for this many consecutive
#: hourly buckets in the same state before it is annunciated. Removes chatter around a
#: limit without needing a separate deadband.
PERSISTENCE_BUCKETS = 3

#: EEMUA 191 / ISA-18.2 guidance for a single operator position. Used to judge whether
#: the configured detection is usable, not as a hard limit.
ALARM_RATE_TARGET_PER_DAY = 150
ALARM_RATE_MAX_PER_DAY = 300


@dataclass
class Anomaly:
    """One detection, before it is written to ``analytics.Anomaly``."""

    sensor_id: int
    sensor_code: str
    reading_ts: datetime
    value: float
    expected_low: float | None
    expected_high: float | None
    method: str
    score: float
    severity: str
    operating_state: str | None


def _load_state_hourly(conn: pyodbc.Connection) -> pd.DataFrame:
    """Read the state-aware hourly aggregate for analogue sensors."""
    rows = fetch_all(
        conn,
        """
        SELECT a.SensorId, s.SensorCode, a.OperatingState, a.BucketStart,
               a.AvgValue, a.MinValue, a.MaxValue, a.SampleCount
        FROM analytics.SensorHourlyStateAgg AS a
        INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
        ORDER BY a.SensorId, a.OperatingState, a.BucketStart
        """,
    )
    return pd.DataFrame.from_records(
        rows,
        columns=["sensor_id", "sensor_code", "operating_state", "bucket_start",
                 "avg_value", "min_value", "max_value", "sample_count"],
    )


def _apply_persistence(flags: np.ndarray, required: int) -> np.ndarray:
    """Keep only flags that are part of a run of at least *required* consecutive flags.

    The ISA-18.2 on-delay. A single bucket over a limit is a fluctuation; the same
    condition holding for three hours is something an operator should look at. This one
    rule is the difference between a readable alarm list and an alarm flood.
    """
    if required <= 1 or flags.size == 0:
        return flags

    kept = np.zeros_like(flags)
    run_start = None
    for index, flagged in enumerate(flags):
        if flagged and run_start is None:
            run_start = index
        elif not flagged and run_start is not None:
            if index - run_start >= required:
                kept[run_start:index] = True
            run_start = None
    if run_start is not None and flags.size - run_start >= required:
        kept[run_start:] = True
    return kept


def detect_adaptive_mad(
    conn: pyodbc.Connection,
    *,
    window: int = ADAPTIVE_WINDOW_BUCKETS,
    min_history: int = ADAPTIVE_MIN_HISTORY,
    persistence: int = PERSISTENCE_BUCKETS,
) -> list[Anomaly]:
    """Primary detector: robust z-score against a trailing same-state window.

    Median and MAD rather than mean and standard deviation, because a developing fault
    inflates the very statistics meant to detect it. The window excludes the current
    bucket, so a reading is never compared against a baseline it helped create.
    """
    frame = _load_state_hourly(conn)
    if frame.empty:
        logger.warning("No state-aware hourly aggregates; run aggregates.sql first.")
        return []

    anomalies: list[Anomaly] = []
    for (sensor_id, state), group in frame.groupby(["sensor_id", "operating_state"],
                                                   sort=False):
        group = group.sort_values("bucket_start")
        values = group["avg_value"].to_numpy(dtype=float)
        if values.size <= min_history:
            continue

        series = pd.Series(values)
        # shift(1) makes the window strictly historical.
        rolling = series.shift(1).rolling(window=window, min_periods=min_history)
        median = rolling.median().to_numpy()
        mad = rolling.apply(
            lambda w: np.median(np.abs(w - np.median(w))), raw=True
        ).to_numpy()

        with np.errstate(divide="ignore", invalid="ignore"):
            robust_z = MAD_SCALE * (values - median) / mad

        # A MAD of zero means the signal did not move at all across the window. That is
        # a quality question, not an anomaly, and dividing by it yields infinity.
        usable = np.isfinite(robust_z) & (mad > 0)
        flagged = usable & (np.abs(robust_z) >= ROBUST_Z_WARNING)
        flagged = _apply_persistence(flagged, persistence)

        codes = group["sensor_code"].to_numpy()
        stamps = group["bucket_start"].to_numpy()
        for index in np.flatnonzero(flagged):
            score = float(abs(robust_z[index]))
            severity = SEVERITY_CRITICAL if score >= ROBUST_Z_CRITICAL else SEVERITY_WARNING
            spread = mad[index] / MAD_SCALE
            anomalies.append(
                Anomaly(
                    sensor_id=int(sensor_id), sensor_code=str(codes[index]),
                    reading_ts=pd.Timestamp(stamps[index]).to_pydatetime(),
                    value=float(values[index]),
                    expected_low=float(median[index] - ROBUST_Z_WARNING * spread),
                    expected_high=float(median[index] + ROBUST_Z_WARNING * spread),
                    method=METHOD_ADAPTIVE_MAD, score=score, severity=severity,
                    operating_state=str(state),
                )
            )
    return anomalies


def detect_baseline_iqr(
    conn: pyodbc.Connection,
    *,
    baseline_name: str = REFERENCE_BASELINE,
    persistence: int = PERSISTENCE_BUCKETS,
) -> list[Anomaly]:
    """Fixed-baseline IQR against the February reference window.

    Kept for contrast: it asks how far a signal has drifted from where it started, which
    is a legitimate question but not an alarm. Degenerate baselines (zero IQR) are
    skipped, since their fences collapse onto a single value.
    """
    frame = _load_state_hourly(conn)
    if frame.empty:
        return []

    baselines = fetch_all(
        conn,
        """
        SELECT b.SensorId, b.OperatingState, b.P25, b.P75, b.IqrLowerFence, b.IqrUpperFence
        FROM analytics.SensorBaseline AS b
        WHERE b.BaselineName = ? AND (b.P75 - b.P25) > 0
        """,
        (baseline_name,),
    )
    fences = {
        (int(r[0]), r[1]): (float(r[4]), float(r[5]), float(r[3]) - float(r[2]))
        for r in baselines
    }
    if not fences:
        logger.warning("No usable baselines named %s.", baseline_name)
        return []

    anomalies: list[Anomaly] = []
    for (sensor_id, state), group in frame.groupby(["sensor_id", "operating_state"],
                                                   sort=False):
        fence = fences.get((int(sensor_id), state))
        if fence is None:
            continue
        low, high, iqr = fence

        group = group.sort_values("bucket_start")
        values = group["avg_value"].to_numpy(dtype=float)
        flagged = _apply_persistence((values < low) | (values > high), persistence)

        codes = group["sensor_code"].to_numpy()
        stamps = group["bucket_start"].to_numpy()
        for index in np.flatnonzero(flagged):
            value = float(values[index])
            exceedance = (low - value) if value < low else (value - high)
            # Score in IQR multiples beyond the fence, so 0 is at the fence and 1.5 is at
            # the conventional 'far out' 3x IQR boundary.
            score = exceedance / iqr if iqr else 0.0
            severity = SEVERITY_CRITICAL if score >= 1.5 else SEVERITY_WARNING
            anomalies.append(
                Anomaly(
                    sensor_id=int(sensor_id), sensor_code=str(codes[index]),
                    reading_ts=pd.Timestamp(stamps[index]).to_pydatetime(),
                    value=value, expected_low=low, expected_high=high,
                    method=METHOD_BASELINE_IQR, score=float(score), severity=severity,
                    operating_state=str(state),
                )
            )
    return anomalies


def detect_setpoint(conn: pyodbc.Connection) -> list[Anomaly]:
    """Documented manufacturer setpoints, the only non-statistical detector.

    LPS activates below 7 bar and MPG starts the compressor below 8.2 bar, both stated by
    the equipment owner rather than chosen here. Reported per contiguous episode, because
    240 identical alarms are not useful and one saying "below 7 bar for 40 minutes" is.
    """
    rows = fetch_all(
        conn,
        f"""
        WITH low AS (
            SELECT r.ReadingTs, r.Value,
                   DATEDIFF(minute, '2020-01-01', r.ReadingTs)
                     - ROW_NUMBER() OVER (ORDER BY r.ReadingTs) AS Grp
            FROM ts.SensorReading AS r
            INNER JOIN asset.Sensor    AS s ON s.SensorId      = r.SensorId
            INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
            WHERE s.SensorCode = 'TP3'
              AND r.Value < 7.0
              AND q.IsUsable = 1
              AND r.QualityCodeId <> {int(QualityCode.UNCERTAIN_STALE)}
        )
        SELECT MIN(ReadingTs) AS EpisodeStart, MAX(ReadingTs) AS EpisodeEnd,
               COUNT_BIG(*) AS Scans, MIN(Value) AS LowestValue
        FROM low
        GROUP BY Grp
        HAVING COUNT_BIG(*) >= 3
        ORDER BY EpisodeStart
        """,
    )

    sensor_id = fetch_all(conn, "SELECT SensorId FROM asset.Sensor WHERE SensorCode = 'TP3'")
    if not sensor_id:
        return []
    tp3_id = int(sensor_id[0][0])

    anomalies: list[Anomaly] = []
    for start, _end, _scans, lowest in rows:
        # Severity by depth below the documented setpoint: 1 bar under is a different
        # situation from 0.05 bar under.
        severity = SEVERITY_CRITICAL if float(lowest) < 6.0 else SEVERITY_WARNING
        anomalies.append(
            Anomaly(
                sensor_id=tp3_id, sensor_code="TP3", reading_ts=start,
                value=float(lowest), expected_low=7.0, expected_high=None,
                method=METHOD_SETPOINT, score=float(7.0 - float(lowest)),
                severity=severity, operating_state=None,
            )
        )
    logger.info("Setpoint detector: %d low-pressure episode(s) below the documented 7 bar",
                len(anomalies))
    return anomalies


def store(conn: pyodbc.Connection, anomalies: list[Anomaly], *, replace_methods: bool = True
          ) -> int:
    """Write detections, replacing any previous run of the same methods."""
    if not anomalies:
        logger.warning("No anomalies to store.")
        return 0

    cursor = conn.cursor()
    if replace_methods:
        for method in sorted({a.method for a in anomalies}):
            cursor.execute("DELETE FROM analytics.Anomaly WHERE Method = ?", method)
    cursor.fast_executemany = True
    cursor.executemany(
        """
        INSERT INTO analytics.Anomaly
            (SensorId, ReadingTs, Value, ExpectedLow, ExpectedHigh, Method, Score,
             Severity, OperatingState)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (a.sensor_id, a.reading_ts, a.value, a.expected_low, a.expected_high,
             a.method, a.score, a.severity, a.operating_state)
            for a in anomalies
        ],
    )
    conn.commit()
    cursor.close()
    logger.info("Stored %s anomaly row(s)", f"{len(anomalies):,}")
    return len(anomalies)


def alarm_rate(anomalies: list[Anomaly], archive_days: float) -> dict[str, Any]:
    """Judge the configuration against published alarm-management guidance.

    A detector that produces more alarms than a person can read has not detected
    anything - it has moved the problem. EEMUA 191 and ISA-18.2 put the manageable rate
    at roughly 150 per day per operator position, with about 300 as the ceiling.
    """
    per_day = len(anomalies) / archive_days if archive_days else 0.0
    if per_day <= ALARM_RATE_TARGET_PER_DAY:
        verdict = "within EEMUA 191 target"
    elif per_day <= ALARM_RATE_MAX_PER_DAY:
        verdict = "above target, below maximum manageable"
    else:
        verdict = "ALARM FLOOD - exceeds manageable rate"
    return {"total": len(anomalies), "per_day": per_day, "verdict": verdict}
