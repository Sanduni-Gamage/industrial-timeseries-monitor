/* =====================================================================================
   Rebuild the aggregate archive: hourly and daily rollups.
   
   The aggregate half of the pattern historians use, so a six-month trend does not scan
   22.7 million rows to draw a 4,000-pixel line.
   
   A full rebuild, because at this volume it takes seconds and cannot drift out of step
   with the raw archive the way an incremental update can. Safe to re-run.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   Hourly rollup
   
   FirstValue/LastValue use CROSS APPLY rather than FIRST_VALUE/LAST_VALUE, which would
   sort all 22.7M rows and spill to tempdb on Express. The APPLY form does two index seeks
   per bucket instead.
   
   GoodCount travels with SampleCount so the UI can show coverage. Value is CAST to FLOAT
   before averaging, because summing tens of thousands of REALs accumulates visible
   error.
   ------------------------------------------------------------------------------------- */

TRUNCATE TABLE analytics.SensorHourlyAgg;

INSERT INTO analytics.SensorHourlyAgg
    (SensorId, BucketStart, SampleCount, GoodCount, MinValue, MaxValue,
     AvgValue, StdDevValue, FirstValue, LastValue)
SELECT
    b.SensorId,
    b.BucketStart,
    b.SampleCount,
    b.GoodCount,
    b.MinValue,
    b.MaxValue,
    CAST(b.AvgValue AS REAL),
    CAST(b.StdDevValue AS REAL),
    f.Value,
    l.Value
FROM (
    SELECT
        r.SensorId,
        DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0) AS BucketStart,
        COUNT_BIG(*)                                     AS SampleCount,
        SUM(CASE WHEN q.IsUsable = 1 THEN 1 ELSE 0 END)  AS GoodCount,
        MIN(r.Value)                                     AS MinValue,
        MAX(r.Value)                                     AS MaxValue,
        AVG(CAST(r.Value AS FLOAT))                      AS AvgValue,
        STDEV(CAST(r.Value AS FLOAT))                    AS StdDevValue,
        MIN(r.ReadingTs)                                 AS FirstTs,
        MAX(r.ReadingTs)                                 AS LastTs
    FROM ts.SensorReading AS r
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    GROUP BY r.SensorId, DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0)
) AS b
CROSS APPLY (
    SELECT r.Value FROM ts.SensorReading AS r
    WHERE r.SensorId = b.SensorId AND r.ReadingTs = b.FirstTs
) AS f
CROSS APPLY (
    SELECT r.Value FROM ts.SensorReading AS r
    WHERE r.SensorId = b.SensorId AND r.ReadingTs = b.LastTs
) AS l;

/* PRINT takes only scalar expressions - a subquery inline here is a COMPILE-time error
   that fails the entire batch, including the INSERT above it. Assign first. */
DECLARE @HourlyBuckets BIGINT = (SELECT COUNT_BIG(*) FROM analytics.SensorHourlyAgg);
PRINT CONCAT('aggregates.sql: hourly buckets rebuilt = ', @HourlyBuckets);
GO

/* -------------------------------------------------------------------------------------
   Daily rollup - same shape, day buckets.
   ------------------------------------------------------------------------------------- */

TRUNCATE TABLE analytics.SensorDailyAgg;

INSERT INTO analytics.SensorDailyAgg
    (SensorId, BucketDate, SampleCount, GoodCount, MinValue, MaxValue,
     AvgValue, StdDevValue, FirstValue, LastValue)
SELECT
    b.SensorId,
    b.BucketDate,
    b.SampleCount,
    b.GoodCount,
    b.MinValue,
    b.MaxValue,
    CAST(b.AvgValue AS REAL),
    CAST(b.StdDevValue AS REAL),
    f.Value,
    l.Value
FROM (
    SELECT
        r.SensorId,
        CAST(r.ReadingTs AS DATE)                        AS BucketDate,
        COUNT_BIG(*)                                     AS SampleCount,
        SUM(CASE WHEN q.IsUsable = 1 THEN 1 ELSE 0 END)  AS GoodCount,
        MIN(r.Value)                                     AS MinValue,
        MAX(r.Value)                                     AS MaxValue,
        AVG(CAST(r.Value AS FLOAT))                      AS AvgValue,
        STDEV(CAST(r.Value AS FLOAT))                    AS StdDevValue,
        MIN(r.ReadingTs)                                 AS FirstTs,
        MAX(r.ReadingTs)                                 AS LastTs
    FROM ts.SensorReading AS r
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    GROUP BY r.SensorId, CAST(r.ReadingTs AS DATE)
) AS b
CROSS APPLY (
    SELECT r.Value FROM ts.SensorReading AS r
    WHERE r.SensorId = b.SensorId AND r.ReadingTs = b.FirstTs
) AS f
CROSS APPLY (
    SELECT r.Value FROM ts.SensorReading AS r
    WHERE r.SensorId = b.SensorId AND r.ReadingTs = b.LastTs
) AS l;

DECLARE @DailyBuckets BIGINT = (SELECT COUNT_BIG(*) FROM analytics.SensorDailyAgg);
PRINT CONCAT('aggregates.sql: daily buckets rebuilt = ', @DailyBuckets);
GO

/* -------------------------------------------------------------------------------------
   State-aware hourly rollup.
   
   Held scans are excluded rather than averaged in, since a frozen logger contributes a
   perfectly steady value that drags the mean and standard deviation toward "very stable".
   Analogue sensors only: an hourly mean of a 0/1 valve is a duty ratio, not a
   measurement.
   ------------------------------------------------------------------------------------- */

TRUNCATE TABLE analytics.SensorHourlyStateAgg;

INSERT INTO analytics.SensorHourlyStateAgg
    (SensorId, BucketStart, OperatingState, SampleCount, MinValue, MaxValue,
     AvgValue, StdDevValue)
SELECT
    r.SensorId,
    DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0),
    st.OperatingState,
    COUNT_BIG(*),
    MIN(r.Value),
    MAX(r.Value),
    CAST(AVG(CAST(r.Value AS FLOAT)) AS REAL),
    CAST(STDEV(CAST(r.Value AS FLOAT)) AS REAL)
FROM ts.SensorReading AS r
INNER JOIN asset.Sensor        AS s  ON s.SensorId      = r.SensorId
INNER JOIN ref.QualityCode     AS q  ON q.QualityCodeId = r.QualityCodeId
INNER JOIN analytics.ScanState AS st ON st.ReadingTs    = r.ReadingTs
WHERE s.SensorClass = 'Analogue'
  AND q.IsUsable = 1
  AND r.QualityCodeId <> 65     -- UNCERTAIN_STALE: held, not measured
  AND st.IsStale = 0
GROUP BY r.SensorId, DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0), st.OperatingState;

DECLARE @StateBuckets BIGINT = (SELECT COUNT_BIG(*) FROM analytics.SensorHourlyStateAgg);
PRINT CONCAT('aggregates.sql: hourly state buckets rebuilt = ', @StateBuckets);
GO

/* -------------------------------------------------------------------------------------
   Time-weighted hourly average.
   
   A historian weights each reading by how long it held, not by how many arrived. With
   9-13 s jitter and 331 gaps the simple mean over-weights whatever the logger sampled
   densely.
   
   Each weight is the time to the next reading, clamped at the 30 s gap threshold, so the
   reading before a 48-hour gap cannot carry 48 hours of weight. LEAST() requires SQL
   Server 2022 or later.
   ------------------------------------------------------------------------------------- */

WITH weighted AS (
    SELECT
        r.SensorId,
        DATEADD(hour, DATEDIFF(hour, 0, r.ReadingTs), 0) AS BucketStart,
        CAST(r.Value AS FLOAT) AS Value,
        LEAST(
            ISNULL(DATEDIFF(second, r.ReadingTs,
                            LEAD(r.ReadingTs) OVER (PARTITION BY r.SensorId
                                                    ORDER BY r.ReadingTs)), 10),
            30
        ) AS Weight
    FROM ts.SensorReading AS r
),
bucketed AS (
    SELECT SensorId, BucketStart,
           SUM(Value * Weight) / NULLIF(SUM(Weight), 0) AS TwAvg
    FROM weighted
    GROUP BY SensorId, BucketStart
)
UPDATE a
SET TimeWeightedAvg = CAST(b.TwAvg AS REAL)
FROM analytics.SensorHourlyAgg AS a
INNER JOIN bucketed AS b
        ON b.SensorId = a.SensorId AND b.BucketStart = a.BucketStart;

DECLARE @TwRows INT = @@ROWCOUNT;
PRINT CONCAT('aggregates.sql: time-weighted averages computed for ', @TwRows, ' bucket(s)');
GO
