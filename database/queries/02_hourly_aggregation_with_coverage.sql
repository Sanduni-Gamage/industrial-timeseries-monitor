/* =====================================================================================
   Q2 - Hourly aggregation computed live, with coverage.

   DATEADD(hour, DATEDIFF(hour, 0, ts), 0) truncates to the hour and stays a datetime,
   so it remains sargable and ignores session language settings.

   A full hour is 360 samples at 10 s. This archive is 82.4% covered, so an average
   without its sample count invites false confidence.
   ===================================================================================== */

DECLARE @SensorCode VARCHAR(32) = 'OIL_TEMPERATURE';
DECLARE @From DATETIME2(3) = '2020-06-01T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-06-08T00:00:00';

SELECT
    DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0) AS BucketStart,
    COUNT_BIG(*)                                     AS SampleCount,
    CAST(100.0 * COUNT_BIG(*) / 360.0 AS DECIMAL(5,1)) AS CoveragePct,
    SUM(CASE WHEN q.IsUsable = 1 THEN 1 ELSE 0 END)  AS UsableCount,
    CAST(MIN(r.Value) AS DECIMAL(10,3))              AS MinValue,
    CAST(AVG(CAST(r.Value AS FLOAT)) AS DECIMAL(10,3)) AS AvgValue,
    CAST(MAX(r.Value) AS DECIMAL(10,3))              AS MaxValue,
    CAST(STDEV(CAST(r.Value AS FLOAT)) AS DECIMAL(10,4)) AS StdDev
FROM ts.SensorReading AS r
INNER JOIN asset.Sensor    AS s ON s.SensorId      = r.SensorId
INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
WHERE s.SensorCode = @SensorCode
  AND r.ReadingTs >= @From AND r.ReadingTs < @To
GROUP BY DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0)
ORDER BY BucketStart;
