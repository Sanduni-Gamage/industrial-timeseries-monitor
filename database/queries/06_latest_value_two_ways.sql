/* =====================================================================================
   Q6 - Latest value per sensor, written two ways.

   Form A (CROSS APPLY ... TOP 1 DESC) is one backward index seek per sensor. Form B
   (ROW_NUMBER) must rank every row to discard all but fifteen. The optimiser cannot
   rewrite B into A, because the window function is defined over the whole partition.

   Run both with SET STATISTICS IO ON to see the difference.
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
