/* =====================================================================================
   Q6 - Latest value per sensor, written two ways.

   Purpose      A concrete example of why the shape of a query matters more than its
                correctness once a table has 22.7 million rows. Both forms below return
                identical results.

   Form A (used in ts.vw_LatestReading): CROSS APPLY ... TOP 1 ORDER BY ReadingTs DESC.
   With the clustered key (SensorId, ReadingTs) this is one BACKWARD index seek per
   sensor. Fifteen sensors, fifteen seeks, a handful of pages read.

   Form B: ROW_NUMBER() OVER (PARTITION BY SensorId ORDER BY ReadingTs DESC). This must
   rank every row in the table to discard all but fifteen of them.

   The optimiser cannot rewrite B into A: the window function is defined over the whole
   partition, so the ranking is semantically required. Choosing the access pattern is
   the engineer's job, not the optimiser's.

   Run both with SET STATISTICS IO ON to see the difference in logical reads.
   ===================================================================================== */

-- ---- Form A: seek per sensor (preferred) --------------------------------------------
SELECT
    s.SensorCode,
    latest.ReadingTs,
    latest.Value,
    s.Unit,
    q.Code AS Quality
FROM asset.Sensor AS s
CROSS APPLY (
    SELECT TOP (1) r.ReadingTs, r.Value, r.QualityCodeId
    FROM ts.SensorReading AS r
    WHERE r.SensorId = s.SensorId
    ORDER BY r.ReadingTs DESC
) AS latest
INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = latest.QualityCodeId
WHERE s.IsActive = 1
ORDER BY s.DisplayOrder;

-- ---- Form B: rank everything, keep 15 (shown for contrast; do not ship this) ---------
/*
WITH ranked AS (
    SELECT r.SensorId, r.ReadingTs, r.Value, r.QualityCodeId,
           ROW_NUMBER() OVER (PARTITION BY r.SensorId ORDER BY r.ReadingTs DESC) AS rn
    FROM ts.SensorReading AS r
)
SELECT s.SensorCode, ranked.ReadingTs, ranked.Value, s.Unit
FROM ranked
INNER JOIN asset.Sensor AS s ON s.SensorId = ranked.SensorId
WHERE ranked.rn = 1
ORDER BY s.DisplayOrder;
*/
