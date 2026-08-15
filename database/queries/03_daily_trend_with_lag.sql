/* =====================================================================================
   Q3 - Day-over-day trend and percentage change.

   Purpose      "Is this drifting?" is a different question from "what is it now?", and
                it is the one that matters for condition monitoring. An oil temperature
                of 70 C means little on its own; 70 C after a week at 62 C means a lot.

   Technique    LAG() over the daily aggregate. Reading the aggregate archive rather than
                the fact table turns a 22.7-million-row scan into a 212-row one.

   Guard        NULLIF on the denominator: day one has no predecessor, and a sensor that
                legitimately averages zero would otherwise divide by it.
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
