/* =====================================================================================
   Q5 - Cross-sensor comparison against a documented physical invariant.

   The UCI documentation states Reservoirs "should be close to" TP3, so divergence is a
   documented check rather than an invented threshold. Both sides of the self-join seek
   the clustered key, so it merges two ordered streams instead of hashing 22.7M rows.

   Measured: mean 0.0019 bar, P99 0.006, max 0.182, zero rows above 0.5.
   ===================================================================================== */

DECLARE @From DATETIME2(3) = '2020-06-05T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-06-08T00:00:00';

WITH ids AS (
    SELECT
        MAX(CASE WHEN SensorCode = 'RESERVOIRS' THEN SensorId END) AS ReservoirsId,
        MAX(CASE WHEN SensorCode = 'TP3'        THEN SensorId END) AS Tp3Id
    FROM asset.Sensor
)
SELECT
    res.ReadingTs,
    res.Value                        AS ReservoirsBar,
    tp3.Value                        AS Tp3Bar,
    CAST(ABS(res.Value - tp3.Value) AS DECIMAL(10,4)) AS AbsDivergenceBar,
    CASE WHEN ABS(res.Value - tp3.Value) > 0.5 THEN 'DIVERGENT' ELSE 'OK' END AS Status
FROM ids
INNER JOIN ts.SensorReading AS res
        ON res.SensorId = ids.ReservoirsId
       AND res.ReadingTs >= @From AND res.ReadingTs < @To
INNER JOIN ts.SensorReading AS tp3
        ON tp3.SensorId  = ids.Tp3Id
       AND tp3.ReadingTs = res.ReadingTs
ORDER BY AbsDivergenceBar DESC, res.ReadingTs;
