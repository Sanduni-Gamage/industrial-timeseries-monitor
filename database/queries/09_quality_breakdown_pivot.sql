/* =====================================================================================
   Q9 - Sensor quality-code breakdown per day (PIVOT).

   Purpose      A per-day, per-sensor view of how trustworthy the archive is. This is the
                query behind the dashboard's Data Quality screen.

   Why it matters here: the source metadata declares has_missing_values = "no", and
   cell-wise that is true - there is not one null in 22.7 million readings. But 50,855
   of them are held values from a frozen logger. Completeness is not correctness, and
   this breakdown is where the difference becomes visible.

   Technique    PIVOT with a fixed column list. The alternative - conditional aggregation
                with SUM(CASE WHEN ...) - is equivalent and often clearer; PIVOT is shown
                because the fixed set of quality codes is exactly the case it suits.
   ===================================================================================== */

DECLARE @From DATETIME2(3) = '2020-06-20T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-06-27T00:00:00';

SELECT
    SensorCode,
    BucketDate,
    ISNULL([GOOD], 0)            AS Good,
    ISNULL([UNCERTAIN_STALE], 0) AS HeldValue,
    ISNULL([UNCERTAIN_RANGE], 0) AS OutOfBaseline,
    ISNULL([BAD_RANGE], 0)       AS Impossible,
    ISNULL([BAD_DIGITAL], 0)     AS NonBinary,
    ISNULL([BAD_MISSING], 0)     AS Missing,
    CAST(100.0 * ISNULL([UNCERTAIN_STALE], 0)
         / NULLIF(ISNULL([GOOD],0) + ISNULL([UNCERTAIN_STALE],0)
                  + ISNULL([UNCERTAIN_RANGE],0) + ISNULL([BAD_RANGE],0)
                  + ISNULL([BAD_DIGITAL],0) + ISNULL([BAD_MISSING],0), 0)
         AS DECIMAL(5,1)) AS HeldPct
FROM (
    SELECT
        s.SensorCode,
        CAST(r.ReadingTs AS DATE) AS BucketDate,
        q.Code                    AS QualityCode
    FROM ts.SensorReading AS r
    INNER JOIN asset.Sensor    AS s ON s.SensorId      = r.SensorId
    INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
    WHERE r.ReadingTs >= @From AND r.ReadingTs < @To
) AS src
PIVOT (
    COUNT(QualityCode)
    FOR QualityCode IN ([GOOD], [UNCERTAIN_STALE], [UNCERTAIN_RANGE],
                        [BAD_RANGE], [BAD_DIGITAL], [BAD_MISSING])
) AS p
ORDER BY BucketDate, SensorCode;
