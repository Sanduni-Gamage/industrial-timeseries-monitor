/* =====================================================================================
   Q7 - Daily statistics including a true median.

   Mean and median disagree sharply here, and that is the finding: the compressor is
   unloaded 54.65% of the time, so TP2's median sits near idle (-0.012 bar) while its
   mean is pulled to 1.368 bar by the loaded periods. An operator needs both.

   PERCENTILE_CONT is a window function, not an aggregate, so it needs DISTINCT to
   collapse to one row per group. This scans raw readings, so keep the window bounded.
   ===================================================================================== */

DECLARE @SensorCode VARCHAR(32) = 'TP2';
DECLARE @From DATETIME2(3) = '2020-06-01T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-06-15T00:00:00';

SELECT DISTINCT
    CAST(r.ReadingTs AS DATE) AS BucketDate,
    COUNT_BIG(*)      OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS SampleCount,
    CAST(MIN(r.Value) OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS DECIMAL(10,3)) AS MinValue,
    CAST(AVG(CAST(r.Value AS FLOAT))
                      OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS DECIMAL(10,3)) AS MeanValue,
    CAST(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY r.Value)
                      OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS DECIMAL(10,3)) AS MedianValue,
    CAST(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY r.Value)
                      OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS DECIMAL(10,3)) AS P95Value,
    CAST(MAX(r.Value) OVER (PARTITION BY CAST(r.ReadingTs AS DATE)) AS DECIMAL(10,3)) AS MaxValue
FROM ts.SensorReading AS r
INNER JOIN asset.Sensor AS s ON s.SensorId = r.SensorId
WHERE s.SensorCode = @SensorCode
  AND r.ReadingTs >= @From AND r.ReadingTs < @To
ORDER BY BucketDate;
