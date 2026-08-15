/* =====================================================================================
   Q7 - Daily statistics including a true median.

   Purpose      Mean and median disagree sharply on this machine, and the disagreement
                is the finding. The compressor is unloaded 54.65% of the time, so a
                pressure signal like TP2 has a median near its idle value (-0.012 bar)
                and a mean pulled upward by the loaded periods (1.368 bar overall).

                Reporting only the mean hides the duty cycle; reporting only the median
                hides the load. An operator needs both, which is why the dashboard shows
                a band rather than a single line.

   Technique    PERCENTILE_CONT is a window function, not an aggregate - it returns a
                value per row, so it needs DISTINCT (or a windowed CTE) to collapse to
                one row per group. That surprises people; it is called out here on
                purpose.

   Cost         This one scans raw readings for the chosen window, because a median
                cannot be derived from stored min/max/avg. Keep the window bounded.
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
