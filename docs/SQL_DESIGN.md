# SQL Design - Proposed Schema (Phase 1, for review)

Target: Microsoft SQL Server 2025 Express (17.0.1000.7), database `IndustrialMonitor`.

> Status: proposal, profile-informed. The file has now been measured
> (`docs/DATA_PROFILE.md`), so the values below are derived from real data rather than
> assumed. Section 6 lists what the profile changed.

## 1. Constraints that shaped the design

| Constraint | Consequence |
|---|---|
| Express Edition: 10 GB max per database, 1410 MB buffer pool, 4 cores | Narrow rows, `REAL` not `FLOAT`, `SMALLINT` keys, page compression, materialised rollups instead of repeated full scans |
| ~22.7 M reading rows | No identity column on the fact table; bulk load, not row-by-row |
| Time-window queries are the dominant access pattern | Clustered key ordered `(SensorId, ReadingTs)` |
| Dashboard also needs cross-sensor scans | Nonclustered columnstore index alongside the rowstore clustered index |

Express does support table partitioning (since 2016 SP1), columnstore, and
compression, so none of these techniques require a paid edition.

## 2. Schemas (namespaces)

| Schema | Purpose |
|---|---|
| `ref` | Static reference data (quality codes, severity levels) |
| `asset` | Equipment and sensor master data |
| `stg` | Transient staging for bulk loads |
| `ts` | Time-series fact data |
| `analytics` | Baselines, anomalies, aggregates |
| `ops` | Operational metadata: ingestion runs, quality issues, rejected rows, failure events |

Using schemas rather than table-name prefixes keeps permissions grantable per layer
and makes the layering visible in any client tool.

## 3. Tables

### 3.1 `ref.QualityCode`

Modelled on OPC DA quality conventions so that every stored value carries trust
information.

```sql
CREATE TABLE ref.QualityCode (
    QualityCodeId  TINYINT       NOT NULL CONSTRAINT PK_QualityCode PRIMARY KEY,
    Code           VARCHAR(16)   NOT NULL,   -- GOOD, UNCERTAIN, BAD_RANGE, ...
    DisplayName    NVARCHAR(64)  NOT NULL,
    IsUsable       BIT           NOT NULL,   -- may analytics include it?
    Description    NVARCHAR(400) NOT NULL,
    CONSTRAINT UQ_QualityCode_Code UNIQUE (Code)
);
```

Seed (proposed):

| Id | Code | IsUsable | Meaning |
|---|---|---|---|
| 192 | `GOOD` | 1 | Passed all validation |
| 200 | `GOOD_SUBSTITUTED` | 1 | Value retained but timestamp normalised |
| 64 | `UNCERTAIN_RANGE` | 1 | Outside the calibrated baseline range but physically possible |
| 65 | `UNCERTAIN_STALE` | 1 | Value repeated beyond the flatline threshold `[TBC-profile]` |
| 0 | `BAD_MISSING` | 0 | Null / unparseable in source |
| 8 | `BAD_RANGE` | 0 | Physically impossible (e.g. negative absolute pressure) |
| 16 | `BAD_DIGITAL` | 0 | Digital tag with a value that is neither 0 nor 1 |

Numeric ids follow OPC DA's Good=192 / Uncertain=64 / Bad=0 families deliberately, so
the convention is recognisable to anyone from an OT background.

### 3.2 `asset.Equipment`

```sql
CREATE TABLE asset.Equipment (
    EquipmentId    SMALLINT      NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_Equipment PRIMARY KEY,
    EquipmentCode  VARCHAR(32)   NOT NULL,   -- 'APU-01'
    EquipmentName  NVARCHAR(128) NOT NULL,   -- 'Metro Train Air Production Unit'
    EquipmentType  NVARCHAR(64)  NOT NULL,   -- 'Compressor / APU'
    Location       NVARCHAR(128) NULL,       -- 'Onboard metro train (Porto Metro)'
    Description    NVARCHAR(600) NULL,
    IsActive       BIT           NOT NULL CONSTRAINT DF_Equipment_IsActive DEFAULT (1),
    CreatedUtc     DATETIME2(3)  NOT NULL CONSTRAINT DF_Equipment_Created DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_Equipment_Code UNIQUE (EquipmentCode)
);
```

The dataset covers one APU, so this table has one row. It exists anyway because
(a) the schema must generalise, and (b) the dashboard's equipment-selection UX and the
`/equipment` endpoints must be real, not faked.

### 3.3 `asset.Sensor`

```sql
CREATE TABLE asset.Sensor (
    SensorId         SMALLINT      NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_Sensor PRIMARY KEY,
    EquipmentId      SMALLINT      NOT NULL
        CONSTRAINT FK_Sensor_Equipment REFERENCES asset.Equipment(EquipmentId),
    SensorCode       VARCHAR(32)   NOT NULL,   -- 'TP2', 'MOTOR_CURRENT'
    SourceColumn     VARCHAR(32)   NOT NULL,   -- exact CSV header, e.g. 'DV_eletric'
    SensorName       NVARCHAR(128) NOT NULL,
    SensorClass      VARCHAR(16)   NOT NULL,   -- 'Analogue' | 'Digital'
    MeasurementType  VARCHAR(32)   NOT NULL,   -- 'Pressure','Temperature','Current','ValveState',...
    Unit             VARCHAR(16)   NULL,       -- 'bar','A','degC', NULL for digital
    PhysicalMin      REAL          NULL,       -- hard plausibility floor  [TBC-profile]
    PhysicalMax      REAL          NULL,       -- hard plausibility ceiling [TBC-profile]
    DocumentedSetpoint REAL        NULL,       -- 8.2 (MPG), 7.0 (LPS) - from source docs
    Description      NVARCHAR(600) NOT NULL,   -- verbatim UCI description
    IsActive         BIT           NOT NULL CONSTRAINT DF_Sensor_IsActive DEFAULT (1),
    CONSTRAINT UQ_Sensor_Equipment_Code UNIQUE (EquipmentId, SensorCode),
    CONSTRAINT UQ_Sensor_SourceColumn   UNIQUE (SourceColumn),
    CONSTRAINT CK_Sensor_Class  CHECK (SensorClass IN ('Analogue','Digital')),
    CONSTRAINT CK_Sensor_Limits CHECK (PhysicalMin IS NULL OR PhysicalMax IS NULL
                                       OR PhysicalMin < PhysicalMax)
);
```

`SourceColumn` is what makes ingestion data-driven: the loader reads this table to
learn which CSV column maps to which `SensorId`. Adding a sensor never requires a code
change. `Description` holds the verbatim UCI text so the dashboard can explain a tag to
an operator without a lookup elsewhere.

### 3.4 `ts.SensorReading` - the fact table

```sql
CREATE TABLE ts.SensorReading (
    SensorId       SMALLINT     NOT NULL
        CONSTRAINT FK_Reading_Sensor REFERENCES asset.Sensor(SensorId),
    ReadingTs      DATETIME2(3) NOT NULL,
    Value          REAL         NOT NULL,
    QualityCodeId  TINYINT      NOT NULL
        CONSTRAINT FK_Reading_Quality REFERENCES ref.QualityCode(QualityCodeId),
    CONSTRAINT PK_SensorReading PRIMARY KEY CLUSTERED (SensorId, ReadingTs)
) WITH (DATA_COMPRESSION = PAGE);
```

Why this key. Tag-major then time-ordered is the physical order that "give me
sensor X between t0 and t1" wants - a single range seek, no sort. It also makes the
`(SensorId, ReadingTs)` pair the natural key that guarantees idempotent re-ingestion.

Why `REAL` (4-byte float). Sensor telemetry is float32 in the source device and in
every historian. `REAL` gives ~7 significant digits - more than the ~4 decimals present
in the file - at half the storage of `FLOAT`. `DECIMAL(9,4)` was considered and rejected:
5 bytes, and it would impose a precision the instrument does not actually have.

Why `NOT NULL` on `Value`. A missing measurement is represented as a row with a Bad
quality code, or as an absent row recorded in the gap report, never as a NULL that
silently propagates through `AVG()`.

Supporting indexes:

```sql
-- Cross-sensor time-range scans and rollup builds
CREATE NONCLUSTERED COLUMNSTORE INDEX NCCX_SensorReading
    ON ts.SensorReading (SensorId, ReadingTs, Value, QualityCodeId);

-- 'What happened across all tags at this time?' (the pivot query)
CREATE NONCLUSTERED INDEX IX_SensorReading_Ts
    ON ts.SensorReading (ReadingTs) INCLUDE (SensorId, Value, QualityCodeId)
    WITH (DATA_COMPRESSION = PAGE);
```

Partitioning `[decision pending profile]`. Monthly range partitioning on
`ReadingTs` gives partition elimination and cheap month-level maintenance. With only
7 months of data the benefit is modest and it complicates setup for a reviewer, so it
will be implemented only if profiling confirms even monthly distribution, and it
will be behind a flag in `schema.sql`.

### 3.5 `stg.SensorReadingStage`

Heap, no constraints, truncated per run - the target for bulk insert.

```sql
CREATE TABLE stg.SensorReadingStage (
    IngestionRunId INT          NOT NULL,
    SensorId       SMALLINT     NOT NULL,
    ReadingTs      DATETIME2(3) NOT NULL,
    Value          REAL         NOT NULL,
    QualityCodeId  TINYINT      NOT NULL
);
```

### 3.6 `ops.IngestionRun` - the run ledger

```sql
CREATE TABLE ops.IngestionRun (
    IngestionRunId  INT           NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_IngestionRun PRIMARY KEY,
    SourceFile      NVARCHAR(400) NOT NULL,
    SourceSha256    CHAR(64)      NULL,
    StartedUtc      DATETIME2(3)  NOT NULL CONSTRAINT DF_Run_Started DEFAULT SYSUTCDATETIME(),
    CompletedUtc    DATETIME2(3)  NULL,
    Status          VARCHAR(16)   NOT NULL,   -- RUNNING | SUCCEEDED | FAILED | PARTIAL
    RowsRead        BIGINT        NOT NULL CONSTRAINT DF_Run_Read     DEFAULT (0),
    RowsInserted    BIGINT        NOT NULL CONSTRAINT DF_Run_Inserted DEFAULT (0),
    RowsSkippedDup  BIGINT        NOT NULL CONSTRAINT DF_Run_Dup      DEFAULT (0),
    RowsRejected    BIGINT        NOT NULL CONSTRAINT DF_Run_Rej      DEFAULT (0),
    MinReadingTs    DATETIME2(3)  NULL,
    MaxReadingTs    DATETIME2(3)  NULL,
    ToolVersion     VARCHAR(32)   NULL,
    Notes           NVARCHAR(MAX) NULL,
    CONSTRAINT CK_Run_Status CHECK (Status IN ('RUNNING','SUCCEEDED','FAILED','PARTIAL'))
);
```

This single table answers "when did data last land, and was it clean?" - which is what
`health-check.ps1` and the dashboard's Data Quality panel both need. It is also what
makes the file hash meaningful: re-running the same file is detectable, not just
harmless.

### 3.7 `ops.DataQualityIssue` and `ops.RejectedRow`

```sql
CREATE TABLE ops.DataQualityIssue (
    IssueId        BIGINT        NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_DataQualityIssue PRIMARY KEY,
    IngestionRunId INT           NOT NULL
        CONSTRAINT FK_Issue_Run REFERENCES ops.IngestionRun(IngestionRunId),
    SensorId       SMALLINT      NULL
        CONSTRAINT FK_Issue_Sensor REFERENCES asset.Sensor(SensorId),
    IssueType      VARCHAR(48)   NOT NULL,  -- MISSING_VALUE, DUP_TIMESTAMP, GAP,
                                            -- OUT_OF_RANGE, NON_BINARY_DIGITAL,
                                            -- NON_MONOTONIC_TS, FLATLINE, XSENSOR_DIVERGENCE
    Severity       VARCHAR(12)   NOT NULL,  -- INFO | WARNING | CRITICAL
    WindowStartTs  DATETIME2(3)  NULL,
    WindowEndTs    DATETIME2(3)  NULL,
    AffectedRows   BIGINT        NOT NULL CONSTRAINT DF_Issue_Rows DEFAULT (1),
    ObservedValue  FLOAT         NULL,
    Details        NVARCHAR(1000) NOT NULL,
    DetectedUtc    DATETIME2(3)  NOT NULL CONSTRAINT DF_Issue_Det DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_Issue_Sev CHECK (Severity IN ('INFO','WARNING','CRITICAL'))
);

CREATE TABLE ops.RejectedRow (
    RejectedRowId  BIGINT        NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_RejectedRow PRIMARY KEY,
    IngestionRunId INT           NOT NULL
        CONSTRAINT FK_Rej_Run REFERENCES ops.IngestionRun(IngestionRunId),
    SourceLineNo   BIGINT        NULL,
    RawPayload     NVARCHAR(MAX) NOT NULL,   -- the original CSV line, unmodified
    ReasonCode     VARCHAR(48)   NOT NULL,
    ReasonDetail   NVARCHAR(1000) NULL
);
```

`RejectedRow` is the dead-letter queue. It is why the project can claim "nothing is
silently deleted" and prove it with a query.

### 3.8 `ops.FailureEvent`

```sql
CREATE TABLE ops.FailureEvent (
    FailureEventId  SMALLINT      NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_FailureEvent PRIMARY KEY,
    EquipmentId     SMALLINT      NOT NULL
        CONSTRAINT FK_Failure_Equipment REFERENCES asset.Equipment(EquipmentId),
    StartTs         DATETIME2(3)  NOT NULL,
    EndTs           DATETIME2(3)  NOT NULL,
    FailureType     NVARCHAR(64)  NOT NULL,   -- 'Air Leak'
    Severity        NVARCHAR(32)  NOT NULL,   -- 'High stress'
    SourceReference NVARCHAR(32)  NULL,       -- '#1', '#3' - verbatim, defects included
    ReportNote      NVARCHAR(400) NULL,       -- verbatim maintenance note
    DataQualityNote NVARCHAR(400) NULL,       -- our flag, e.g. duplicate '#1' id
    CONSTRAINT CK_Failure_Window CHECK (EndTs >= StartTs)
);
```

Seeded from the four documented reports. The duplicate `#1` identifier and the
May/April mismatch are recorded in `DataQualityNote`, not corrected.

### 3.9 `analytics.SensorBaseline` - how thresholds stop being arbitrary

```sql
CREATE TABLE analytics.SensorBaseline (
    SensorBaselineId INT          NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_SensorBaseline PRIMARY KEY,
    SensorId         SMALLINT     NOT NULL
        CONSTRAINT FK_Baseline_Sensor REFERENCES asset.Sensor(SensorId),
    BaselineName     VARCHAR(64)  NOT NULL,   -- 'REFERENCE_FEB2020'
    OperatingState   VARCHAR(16)  NOT NULL,   -- OFF | OFFLOADED | LOADED | STARTING | ALL
    WindowStartTs    DATETIME2(3) NOT NULL,
    WindowEndTs      DATETIME2(3) NOT NULL,
    SampleCount      BIGINT       NOT NULL,
    MeanValue        FLOAT        NOT NULL,
    StdDevValue      FLOAT        NOT NULL,
    P01 FLOAT NOT NULL, P25 FLOAT NOT NULL, P50 FLOAT NOT NULL,
    P75 FLOAT NOT NULL, P99 FLOAT NOT NULL,
    IqrLowerFence    FLOAT        NOT NULL,   -- P25 - 1.5*IQR
    IqrUpperFence    FLOAT        NOT NULL,   -- P75 + 1.5*IQR
    ComputedUtc      DATETIME2(3) NOT NULL CONSTRAINT DF_Baseline_Utc DEFAULT SYSUTCDATETIME(),
    Method           NVARCHAR(200) NOT NULL,  -- prose: how, and why this window
    CONSTRAINT UQ_Baseline UNIQUE (SensorId, BaselineName, OperatingState),
    CONSTRAINT CK_Baseline_State CHECK (OperatingState IN
        ('OFF','OFFLOADED','LOADED','STARTING','ALL'))
);
```

The rule this table enforces: no alert limit is ever typed into code. A limit is a
row here, computed from a named window that is documented and reproducible.

Profiling showed that a single global baseline per sensor is actively wrong on this
machine - the compressor duty-cycles, so every pressure signal is bimodal and its
quartiles collapse into whichever mode is more common. Hence `OperatingState`: a
`LOADED` reading is compared against `LOADED` statistics only. Full reasoning and
numbers in section 6.6.

Reference window: 2020-02-01 → 2020-02-29, the first full month - before all four
documented failures, and free of the frozen-data blocks described in section 6.4.

### 3.10 `analytics.Anomaly`

```sql
CREATE TABLE analytics.Anomaly (
    AnomalyId     BIGINT       NOT NULL IDENTITY(1,1)
        CONSTRAINT PK_Anomaly PRIMARY KEY,
    SensorId      SMALLINT     NOT NULL
        CONSTRAINT FK_Anomaly_Sensor REFERENCES asset.Sensor(SensorId),
    ReadingTs     DATETIME2(3) NOT NULL,
    Value         REAL         NOT NULL,
    ExpectedLow   REAL         NULL,
    ExpectedHigh  REAL         NULL,
    Method        VARCHAR(32)  NOT NULL,   -- ROLLING_ZSCORE | IQR | THRESHOLD | ISOLATION_FOREST
    Score         FLOAT        NOT NULL,
    Severity      VARCHAR(12)  NOT NULL,   -- NORMAL | WARNING | CRITICAL
    DetectionRunId INT         NOT NULL,
    DetectedUtc   DATETIME2(3) NOT NULL CONSTRAINT DF_Anom_Utc DEFAULT SYSUTCDATETIME(),
    CONSTRAINT CK_Anomaly_Sev CHECK (Severity IN ('NORMAL','WARNING','CRITICAL')),
    CONSTRAINT UQ_Anomaly UNIQUE (SensorId, ReadingTs, Method)
);
CREATE NONCLUSTERED INDEX IX_Anomaly_Ts_Sev ON analytics.Anomaly (ReadingTs, Severity)
    INCLUDE (SensorId, Value, Score);
```

`Method` is part of the unique key on purpose: the same instant may legitimately be
flagged by z-score and by IQR, and the dashboard should be able to say which rule
fired.

### 3.11 `analytics.SensorHourlyAgg` / `SensorDailyAgg`

```sql
CREATE TABLE analytics.SensorHourlyAgg (
    SensorId    SMALLINT     NOT NULL
        CONSTRAINT FK_HourAgg_Sensor REFERENCES asset.Sensor(SensorId),
    BucketStart DATETIME2(0) NOT NULL,
    SampleCount INT          NOT NULL,
    GoodCount   INT          NOT NULL,
    MinValue    REAL NOT NULL, MaxValue REAL NOT NULL,
    AvgValue    REAL NOT NULL, StdDevValue REAL NULL,
    FirstValue  REAL NOT NULL, LastValue REAL NOT NULL,
    CONSTRAINT PK_SensorHourlyAgg PRIMARY KEY CLUSTERED (SensorId, BucketStart)
);
```

`SensorDailyAgg` is identical with a `BucketDate DATE` column. `GoodCount` alongside
`SampleCount` lets the UI show coverage - an hourly average built from 12 samples
instead of 300 is not the same number, and the dashboard should say so.

## 4. Views (7 required + 2 useful additions)

| View | Purpose |
|---|---|
| `ts.vw_LatestReading` | Last value + timestamp + quality per sensor (`ROW_NUMBER` over `SensorId`) |
| `analytics.vw_HourlyAverage` | Hourly rollup joined to sensor/equipment metadata |
| `analytics.vw_DailyAverage` | Daily rollup, same shape |
| `ts.vw_SensorMinMax` | Lifetime min / max / range per sensor with the timestamps they occurred |
| `ts.vw_RollingStats` | 60-sample rolling mean and stddev via `AVG() OVER (PARTITION BY SensorId ORDER BY ReadingTs ROWS 59 PRECEDING)` |
| `analytics.vw_AbnormalReading` | Readings outside their sensor's baseline IQR fences, with severity |
| `asset.vw_EquipmentHealth` | Per-equipment roll-up: worst active severity, open critical count, staleness |
| `ops.vw_DataQualitySummary` | Issue counts by type/severity per ingestion run |
| `ops.vw_TimestampGap` | `LAG()`-based gap detection: consecutive readings more than N seconds apart |

`vw_RollingStats` is deliberately a view over the aggregate table, not the 22.7 M-row
fact table - a rolling window over raw data is fine for a single sensor and a bounded
time range (which the API always supplies) but must never be materialised whole.

## 5. The demonstration queries - built and measured

All eleven live in `database/queries/`. Each carries a header stating its purpose, its
access path, and why it is written the way it is.

They are executed, not just written: `python scripts/run_queries.py` runs every one
against the live database and reports rows and timings, so a schema change that breaks a
query is caught immediately and the numbers below are measured rather than claimed.

Measured on this machine (SQL Server 2025 Express, 22,754,220 readings, warm cache):

| # | Query | Demonstrates | Rows | Time |
|---|---|---|---|---|
| 01 | `time_window_retrieval` | Clustered range seek, quality filtering | 8,716 | 46 ms |
| 02 | `hourly_aggregation_with_coverage` | `DATEADD/DATEDIFF` bucketing, coverage % | 148 | 104 ms |
| 03 | `daily_trend_with_lag` | `LAG()`, % change, trailing mean | 212 | 19 ms |
| 04 | `anomaly_retrieval` | Covering index, severity ranking | 0 † | 21 ms |
| 05 | `cross_sensor_comparison` | Self-join on the documented invariant | 20,947 | 182 ms |
| 06 | `latest_value_two_ways` | `CROSS APPLY TOP 1` vs `ROW_NUMBER()` | 15 | 16 ms |
| 07 | `daily_statistics_with_median` | `PERCENTILE_CONT`, mean-vs-median divergence | 14 | 1,972 ms |
| 08 | `gap_detection` | `LAG()` gap derivation | 331 | (slow ‡) |
| 09 | `quality_breakdown_pivot` | `PIVOT` over quality codes | 105 | 1,840 ms |
| 10 | `failure_correlation` | Pre-failure windows with usability gating | 16 | 4,907 ms |
| 11 | `compressor_duty_cycle` | Operating states from documented current bands | 28 | 618 ms |

† Returns nothing until Phase 3 populates `analytics.Anomaly`. That is the correct
behaviour, not a defect: with no computed baseline there is no defensible notion of
"abnormal".

‡ Excluded from the default run because it scans a full tag. `ops.vw_TimestampGap`
answers the same question from the issues recorded at ingestion time - 331 rows instead
of a 1.5-million-row scan. Query 08 is the derivation; the view is the cache.

### What query 10 actually found

The pre-failure query gates every window on data usability before reporting a statistic,
and that gate fires:

| Event | Lead-up | Usable | Verdict |
|---|---|---|---|
| #1 (18 Apr) | 24 h | 30.5% | INSUFFICIENT DATA |
| #1 (18 Apr) | 12 / 6 / 1 h | 0.0% | INSUFFICIENT DATA |
| #2 (29 May) | all | 100% | comparable |
| #3 (5 Jun) | all | 100% | comparable |
| #4 (15 Jul) | all | 100% | comparable |

The 12-, 6- and 1-hour run-ups to event #1 are entirely held data. The query returns
`NULL` for those rather than an average, because an average there would describe a frozen
logger rather than a compressor.

Across the three usable events, mean `TP2` rises monotonically as failure approaches
(z vs the February baseline):

| Event | 24 h | 12 h | 6 h | 1 h |
|---|---|---|---|---|
| #2 | 0.319 | 0.308 | 0.329 | 0.957 |
| #3 | 0.206 | 0.235 | 0.328 | 0.781 |
| #4 | 1.402 | 1.612 | 1.862 | 2.849 |

That is a real and physically coherent pattern - an air leak makes the compressor work
harder, raising mean compressor pressure. It is also confounded with duty cycle: a
higher mean `TP2` partly just means the machine ran more often. Separating the two is
Phase 3's job, and with n=3 usable events no predictive-performance claim is supportable
either way.

## 6. Values resolved by profiling

Every item below was an assumption in the first draft. Each is now a measurement.

### 6.1 Plausibility limits (`asset.Sensor.PhysicalMin/Max`)

Observed ranges over all 1,516,948 rows, with limits set generously outside them so
the constraint catches corruption, not normal operation:

| Sensor | Observed min | Observed max | `PhysicalMin` | `PhysicalMax` |
|---|---|---|---|---|
| `TP2` | -0.032 | 10.68 | -1.0 | 16.0 |
| `TP3` | 0.730 | 10.30 | -1.0 | 16.0 |
| `H1` | -0.036 | 10.29 | -1.0 | 16.0 |
| `DV_pressure` | -0.032 | 9.844 | -1.0 | 16.0 |
| `Reservoirs` | 0.712 | 10.30 | -1.0 | 16.0 |
| `Oil_temperature` | 15.40 | 89.05 | -20.0 | 150.0 |
| `Motor_current` | 0.020 | 9.295 | -1.0 | 30.0 |

The negative-pressure trap. The obvious constraint `Value >= 0` on a pressure tag
would reject 1,275,474 of 1,516,948 `TP2` readings (84%) and 95% of
`DV_pressure`. These are gauge pressures with a small zero offset (around
-0.012 bar) that the sensor reports whenever the compressor is unloaded. They are
correct readings. The floor is therefore -1.0 bar, not 0.

### 6.2 Digital tags are clean

All eight digital columns contain only `0.0` and `1.0` - zero non-binary values
across the whole file. `CK_Reading_DigitalBinary` is enforceable as written, and the
`BAD_DIGITAL` quality code will (correctly) never fire on this dataset. It stays in the
model because the next file might not be this clean.

### 6.3 Gap threshold

Modal interval 10 s, median 10 s, mean 12.14 s. The interval distribution
is not clean - 1,337,521 steps of 10 s, but also 128,277 of 9 s and 38,321 of 12 s, i.e.
about ±1-3 s of logger jitter. A threshold of exactly 10 s would report a million
false gaps.

Decision: gap threshold = 30 s (3 × modal interval). That absorbs all observed
jitter and still detects every real dropout. It yields 331 gaps, the largest
48.03 h, and an overall coverage of 82.36%. The threshold is stored as
configuration, not hard-coded.

### 6.4 Flatline / stale-value threshold - and a serious finding

Profiling found windows where all seven analogue signals are bit-identical to the
previous scan simultaneously. That is not physically possible: oil temperature
drifts, pressure ripples, motor current fluctuates. It is a data-acquisition freeze -
the logger repeating its last good scan.

- 50,870 samples (3.35% of the file) across 24 contiguous blocks
- Longest block: 2020-06-22 15:06 → 2020-06-25 05:08, 51.4 hours
- During the longest block, `Motor_current` is held at exactly 5.575 A while `COMP`
  reads 0 - a compressor drawing load current with its intake valve shut, for two days

This is the single most important quality finding in the dataset, and neither of the
two checks people reach for first would catch it: there are zero null cells (UCI
correctly declares `has_missing_values: no`) and every held value is inside its
plausible range.

Decision: flatline threshold = 6 consecutive identical samples (60 s). Chosen
because a 1-minute hold is already longer than any genuine plateau in a 10-second
signal on a duty-cycling compressor. Rows are marked `UNCERTAIN_STALE` (id 65,
`IsUsable = 1`): kept and queryable, excluded from baselines, shown as a distinct band
in the dashboard.

### 6.5 Failure-window contamination - this changes the analysis plan

Cross-referencing frozen blocks against the four documented failure windows:

| Event | Ref | Frozen inside event | Frozen in 24 h lead-up |
|---|---|---|---|
| 1 | `#1` (18 Apr) | 91 rows (1.05%) | 4,378 rows (69.48%) |
| 2 | `#1` (29-30 May) | 0 (0%) | 0 (0%) |
| 3 | `#3` (5-7 Jun) | 0 (0%) | 0 (0%) |
| 4 | `#4` (15 Jul) | 0 (0%) | 0 (0%) |

Nearly 70% of the 24-hour run-up to failure event #1 is held data. A naive
"average the sensors in the 24 h before failure" analysis would, for that event, be
describing a logger fault rather than a compressor fault.

Consequence for Phase 3: pre-failure analysis reports usable sample counts per
window and excludes stale rows. Event #1's 24 h lead-up is reported as
insufficient data, not quietly averaged. Effective events for lead-up analysis:
3, not 4.

### 6.6 Baseline windows - global statistics are unusable

The compressor duty-cycles: `Motor_current` is below 1 A for 54.65% of samples,
1-5 A for 30.15%, 5-8 A for 15.20%. Every pressure signal is therefore bimodal.

Global IQR fences computed from the whole file are nonsense as alarm limits:

| Sensor | P25 | P75 | IQR lower fence | IQR upper fence | Problem |
|---|---|---|---|---|---|
| `TP2` | -0.020 | -0.004 | -0.020 | -0.004 | Both quartiles sit in the idle mode; every loaded sample (max 10.68 bar) is "anomalous" |
| `Motor_current` | - | - | -5.611 | 9.459 | A negative current is not a physical lower limit |
| `H1` | - | - | 6.574 | 11.054 | Excludes the entire unloaded mode (min -0.036) |

Decision (amends AD-1 in ARCHITECTURE.md): baselines are computed per operating
state, not globally. `analytics.SensorBaseline` gains an `OperatingState` column and
its unique key becomes `(SensorId, BaselineName, OperatingState)`.

Operating state is derived from the documented `Motor_current` bands and the `COMP`
valve signal - not from clustering:

| State | Rule | Share of file |
|---|---|---|
| `OFF` | `Motor_current < 1.0` | 54.65% |
| `OFFLOADED` | `1.0 <= Motor_current < 5.0` | 30.15% |
| `LOADED` | `5.0 <= Motor_current < 8.0` | 15.20% |
| `STARTING` | `Motor_current >= 8.0` | 0.003% (44 rows) |

Band edges are midpoints between the four nominal values UCI documents (0 / 4 / 7 / 9 A).
They are traceable to the source, not chosen to make a result look good.

Reference baseline window: 2020-02-01 → 2020-02-29 - the first full month, which
precedes all four documented failures and contains no frozen block longer than 2
samples. 214,850 rows.

### 6.7 Cross-sensor invariant is confirmed and usable

`|Reservoirs - TP3|`: mean 0.0019 bar, P99 0.006 bar, max 0.182 bar, with
zero rows above 0.5 bar. The documented "should be close to" relationship holds
tightly, so `XSENSOR_DIVERGENCE` fires at > 0.5 bar - roughly 3× the largest
divergence ever observed, so it flags instrument failure rather than noise.

### 6.8 Documented setpoints corroborated

`LPS` (documented: activates below 7 bar) is active on 5,188 rows; `TP3 < 7 bar` on
4,531 rows; both conditions hold together on 4,362. The overlap is strong enough to
treat the 7 bar setpoint as real and use it as a labelled reference line on pressure
charts.

### 6.9 Partitioning - decided against

Rows are evenly distributed across months (198,734-230,448 per month, plus 530 rows on
1 September). With seven partitions and a database this size, monthly partitioning adds
setup complexity for a reviewer without measurable query benefit; the clustered
`(SensorId, ReadingTs)` key already gives range seeks. Not implemented. The
rationale is recorded here so the omission reads as a decision, not an oversight.

### 6.10 Storage - estimated, then measured

Predicted: ~480-700 MB for the fact table, ~300-400 MB with page compression.

Measured after the full load of 22,754,220 readings:

| Object | Size |
|---|---|
| `ts.SensorReading` clustered PK (page compressed) | 381.1 MB |
| `IX_SensorReading_Ts` nonclustered, covering | 228.8 MB |
| `NCCX_SensorReading` nonclustered columnstore | 178.2 MB |
| `analytics.SensorHourlyAgg` (66,240 rows) | 3.2 MB |
| `analytics.SensorDailyAgg` (3,180 rows) | 0.2 MB |
| Database data file | 1,032 MB |
| Transaction log | 584 MB |

Comfortably inside the Express 10 GB per-database cap - about 10% of it - so the full
dataset is loaded, not a sample. The columnstore index compresses 22.7 million rows into
178 MB, less than half the rowstore it duplicates, which is why it is worth having
alongside rather than instead of the clustered index.

### 6.11 Quality-code distribution, measured

| Code | Family | Readings | Share |
|---|---|---|---|
| `GOOD` | GOOD | 21,991,395 | 96.648% |
| `UNCERTAIN_STALE` | UNCERTAIN | 762,825 | 3.352% |

No `BAD_*` readings at all: zero missing cells, zero out-of-range values, zero non-binary
digital values, zero quarantined rows. The source file is genuinely clean in every
respect that a null check or a range check can measure.

762,825 held readings / 15 sensors = 50,855 frozen scans, which matches the
independent Python profiler's count of 50,870 to within the 6-sample flatline threshold
(runs shorter than six scans are not counted as a freeze). Two implementations, written
against different data structures, agreeing on the finding.
