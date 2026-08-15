/* =====================================================================================
   Q4 - Anomaly retrieval, ranked, with the metadata needed to act on it.

   Purpose      Feeds the dashboard's anomaly table. An operator needs the value, what
                was expected, how far outside it fell, and which rule said so - a bare
                "anomaly" row is not actionable.

   Access path  IX_Anomaly_Ts_Severity (ReadingTs DESC, Severity) INCLUDE(...) is
                covering for this query, so it never touches the base table.

   Note         Method is part of the anomaly table's unique key on purpose. The same
                instant can legitimately be flagged by the rolling z-score and by the IQR
                rule, and collapsing them would hide which rule is actually firing.

   Returns nothing until Phase 3 populates analytics.Anomaly. That is correct: with no
   computed baseline there is no defensible notion of "abnormal".
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
