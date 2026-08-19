/* =====================================================================================
   Historian retrieval semantics.
   
   Two things a historian does that a plain table does not, implemented against the
   compressed archive, because that is the archive a historian would actually hold:
   interpolated retrieval at an arbitrary instant, and a summary of what compression cost.
   
   Safe to re-run.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   analytics.fn_ValueAt - the value of a tag at any instant, stored or not.
   
   The retrieval primitive a historian is built around: not "give me the rows between A
   and B", but "what was this tag reading at 14:07:33", answered from the two archived
   points that bracket it.
   
   An inline TVF, not a scalar UDF, so SQL Server folds it into the calling plan instead
   of invoking it once per row.
   
   Three behaviours: linear between bracketing points, which is the reconstruction the
   error bound was verified against; no row at all across a gap, because a line there
   would invent a measurement; and digital tags step rather than ramp.
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
   
   A view rather than a one-off query, because the difference is the argument for
   time-weighting existing at all and should be checkable at any time.
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
