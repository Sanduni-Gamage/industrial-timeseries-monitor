/* =====================================================================================
   Views.
   
   Idempotent via CREATE OR ALTER, avoiding the DROP-then-CREATE dance that briefly leaves
   dependent objects broken.
   
   Deliberately absent: any view that scans the full fact table without a predicate.
   Anything trend-shaped reads the aggregate archive; anything raw-shaped requires a time
   window from the caller.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   1. ts.vw_LatestReading - the current value of every sensor.
   
   CROSS APPLY ... TOP 1 rather than ROW_NUMBER(): with the clustered key this is one
   backward index seek per sensor, 15 in total, against ranking 22.7 million rows to
   discard all but 15.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW ts.vw_LatestReading
AS
SELECT
    e.EquipmentId,
    e.EquipmentCode,
    e.EquipmentName,
    s.SensorId,
    s.SensorCode,
    s.SensorName,
    s.SensorClass,
    s.MeasurementType,
    s.Unit,
    s.DisplayOrder,
    last_reading.ReadingTs        AS LastReadingTs,
    last_reading.Value            AS LastValue,
    q.Code                        AS QualityCode,
    q.DisplayName                 AS QualityName,
    q.IsUsable                    AS QualityIsUsable,
    DATEDIFF(second, last_reading.ReadingTs,
             (SELECT MAX(ReadingTs) FROM ts.SensorReading)) AS SecondsBehindArchive
FROM asset.Sensor AS s
INNER JOIN asset.Equipment AS e ON e.EquipmentId = s.EquipmentId
CROSS APPLY (
    SELECT TOP (1) r.ReadingTs, r.Value, r.QualityCodeId
    FROM ts.SensorReading AS r
    WHERE r.SensorId = s.SensorId
    ORDER BY r.ReadingTs DESC
) AS last_reading
INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = last_reading.QualityCodeId
WHERE s.IsActive = 1;
GO

/* -------------------------------------------------------------------------------------
   2. analytics.vw_HourlyAverage - hourly trend with coverage.
   
   CoveragePct is the honest part. A full hour is 360 samples at 10 s and this archive is
   82% covered, so an average without its sample count invites false confidence.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_HourlyAverage
AS
SELECT
    s.EquipmentId,
    a.SensorId,
    s.SensorCode,
    s.SensorName,
    s.Unit,
    a.BucketStart,
    DATEADD(hour, 1, a.BucketStart)          AS BucketEnd,
    a.SampleCount,
    a.GoodCount,
    a.MinValue,
    a.MaxValue,
    a.AvgValue,
    a.StdDevValue,
    a.FirstValue,
    a.LastValue,
    CAST(100.0 * a.SampleCount / 360.0 AS DECIMAL(5, 1)) AS CoveragePct,
    CAST(100.0 * a.GoodCount   / NULLIF(a.SampleCount, 0) AS DECIMAL(5, 1)) AS GoodPct
FROM analytics.SensorHourlyAgg AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId;
GO

/* -------------------------------------------------------------------------------------
   3. analytics.vw_DailyAverage - daily trend with coverage.
   A fully covered day is 8,640 samples at 10 s.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_DailyAverage
AS
SELECT
    s.EquipmentId,
    a.SensorId,
    s.SensorCode,
    s.SensorName,
    s.Unit,
    a.BucketDate,
    a.SampleCount,
    a.GoodCount,
    a.MinValue,
    a.MaxValue,
    a.AvgValue,
    a.StdDevValue,
    a.FirstValue,
    a.LastValue,
    CAST(100.0 * a.SampleCount / 8640.0 AS DECIMAL(5, 1)) AS CoveragePct,
    CAST(100.0 * a.GoodCount   / NULLIF(a.SampleCount, 0) AS DECIMAL(5, 1)) AS GoodPct
FROM analytics.SensorDailyAgg AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId;
GO

/* -------------------------------------------------------------------------------------
   4. ts.vw_SensorMinMax - lifetime extremes, and when they happened.
   
   Built from the daily aggregate: the extremes of the daily minima are the extremes of
   the underlying values, so this is exact and reads ~3,000 rows instead of 22.7 million.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW ts.vw_SensorMinMax
AS
SELECT
    s.EquipmentId,
    s.SensorId,
    s.SensorCode,
    s.SensorName,
    s.Unit,
    s.PhysicalMin,
    s.PhysicalMax,
    extremes.MinValue,
    extremes.MaxValue,
    extremes.MaxValue - extremes.MinValue AS ValueRange,
    extremes.FirstDate,
    extremes.LastDate,
    extremes.TotalSamples,
    min_day.BucketDate AS MinValueDate,
    max_day.BucketDate AS MaxValueDate
FROM asset.Sensor AS s
CROSS APPLY (
    SELECT MIN(a.MinValue)    AS MinValue,
           MAX(a.MaxValue)    AS MaxValue,
           MIN(a.BucketDate)  AS FirstDate,
           MAX(a.BucketDate)  AS LastDate,
           SUM(CAST(a.SampleCount AS BIGINT)) AS TotalSamples
    FROM analytics.SensorDailyAgg AS a
    WHERE a.SensorId = s.SensorId
) AS extremes
OUTER APPLY (
    SELECT TOP (1) a.BucketDate FROM analytics.SensorDailyAgg AS a
    WHERE a.SensorId = s.SensorId AND a.MinValue = extremes.MinValue
    ORDER BY a.BucketDate
) AS min_day
OUTER APPLY (
    SELECT TOP (1) a.BucketDate FROM analytics.SensorDailyAgg AS a
    WHERE a.SensorId = s.SensorId AND a.MaxValue = extremes.MaxValue
    ORDER BY a.BucketDate
) AS max_day
WHERE s.IsActive = 1;
GO

/* -------------------------------------------------------------------------------------
   5. analytics.vw_RollingStats - 24-hour rolling mean and standard deviation.
   
   Window functions over the hourly aggregate, never the raw archive: correct either way,
   usable only one way. RateOfChange is the hour-on-hour delta, which is what an operator
   means by "is it drifting?".
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_RollingStats
AS
SELECT
    a.SensorId,
    s.SensorCode,
    s.Unit,
    a.BucketStart,
    a.AvgValue,
    a.SampleCount,
    AVG(a.AvgValue) OVER (
        PARTITION BY a.SensorId ORDER BY a.BucketStart
        ROWS BETWEEN 23 PRECEDING AND CURRENT ROW
    ) AS Rolling24hMean,
    STDEV(a.AvgValue) OVER (
        PARTITION BY a.SensorId ORDER BY a.BucketStart
        ROWS BETWEEN 23 PRECEDING AND CURRENT ROW
    ) AS Rolling24hStdDev,
    a.AvgValue - LAG(a.AvgValue) OVER (
        PARTITION BY a.SensorId ORDER BY a.BucketStart
    ) AS RateOfChangePerHour,
    COUNT(*) OVER (
        PARTITION BY a.SensorId ORDER BY a.BucketStart
        ROWS BETWEEN 23 PRECEDING AND CURRENT ROW
    ) AS WindowBucketCount
FROM analytics.SensorHourlyAgg AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId;
GO

/* -------------------------------------------------------------------------------------
   6. analytics.vw_AbnormalReading - hourly buckets outside their baseline.
   
   Compares against analytics.SensorBaseline, so a threshold is always a stored,
   reproducible number. Returns nothing until baselines exist, which is correct: no
   baseline means no defensible notion of "abnormal".
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW analytics.vw_AbnormalReading
AS
SELECT
    a.SensorId,
    s.SensorCode,
    s.SensorName,
    s.Unit,
    a.BucketStart,
    a.OperatingState,
    a.AvgValue,
    a.MinValue,
    a.MaxValue,
    a.SampleCount,
    b.BaselineName,
    b.IqrLowerFence AS ExpectedLow,
    b.IqrUpperFence AS ExpectedHigh,
    CASE
        WHEN a.AvgValue < b.IqrLowerFence THEN b.IqrLowerFence - a.AvgValue
        ELSE a.AvgValue - b.IqrUpperFence
    END AS ExceedanceAmount,
    CASE
        WHEN b.StdDevValue > 0
        THEN ABS(a.AvgValue - b.MeanValue) / b.StdDevValue
        ELSE NULL
    END AS ZScore,
    CASE
        WHEN b.StdDevValue > 0 AND ABS(a.AvgValue - b.MeanValue) / b.StdDevValue >= 5 THEN 'CRITICAL'
        WHEN b.StdDevValue > 0 AND ABS(a.AvgValue - b.MeanValue) / b.StdDevValue >= 3 THEN 'WARNING'
        ELSE 'NORMAL'
    END AS Severity
/* State-aware on both sides. The first draft of this view joined a whole-hour average to
   an 'ALL' baseline, which Phase 3 established is not a meaningful comparison on a
   duty-cycled machine: the hourly mean blends OFF and LOADED behaviour, and a global
   baseline describes neither. Both sides are now partitioned by operating state.
   See docs/ANALYTICS_FINDINGS.md. */
FROM analytics.SensorHourlyStateAgg AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
INNER JOIN analytics.SensorBaseline AS b
        ON b.SensorId = a.SensorId
       AND b.OperatingState = a.OperatingState
WHERE (b.P75 - b.P25) > 0            -- skip degenerate baselines; their fences collapse
  AND (a.AvgValue < b.IqrLowerFence OR a.AvgValue > b.IqrUpperFence);
GO

/* -------------------------------------------------------------------------------------
   7. asset.vw_EquipmentHealth - one row per machine, for the Overview screen.

   Health is expressed as a single worst-case severity plus the counts that justify it,
   so the UI never has to say "CRITICAL" without being able to show why.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW asset.vw_EquipmentHealth
AS
SELECT
    e.EquipmentId,
    e.EquipmentCode,
    e.EquipmentName,
    e.EquipmentType,
    e.Location,
    counts.SensorCount,
    archive.LastReadingTs,
    archive.TotalReadings,
    ISNULL(anomalies.CriticalCount, 0) AS CriticalAnomalies,
    ISNULL(anomalies.WarningCount, 0)  AS WarningAnomalies,
    ISNULL(failures.FailureCount, 0)   AS RecordedFailures,
    CASE
        WHEN archive.LastReadingTs IS NULL           THEN 'NO DATA'
        WHEN ISNULL(anomalies.CriticalCount, 0) > 0  THEN 'CRITICAL'
        WHEN ISNULL(anomalies.WarningCount, 0)  > 0  THEN 'WARNING'
        ELSE 'NORMAL'
    END AS HealthStatus
FROM asset.Equipment AS e
CROSS APPLY (
    SELECT COUNT(*) AS SensorCount
    FROM asset.Sensor AS s WHERE s.EquipmentId = e.EquipmentId AND s.IsActive = 1
) AS counts
CROSS APPLY (
    SELECT MAX(v.LastReadingTs) AS LastReadingTs,
           SUM(CAST(d.TotalSamples AS BIGINT)) AS TotalReadings
    FROM ts.vw_LatestReading AS v
    OUTER APPLY (
        SELECT SUM(CAST(a.SampleCount AS BIGINT)) AS TotalSamples
        FROM analytics.SensorDailyAgg AS a WHERE a.SensorId = v.SensorId
    ) AS d
    WHERE v.EquipmentId = e.EquipmentId
) AS archive
OUTER APPLY (
    /* ACTIVE anomalies only - the last 24 hours of the archive, not its whole history. The
   first version counted every anomaly ever detected, so a machine that had one bad
   afternoon in March showed as CRITICAL forever.
   
   "Now" is the archive's last reading rather than the wall clock, because against the
   wall clock every reading is stale and the panel would be permanently blank. */
    SELECT SUM(CASE WHEN an.Severity = 'CRITICAL' THEN 1 ELSE 0 END) AS CriticalCount,
           SUM(CASE WHEN an.Severity = 'WARNING'  THEN 1 ELSE 0 END) AS WarningCount
    FROM analytics.Anomaly AS an
    INNER JOIN asset.Sensor AS s2 ON s2.SensorId = an.SensorId
    WHERE s2.EquipmentId = e.EquipmentId
      AND an.ReadingTs >= DATEADD(hour, -24, archive.LastReadingTs)
) AS anomalies
OUTER APPLY (
    SELECT COUNT(*) AS FailureCount
    FROM ops.FailureEvent AS f WHERE f.EquipmentId = e.EquipmentId
) AS failures;
GO

/* -------------------------------------------------------------------------------------
   8. ops.vw_DataQualitySummary - issue counts per ingestion run.
   
   Two kinds of row live in ops.DataQualityIssue and must never be added together. A
   detail row describes one event; a summary row carries the run total for its issue type.
   Summing both double-counts: a run with one 6-row flatline reported 12.
   
   AffectedRows therefore comes from summary rows only, with detail rows counted
   separately. The IsSummary flag exists to keep them apart.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW ops.vw_DataQualitySummary
AS
SELECT
    r.IngestionRunId,
    r.SourceFile,
    r.SourceSha256,
    r.Status,
    r.StartedUtc,
    r.CompletedUtc,
    DATEDIFF(second, r.StartedUtc, r.CompletedUtc) AS DurationSeconds,
    r.RowsRead,
    r.RowsInserted,
    r.RowsSkippedDup,
    r.RowsRejected,
    r.MinReadingTs,
    r.MaxReadingTs,
    i.IssueType,
    i.Severity,
    /* Exact total for the issue type, taken from the summary row alone. */
    SUM(CASE WHEN i.IsSummary = 1 THEN CAST(i.AffectedRows AS BIGINT) ELSE 0 END)
        AS AffectedRows,
    /* How many individual events were recorded, which may be capped at 1,000. */
    SUM(CASE WHEN i.IsSummary = 0 THEN 1 ELSE 0 END) AS DetailRows
FROM ops.IngestionRun AS r
LEFT JOIN ops.DataQualityIssue AS i ON i.IngestionRunId = r.IngestionRunId
GROUP BY
    r.IngestionRunId, r.SourceFile, r.SourceSha256, r.Status, r.StartedUtc, r.CompletedUtc,
    r.RowsRead, r.RowsInserted, r.RowsSkippedDup, r.RowsRejected,
    r.MinReadingTs, r.MaxReadingTs, i.IssueType, i.Severity;
GO

/* -------------------------------------------------------------------------------------
   9. ops.vw_TimestampGap - where the archive has no data.
   
   Reads the GAP issues recorded at ingestion rather than re-deriving them with LAG().
   Finding them again per dashboard load would be a full scan for a known answer.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW ops.vw_TimestampGap
AS
SELECT
    i.IssueId,
    i.IngestionRunId,
    i.WindowStartTs   AS GapStart,
    i.WindowEndTs     AS GapEnd,
    i.ObservedValue   AS GapSeconds,
    CAST(i.ObservedValue / 3600.0 AS DECIMAL(10, 2)) AS GapHours,
    i.AffectedRows    AS EstimatedMissingScans,
    i.Details
FROM ops.DataQualityIssue AS i
WHERE i.IssueType = 'GAP'
  AND i.IsSummary = 0
  AND i.WindowStartTs IS NOT NULL
  /* Scoped to the most recent FULL run. PARTIAL runs are --limit-days dev subsets and must
   not define the archive's quality picture: after one 1-day test run the report claimed
   the whole archive had 2 gaps.
   
   Scoped to a single run because each run re-detects the same gaps in the same file.
   Without this the archive appears to gain 331 new gaps every time ingestion runs. */
  AND i.IngestionRunId = (
        SELECT MAX(IngestionRunId) FROM ops.IngestionRun
        WHERE Status = 'SUCCEEDED'
  );
GO

/* -------------------------------------------------------------------------------------
   10. ops.vw_FailureContext - failure events with their surrounding data coverage.
   
   Answers the question that precedes any pre-failure analysis: is there enough usable
   data in the run-up to say anything? For event #1, whose 24-hour lead-up is 69.5% frozen
   data, the answer is no. A view makes that hard to forget.
   ------------------------------------------------------------------------------------- */

CREATE OR ALTER VIEW ops.vw_FailureContext
AS
SELECT
    f.FailureEventId,
    f.EquipmentId,
    e.EquipmentCode,
    f.StartTs,
    f.EndTs,
    f.FailureType,
    f.Severity,
    f.SourceReference,
    f.ReportNote,
    f.DataQualityNote,
    DATEDIFF(minute, f.StartTs, f.EndTs) AS DurationMinutes,
    lead_up.TotalScans   AS LeadUp24hScans,
    lead_up.StaleScans   AS LeadUp24hStaleScans,
    CAST(100.0 * lead_up.StaleScans / NULLIF(lead_up.TotalScans, 0) AS DECIMAL(5, 1))
        AS LeadUp24hStalePct,
    CASE WHEN lead_up.TotalScans = 0 THEN 'NO DATA'
         WHEN 100.0 * lead_up.StaleScans / NULLIF(lead_up.TotalScans, 0) > 25 THEN 'UNUSABLE'
         ELSE 'USABLE'
    END AS LeadUpUsability
FROM ops.FailureEvent AS f
INNER JOIN asset.Equipment AS e ON e.EquipmentId = f.EquipmentId
CROSS APPLY (
    /* One representative analogue tag is enough to measure scan coverage: the freeze
       affects every tag in the same scan, so counting one avoids a 15x fan-out. */
    SELECT COUNT_BIG(*) AS TotalScans,
           SUM(CASE WHEN r.QualityCodeId = 65 THEN 1 ELSE 0 END) AS StaleScans
    FROM ts.SensorReading AS r
    WHERE r.SensorId = (SELECT SensorId FROM asset.Sensor WHERE SensorCode = 'TP3')
      AND r.ReadingTs >= DATEADD(hour, -24, f.StartTs)
      AND r.ReadingTs <  f.StartTs
) AS lead_up;
GO

PRINT 'views.sql: views are present.';
GO
