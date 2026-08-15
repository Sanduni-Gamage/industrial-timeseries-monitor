/* =====================================================================================
   Rebuild the aggregate archive: hourly and daily rollups.

   This is the "aggregate archive" half of the pattern process historians use. The raw
   archive (ts.SensorReading) keeps every scan; the aggregate archive keeps pre-computed
   summaries so that a dashboard asking for six months of trend does not scan 22.7
   million rows to draw a line 4,000 pixels wide.

   Full rebuild rather than incremental. At this data volume it takes seconds, and a
   rebuild cannot drift out of step with the raw archive the way an incremental update
   can after a late-arriving backfill. Revisit if the raw archive grows an order of
   magnitude.

   Safe to re-run at any time.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   Hourly rollup

   FirstValue and LastValue are resolved with CROSS APPLY rather than FIRST_VALUE /
   LAST_VALUE window functions. The window form would sort all 22.7M rows, which on
   Express (capped at ~1.4 GB buffer pool) spills to tempdb. The APPLY form does two
   index seeks per bucket instead - roughly 76,000 buckets, ~150,000 seeks, each landing
   directly on the clustered key. Measurably faster and it does not spill.

   GoodCount is carried alongside SampleCount so the UI can show coverage. An hourly
   average built from 12 samples is not the same number as one built from 360, and the
   dashboard should be able to say so rather than drawing both as a confident line.

   Value is CAST to FLOAT before averaging: summing tens of thousands of REAL values
   accumulates visible error in 4-byte floating point.
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

   Held scans are excluded outright rather than averaged in: a frozen logger would
   otherwise contribute a perfectly steady value that drags both the mean and the
   standard deviation toward "this machine is very stable".

   Analogue sensors only. An hourly mean of a 0/1 valve signal is a duty ratio, not a
   measurement, and feeding it to a z-score detector produces confident nonsense.
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

   A historian weights each reading by HOW LONG IT HELD, not by how many samples arrived.
   The two differ whenever sampling is irregular - this archive has 9-13 s jitter and 331
   gaps - and the simple mean is the one that is quietly wrong, because it over-weights
   whatever the logger happened to sample densely.

   Each reading's weight is the time until the next one, CLAMPED at the 30 s gap
   threshold. Without the clamp the single reading before a 48-hour gap would carry 48
   hours of weight and dominate its bucket entirely. Clamping is what a historian does
   under a "maximum interval" setting; the alternative is a number that says more about
   the outage than about the machine.

   LEAST() requires SQL Server 2022 or later.
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
