"""Trend statistics: rolling mean, rolling spread, and rate of change.

Everything here reads the aggregate archive, never the 22.7-million-row fact table.
A rolling window over raw readings is correct and unusable; over hourly buckets it is
instant and at the resolution anyone actually looks at a trend.

All windows are state-aware for the same reason baselines are (see
``docs/ANALYTICS_FINDINGS.md`` §1): a rolling mean that blends OFF and LOADED behaviour
describes neither.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pyodbc

from ingestion.db import fetch_all

logger = logging.getLogger(__name__)

#: Rolling window in hourly buckets. 24 = one day of the same operating state.
DEFAULT_WINDOW_HOURS = 24


def sensor_trend(
    conn: pyodbc.Connection,
    sensor_code: str,
    *,
    operating_state: str | None = None,
    start: str | None = None,
    end: str | None = None,
    window: int = DEFAULT_WINDOW_HOURS,
) -> pd.DataFrame:
    """Return a trend frame for one sensor: value, rolling stats and rate of change.

    ``operating_state=None`` uses the state-blind hourly aggregate, which is the right
    choice for a chart an operator reads as "what did this tag do", and the wrong choice
    for anything statistical.
    """
    if operating_state:
        sql = """
            SELECT a.BucketStart, a.AvgValue, a.MinValue, a.MaxValue, a.SampleCount
            FROM analytics.SensorHourlyStateAgg AS a
            INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
            WHERE s.SensorCode = ? AND a.OperatingState = ?
              AND (? IS NULL OR a.BucketStart >= ?) AND (? IS NULL OR a.BucketStart < ?)
            ORDER BY a.BucketStart
        """
        params: tuple = (sensor_code, operating_state, start, start, end, end)
    else:
        sql = """
            SELECT a.BucketStart, a.AvgValue, a.MinValue, a.MaxValue, a.SampleCount
            FROM analytics.SensorHourlyAgg AS a
            INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
            WHERE s.SensorCode = ?
              AND (? IS NULL OR a.BucketStart >= ?) AND (? IS NULL OR a.BucketStart < ?)
            ORDER BY a.BucketStart
        """
        params = (sensor_code, start, start, end, end)

    rows = fetch_all(conn, sql, params)
    frame = pd.DataFrame.from_records(
        rows, columns=["bucket_start", "avg_value", "min_value", "max_value", "sample_count"]
    )
    if frame.empty:
        return frame

    values = frame["avg_value"].astype(float)
    frame["rolling_mean"] = values.rolling(window, min_periods=max(window // 4, 2)).mean()
    frame["rolling_std"] = values.rolling(window, min_periods=max(window // 4, 2)).std()
    frame["rolling_min"] = values.rolling(window, min_periods=max(window // 4, 2)).min()
    frame["rolling_max"] = values.rolling(window, min_periods=max(window // 4, 2)).max()

    # Rate of change per hour. Buckets can be missing (17.6% of the archive is absent),
    # so divide by the real elapsed time rather than assuming one hour per row -
    # otherwise a 48-hour gap reads as a violent one-hour swing.
    elapsed = frame["bucket_start"].diff().dt.total_seconds() / 3600.0
    frame["rate_of_change_per_hour"] = values.diff() / elapsed.replace(0, np.nan)

    # Slope of the rolling mean over the window: the direction of travel, which is what
    # "is this drifting?" actually asks.
    frame["trend_slope_per_hour"] = (
        frame["rolling_mean"].diff() / elapsed.replace(0, np.nan)
    )
    return frame


def describe_trend(frame: pd.DataFrame) -> dict[str, float | None]:
    """Summarise a trend frame into the numbers a status panel would show."""
    if frame.empty:
        return {}
    values = frame["avg_value"].astype(float)
    slope = frame["trend_slope_per_hour"].dropna()
    return {
        "buckets": int(len(frame)),
        "min": float(values.min()),
        "max": float(values.max()),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "std": float(values.std()),
        "latest": float(values.iloc[-1]),
        "latest_rolling_mean": (float(frame["rolling_mean"].iloc[-1])
                                if pd.notna(frame["rolling_mean"].iloc[-1]) else None),
        "mean_slope_per_hour": float(slope.mean()) if not slope.empty else None,
        "max_abs_rate_of_change": (
            float(frame["rate_of_change_per_hour"].abs().max())
            if frame["rate_of_change_per_hour"].notna().any() else None
        ),
    }


def daily_duty_cycle(conn: pyodbc.Connection) -> pd.DataFrame:
    """Running time per day, derived from operating state.

    The most operationally meaningful trend on a compressor: not what any single tag
    read, but how hard the machine worked. Held scans are excluded so a frozen logger
    cannot be mistaken for a quiet day.
    """
    rows = fetch_all(
        conn,
        """
        SELECT CAST(ReadingTs AS DATE) AS BucketDate,
               COUNT_BIG(*) AS Scans,
               SUM(CASE WHEN OperatingState <> 'OFF' THEN 1 ELSE 0 END) AS RunningScans,
               SUM(CASE WHEN OperatingState = 'LOADED' THEN 1 ELSE 0 END) AS LoadedScans
        FROM analytics.ScanState
        WHERE IsStale = 0
        GROUP BY CAST(ReadingTs AS DATE)
        ORDER BY BucketDate
        """,
    )
    frame = pd.DataFrame.from_records(
        rows, columns=["bucket_date", "scans", "running_scans", "loaded_scans"]
    )
    if frame.empty:
        return frame
    frame["running_pct"] = 100.0 * frame["running_scans"] / frame["scans"]
    frame["loaded_pct"] = 100.0 * frame["loaded_scans"] / frame["scans"]
    # At the measured 10 s sampling interval.
    frame["running_hours"] = frame["running_scans"] * 10.0 / 3600.0
    frame["running_pct_7d"] = frame["running_pct"].rolling(7, min_periods=3).mean()
    return frame
