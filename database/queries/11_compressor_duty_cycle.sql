/* =====================================================================================
   Q11 - Compressor duty cycle derived from documented operating states.

   The band edges are midpoints between the four nominal currents the UCI documentation
   states outright (0 / 4 / 7 / 9 A), so they are traceable rather than tuned.

   Measured: OFF 54.65%, OFFLOADED 30.15%, LOADED 15.20%, STARTING 0.003% (44 scans).

   State is also what makes anomaly detection work here: global IQR fences for TP2 come
   out at -0.020..-0.004 bar against a real range of -0.032..10.68. See
   docs/SQL_DESIGN.md section 6.6.
   ===================================================================================== */

DECLARE @From DATETIME2(3) = '2020-06-01T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-07-01T00:00:00';

WITH scans AS (
    SELECT
        CAST(r.ReadingTs AS DATE) AS BucketDate,
        CASE
            WHEN r.Value <  1.0 THEN 'OFF'
            WHEN r.Value <  5.0 THEN 'OFFLOADED'
            WHEN r.Value <  8.0 THEN 'LOADED'
            ELSE                     'STARTING'
        END AS OperatingState
    FROM ts.SensorReading AS r
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    WHERE r.SensorId = (SELECT SensorId FROM asset.Sensor WHERE SensorCode = 'MOTOR_CURRENT')
      AND r.ReadingTs >= @From AND r.ReadingTs < @To
      AND q.IsUsable = 1
      AND r.QualityCodeId <> 65   -- held values would inflate whichever state was frozen
)
SELECT
    BucketDate,
    COUNT_BIG(*) AS UsableScans,
    SUM(CASE WHEN OperatingState = 'OFF'       THEN 1 ELSE 0 END) AS OffScans,
    SUM(CASE WHEN OperatingState = 'OFFLOADED' THEN 1 ELSE 0 END) AS OffloadedScans,
    SUM(CASE WHEN OperatingState = 'LOADED'    THEN 1 ELSE 0 END) AS LoadedScans,
    SUM(CASE WHEN OperatingState = 'STARTING'  THEN 1 ELSE 0 END) AS StartingScans,
    CAST(100.0 * SUM(CASE WHEN OperatingState = 'LOADED' THEN 1 ELSE 0 END)
         / COUNT_BIG(*) AS DECIMAL(5,2)) AS LoadedPct,
    /* Running hours, at the measured 10 s sampling interval. */
    CAST(SUM(CASE WHEN OperatingState IN ('LOADED', 'OFFLOADED', 'STARTING')
                  THEN 1 ELSE 0 END) * 10.0 / 3600.0 AS DECIMAL(6,2)) AS RunningHours
FROM scans
GROUP BY BucketDate
ORDER BY BucketDate;
