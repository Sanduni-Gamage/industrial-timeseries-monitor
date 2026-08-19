/* =====================================================================================
   Q4 - Anomaly retrieval, ranked, with the metadata needed to act on it.

   IX_Anomaly_Ts_Severity is covering here, so the base table is never touched. Method
   is part of the unique key on purpose: the same instant can be flagged by two rules,
   and collapsing them would hide which one is firing.
   ===================================================================================== */

DECLARE @From DATETIME2(3) = '2020-06-01T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-07-01T00:00:00';
DECLARE @MinSeverity VARCHAR(12) = 'WARNING';

SELECT TOP (200)
    a.ReadingTs,
    e.EquipmentCode,
    s.SensorCode,
    s.SensorName,
    a.Value,
    s.Unit,
    a.ExpectedLow,
    a.ExpectedHigh,
    a.Method,
    a.OperatingState,
    CAST(a.Score AS DECIMAL(10,3)) AS AnomalyScore,
    a.Severity
FROM analytics.Anomaly AS a
INNER JOIN asset.Sensor    AS s ON s.SensorId    = a.SensorId
INNER JOIN asset.Equipment AS e ON e.EquipmentId = s.EquipmentId
WHERE a.ReadingTs >= @From AND a.ReadingTs < @To
  AND a.Severity IN (SELECT Severity FROM (VALUES ('WARNING'), ('CRITICAL')) AS v(Severity)
                     WHERE @MinSeverity = 'WARNING' OR v.Severity = 'CRITICAL')
ORDER BY
    CASE a.Severity WHEN 'CRITICAL' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END,
    a.Score DESC,
    a.ReadingTs DESC;
