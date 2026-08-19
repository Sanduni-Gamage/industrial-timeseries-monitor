"""Sensor behaviour around the four documented air-leak events.

Four failures of one mode, and event #1's lead-up is entirely held data, so three are
usable. That cannot validate a predictive model and none is attempted; this is a
descriptive comparison only.

Two traps it avoids: averaging held data (windows under 50% usable return nothing), and
mistaking duty cycle for signal (comparisons are made within operating state).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import pyodbc

from ingestion.db import fetch_all
from ingestion.quality import QualityCode

logger = logging.getLogger(__name__)

#: Lead-up horizons, as the brief specifies.
LEAD_UP_HOURS = (24, 12, 6, 1)

#: A window with less usable data than this cannot support a comparison.
MIN_USABLE_FRACTION = 0.5

#: Sensors worth examining. The digital tags carry no magnitude to compare.
ANALYSIS_SENSORS = ("TP2", "TP3", "H1", "DV_PRESSURE", "RESERVOIRS",
                    "OIL_TEMPERATURE", "MOTOR_CURRENT")


@dataclass
class FailureEvent:
    """One documented event, as stored."""

    event_id: int
    source_reference: str
    start_ts: datetime
    end_ts: datetime
    failure_type: str
    data_quality_note: str | None


@dataclass
class WindowStat:
    """One (event, horizon, sensor, state) comparison."""

    source_reference: str
    hours: int
    sensor_code: str
    operating_state: str
    total_scans: int
    usable_scans: int
    mean_value: float | None
    baseline_mean: float | None
    baseline_sd: float | None

    @property
    def usable_fraction(self) -> float:
        return self.usable_scans / self.total_scans if self.total_scans else 0.0

    @property
    def is_usable(self) -> bool:
        return self.usable_fraction >= MIN_USABLE_FRACTION and self.mean_value is not None

    @property
    def z_vs_baseline(self) -> float | None:
        """Deviation from the reference baseline, in baseline standard deviations.

        Returned as ``None`` rather than infinity when the baseline does not vary: a
        z-score against zero spread is not a large number, it is an undefined one.
        """
        # Every operand is checked. The first draft guarded baseline_sd but not
        # baseline_mean, which would have raised on any (sensor, state) pair that has no
        # stored baseline - exactly the pairs a new sensor or a rare state produces.
        # A type checker found it; no test had reached that combination.
        if not self.is_usable:
            return None
        if self.mean_value is None or self.baseline_mean is None:
            return None
        if self.baseline_sd is None or self.baseline_sd <= 0:
            return None
        return (self.mean_value - self.baseline_mean) / self.baseline_sd


def load_events(conn: pyodbc.Connection) -> list[FailureEvent]:
    rows = fetch_all(
        conn,
        """
        SELECT FailureEventId, SourceReference, StartTs, EndTs, FailureType, DataQualityNote
        FROM ops.FailureEvent ORDER BY StartTs
        """,
    )
    return [FailureEvent(int(r[0]), r[1], r[2], r[3], r[4], r[5]) for r in rows]


def analyse_windows(conn: pyodbc.Connection) -> list[WindowStat]:
    """Compare each pre-failure window against the reference baseline, within state.

    One query per (event, horizon) rather than a single monster query: the windows are
    tiny, the readability gain is large, and the whole thing runs in a couple of seconds.
    """
    events = load_events(conn)
    sensor_list = ", ".join(f"'{code}'" for code in ANALYSIS_SENSORS)
    stats: list[WindowStat] = []

    for event in events:
        for hours in LEAD_UP_HOURS:
            window_start = event.start_ts - timedelta(hours=hours)
            rows = fetch_all(
                conn,
                f"""
                SELECT
                    s.SensorCode,
                    st.OperatingState,
                    COUNT_BIG(*)                                    AS TotalScans,
                    SUM(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> ?
                             THEN 1 ELSE 0 END)                     AS UsableScans,
                    AVG(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> ?
                             THEN CAST(r.Value AS FLOAT) END)       AS MeanValue,
                    MAX(b.MeanValue)                                AS BaselineMean,
                    MAX(b.StdDevValue)                              AS BaselineSd
                FROM ts.SensorReading AS r
                INNER JOIN asset.Sensor        AS s  ON s.SensorId      = r.SensorId
                INNER JOIN ref.QualityCode     AS q  ON q.QualityCodeId = r.QualityCodeId
                INNER JOIN analytics.ScanState AS st ON st.ReadingTs    = r.ReadingTs
                LEFT  JOIN analytics.SensorBaseline AS b
                        ON b.SensorId = r.SensorId
                       AND b.OperatingState = st.OperatingState
                       AND b.BaselineName = 'REFERENCE_FEB2020'
                WHERE s.SensorCode IN ({sensor_list})
                  AND r.ReadingTs >= ? AND r.ReadingTs < ?
                GROUP BY s.SensorCode, st.OperatingState
                """,
                (int(QualityCode.UNCERTAIN_STALE), int(QualityCode.UNCERTAIN_STALE),
                 window_start, event.start_ts),
            )
            for row in rows:
                stats.append(
                    WindowStat(
                        source_reference=event.source_reference, hours=hours,
                        sensor_code=row[0], operating_state=row[1],
                        total_scans=int(row[2]), usable_scans=int(row[3]),
                        mean_value=float(row[4]) if row[4] is not None else None,
                        baseline_mean=float(row[5]) if row[5] is not None else None,
                        baseline_sd=float(row[6]) if row[6] is not None else None,
                    )
                )
    return stats


def anomaly_lead_time(conn: pyodbc.Connection, *, horizon_hours: int = 24) -> pd.DataFrame:
    """Do detected anomalies concentrate before failures, or are they everywhere?

    Measures the anomaly rate inside the run-up windows against the rest of the archive.
    A ratio near 1 means no relationship, which is worth reporting plainly. An association
    over three events, not a validated model.
    """
    events = load_events(conn)
    windows = [
        (e.start_ts - timedelta(hours=horizon_hours), e.end_ts, e.source_reference)
        for e in events
    ]

    total_hours = fetch_all(
        conn,
        "SELECT DATEDIFF(hour, MIN(ReadingTs), MAX(ReadingTs)) FROM ts.SensorReading",
    )[0][0]

    records: list[dict[str, Any]] = []
    for method in ("ADAPTIVE_MAD", "BASELINE_IQR", "SETPOINT"):
        total = fetch_all(
            conn, "SELECT COUNT_BIG(*) FROM analytics.Anomaly WHERE Method = ?", (method,)
        )[0][0]
        if not total:
            continue

        in_window = 0
        # Explicitly a float: hours accumulate fractionally, and letting this start as an
        # int would silently make the first += a type change rather than an addition.
        window_hours = 0.0
        for start, end, _ref in windows:
            in_window += fetch_all(
                conn,
                """SELECT COUNT_BIG(*) FROM analytics.Anomaly
                   WHERE Method = ? AND ReadingTs >= ? AND ReadingTs < ?""",
                (method, start, end),
            )[0][0]
            window_hours += (end - start).total_seconds() / 3600

        outside = total - in_window
        outside_hours = max(total_hours - window_hours, 1)
        rate_in = in_window / window_hours if window_hours else 0.0
        rate_out = outside / outside_hours
        records.append({
            "method": method,
            "anomalies_in_windows": in_window,
            "anomalies_elsewhere": outside,
            "window_hours": round(window_hours, 1),
            "other_hours": round(outside_hours, 1),
            "rate_in_windows_per_hour": round(rate_in, 3),
            "rate_elsewhere_per_hour": round(rate_out, 3),
            "enrichment": round(rate_in / rate_out, 2) if rate_out else None,
        })
    return pd.DataFrame.from_records(records)


def state_comparison(conn: pyodbc.Connection, sensor_code: str = "TP2") -> pd.DataFrame:
    """Per-event, per-state means for one sensor, next to the reference baseline.

    Restricting the comparison to a single operating state is what separates "the machine
    behaved abnormally" from "the machine simply ran more", which an air leak guarantees.
    """
    events = load_events(conn)
    records: list[dict[str, Any]] = []
    for event in events:
        for hours in LEAD_UP_HOURS:
            rows = fetch_all(
                conn,
                """
                SELECT st.OperatingState,
                       COUNT_BIG(*),
                       SUM(CASE WHEN r.QualityCodeId = ? THEN 1 ELSE 0 END),
                       AVG(CASE WHEN r.QualityCodeId <> ? THEN CAST(r.Value AS FLOAT) END),
                       MAX(b.MeanValue), MAX(b.StdDevValue)
                FROM ts.SensorReading AS r
                INNER JOIN asset.Sensor        AS s  ON s.SensorId   = r.SensorId
                INNER JOIN analytics.ScanState AS st ON st.ReadingTs = r.ReadingTs
                LEFT  JOIN analytics.SensorBaseline AS b
                        ON b.SensorId = r.SensorId AND b.OperatingState = st.OperatingState
                       AND b.BaselineName = 'REFERENCE_FEB2020'
                WHERE s.SensorCode = ?
                  AND r.ReadingTs >= ? AND r.ReadingTs < ?
                GROUP BY st.OperatingState
                """,
                (int(QualityCode.UNCERTAIN_STALE), int(QualityCode.UNCERTAIN_STALE),
                 sensor_code, event.start_ts - timedelta(hours=hours), event.start_ts),
            )
            for state, total, held, mean, base_mean, base_sd in rows:
                usable = int(total) - int(held)
                records.append({
                    "event": event.source_reference,
                    "start": event.start_ts,
                    "hours_before": hours,
                    "state": state,
                    "scans": int(total),
                    "held": int(held),
                    "usable_pct": round(100.0 * usable / int(total), 1) if total else 0.0,
                    "mean": round(float(mean), 4) if mean is not None else None,
                    "baseline_mean": round(float(base_mean), 4) if base_mean else None,
                    "z": (round((float(mean) - float(base_mean)) / float(base_sd), 2)
                          if mean is not None and base_mean is not None
                          and base_sd and float(base_sd) > 0 else None),
                })
    return pd.DataFrame.from_records(records)
