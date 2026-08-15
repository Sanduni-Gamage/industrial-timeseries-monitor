/* =====================================================================================
   Q10 - Failure correlation: sensor behaviour before each documented event.

   Purpose      Compare the 24 / 12 / 6 / 1 hour run-up to each documented air-leak
                event against a failure-free reference window, to see whether any signal
                moves before the failure is reported.

   The honest part, and the reason this query is shaped the way it is:

   Profiling found that 69.5% of the 24-hour lead-up to event #1 is HELD data from a
   frozen logger. Averaging it would produce a confident number describing an
   instrumentation fault rather than a compressor fault. So this query:

     - counts usable vs held scans in every window, and reports both;
     - computes statistics over usable readings only (q.IsUsable = 1 AND not held);
     - labels any window with under 50% usable data as INSUFFICIENT rather than
       returning an average that looks comparable to the others.

   With four documented events, only three of which have usable lead-up data, this is a
   descriptive comparison. It is not, and is not presented as, a validated predictive
   model - n=3 cannot support that claim.

   Reference window: February 2020, which precedes all four events and contains no
   frozen block longer than two scans.
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
