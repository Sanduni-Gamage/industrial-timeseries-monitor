/* =====================================================================================
   Q8 - Missing-data / gap detection with LAG().

   Separates "the machine was fine" from "we were not watching". One tag is enough,
   because the logger writes all 15 signals in the same scan.

   30 s = 3x the measured 10 s modal interval, which absorbs the +/-1-3 s of logger
   jitter an exact-interval test would report as a million false gaps.

   Measured: 331 gaps, largest 48.03 h, coverage 82.36%. ops.vw_TimestampGap serves the
   same answer from 331 recorded rows; this query is the derivation.
   ===================================================================================== */

DECLARE @GapThresholdSeconds INT = 30;

WITH scans AS (
    SELECT
        r.ReadingTs,
        LAG(r.ReadingTs) OVER (ORDER BY r.ReadingTs) AS PrevReadingTs
    FROM ts.SensorReading AS r
    WHERE r.SensorId = (SELECT SensorId FROM asset.Sensor WHERE SensorCode = 'TP3')
)
SELECT
    PrevReadingTs                                    AS GapStart,
    ReadingTs                                        AS GapEnd,
    DATEDIFF(second, PrevReadingTs, ReadingTs)       AS GapSeconds,
    CAST(DATEDIFF(second, PrevReadingTs, ReadingTs) / 3600.0 AS DECIMAL(10,2)) AS GapHours,
    DATEDIFF(second, PrevReadingTs, ReadingTs) / 10 - 1 AS EstimatedMissingScans
FROM scans
WHERE PrevReadingTs IS NOT NULL
  AND DATEDIFF(second, PrevReadingTs, ReadingTs) > @GapThresholdSeconds
ORDER BY GapSeconds DESC;
