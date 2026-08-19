"""Data access for the API.

One class per concern, all behind a protocol so tests can substitute a fake and cover the
entire HTTP surface with no database running. Resolution (raw, hourly, daily) is chosen
from the requested span so a six-month query never scans 22.7 million rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pyodbc

from api.models.schemas import Resolution

logger = logging.getLogger(__name__)

#: Hard ceiling on points returned in one response. Reached only when a caller asks for
#: raw resolution over a wide window; the response says so rather than trimming quietly.
MAX_POINTS = 50_000

#: Windows wider than this are served from the hourly aggregate when resolution is
#: 'auto'. Six hours of raw data is 2,160 points per sensor - about as much as a chart
#: 2,000 pixels wide can distinguish.
AUTO_RAW_MAX_HOURS = 6

#: Beyond this, 'auto' drops to daily. 30 days of hourly is 720 points.
AUTO_HOURLY_MAX_DAYS = 30

#: Samples in a fully-covered bucket at the measured 10 s sampling interval.
SAMPLES_PER_HOUR = 360
SAMPLES_PER_DAY = 8_640

#: Trailing window used to decide whether an anomaly is "active". Relative to the end of
#: the archive, not the wall clock - every reading in a 2020 dataset is stale against now.
ACTIVE_WINDOW_HOURS = 24


#: Seconds to cache the archive-wide quality summary.
#:
#: Those counts require a scan of all 22.7 million readings (~540 ms), and they change
#: only when ingestion runs - which is minutes of work, not seconds. Recomputing them on
#: every dashboard poll spends half a second to get an identical answer. A short TTL keeps
#: the figure honest while taking /summary from ~724 ms to ~190 ms.
#:
#: Deliberately in-process and deliberately small: one number, one lifetime. A general
#: caching layer here would be a second source of truth to keep correct.
QUALITY_SUMMARY_TTL_SECONDS = 60.0


class NotFoundError(LookupError):
    """A requested resource does not exist."""


class _TimedCache:
    """A single cached value with a time-to-live."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._value: Any = None
        self._stored_at: float = 0.0

    def get(self) -> Any | None:
        import time as _time
        if self._value is None or (_time.monotonic() - self._stored_at) > self._ttl:
            return None
        return self._value

    def set(self, value: Any) -> Any:
        import time as _time
        self._value, self._stored_at = value, _time.monotonic()
        return value

    def clear(self) -> None:
        self._value, self._stored_at = None, 0.0


#: Module-level so it survives the per-request Repository instance.
_quality_cache = _TimedCache(QUALITY_SUMMARY_TTL_SECONDS)


class Repository:
    """Read-only queries backing the API."""

    def __init__(self, conn: pyodbc.Connection) -> None:
        self._conn = conn

    # -- helpers --------------------------------------------------------------------

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        cursor = self._conn.cursor()
        try:
            return [tuple(row) for row in cursor.execute(sql, *params).fetchall()]
        finally:
            cursor.close()

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # -- health ---------------------------------------------------------------------

    def ping(self) -> str:
        """Cheapest possible round trip, for the health check."""
        row = self._one("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64))")
        return row[0] if row else "unknown"

    def table_counts(self) -> dict[str, int]:
        """Row counts from metadata rather than COUNT(*).

        ``sys.dm_db_partition_stats`` gives an approximate count without touching the
        table. A health check that does a full count on a 22.7-million-row table is a
        health check that will eventually cause the outage it exists to detect.
        """
        rows = self._rows("""
            SELECT CONCAT(SCHEMA_NAME(t.schema_id), '.', t.name), SUM(ps.row_count)
            FROM sys.tables AS t
            INNER JOIN sys.dm_db_partition_stats AS ps
                    ON ps.object_id = t.object_id AND ps.index_id IN (0, 1)
            GROUP BY t.schema_id, t.name
        """)
        return {row[0]: int(row[1]) for row in rows}

    def last_ingestion(self) -> tuple | None:
        return self._one("""
            SELECT TOP (1) IngestionRunId, Status, StartedUtc, CompletedUtc,
                   RowsInserted, MinReadingTs, MaxReadingTs
            FROM ops.IngestionRun
            WHERE Status = 'SUCCEEDED'
            ORDER BY IngestionRunId DESC
        """)

    # -- assets ---------------------------------------------------------------------

    def list_equipment(self) -> list[tuple]:
        return self._rows("""
            SELECT e.EquipmentId, e.EquipmentCode, e.EquipmentName, e.EquipmentType,
                   e.Location, e.Description, e.IsActive,
                   (SELECT COUNT(*) FROM asset.Sensor s
                    WHERE s.EquipmentId = e.EquipmentId AND s.IsActive = 1)
            FROM asset.Equipment AS e
            ORDER BY e.EquipmentCode
        """)

    def get_equipment(self, equipment_id: int) -> tuple:
        row = self._one("""
            SELECT e.EquipmentId, e.EquipmentCode, e.EquipmentName, e.EquipmentType,
                   e.Location, e.Description, e.IsActive,
                   (SELECT COUNT(*) FROM asset.Sensor s
                    WHERE s.EquipmentId = e.EquipmentId AND s.IsActive = 1)
            FROM asset.Equipment AS e
            WHERE e.EquipmentId = ?
        """, (equipment_id,))
        if row is None:
            raise NotFoundError(f"Equipment {equipment_id} does not exist.")
        return row

    def list_sensors(self, equipment_id: int | None = None,
                     sensor_class: str | None = None) -> list[tuple]:
        return self._rows("""
            SELECT SensorId, EquipmentId, SensorCode, SensorName, SensorClass,
                   MeasurementType, Unit, PhysicalMin, PhysicalMax, DocumentedSetpoint,
                   Description, IsActive
            FROM asset.Sensor
            WHERE (? IS NULL OR EquipmentId = ?)
              AND (? IS NULL OR SensorClass = ?)
            ORDER BY DisplayOrder, SensorId
        """, (equipment_id, equipment_id, sensor_class, sensor_class))

    def get_sensor(self, sensor_id: int) -> tuple:
        row = self._one("""
            SELECT SensorId, EquipmentId, SensorCode, SensorName, SensorClass,
                   MeasurementType, Unit, PhysicalMin, PhysicalMax, DocumentedSetpoint,
                   Description, IsActive
            FROM asset.Sensor WHERE SensorId = ?
        """, (sensor_id,))
        if row is None:
            raise NotFoundError(f"Sensor {sensor_id} does not exist.")
        return row

    def resolve_sensor(self, identifier: str) -> tuple:
        """Look up a sensor by numeric id or by code.

        Accepting both is a small kindness: ``/readings/TP3`` is far easier to work with
        by hand than ``/readings/15``, and the ids are assignment-order artefacts with no
        meaning to a user.
        """
        if identifier.isdigit():
            return self.get_sensor(int(identifier))
        row = self._one("""
            SELECT SensorId, EquipmentId, SensorCode, SensorName, SensorClass,
                   MeasurementType, Unit, PhysicalMin, PhysicalMax, DocumentedSetpoint,
                   Description, IsActive
            FROM asset.Sensor WHERE SensorCode = ?
        """, (identifier.upper(),))
        if row is None:
            raise NotFoundError(f"No sensor with id or code {identifier!r}.")
        return row

    def archive_bounds(self) -> tuple[datetime | None, datetime | None]:
        row = self._one("SELECT MIN(ReadingTs), MAX(ReadingTs) FROM ts.SensorReading")
        return (row[0], row[1]) if row else (None, None)

    # -- readings -------------------------------------------------------------------

    @staticmethod
    def choose_resolution(requested: Resolution, start: datetime,
                          end: datetime) -> Resolution:
        """Pick a resolution that will not return an unusable number of points.

        An explicit request is honoured - the caller may genuinely want raw data and is
        told if it was capped. ``auto`` is the default because the common mistake is
        asking for six months at 10-second resolution and receiving 1.8 million points
        no chart can draw.
        """
        if requested is not Resolution.AUTO:
            return requested
        span = end - start
        if span <= timedelta(hours=AUTO_RAW_MAX_HOURS):
            return Resolution.RAW
        if span <= timedelta(days=AUTO_HOURLY_MAX_DAYS):
            return Resolution.HOURLY
        return Resolution.DAILY

    def raw_readings(self, sensor_id: int, start: datetime, end: datetime,
                     limit: int = MAX_POINTS) -> tuple[list[tuple], bool]:
        """Raw series for one sensor. Returns ``(rows, truncated)``.

        Fetches ``limit + 1`` rows so truncation is detected without a second COUNT over
        the same range.
        """
        rows = self._rows("""
            SELECT TOP (?) r.ReadingTs, r.Value, q.Code, q.IsUsable
            FROM ts.SensorReading AS r
            INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
            WHERE r.SensorId = ? AND r.ReadingTs >= ? AND r.ReadingTs < ?
            ORDER BY r.ReadingTs
        """, (limit + 1, sensor_id, start, end))
        truncated = len(rows) > limit
        return rows[:limit], truncated

    def hourly_readings(self, sensor_id: int, start: datetime, end: datetime,
                        limit: int = MAX_POINTS) -> tuple[list[tuple], bool]:
        rows = self._rows("""
            SELECT TOP (?) BucketStart, AvgValue, MinValue, MaxValue, SampleCount
            FROM analytics.SensorHourlyAgg
            WHERE SensorId = ? AND BucketStart >= ? AND BucketStart < ?
            ORDER BY BucketStart
        """, (limit + 1, sensor_id, start, end))
        return rows[:limit], len(rows) > limit

    def daily_readings(self, sensor_id: int, start: datetime, end: datetime,
                       limit: int = MAX_POINTS) -> tuple[list[tuple], bool]:
        rows = self._rows("""
            SELECT TOP (?) CAST(BucketDate AS datetime2(0)), AvgValue, MinValue,
                   MaxValue, SampleCount
            FROM analytics.SensorDailyAgg
            WHERE SensorId = ? AND BucketDate >= ? AND BucketDate < ?
            ORDER BY BucketDate
        """, (limit + 1, sensor_id, start, end))
        return rows[:limit], len(rows) > limit

    # -- trends ---------------------------------------------------------------------

    def trend(self, sensor_id: int, start: datetime, end: datetime,
              operating_state: str | None, window_hours: int) -> list[tuple]:
        """Hourly series with rolling statistics computed in SQL.

        The rolling window is evaluated by the database rather than in Python because the
        window functions are the natural expression of it and the aggregate table is
        small enough that pulling rows out to compute them would be pure overhead.
        """
        # SQL Server requires the window-frame extent to be a literal: a parameter marker
        # there fails to compile with "Incorrect syntax near '@P1'". The value is coerced
        # to a bounded int here rather than trusted from the route, so the interpolation
        # cannot carry anything but a number even if a caller reaches the repository
        # directly. Every other value stays a bound parameter.
        preceding = max(1, min(int(window_hours), 168)) - 1

        if operating_state:
            return self._rows(f"""
                SELECT BucketStart, AvgValue, SampleCount,
                       AVG(CAST(AvgValue AS FLOAT)) OVER (
                           ORDER BY BucketStart
                           ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW),
                       STDEV(CAST(AvgValue AS FLOAT)) OVER (
                           ORDER BY BucketStart
                           ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW),
                       CAST(AvgValue AS FLOAT) - LAG(CAST(AvgValue AS FLOAT))
                           OVER (ORDER BY BucketStart),
                       DATEDIFF(hour, LAG(BucketStart) OVER (ORDER BY BucketStart),
                                BucketStart)
                FROM analytics.SensorHourlyStateAgg
                WHERE SensorId = ? AND OperatingState = ?
                  AND BucketStart >= ? AND BucketStart < ?
                ORDER BY BucketStart
            """, (sensor_id, operating_state, start, end))

        return self._rows(f"""
            SELECT BucketStart, AvgValue, SampleCount,
                   AVG(CAST(AvgValue AS FLOAT)) OVER (
                       ORDER BY BucketStart
                       ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW),
                   STDEV(CAST(AvgValue AS FLOAT)) OVER (
                       ORDER BY BucketStart
                       ROWS BETWEEN {preceding} PRECEDING AND CURRENT ROW),
                   CAST(AvgValue AS FLOAT) - LAG(CAST(AvgValue AS FLOAT))
                       OVER (ORDER BY BucketStart),
                   DATEDIFF(hour, LAG(BucketStart) OVER (ORDER BY BucketStart), BucketStart)
            FROM analytics.SensorHourlyAgg
            WHERE SensorId = ? AND BucketStart >= ? AND BucketStart < ?
            ORDER BY BucketStart
        """, (sensor_id, start, end))

    # -- anomalies ------------------------------------------------------------------

    def count_anomalies(self, start: datetime | None, end: datetime | None,
                        sensor_id: int | None, severity: str | None,
                        method: str | None) -> int:
        row = self._one("""
            SELECT COUNT_BIG(*)
            FROM analytics.Anomaly AS a
            WHERE (? IS NULL OR a.ReadingTs >= ?) AND (? IS NULL OR a.ReadingTs < ?)
              AND (? IS NULL OR a.SensorId = ?)
              AND (? IS NULL OR a.Severity = ?)
              AND (? IS NULL OR a.Method = ?)
        """, (start, start, end, end, sensor_id, sensor_id, severity, severity,
              method, method))
        return int(row[0]) if row else 0

    def list_anomalies(self, start: datetime | None, end: datetime | None,
                       sensor_id: int | None, severity: str | None, method: str | None,
                       limit: int, offset: int) -> list[tuple]:
        return self._rows("""
            SELECT a.AnomalyId, a.SensorId, s.SensorCode, s.SensorName, e.EquipmentCode,
                   a.ReadingTs, a.Value, s.Unit, a.ExpectedLow, a.ExpectedHigh,
                   a.Method, a.Score, a.Severity, a.OperatingState, a.DetectedUtc
            FROM analytics.Anomaly AS a
            INNER JOIN asset.Sensor    AS s ON s.SensorId    = a.SensorId
            INNER JOIN asset.Equipment AS e ON e.EquipmentId = s.EquipmentId
            WHERE (? IS NULL OR a.ReadingTs >= ?) AND (? IS NULL OR a.ReadingTs < ?)
              AND (? IS NULL OR a.SensorId = ?)
              AND (? IS NULL OR a.Severity = ?)
              AND (? IS NULL OR a.Method = ?)
            ORDER BY
                CASE a.Severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
                a.ReadingTs DESC
            OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
        """, (start, start, end, end, sensor_id, sensor_id, severity, severity,
              method, method, offset, limit))

    def active_anomalies(self, limit: int = 50) -> list[tuple]:
        """Anomalies in the trailing window, relative to the end of the archive."""
        return self._rows("""
            DECLARE @End DATETIME2(3) = (SELECT MAX(ReadingTs) FROM ts.SensorReading);
            SELECT TOP (?) a.AnomalyId, a.SensorId, s.SensorCode, s.SensorName,
                   e.EquipmentCode, a.ReadingTs, a.Value, s.Unit, a.ExpectedLow,
                   a.ExpectedHigh, a.Method, a.Score, a.Severity, a.OperatingState,
                   a.DetectedUtc
            FROM analytics.Anomaly AS a
            INNER JOIN asset.Sensor    AS s ON s.SensorId    = a.SensorId
            INNER JOIN asset.Equipment AS e ON e.EquipmentId = s.EquipmentId
            WHERE a.ReadingTs >= DATEADD(hour, -?, @End)
            ORDER BY
                CASE a.Severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
                a.Score DESC
        """, (limit, ACTIVE_WINDOW_HOURS))

    # -- failures -------------------------------------------------------------------

    def list_failures(self, equipment_id: int | None = None) -> list[tuple]:
        return self._rows("""
            SELECT FailureEventId, EquipmentId, EquipmentCode, StartTs, EndTs,
                   DurationMinutes, FailureType, Severity, SourceReference, ReportNote,
                   DataQualityNote, LeadUp24hScans, LeadUp24hStaleScans,
                   LeadUp24hStalePct, LeadUpUsability
            FROM ops.vw_FailureContext
            WHERE (? IS NULL OR EquipmentId = ?)
            ORDER BY StartTs
        """, (equipment_id, equipment_id))

    # -- health and summary ---------------------------------------------------------

    def equipment_health(self, equipment_id: int | None = None) -> list[tuple]:
        return self._rows("""
            SELECT EquipmentId, EquipmentCode, EquipmentName, SensorCount, LastReadingTs,
                   TotalReadings, CriticalAnomalies, WarningAnomalies, RecordedFailures,
                   HealthStatus
            FROM asset.vw_EquipmentHealth
            WHERE (? IS NULL OR EquipmentId = ?)
            ORDER BY EquipmentCode
        """, (equipment_id, equipment_id))

    def latest_readings(self) -> list[tuple]:
        return self._rows("""
            SELECT SensorId, SensorCode, SensorName, SensorClass, Unit,
                   LastReadingTs, LastValue, QualityCode, QualityIsUsable
            FROM ts.vw_LatestReading
            ORDER BY DisplayOrder
        """)

    def data_quality_summary(self, *, use_cache: bool = True) -> dict[str, Any]:
        """Archive-wide quality figures. Cached; see QUALITY_SUMMARY_TTL_SECONDS."""
        if use_cache:
            cached = _quality_cache.get()
            if cached is not None:
                return cached
        value = self._compute_quality_summary()
        return _quality_cache.set(value) if use_cache else value

    def _compute_quality_summary(self) -> dict[str, Any]:
        totals = self._one("""
            SELECT COUNT_BIG(*),
                   SUM(CASE WHEN QualityCodeId = 192 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN QualityCodeId = 65 THEN 1 ELSE 0 END)
            FROM ts.SensorReading
        """) or (0, 0, 0)
        total = int(totals[0] or 0)
        good = int(totals[1] or 0)
        held = int(totals[2] or 0)

        rejected = self._one("SELECT COUNT_BIG(*) FROM ops.RejectedRow") or (0,)
        gaps = self._one("SELECT COUNT(*) FROM ops.vw_TimestampGap") or (0,)
        scans = self._one("SELECT COUNT_BIG(*) FROM analytics.ScanState") or (0,)
        start, end = self.archive_bounds()

        coverage = 0.0
        if start and end and int(scans[0]):
            expected = (end - start).total_seconds() / 10.0 + 1
            coverage = 100.0 * int(scans[0]) / expected

        run = self.last_ingestion()
        return {
            "total_readings": total,
            "good_readings": good,
            "good_pct": 100.0 * good / total if total else 0.0,
            "held_readings": held,
            "held_pct": 100.0 * held / total if total else 0.0,
            "quarantined_rows": int(rejected[0] or 0),
            "gap_count": int(gaps[0] or 0),
            "coverage_pct": coverage,
            "last_successful_ingestion": run[3] if run else None,
            "last_ingestion_status": run[1] if run else None,
        }
