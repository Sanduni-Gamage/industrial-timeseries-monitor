/* =====================================================================================
   Q8 - Missing-data / gap detection with LAG().

   Purpose      Find intervals where the archive holds no data at all. This is the check
                that separates "the machine was fine" from "we were not watching".

   Technique    LAG() over ReadingTs for a single representative tag. One tag is enough:
                the logger writes all 15 signals in the same scan, so a gap affects all
                of them identically. Running this across every tag would multiply the
                work by 15 to produce 15 copies of the same answer.

   Threshold    30 s = 3x the measured 10 s modal interval. Not arbitrary: the interval
                distribution shows 1,337,521 steps of 10 s but also 128,277 of 9 s and
                38,321 of 12 s, roughly +/-1-3 s of logger jitter. An exact-interval
                test would report a million false gaps.

   Measured     331 gaps, largest 48.03 h, overall coverage 82.36%.

   Production note: ops.vw_TimestampGap serves the same answer from the issues recorded
   at ingestion time, which costs 331 rows instead of a 1.5-million-row scan. This query
   is the derivation; the view is the cache.
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
