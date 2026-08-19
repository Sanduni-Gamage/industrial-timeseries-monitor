/* =====================================================================================
   Industrial Time-Series Monitoring Platform - core schema
   Target: Microsoft SQL Server 2016 SP1 or later (developed on 2025 Express 17.0)
   
   Idempotent: every object is guarded, so re-running is a no-op.
   Run order: schema.sql -> seed.sql -> [ingest] -> indexes.sql -> views.sql
   
   Secondary indexes live in indexes.sql, because building a columnstore before loading
   22.7 million rows makes the load dramatically slower.
   
   Rationale: docs/SQL_DESIGN.md. Measurements: docs/DATA_PROFILE.md.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   1. Schemas (namespaces)
   
   Layer separation by schema rather than name prefix, so permissions can be granted per
   layer. CREATE SCHEMA must start its own batch, hence the EXEC wrapper.
   ------------------------------------------------------------------------------------- */

IF SCHEMA_ID('ref')       IS NULL EXEC('CREATE SCHEMA ref');
IF SCHEMA_ID('asset')     IS NULL EXEC('CREATE SCHEMA asset');
IF SCHEMA_ID('stg')       IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('ts')        IS NULL EXEC('CREATE SCHEMA ts');
IF SCHEMA_ID('analytics') IS NULL EXEC('CREATE SCHEMA analytics');
IF SCHEMA_ID('ops')       IS NULL EXEC('CREATE SCHEMA ops');
GO

/* -------------------------------------------------------------------------------------
   2. ref.QualityCode
   
   Every reading carries a quality code, so a value that failed validation is marked
   rather than deleted. Ids follow OPC DA: Good 192, Uncertain 64, Bad 0.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ref.QualityCode', 'U') IS NULL
BEGIN
    CREATE TABLE ref.QualityCode (
        QualityCodeId TINYINT       NOT NULL CONSTRAINT PK_QualityCode PRIMARY KEY,
        Code          VARCHAR(24)   NOT NULL,
        DisplayName   NVARCHAR(64)  NOT NULL,
        Family        VARCHAR(12)   NOT NULL,
        IsUsable      BIT           NOT NULL,
        Description   NVARCHAR(400) NOT NULL,
        CONSTRAINT UQ_QualityCode_Code UNIQUE (Code),
        CONSTRAINT CK_QualityCode_Family CHECK (Family IN ('GOOD', 'UNCERTAIN', 'BAD'))
    );
END
GO

/* -------------------------------------------------------------------------------------
   3. asset.Equipment
   
   One row, because the dataset covers one Air Production Unit. It exists so a second
   machine needs no schema change, and so the /equipment endpoints have something real
   behind them.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('asset.Equipment', 'U') IS NULL
BEGIN
    CREATE TABLE asset.Equipment (
        EquipmentId   SMALLINT      IDENTITY(1,1) NOT NULL CONSTRAINT PK_Equipment PRIMARY KEY,
        EquipmentCode VARCHAR(32)   NOT NULL,
        EquipmentName NVARCHAR(128) NOT NULL,
        EquipmentType NVARCHAR(64)  NOT NULL,
        Location      NVARCHAR(128) NULL,
        Description   NVARCHAR(800) NULL,
        IsActive      BIT           NOT NULL CONSTRAINT DF_Equipment_IsActive DEFAULT (1),
        CreatedUtc    DATETIME2(3)  NOT NULL CONSTRAINT DF_Equipment_CreatedUtc DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_Equipment_Code UNIQUE (EquipmentCode)
    );
END
GO

/* -------------------------------------------------------------------------------------
   4. asset.Sensor
   
   SourceColumn makes ingestion data-driven: the loader reads this table to map CSV
   columns to sensors, so adding a sensor is a seed row rather than a code change.
   
   PhysicalMin/Max are plausibility limits, not alarm limits, set generously outside the
   observed range. The pressure floors are negative because these are gauge pressures
   with a zero offset, and 84% of TP2 readings fall below zero.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('asset.Sensor', 'U') IS NULL
BEGIN
    CREATE TABLE asset.Sensor (
        SensorId           SMALLINT      IDENTITY(1,1) NOT NULL CONSTRAINT PK_Sensor PRIMARY KEY,
        EquipmentId        SMALLINT      NOT NULL,
        SensorCode         VARCHAR(32)   NOT NULL,
        SourceColumn       VARCHAR(32)   NOT NULL,
        SensorName         NVARCHAR(128) NOT NULL,
        SensorClass        VARCHAR(16)   NOT NULL,
        MeasurementType    VARCHAR(32)   NOT NULL,
        Unit               VARCHAR(16)   NULL,
        PhysicalMin        REAL          NULL,
        PhysicalMax        REAL          NULL,
        DocumentedSetpoint REAL          NULL,
        DisplayOrder       SMALLINT      NOT NULL CONSTRAINT DF_Sensor_DisplayOrder DEFAULT (100),
        Description        NVARCHAR(800) NOT NULL,
        IsActive           BIT           NOT NULL CONSTRAINT DF_Sensor_IsActive DEFAULT (1),
        CONSTRAINT FK_Sensor_Equipment FOREIGN KEY (EquipmentId)
            REFERENCES asset.Equipment (EquipmentId),
        CONSTRAINT UQ_Sensor_Equipment_Code UNIQUE (EquipmentId, SensorCode),
        CONSTRAINT UQ_Sensor_SourceColumn   UNIQUE (SourceColumn),
        CONSTRAINT CK_Sensor_Class  CHECK (SensorClass IN ('Analogue', 'Digital')),
        CONSTRAINT CK_Sensor_Limits CHECK (PhysicalMin IS NULL OR PhysicalMax IS NULL
                                           OR PhysicalMin < PhysicalMax)
    );
END
GO

/* -------------------------------------------------------------------------------------
   5. ops.IngestionRun - the run ledger
   
   Answers "when did data last land, and was it clean?". Recording the source hash makes
   a re-run detectable rather than merely harmless.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ops.IngestionRun', 'U') IS NULL
BEGIN
    CREATE TABLE ops.IngestionRun (
        IngestionRunId INT           IDENTITY(1,1) NOT NULL CONSTRAINT PK_IngestionRun PRIMARY KEY,
        SourceFile     NVARCHAR(400) NOT NULL,
        SourceSha256   CHAR(64)      NULL,
        SourceBytes    BIGINT        NULL,
        StartedUtc     DATETIME2(3)  NOT NULL CONSTRAINT DF_IngestionRun_StartedUtc DEFAULT SYSUTCDATETIME(),
        CompletedUtc   DATETIME2(3)  NULL,
        Status         VARCHAR(16)   NOT NULL,
        RowsRead       BIGINT        NOT NULL CONSTRAINT DF_IngestionRun_RowsRead     DEFAULT (0),
        RowsInserted   BIGINT        NOT NULL CONSTRAINT DF_IngestionRun_RowsInserted DEFAULT (0),
        RowsSkippedDup BIGINT        NOT NULL CONSTRAINT DF_IngestionRun_RowsDup      DEFAULT (0),
        RowsRejected   BIGINT        NOT NULL CONSTRAINT DF_IngestionRun_RowsRejected DEFAULT (0),
        MinReadingTs   DATETIME2(3)  NULL,
        MaxReadingTs   DATETIME2(3)  NULL,
        ToolVersion    VARCHAR(32)   NULL,
        HostName       NVARCHAR(128) NULL,
        Notes          NVARCHAR(MAX) NULL,
        CONSTRAINT CK_IngestionRun_Status
            CHECK (Status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'PARTIAL', 'SKIPPED'))
    );
END
GO

/* -------------------------------------------------------------------------------------
   6. ts.SensorReading - the fact table (~22.7 million rows)
   
   Clustered on (SensorId, ReadingTs), the exact order a "sensor X between t0 and t1"
   query wants, so those become a single range seek with no sort. That pair is also the
   natural key making re-ingestion idempotent, which is why there is no surrogate id
   (docs/SQL_DESIGN.md, AD-2).
   
   Value is REAL: telemetry is float32 at the source, and REAL carries ~7 significant
   digits against the ~4 decimals present, at half the storage of FLOAT.
   
   NOT NULL by design. A missing measurement is a Bad-quality row or an absent row in the
   gap report, never a NULL silently skewing AVG().
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ts.SensorReading', 'U') IS NULL
BEGIN
    CREATE TABLE ts.SensorReading (
        SensorId      SMALLINT     NOT NULL,
        ReadingTs     DATETIME2(3) NOT NULL,
        Value         REAL         NOT NULL,
        QualityCodeId TINYINT      NOT NULL,
        CONSTRAINT PK_SensorReading PRIMARY KEY CLUSTERED (SensorId, ReadingTs)
            WITH (DATA_COMPRESSION = PAGE),
        CONSTRAINT FK_SensorReading_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId),
        CONSTRAINT FK_SensorReading_Quality FOREIGN KEY (QualityCodeId)
            REFERENCES ref.QualityCode (QualityCodeId)
    );
END
GO

/* -------------------------------------------------------------------------------------
   7. stg.SensorReadingStage - bulk-load target
   
   No constraints or foreign keys, which would be evaluated per row during the bulk
   insert; the set-based INSERT out of this table validates in one pass.
   
   It does carry a clustered index on the fact table's key, for the anti-join that
   follows. Against a heap the optimiser chose a hash plan needing a ~62 MB grant per
   chunk, which queued on RESOURCE_SEMAPHORE past 16 million rows and stalled one chunk
   for three minutes. See docs/DEV_LOG.md DL-012.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('stg.SensorReadingStage', 'U') IS NULL
BEGIN
    CREATE TABLE stg.SensorReadingStage (
        SensorId      SMALLINT     NOT NULL,
        ReadingTs     DATETIME2(3) NOT NULL,
        Value         REAL         NOT NULL,
        QualityCodeId TINYINT      NOT NULL
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'CIX_SensorReadingStage'
                 AND object_id = OBJECT_ID('stg.SensorReadingStage'))
BEGIN
    CREATE CLUSTERED INDEX CIX_SensorReadingStage
        ON stg.SensorReadingStage (SensorId, ReadingTs);
END
GO

/* -------------------------------------------------------------------------------------
   8. ops.DataQualityIssue and ops.RejectedRow

   RejectedRow is the dead-letter queue. It is what lets this project claim that nothing
   is silently discarded and then prove it with a single query.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ops.DataQualityIssue', 'U') IS NULL
BEGIN
    CREATE TABLE ops.DataQualityIssue (
        IssueId        BIGINT         IDENTITY(1,1) NOT NULL CONSTRAINT PK_DataQualityIssue PRIMARY KEY,
        IngestionRunId INT            NOT NULL,
        SensorId       SMALLINT       NULL,
        IssueType      VARCHAR(48)    NOT NULL,
        Severity       VARCHAR(12)    NOT NULL,
        WindowStartTs  DATETIME2(3)   NULL,
        WindowEndTs    DATETIME2(3)   NULL,
        AffectedRows   BIGINT         NOT NULL CONSTRAINT DF_DataQualityIssue_Rows DEFAULT (1),
        ObservedValue  FLOAT          NULL,
        /* Two kinds of row live in this table and they MUST NOT be added together.
           A detail row describes one specific event (this gap, this frozen block); a
           summary row carries the exact run total for its issue type. Summing
           AffectedRows across both double-counts every issue. */
        IsSummary      BIT            NOT NULL CONSTRAINT DF_DataQualityIssue_IsSummary DEFAULT (0),
        Details        NVARCHAR(1000) NOT NULL,
        DetectedUtc    DATETIME2(3)   NOT NULL CONSTRAINT DF_DataQualityIssue_Utc DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_DataQualityIssue_Run FOREIGN KEY (IngestionRunId)
            REFERENCES ops.IngestionRun (IngestionRunId),
        CONSTRAINT FK_DataQualityIssue_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId),
        CONSTRAINT CK_DataQualityIssue_Severity CHECK (Severity IN ('INFO', 'WARNING', 'CRITICAL'))
    );
END
GO

/* Additive migration for databases created before IsSummary existed. The guarded
   CREATE TABLE above is a no-op once a table exists, so a new column needs its own
   guard or an established database would silently miss it. */
IF OBJECT_ID('ops.DataQualityIssue', 'U') IS NOT NULL
   AND COL_LENGTH('ops.DataQualityIssue', 'IsSummary') IS NULL
BEGIN
    ALTER TABLE ops.DataQualityIssue
        ADD IsSummary BIT NOT NULL
            CONSTRAINT DF_DataQualityIssue_IsSummary DEFAULT (0);
    PRINT 'schema.sql: added ops.DataQualityIssue.IsSummary';
END
GO

/* Backfill rows written before the flag existed. They default to 0, which would make
   every historical summary row look like a detail row and report its run total as zero.
   
   Guarded on the mis-flagged rows themselves, so it is idempotent and self-healing. It
   must be its own batch, because the column does not exist at compile time in the batch
   that adds it. */
IF OBJECT_ID('ops.DataQualityIssue', 'U') IS NOT NULL
   AND EXISTS (SELECT 1 FROM ops.DataQualityIssue
               WHERE IsSummary = 0
                 AND (Details LIKE 'Run total:%'
                      OR Details LIKE '%was capped at%'
                      OR IssueType = 'LOAD_RECONCILIATION'))
BEGIN
    UPDATE ops.DataQualityIssue
    SET IsSummary = 1
    WHERE IsSummary = 0
      AND (Details LIKE 'Run total:%'
           OR Details LIKE '%was capped at%'
           OR IssueType = 'LOAD_RECONCILIATION');
    PRINT CONCAT('schema.sql: backfilled IsSummary on ', @@ROWCOUNT, ' pre-existing row(s)');
END
GO

IF OBJECT_ID('ops.RejectedRow', 'U') IS NULL
BEGIN
    CREATE TABLE ops.RejectedRow (
        RejectedRowId  BIGINT         IDENTITY(1,1) NOT NULL CONSTRAINT PK_RejectedRow PRIMARY KEY,
        IngestionRunId INT            NOT NULL,
        SourceLineNo   BIGINT         NULL,
        RawPayload     NVARCHAR(MAX)  NOT NULL,
        ReasonCode     VARCHAR(48)    NOT NULL,
        ReasonDetail   NVARCHAR(1000) NULL,
        RejectedUtc    DATETIME2(3)   NOT NULL CONSTRAINT DF_RejectedRow_Utc DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_RejectedRow_Run FOREIGN KEY (IngestionRunId)
            REFERENCES ops.IngestionRun (IngestionRunId)
    );
END
GO

/* -------------------------------------------------------------------------------------
   9. ops.FailureEvent
   
   Seeded from the four published maintenance reports. The source table's defects, a
   duplicated identifier and a note dated April against a May window, are preserved
   verbatim and flagged in DataQualityNote rather than corrected.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ops.FailureEvent', 'U') IS NULL
BEGIN
    CREATE TABLE ops.FailureEvent (
        FailureEventId  SMALLINT      IDENTITY(1,1) NOT NULL CONSTRAINT PK_FailureEvent PRIMARY KEY,
        EquipmentId     SMALLINT      NOT NULL,
        StartTs         DATETIME2(3)  NOT NULL,
        EndTs           DATETIME2(3)  NOT NULL,
        FailureType     NVARCHAR(64)  NOT NULL,
        Severity        NVARCHAR(32)  NOT NULL,
        SourceReference NVARCHAR(32)  NULL,
        ReportNote      NVARCHAR(400) NULL,
        DataQualityNote NVARCHAR(400) NULL,
        CONSTRAINT FK_FailureEvent_Equipment FOREIGN KEY (EquipmentId)
            REFERENCES asset.Equipment (EquipmentId),
        CONSTRAINT CK_FailureEvent_Window CHECK (EndTs >= StartTs)
    );
END
GO

/* -------------------------------------------------------------------------------------
   10. analytics.SensorBaseline
   
   No alert limit is ever typed into code. A limit is a row here, computed from a named,
   reproducible window.
   
   OperatingState exists because a single global baseline is actively wrong on this
   machine: the compressor is off 54.65% of the time, so the global IQR fence for TP2
   came out at -0.020..-0.004 bar against a real range of -0.032..10.68 bar.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('analytics.SensorBaseline', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.SensorBaseline (
        SensorBaselineId INT           IDENTITY(1,1) NOT NULL CONSTRAINT PK_SensorBaseline PRIMARY KEY,
        SensorId         SMALLINT      NOT NULL,
        BaselineName     VARCHAR(64)   NOT NULL,
        OperatingState   VARCHAR(16)   NOT NULL,
        WindowStartTs    DATETIME2(3)  NOT NULL,
        WindowEndTs      DATETIME2(3)  NOT NULL,
        SampleCount      BIGINT        NOT NULL,
        MeanValue        FLOAT         NOT NULL,
        StdDevValue      FLOAT         NOT NULL,
        P01              FLOAT         NOT NULL,
        P25              FLOAT         NOT NULL,
        P50              FLOAT         NOT NULL,
        P75              FLOAT         NOT NULL,
        P99              FLOAT         NOT NULL,
        IqrLowerFence    FLOAT         NOT NULL,
        IqrUpperFence    FLOAT         NOT NULL,
        ComputedUtc      DATETIME2(3)  NOT NULL CONSTRAINT DF_SensorBaseline_Utc DEFAULT SYSUTCDATETIME(),
        Method           NVARCHAR(400) NOT NULL,
        CONSTRAINT FK_SensorBaseline_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId),
        CONSTRAINT UQ_SensorBaseline UNIQUE (SensorId, BaselineName, OperatingState),
        CONSTRAINT CK_SensorBaseline_State
            CHECK (OperatingState IN ('OFF', 'OFFLOADED', 'LOADED', 'STARTING', 'ALL'))
    );
END
GO

/* -------------------------------------------------------------------------------------
   10b. analytics.ScanState - the derived operating state of the machine
   
   One row per scan, not per reading: operating state is a property of the machine at an
   instant, not of any tag. Materialised because deriving it per query would self-join a
   22.7-million-row table; 1.5 million rows here removes that join downstream.
   
   Band edges are midpoints between the four nominal currents the documentation states
   outright (0 / 4 / 7 / 9 A), not clustered or tuned.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('analytics.ScanState', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.ScanState (
        ReadingTs      DATETIME2(3) NOT NULL,
        MotorCurrent   REAL         NOT NULL,
        OperatingState VARCHAR(16)  NOT NULL,
        CompActive     BIT          NULL,
        IsStale        BIT          NOT NULL,
        CONSTRAINT PK_ScanState PRIMARY KEY CLUSTERED (ReadingTs)
            WITH (DATA_COMPRESSION = PAGE),
        CONSTRAINT CK_ScanState_State
            CHECK (OperatingState IN ('OFF', 'OFFLOADED', 'LOADED', 'STARTING'))
    );
END
GO

IF OBJECT_ID('analytics.ScanState', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.indexes
                   WHERE name = 'IX_ScanState_State'
                     AND object_id = OBJECT_ID('analytics.ScanState'))
BEGIN
    -- Baselines and duty-cycle queries filter by state first, then by time.
    CREATE NONCLUSTERED INDEX IX_ScanState_State
        ON analytics.ScanState (OperatingState, ReadingTs)
        INCLUDE (IsStale, MotorCurrent)
        WITH (DATA_COMPRESSION = PAGE);
END
GO

/* -------------------------------------------------------------------------------------
   11. analytics.Anomaly
   
   Method is part of the unique key on purpose: one instant may legitimately be flagged
   by both the z-score and the IQR rule, and the dashboard should say which fired.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('analytics.Anomaly', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.Anomaly (
        AnomalyId      BIGINT       IDENTITY(1,1) NOT NULL CONSTRAINT PK_Anomaly PRIMARY KEY,
        SensorId       SMALLINT     NOT NULL,
        ReadingTs      DATETIME2(3) NOT NULL,
        Value          REAL         NOT NULL,
        ExpectedLow    REAL         NULL,
        ExpectedHigh   REAL         NULL,
        Method         VARCHAR(32)  NOT NULL,
        Score          FLOAT        NOT NULL,
        Severity       VARCHAR(12)  NOT NULL,
        OperatingState VARCHAR(16)  NULL,
        DetectionRunId INT          NULL,
        DetectedUtc    DATETIME2(3) NOT NULL CONSTRAINT DF_Anomaly_Utc DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_Anomaly_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId),
        /* OperatingState is part of the key because detection is per-state. A sensor can
           legitimately be abnormal in its LOADED behaviour and entirely normal while
           OFFLOADED during the same hour, and collapsing those into one row would
           discard the more useful half of the finding. */
        CONSTRAINT UQ_Anomaly UNIQUE (SensorId, ReadingTs, Method, OperatingState),
        CONSTRAINT CK_Anomaly_Severity CHECK (Severity IN ('NORMAL', 'WARNING', 'CRITICAL'))
    );
END
GO

/* Migration for databases created before OperatingState joined the anomaly key.
   Guarded on the constraint's actual column list, so it is idempotent. */
IF OBJECT_ID('analytics.Anomaly', 'U') IS NOT NULL
   AND EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UQ_Anomaly'
                 AND object_id = OBJECT_ID('analytics.Anomaly'))
   AND NOT EXISTS (
       SELECT 1
       FROM sys.index_columns AS ic
       INNER JOIN sys.columns AS c
               ON c.object_id = ic.object_id AND c.column_id = ic.column_id
       WHERE ic.object_id = OBJECT_ID('analytics.Anomaly')
         AND ic.index_id = (SELECT index_id FROM sys.indexes
                            WHERE name = 'UQ_Anomaly'
                              AND object_id = OBJECT_ID('analytics.Anomaly'))
         AND c.name = 'OperatingState')
BEGIN
    ALTER TABLE analytics.Anomaly DROP CONSTRAINT UQ_Anomaly;
    ALTER TABLE analytics.Anomaly
        ADD CONSTRAINT UQ_Anomaly UNIQUE (SensorId, ReadingTs, Method, OperatingState);
    PRINT 'schema.sql: widened UQ_Anomaly to include OperatingState';
END
GO

/* -------------------------------------------------------------------------------------
   12. analytics.SensorHourlyAgg / SensorDailyAgg - the aggregate archive
   
   Materialised rather than indexed views, which cannot contain the window functions or
   STDEV needed here. Mirrors the raw-plus-aggregate archive split historians use.
   
   GoodCount alongside SampleCount lets the UI show coverage: an average from 12 samples
   is not the same number as one from 360.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('analytics.SensorHourlyAgg', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.SensorHourlyAgg (
        SensorId    SMALLINT     NOT NULL,
        BucketStart DATETIME2(0) NOT NULL,
        SampleCount INT          NOT NULL,
        GoodCount   INT          NOT NULL,
        MinValue    REAL         NOT NULL,
        MaxValue    REAL         NOT NULL,
        AvgValue    REAL         NOT NULL,
        StdDevValue REAL         NULL,
        FirstValue  REAL         NOT NULL,
        LastValue   REAL         NOT NULL,
        CONSTRAINT PK_SensorHourlyAgg PRIMARY KEY CLUSTERED (SensorId, BucketStart),
        CONSTRAINT FK_SensorHourlyAgg_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId)
    );
END
GO

/* analytics.SensorHourlyStateAgg - hourly rollup split by operating state.
   
   SensorHourlyAgg blends OFF and LOADED behaviour into a number describing neither.
   Keeping them apart is what makes a trend comparable to a baseline, and what adaptive
   detection needs: both sides of the comparison must be state-aware. */
IF OBJECT_ID('analytics.SensorHourlyStateAgg', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.SensorHourlyStateAgg (
        SensorId       SMALLINT     NOT NULL,
        BucketStart    DATETIME2(0) NOT NULL,
        OperatingState VARCHAR(16)  NOT NULL,
        SampleCount    INT          NOT NULL,
        MinValue       REAL         NOT NULL,
        MaxValue       REAL         NOT NULL,
        AvgValue       REAL         NOT NULL,
        StdDevValue    REAL         NULL,
        CONSTRAINT PK_SensorHourlyStateAgg
            PRIMARY KEY CLUSTERED (SensorId, OperatingState, BucketStart)
            WITH (DATA_COMPRESSION = PAGE),
        CONSTRAINT FK_SensorHourlyStateAgg_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId)
    );
END
GO

IF OBJECT_ID('analytics.SensorDailyAgg', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.SensorDailyAgg (
        SensorId    SMALLINT NOT NULL,
        BucketDate  DATE     NOT NULL,
        SampleCount INT      NOT NULL,
        GoodCount   INT      NOT NULL,
        MinValue    REAL     NOT NULL,
        MaxValue    REAL     NOT NULL,
        AvgValue    REAL     NOT NULL,
        StdDevValue REAL     NULL,
        FirstValue  REAL     NOT NULL,
        LastValue   REAL     NOT NULL,
        CONSTRAINT PK_SensorDailyAgg PRIMARY KEY CLUSTERED (SensorId, BucketDate),
        CONSTRAINT FK_SensorDailyAgg_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId)
    );
END
GO

/* -------------------------------------------------------------------------------------
   12b. Historian concepts: compressed archive and its measured cost
   
   ts.SensorReadingCompressed holds the swinging-door output, the readings a straight line
   cannot reconstruct within a stated tolerance.
   
   Both archives are kept on purpose. A plant would keep only the compressed one; holding
   the raw archive alongside makes the error bound checkable rather than trusted.
   ------------------------------------------------------------------------------------- */

IF OBJECT_ID('ts.SensorReadingCompressed', 'U') IS NULL
BEGIN
    CREATE TABLE ts.SensorReadingCompressed (
        SensorId      SMALLINT     NOT NULL,
        ReadingTs     DATETIME2(3) NOT NULL,
        Value         REAL         NOT NULL,
        QualityCodeId TINYINT      NOT NULL,
        CONSTRAINT PK_SensorReadingCompressed
            PRIMARY KEY CLUSTERED (SensorId, ReadingTs)
            WITH (DATA_COMPRESSION = PAGE),
        CONSTRAINT FK_SensorReadingCompressed_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId)
    );
END
GO

IF OBJECT_ID('analytics.CompressionStat', 'U') IS NULL
BEGIN
    CREATE TABLE analytics.CompressionStat (
        CompressionStatId INT      IDENTITY(1,1) NOT NULL
            CONSTRAINT PK_CompressionStat PRIMARY KEY,
        SensorId       SMALLINT     NOT NULL,
        DeviationPct   DECIMAL(6,3) NOT NULL,
        Deviation      FLOAT        NOT NULL,
        OriginalCount  BIGINT       NOT NULL,
        KeptCount      BIGINT       NOT NULL,
        MaxError       FLOAT        NOT NULL,
        MeanError      FLOAT        NOT NULL,
        WithinBound    BIT          NOT NULL,
        ComputedUtc    DATETIME2(3) NOT NULL
            CONSTRAINT DF_CompressionStat_Utc DEFAULT SYSUTCDATETIME(),
        CONSTRAINT FK_CompressionStat_Sensor FOREIGN KEY (SensorId)
            REFERENCES asset.Sensor (SensorId),
        CONSTRAINT UQ_CompressionStat UNIQUE (SensorId, DeviationPct)
    );
END
GO

/* Time-weighted average, added to the hourly aggregate alongside the simple mean.

   A historian weights each value by HOW LONG IT HELD, not by how many samples happened
   to arrive. With this archive's 9-13 s jitter and 331 gaps the two differ, and the
   simple mean is the one that is quietly wrong: it over-weights whatever the logger
   sampled densely. Both are stored so the difference is visible rather than asserted. */
IF OBJECT_ID('analytics.SensorHourlyAgg', 'U') IS NOT NULL
   AND COL_LENGTH('analytics.SensorHourlyAgg', 'TimeWeightedAvg') IS NULL
BEGIN
    ALTER TABLE analytics.SensorHourlyAgg ADD TimeWeightedAvg REAL NULL;
    PRINT 'schema.sql: added analytics.SensorHourlyAgg.TimeWeightedAvg';
END
GO

PRINT 'schema.sql: core schema is present.';
GO
