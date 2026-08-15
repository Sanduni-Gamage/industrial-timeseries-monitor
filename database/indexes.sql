/* =====================================================================================
   Secondary indexes.

   Applied AFTER ingestion, not before. Maintaining a columnstore index during a
   22.7-million-row load costs far more than building it once at the end, so
   scripts/init_database.py leaves this file until the data is in place.

   Idempotent: every index is guarded, so re-running is a no-op.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   ts.SensorReading

   The clustered primary key (SensorId, ReadingTs) already serves the dominant query
   shape: "one sensor, one time window". These two indexes serve the other two shapes.
   ------------------------------------------------------------------------------------- */

/* Cross-sensor analytical scans: hourly rollup builds, whole-file statistics, anomaly
   sweeps. A columnstore index stores each column separately and compresses it heavily,
   which turns an aggregate over tens of millions of rows from a table scan into a
   segment scan with batch-mode execution.

   Available in Express since SQL Server 2016 SP1 - no paid edition required. */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'NCCX_SensorReading'
                 AND object_id = OBJECT_ID('ts.SensorReading'))
BEGIN
    PRINT 'Building nonclustered columnstore index on ts.SensorReading (this takes a while)...';
    CREATE NONCLUSTERED COLUMNSTORE INDEX NCCX_SensorReading
        ON ts.SensorReading (SensorId, ReadingTs, Value, QualityCodeId);
END
GO

/* "What did every sensor read at this moment?" - the pivot query behind the Overview
   screen. The clustered key is tag-major, so a time-only predicate cannot seek it; this
   index gives a time-ordered path. INCLUDE makes it covering, so the query never has to
   return to the base table. */
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_SensorReading_Ts'
                 AND object_id = OBJECT_ID('ts.SensorReading'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_SensorReading_Ts
        ON ts.SensorReading (ReadingTs)
        INCLUDE (SensorId, Value, QualityCodeId)
        WITH (DATA_COMPRESSION = PAGE);
END
GO

/* -------------------------------------------------------------------------------------
   analytics.Anomaly

   The anomaly table is read two ways: "most recent, worst first" for the dashboard, and
   "everything for this sensor over this window" for the trend chart overlay.
   ------------------------------------------------------------------------------------- */

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Anomaly_Ts_Severity'
                 AND object_id = OBJECT_ID('analytics.Anomaly'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_Anomaly_Ts_Severity
        ON analytics.Anomaly (ReadingTs DESC, Severity)
        INCLUDE (SensorId, Value, Score, Method, ExpectedLow, ExpectedHigh);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_Anomaly_Sensor_Ts'
                 AND object_id = OBJECT_ID('analytics.Anomaly'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_Anomaly_Sensor_Ts
        ON analytics.Anomaly (SensorId, ReadingTs)
        INCLUDE (Value, Score, Severity, Method);
END
GO

/* -------------------------------------------------------------------------------------
   ops.* - operational metadata

   Small tables, but every one of them is read on each dashboard load and on every
   health check, so they get the indexes those access paths need.
   ------------------------------------------------------------------------------------- */

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_IngestionRun_Status_Started'
                 AND object_id = OBJECT_ID('ops.IngestionRun'))
BEGIN
    -- "When did data last land successfully?" is the single most-asked operational
    -- question; DESC ordering lets it be answered by reading one row.
    CREATE NONCLUSTERED INDEX IX_IngestionRun_Status_Started
        ON ops.IngestionRun (Status, StartedUtc DESC)
        INCLUDE (CompletedUtc, RowsInserted, MinReadingTs, MaxReadingTs);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_DataQualityIssue_Run_Type'
                 AND object_id = OBJECT_ID('ops.DataQualityIssue'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_DataQualityIssue_Run_Type
        ON ops.DataQualityIssue (IngestionRunId, IssueType, Severity)
        INCLUDE (AffectedRows, WindowStartTs, WindowEndTs, SensorId);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_DataQualityIssue_Window'
                 AND object_id = OBJECT_ID('ops.DataQualityIssue'))
BEGIN
    -- Lets the trend chart shade quality problems over the window being viewed.
    CREATE NONCLUSTERED INDEX IX_DataQualityIssue_Window
        ON ops.DataQualityIssue (WindowStartTs, WindowEndTs)
        INCLUDE (IssueType, Severity, SensorId, AffectedRows);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_RejectedRow_Run_Reason'
                 AND object_id = OBJECT_ID('ops.RejectedRow'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_RejectedRow_Run_Reason
        ON ops.RejectedRow (IngestionRunId, ReasonCode);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_FailureEvent_Window'
                 AND object_id = OBJECT_ID('ops.FailureEvent'))
BEGIN
    CREATE NONCLUSTERED INDEX IX_FailureEvent_Window
        ON ops.FailureEvent (StartTs, EndTs)
        INCLUDE (EquipmentId, FailureType, Severity);
END
GO

PRINT 'indexes.sql: secondary indexes are present.';
GO
