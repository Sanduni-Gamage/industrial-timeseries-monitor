/* =====================================================================================
   Q10 - Failure correlation: sensor behaviour before each documented event.

   Compares the 24 / 12 / 6 / 1 hour run-up to each air-leak event against a
   failure-free reference window (February 2020).

   69.5% of event #1's 24-hour lead-up is held data from a frozen logger, so this query
   counts usable against held scans, computes statistics over usable readings only, and
   labels any window under 50% usable as INSUFFICIENT rather than returning a
   comparable-looking average.

   Three usable events. A descriptive comparison, not a validated model.
   ===================================================================================== */

DECLARE @SensorCode VARCHAR(32) = 'TP2';
DECLARE @SensorId SMALLINT = (SELECT SensorId FROM asset.Sensor WHERE SensorCode = @SensorCode);

WITH windows AS (
    -- One row per (event, lead-up horizon). CROSS JOIN builds the 4 x 4 grid.
    SELECT
        f.FailureEventId,
        f.SourceReference,
        f.StartTs,
        h.Hours,
        DATEADD(hour, -h.Hours, f.StartTs) AS WindowStart,
        f.StartTs                          AS WindowEnd
    FROM ops.FailureEvent AS f
    CROSS JOIN (VALUES (24), (12), (6), (1)) AS h(Hours)
),
stats AS (
    SELECT
        w.FailureEventId,
        w.SourceReference,
        w.StartTs,
        w.Hours,
        COUNT_BIG(*)                                                AS TotalScans,
        SUM(CASE WHEN r.QualityCodeId = 65 THEN 1 ELSE 0 END)       AS HeldScans,
        SUM(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> 65
                 THEN 1 ELSE 0 END)                                 AS UsableScans,
        AVG(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> 65
                 THEN CAST(r.Value AS FLOAT) END)                   AS UsableMean,
        STDEV(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> 65
                   THEN CAST(r.Value AS FLOAT) END)                 AS UsableStdDev,
        MAX(CASE WHEN q.IsUsable = 1 AND r.QualityCodeId <> 65
                 THEN r.Value END)                                  AS UsableMax
    FROM windows AS w
    INNER JOIN ts.SensorReading AS r
            ON r.SensorId = @SensorId
           AND r.ReadingTs >= w.WindowStart AND r.ReadingTs < w.WindowEnd
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    GROUP BY w.FailureEventId, w.SourceReference, w.StartTs, w.Hours
),
baseline AS (
    -- Failure-free reference: February 2020, usable readings only.
    SELECT
        AVG(CAST(r.Value AS FLOAT))   AS BaselineMean,
        STDEV(CAST(r.Value AS FLOAT)) AS BaselineStdDev
    FROM ts.SensorReading AS r
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    WHERE r.SensorId = @SensorId
      AND r.ReadingTs >= '2020-02-01' AND r.ReadingTs < '2020-03-01'
      AND q.IsUsable = 1 AND r.QualityCodeId <> 65
)
SELECT
    s.SourceReference,
    s.StartTs                     AS FailureStart,
    s.Hours                       AS LeadUpHours,
    s.TotalScans,
    s.HeldScans,
    CAST(100.0 * s.UsableScans / NULLIF(s.TotalScans, 0) AS DECIMAL(5,1)) AS UsablePct,
    CASE WHEN 100.0 * s.UsableScans / NULLIF(s.TotalScans, 0) < 50
         THEN NULL ELSE CAST(s.UsableMean AS DECIMAL(10,4)) END           AS LeadUpMean,
    CAST(b.BaselineMean AS DECIMAL(10,4))                                 AS BaselineMean,
    CASE WHEN 100.0 * s.UsableScans / NULLIF(s.TotalScans, 0) < 50 THEN NULL
         ELSE CAST((s.UsableMean - b.BaselineMean)
                   / NULLIF(b.BaselineStdDev, 0) AS DECIMAL(10,3)) END    AS ZVsBaseline,
    CASE WHEN 100.0 * s.UsableScans / NULLIF(s.TotalScans, 0) < 50
         THEN 'INSUFFICIENT DATA' ELSE 'COMPARABLE' END                   AS Verdict
FROM stats AS s
CROSS JOIN baseline AS b
ORDER BY s.StartTs, s.Hours DESC;
