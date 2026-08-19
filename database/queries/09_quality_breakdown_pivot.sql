/* =====================================================================================
   Q9 - Sensor quality-code breakdown per day (PIVOT).

   The source declares has_missing_values = "no" and cell-wise that is true, but 50,855
   readings are held values from a frozen logger. Completeness is not correctness, and
   this is where the difference becomes visible.

   PIVOT with a fixed column list, which is the case it suits.
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
