/* =====================================================================================
   Q1 - Time-window retrieval for one sensor, with quality filtering.

   Clustered index seek on (SensorId, ReadingTs): one contiguous range read, no sort.
   The join to ref.QualityCode is what stops a held value being silently averaged into
   an operator's decision.
   ===================================================================================== */

DECLARE @SensorCode VARCHAR(32) = 'TP3';
DECLARE @From DATETIME2(3) = '2020-06-05T00:00:00';
DECLARE @To   DATETIME2(3) = '2020-06-06T00:00:00';

SELECT
    r.ReadingTs,
    r.Value,
    s.Unit,
    q.Code        AS Quality,
    q.IsUsable
FROM ts.SensorReading AS r
INNER JOIN asset.Sensor     AS s ON s.SensorId      = r.SensorId
INNER JOIN ref.QualityCode  AS q ON q.QualityCodeId = r.QualityCodeId
WHERE s.SensorCode = @SensorCode
  AND r.ReadingTs >= @From
  AND r.ReadingTs <  @To          -- half-open interval: no double-counting at boundaries
ORDER BY r.ReadingTs;
