/* =====================================================================================
   Q3 - Day-over-day trend and percentage change.

   "Is this drifting?" matters more than "what is it now?". LAG() over the daily
   aggregate turns a 22.7M-row scan into a 212-row one. NULLIF guards day one and any
   sensor that legitimately averages zero.
   ===================================================================================== */

DECLARE @SensorCode VARCHAR(32) = 'OIL_TEMPERATURE';

WITH daily AS (
    SELECT
        a.BucketDate,
        a.AvgValue,
        a.MinValue,
        a.MaxValue,
        a.SampleCount,
        LAG(a.AvgValue) OVER (ORDER BY a.BucketDate) AS PrevAvgValue
    FROM analytics.SensorDailyAgg AS a
    INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
    WHERE s.SensorCode = @SensorCode
)
SELECT
    BucketDate,
    CAST(AvgValue AS DECIMAL(10,3))     AS AvgValue,
    CAST(PrevAvgValue AS DECIMAL(10,3)) AS PrevAvgValue,
    CAST(AvgValue - PrevAvgValue AS DECIMAL(10,3)) AS DeltaVsPrevDay,
    CAST(100.0 * (AvgValue - PrevAvgValue) / NULLIF(ABS(PrevAvgValue), 0) AS DECIMAL(8,2))
        AS PctChange,
    SampleCount,
    /* A 7-day trailing mean smooths the day-to-day noise a duty-cycled machine
       produces, so a genuine drift stands out from ordinary variation. */
    CAST(AVG(AvgValue) OVER (ORDER BY BucketDate ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)
         AS DECIMAL(10,3)) AS TrailingMean7d
FROM daily
ORDER BY BucketDate;
