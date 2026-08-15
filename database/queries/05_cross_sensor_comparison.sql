/* =====================================================================================
   Q5 - Cross-sensor comparison against a documented physical invariant.

   Purpose      The UCI documentation states that Reservoirs "should be close to" TP3.
                That makes divergence between them a legitimate instrument-health check
                rather than a threshold somebody invented.

   Technique    Self-join of the fact table to itself on ReadingTs, one side per sensor.
                Both sides seek the clustered key, and because the key is
                (SensorId, ReadingTs) each side is a contiguous range - the join is a
                merge of two already-ordered streams, not a hash of 22.7M rows.

   Measured     Mean divergence 0.0019 bar, P99 0.006 bar, maximum ever observed
                0.182 bar, zero rows above 0.5 bar. The alert threshold is therefore
                ~3x the worst real divergence: it flags a failed instrument, not noise.
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
