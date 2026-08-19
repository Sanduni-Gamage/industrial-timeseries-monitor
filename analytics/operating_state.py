"""Derive the compressor's operating state for every scan.

The machine is off 54.65% of the time, so every pressure signal is bimodal and whole-file
outlier statistics are wrong rather than merely imprecise: the 1.5x IQR fences for TP2
come out at -0.020..-0.004 bar against a real range reaching 10.68 bar.

Band edges are the midpoints between the four nominal currents the dataset documents
(0 / 4 / 7 / 9 A), so they trace to the source rather than being tuned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pyodbc

from ingestion.db import fetch_all, scalar
from ingestion.quality import QualityCode

logger = logging.getLogger(__name__)

#: Motor current (A) band edges, as midpoints between the documented nominal values.
#:   OFF        < 1.0   (documented ~0 A)
#:   OFFLOADED  1.0-5.0 (documented ~4 A)
#:   LOADED     5.0-8.0 (documented ~7 A)
#:   STARTING   >= 8.0  (documented ~9 A)
STATE_OFF = "OFF"
STATE_OFFLOADED = "OFFLOADED"
STATE_LOADED = "LOADED"
STATE_STARTING = "STARTING"

OFFLOADED_FLOOR = 1.0
LOADED_FLOOR = 5.0
STARTING_FLOOR = 8.0

ALL_STATES = (STATE_OFF, STATE_OFFLOADED, STATE_LOADED, STATE_STARTING)

#: States with enough samples and enough physical meaning to support a baseline.
#: STARTING is excluded by default: profiling found only 44 scans in it across seven
#: months (0.003%), which cannot support a standard deviation, let alone a control limit.
BASELINE_STATES = (STATE_OFF, STATE_OFFLOADED, STATE_LOADED)

#: Minimum scans in a (sensor, state) pair before a baseline is trusted. Below this the
#: statistics are noise dressed as a threshold, and no baseline row is written at all.
MIN_BASELINE_SAMPLES = 500


def classify_expression(column: str = "MotorCurrent") -> str:
    """Return the CASE expression that maps motor current to an operating state.

    Kept as a single string so the SQL builder and any ad-hoc query use exactly the same
    boundaries. A second copy of these numbers elsewhere is a divergence waiting to
    happen.
    """
    return (
        f"CASE WHEN {column} < {OFFLOADED_FLOOR} THEN '{STATE_OFF}' "
        f"WHEN {column} < {LOADED_FLOOR} THEN '{STATE_OFFLOADED}' "
        f"WHEN {column} < {STARTING_FLOOR} THEN '{STATE_LOADED}' "
        f"ELSE '{STATE_STARTING}' END"
    )


def classify(motor_current: float) -> str:
    """Classify a single reading. The Python twin of :func:`classify_expression`."""
    if motor_current < OFFLOADED_FLOOR:
        return STATE_OFF
    if motor_current < LOADED_FLOOR:
        return STATE_OFFLOADED
    if motor_current < STARTING_FLOOR:
        return STATE_LOADED
    return STATE_STARTING


@dataclass
class ScanStateSummary:
    """What the rebuild produced."""

    scans: int
    stale_scans: int
    by_state: dict[str, int]

    def share(self, state: str) -> float:
        return 100.0 * self.by_state.get(state, 0) / self.scans if self.scans else 0.0


def _sensor_id(conn: pyodbc.Connection, sensor_code: str) -> int:
    sensor_id = scalar(conn, "SELECT SensorId FROM asset.Sensor WHERE SensorCode = ?",
                       (sensor_code,))
    if sensor_id is None:
        raise RuntimeError(
            f"Sensor {sensor_code!r} is not present. Run database/seed.sql first."
        )
    return int(sensor_id)


def rebuild(conn: pyodbc.Connection) -> ScanStateSummary:
    """Recompute analytics.ScanState from the archive.

    A full set-based rebuild, because an incremental update can drift out of step after a
    backfill. ``IsStale`` carries the held-data flag forward so downstream consumers can
    exclude a frozen scan with one predicate.
    """
    motor_id = _sensor_id(conn, "MOTOR_CURRENT")
    comp_id = _sensor_id(conn, "COMP")

    cursor = conn.cursor()
    cursor.execute("TRUNCATE TABLE analytics.ScanState")

    # LEFT JOIN on COMP: a scan is defined by the presence of a motor-current reading.
    # If COMP were ever missing for a scan, dropping the whole scan would silently shrink
    # the archive, so CompActive is left NULL instead.
    cursor.execute(
        f"""
        INSERT INTO analytics.ScanState (ReadingTs, MotorCurrent, OperatingState,
                                         CompActive, IsStale)
        SELECT
            mc.ReadingTs,
            mc.Value,
            {classify_expression('mc.Value')},
            CASE WHEN comp.Value = 1 THEN 1 WHEN comp.Value = 0 THEN 0 ELSE NULL END,
            CASE WHEN mc.QualityCodeId = ? THEN 1 ELSE 0 END
        FROM ts.SensorReading AS mc
        LEFT JOIN ts.SensorReading AS comp
               ON comp.SensorId = ? AND comp.ReadingTs = mc.ReadingTs
        WHERE mc.SensorId = ?
        """,
        int(QualityCode.UNCERTAIN_STALE), comp_id, motor_id,
    )
    conn.commit()
    cursor.close()

    rows = fetch_all(
        conn,
        """
        SELECT OperatingState, COUNT_BIG(*), SUM(CAST(IsStale AS INT))
        FROM analytics.ScanState GROUP BY OperatingState
        """,
    )
    by_state = {row[0]: int(row[1]) for row in rows}
    summary = ScanStateSummary(
        scans=sum(by_state.values()),
        stale_scans=sum(int(row[2]) for row in rows),
        by_state=by_state,
    )

    logger.info("Operating states rebuilt over %s scans", f"{summary.scans:,}")
    for state in ALL_STATES:
        logger.info("  %-10s %12s  %5.2f%%", state,
                    f"{summary.by_state.get(state, 0):,}", summary.share(state))
    logger.info("  %-10s %12s  (excluded from baselines)", "held",
                f"{summary.stale_scans:,}")
    return summary
