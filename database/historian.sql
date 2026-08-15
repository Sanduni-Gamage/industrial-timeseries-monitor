/* =====================================================================================
   Historian retrieval semantics.

   Two things a historian does that a plain table does not, implemented against the
   COMPRESSED archive - because that is the archive a historian would actually hold.

     1. Interpolated retrieval at an arbitrary instant
     2. A summary of what compression cost and what it bought

   Safe to re-run.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   analytics.fn_ValueAt - the value of a tag at any instant, stored or not.

   This is the retrieval primitive a historian is built around. You do not ask "give me
   the rows between A and B"; you ask "what was this tag reading at 14:07:33", and the
   system answers from the two archived points that bracket it.

   An inline table-valued function, not a scalar UDF: SQL Server can fold a TVF into the
   calling query's plan, whereas a scalar UDF is invoked once per row and kills
   parallelism.

   Three behaviours worth stating:

   * **Linear between bracketing points.** This is exactly the reconstruction the
     compression error bound was verified against, so any value returned here is
     guaranteed within the configured deviation of what was originally measured.
   * **Nothing is returned across a gap.** If the bracketing points are further apart than
     the gap threshold, the function returns no row. Drawing a line across a 48-hour hole
     would invent a measurement, and every other part of this project refuses to do that.
   * **Digital tags step, they do not ramp.** A valve is open or shut; interpolating it to
     0.63 is meaningless, so the previous value is held instead.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER FUNCTION analytics.fn_ValueAt
(
    @SensorId   SMALLINT,
    @At         DATETIME2(3),
    @GapSeconds INT
)
RETURNS TABLE
AS
RETURN
(
    WITH bracket AS (
        SELECT
            prev.ReadingTs     AS PrevTs,
            prev.Value         AS PrevValue,
            nxt.ReadingTs      AS NextTs,
            nxt.Value          AS NextValue,
            s.SensorClass
        FROM asset.Sensor AS s
        OUTER APPLY (
            SELECT TOP (1) c.ReadingTs, c.Value
            FROM ts.SensorReadingCompressed AS c
            WHERE c.SensorId = @SensorId AND c.ReadingTs <= @At
            ORDER BY c.ReadingTs DESC
        ) AS prev
        OUTER APPLY (
            SELECT TOP (1) c.ReadingTs, c.Value
            FROM ts.SensorReadingCompressed AS c
            WHERE c.SensorId = @SensorId AND c.ReadingTs > @At
            ORDER BY c.ReadingTs
        ) AS nxt
        WHERE s.SensorId = @SensorId
    )
    SELECT
        @At AS RequestedTs,
        CASE
            WHEN PrevTs = @At            THEN PrevValue   -- exactly on a stored point
            WHEN SensorClass = 'Digital' THEN PrevValue   -- step, never ramp
            WHEN NextTs IS NULL          THEN PrevValue   -- end of archive; hold, never extrapolate
            ELSE PrevValue
                 + (NextValue - PrevValue)
                   * (DATEDIFF(millisecond, PrevTs, @At) * 1.0
                      / NULLIF(DATEDIFF(millisecond, PrevTs, NextTs), 0))
        END AS Value,
        CASE
            WHEN PrevTs = @At            THEN 'EXACT'
            WHEN SensorClass = 'Digital' THEN 'STEP'
            WHEN NextTs IS NULL          THEN 'HELD_END_OF_ARCHIVE'
            ELSE 'INTERPOLATED'
        END AS Method,
        PrevTs,
        NextTs,
        DATEDIFF(second, PrevTs, NextTs) AS BracketSeconds
    FROM bracket
    WHERE PrevTs IS NOT NULL
      /* Refuse to answer across a gap. Returning no row is honest; a line is not. */
      AND (NextTs IS NULL
           OR DATEDIFF(second, PrevTs, NextTs) <= @GapSeconds
           OR SensorClass = 'Digital')
);
GO

/* -------------------------------------------------------------------------------------
   analytics.vw_CompressionSummary - what compression cost and what it bought.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_CompressionSummary
AS
SELECT
    s.SensorCode,
    s.SensorName,
    s.Unit,
    c.DeviationPct,
    c.Deviation,
    c.OriginalCount,
    c.KeptCount,
    CAST(c.OriginalCount * 1.0 / NULLIF(c.KeptCount, 0) AS DECIMAL(10, 2)) AS CompressionRatio,
    CAST(100.0 * c.KeptCount / NULLIF(c.OriginalCount, 0) AS DECIMAL(6, 3)) AS RetainedPct,
    c.MaxError,
    c.MeanError,
    /* The guarantee expressed as the fraction of the allowance actually used.
       Anything above 1.0 means the bound was violated and the code is wrong. */
    CAST(c.MaxError / NULLIF(c.Deviation, 0) AS DECIMAL(10, 4)) AS ErrorBudgetUsed,
    c.WithinBound,
    c.ComputedUtc
FROM analytics.CompressionStat AS c
INNER JOIN asset.Sensor AS s ON s.SensorId = c.SensorId;
GO

/* -------------------------------------------------------------------------------------
   analytics.vw_AggregateComparison - simple vs time-weighted mean, side by side.

   Kept as a view rather than a one-off query because the difference is the argument for
   time-weighting existing at all, and it should be checkable at any time rather than
   quoted from a write-up.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_AggregateComparison
AS
SELECT
    s.SensorCode,
    s.Unit,
    a.BucketStart,
    a.SampleCount,
    a.AvgValue                                  AS SimpleMean,
    a.TimeWeightedAvg                           AS TimeWeightedMean,
    a.TimeWeightedAvg - a.AvgValue              AS Difference,
    CASE WHEN a.AvgValue <> 0
         THEN CAST(100.0 * ABS(a.TimeWeightedAvg - a.AvgValue) / ABS(a.AvgValue)
                   AS DECIMAL(10, 4))
    END                                         AS DifferencePct
FROM analytics.SensorHourlyAgg AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
WHERE a.TimeWeightedAvg IS NOT NULL;
GO

PRINT 'historian.sql: retrieval semantics are present.';
GO
