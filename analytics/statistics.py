"""Baseline statistics - the single source of every threshold in the system.

The rule this module exists to enforce: **no alert limit is ever written into code.**
A limit is a row in ``analytics.SensorBaseline``, computed from a named window, over a
named operating state, excluding held data, and recorded with the method that produced
it. Anyone can re-run it and get the same numbers, or challenge the window and see what
changes.

Three deliberate exclusions, each of which would otherwise poison the statistics:

1. **Held (frozen) scans.** 3.35% of the archive is a logger repeating its last reading.
   Those values are plausible and would pass every range check, but they are not
   measurements. Including them shrinks the apparent variance of whichever value happened
   to be frozen - making the control limits *tighter* and the machine look *more* stable
   than it is.
2. **Bad-quality readings.** Anything the validator marked unusable.
3. **The wrong operating state.** See ``operating_state.py`` for why this is not optional.

The reference window is February 2020: the first full month, before all four documented
failures, and free of any frozen block longer than two scans.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import pyodbc

from analytics.operating_state import (
    BASELINE_STATES,
    MIN_BASELINE_SAMPLES,
)
from ingestion.db import fetch_all
from ingestion.quality import QualityCode

logger = logging.getLogger(__name__)

#: Name and bounds of the reference window. Stored on every baseline row so a limit can
#: always be traced back to the data that produced it.
REFERENCE_BASELINE = "REFERENCE_FEB2020"
REFERENCE_START = "2020-02-01T00:00:00"
REFERENCE_END = "2020-03-01T00:00:00"

REFERENCE_RATIONALE = (
    "February 2020: the first full month of the archive. Chosen because it precedes all "
    "four documented air-leak events and contains no frozen block longer than two scans. "
    "Held scans, unusable readings and other operating states are excluded."
)

#: Tukey's constant. 1.5x IQR is the conventional 'outlier' fence and is used here for
#: WARNING; 3.0x is the conventional 'far out' fence and is used for CRITICAL.
IQR_WARNING_MULTIPLIER = 1.5
IQR_CRITICAL_MULTIPLIER = 3.0


@dataclass
class BaselineRow:
    """One computed baseline, before it is written."""

    sensor_id: int
    sensor_code: str
    operating_state: str
    sample_count: int
    mean: float
    stddev: float
    p01: float
    p25: float
    p50: float
    p75: float
    p99: float

    @property
    def iqr(self) -> float:
        return self.p75 - self.p25

    @property
    def lower_fence(self) -> float:
        return self.p25 - IQR_WARNING_MULTIPLIER * self.iqr

    @property
    def upper_fence(self) -> float:
        return self.p75 + IQR_WARNING_MULTIPLIER * self.iqr

    @property
    def is_degenerate(self) -> bool:
        """True when the signal never varies in this state.

        A digital tag that is always 1 while the compressor is off has zero IQR and zero
        standard deviation. Fences computed from it collapse onto a single value, so
        every reading that is not exactly that value becomes an 'outlier'. Such baselines
        are stored - they are true - but detectors must skip them rather than emit a
        million alerts.
        """
        return self.iqr == 0.0 or self.stddev == 0.0


def compute(
    conn: pyodbc.Connection,
    *,
    window_start: str = REFERENCE_START,
    window_end: str = REFERENCE_END,
    states: tuple[str, ...] = BASELINE_STATES,
) -> list[BaselineRow]:
    """Compute per-sensor, per-operating-state statistics over the reference window.

    Percentiles use ``APPROX_PERCENTILE_CONT`` (SQL Server 2022+). The exact
    ``PERCENTILE_CONT`` is a window function that sorts every row in each partition,
    which on Express spills to tempdb at this volume. The approximation's error bound is
    far below the precision of a 4-byte sensor value, and these numbers are control
    limits rather than published measurements - so exactness buys nothing here and costs
    a great deal.
    """
    state_list = ", ".join(f"'{state}'" for state in states)
    sql = f"""
        SELECT
            r.SensorId,
            s.SensorCode,
            st.OperatingState,
            COUNT_BIG(*)                                   AS SampleCount,
            AVG(CAST(r.Value AS FLOAT))                    AS MeanValue,
            STDEV(CAST(r.Value AS FLOAT))                  AS StdDevValue,
            APPROX_PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY CAST(r.Value AS FLOAT)) AS P01,
            APPROX_PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY CAST(r.Value AS FLOAT)) AS P25,
            APPROX_PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY CAST(r.Value AS FLOAT)) AS P50,
            APPROX_PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY CAST(r.Value AS FLOAT)) AS P75,
            APPROX_PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY CAST(r.Value AS FLOAT)) AS P99
        FROM ts.SensorReading  AS r
        INNER JOIN asset.Sensor      AS s  ON s.SensorId      = r.SensorId
        INNER JOIN ref.QualityCode   AS q  ON q.QualityCodeId = r.QualityCodeId
        INNER JOIN analytics.ScanState AS st ON st.ReadingTs  = r.ReadingTs
        WHERE r.ReadingTs >= ? AND r.ReadingTs < ?
          AND q.IsUsable = 1              -- exclude anything the validator distrusted
          AND r.QualityCodeId <> ?        -- exclude held (frozen) readings
          AND st.IsStale = 0              -- and scans whose state came from held data
          AND st.OperatingState IN ({state_list})
        GROUP BY r.SensorId, s.SensorCode, st.OperatingState
        ORDER BY r.SensorId, st.OperatingState
    """
    rows = fetch_all(conn, sql, (window_start, window_end, int(QualityCode.UNCERTAIN_STALE)))

    baselines: list[BaselineRow] = []
    for row in rows:
        sample_count = int(row[3])
        if sample_count < MIN_BASELINE_SAMPLES:
            logger.warning(
                "Skipping baseline for %s / %s: only %s samples (minimum %s). "
                "Statistics from this few readings are noise dressed as a threshold.",
                row[1], row[2], f"{sample_count:,}", MIN_BASELINE_SAMPLES,
            )
            continue
        stddev = float(row[5]) if row[5] is not None else 0.0
        baselines.append(
            BaselineRow(
                sensor_id=int(row[0]), sensor_code=row[1], operating_state=row[2],
                sample_count=sample_count, mean=float(row[4]), stddev=stddev,
                p01=float(row[6]), p25=float(row[7]), p50=float(row[8]),
                p75=float(row[9]), p99=float(row[10]),
            )
        )
    return baselines


def store(
    conn: pyodbc.Connection,
    baselines: list[BaselineRow],
    *,
    baseline_name: str = REFERENCE_BASELINE,
    window_start: str = REFERENCE_START,
    window_end: str = REFERENCE_END,
    rationale: str = REFERENCE_RATIONALE,
) -> int:
    """Write baselines, replacing any previous computation of the same name.

    ``Method`` carries the prose rationale into the database rather than leaving it in a
    docstring, so someone reading the limits in SSMS can see why the window was chosen
    without going to find the source.
    """
    if not baselines:
        logger.warning("No baselines to store.")
        return 0

    # fast_executemany binds by inferred parameter type rather than letting the driver
    # convert per row, so an ISO string bound to DATETIME2 fails with the unhelpful
    # "Invalid character value for cast specification". Convert once, explicitly.
    start_dt = datetime.fromisoformat(window_start)
    end_dt = datetime.fromisoformat(window_end)

    cursor = conn.cursor()
    cursor.execute("DELETE FROM analytics.SensorBaseline WHERE BaselineName = ?",
                   baseline_name)
    cursor.fast_executemany = True
    cursor.executemany(
        """
        INSERT INTO analytics.SensorBaseline
            (SensorId, BaselineName, OperatingState, WindowStartTs, WindowEndTs,
             SampleCount, MeanValue, StdDevValue, P01, P25, P50, P75, P99,
             IqrLowerFence, IqrUpperFence, Method)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                b.sensor_id, baseline_name, b.operating_state, start_dt, end_dt,
                b.sample_count, b.mean, b.stddev, b.p01, b.p25, b.p50, b.p75, b.p99,
                b.lower_fence, b.upper_fence, rationale[:400],
            )
            for b in baselines
        ],
    )
    conn.commit()
    cursor.close()
    logger.info("Stored %d baseline row(s) as %s", len(baselines), baseline_name)
    return len(baselines)


def load(conn: pyodbc.Connection, *, baseline_name: str = REFERENCE_BASELINE
         ) -> dict[tuple[int, str], BaselineRow]:
    """Read stored baselines back, keyed by ``(sensor_id, operating_state)``."""
    rows = fetch_all(
        conn,
        """
        SELECT b.SensorId, s.SensorCode, b.OperatingState, b.SampleCount,
               b.MeanValue, b.StdDevValue, b.P01, b.P25, b.P50, b.P75, b.P99
        FROM analytics.SensorBaseline AS b
        INNER JOIN asset.Sensor AS s ON s.SensorId = b.SensorId
        WHERE b.BaselineName = ?
        """,
        (baseline_name,),
    )
    return {
        (int(r[0]), r[2]): BaselineRow(
            sensor_id=int(r[0]), sensor_code=r[1], operating_state=r[2],
            sample_count=int(r[3]), mean=float(r[4]), stddev=float(r[5]),
            p01=float(r[6]), p25=float(r[7]), p50=float(r[8]),
            p75=float(r[9]), p99=float(r[10]),
        )
        for r in rows
    }


def describe(baselines: list[BaselineRow]) -> str:
    """Render baselines as a readable table for the console and the report."""
    lines = [
        f"{'sensor':<17} {'state':<10} {'samples':>9} {'mean':>10} {'sd':>9} "
        f"{'P25':>9} {'P75':>9} {'fence lo':>10} {'fence hi':>10}",
        "-" * 100,
    ]
    for b in sorted(baselines, key=lambda x: (x.sensor_code, x.operating_state)):
        flag = "  (flat)" if b.is_degenerate else ""
        lines.append(
            f"{b.sensor_code:<17} {b.operating_state:<10} {b.sample_count:>9,} "
            f"{b.mean:>10.4g} {b.stddev:>9.4g} {b.p25:>9.4g} {b.p75:>9.4g} "
            f"{b.lower_fence:>10.4g} {b.upper_fence:>10.4g}{flag}"
        )
    return "\n".join(lines)


def compare_to_global(conn: pyodbc.Connection, sensor_code: str) -> list[tuple]:
    """Show, for one sensor, why a single global baseline is not usable.

    Returns the per-state fences alongside the fences computed over all states together.
    Used in the Phase 3 write-up to demonstrate the failure rather than assert it.
    """
    return fetch_all(
        conn,
        f"""
        WITH usable AS (
            SELECT st.OperatingState, CAST(r.Value AS FLOAT) AS Value
            FROM ts.SensorReading AS r
            INNER JOIN asset.Sensor        AS s  ON s.SensorId      = r.SensorId
            INNER JOIN ref.QualityCode     AS q  ON q.QualityCodeId = r.QualityCodeId
            INNER JOIN analytics.ScanState AS st ON st.ReadingTs    = r.ReadingTs
            WHERE s.SensorCode = ?
              AND r.ReadingTs >= '{REFERENCE_START}' AND r.ReadingTs < '{REFERENCE_END}'
              AND q.IsUsable = 1 AND r.QualityCodeId <> {int(QualityCode.UNCERTAIN_STALE)}
              AND st.IsStale = 0
        )
        SELECT OperatingState, COUNT_BIG(*),
               APPROX_PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Value),
               APPROX_PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Value),
               MIN(Value), MAX(Value)
        FROM usable GROUP BY OperatingState
        UNION ALL
        SELECT '(all states)', COUNT_BIG(*),
               APPROX_PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY Value),
               APPROX_PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY Value),
               MIN(Value), MAX(Value)
        FROM usable
        """,
        (sensor_code,),
    )
