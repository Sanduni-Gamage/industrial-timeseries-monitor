/* =====================================================================================
   Reference and master data.

   Idempotent: every statement is a MERGE or a guarded INSERT, so re-running updates
   descriptions in place without creating duplicates or breaking foreign keys.

   Sensor descriptions are VERBATIM from the UCI dataset documentation. They are stored
   in the database (not only in a markdown file) so the dashboard can explain a tag to an
   operator without a lookup elsewhere, and so the provenance travels with the data.

   Plausibility limits come from docs/DATA_PROFILE.md §5, set generously outside the
   observed range. See docs/SQL_DESIGN.md §6.1 for why the pressure floors are negative.
   ===================================================================================== */

SET NOCOUNT ON;
GO

/* -------------------------------------------------------------------------------------
   1. ref.QualityCode

   Numeric ids follow the OPC DA convention: the Good family is 192+, Uncertain 64+,
   Bad 0+. IsUsable answers "may analytics include this reading?" - Uncertain values are
   real measurements of questionable trust and are included with a caveat; Bad values
   are retained for audit but excluded from statistics.
   ------------------------------------------------------------------------------------- */

MERGE ref.QualityCode AS target
USING (VALUES
    (192, 'GOOD',             N'Good',                    'GOOD',      1,
     N'Passed every validation rule.'),
    (200, 'GOOD_SUBSTITUTED', N'Good (timestamp normalised)', 'GOOD',  1,
     N'Value accepted unchanged; its timestamp was normalised during ingestion.'),
    (64,  'UNCERTAIN_RANGE',  N'Uncertain (out of baseline)', 'UNCERTAIN', 1,
     N'Outside the calibrated baseline range for its operating state but physically possible.'),
    (65,  'UNCERTAIN_STALE',  N'Uncertain (held value)',  'UNCERTAIN', 1,
     N'Part of a data-acquisition freeze: every analogue signal repeated the previous scan exactly. The value is plausible but is not a fresh measurement.'),
    (0,   'BAD_MISSING',      N'Bad (missing)',           'BAD',       0,
     N'Null, empty or unparseable in the source file.'),
    (8,   'BAD_RANGE',        N'Bad (impossible value)',  'BAD',       0,
     N'Outside the sensor''s physical plausibility limits; indicates corruption rather than an unusual reading.'),
    (16,  'BAD_DIGITAL',      N'Bad (non-binary digital)','BAD',       0,
     N'A digital tag carrying a value other than 0 or 1.')
) AS source (QualityCodeId, Code, DisplayName, Family, IsUsable, Description)
    ON target.QualityCodeId = source.QualityCodeId
WHEN MATCHED THEN UPDATE SET
    target.Code        = source.Code,
    target.DisplayName = source.DisplayName,
    target.Family      = source.Family,
    target.IsUsable    = source.IsUsable,
    target.Description = source.Description
WHEN NOT MATCHED BY TARGET THEN
    INSERT (QualityCodeId, Code, DisplayName, Family, IsUsable, Description)
    VALUES (source.QualityCodeId, source.Code, source.DisplayName, source.Family,
            source.IsUsable, source.Description);
GO

/* -------------------------------------------------------------------------------------
   2. asset.Equipment
   ------------------------------------------------------------------------------------- */

MERGE asset.Equipment AS target
USING (VALUES
    ('APU-01',
     N'Metro Train Air Production Unit',
     N'Compressor / Air Production Unit',
     N'Onboard metro train (Porto Metro, Portugal)',
     N'Compressor Air Production Unit supplying compressed air to the train''s pneumatic systems. Instrumented with 7 analogue and 8 digital sensors logged at 1 Hz by an onboard embedded device; the published dataset retains every tenth scan. Source: UCI MetroPT-3 Dataset (DOI 10.24432/C5VW3R).')
) AS source (EquipmentCode, EquipmentName, EquipmentType, Location, Description)
    ON target.EquipmentCode = source.EquipmentCode
WHEN MATCHED THEN UPDATE SET
    target.EquipmentName = source.EquipmentName,
    target.EquipmentType = source.EquipmentType,
    target.Location      = source.Location,
    target.Description   = source.Description
WHEN NOT MATCHED BY TARGET THEN
    INSERT (EquipmentCode, EquipmentName, EquipmentType, Location, Description)
    VALUES (source.EquipmentCode, source.EquipmentName, source.EquipmentType,
            source.Location, source.Description);
GO

/* -------------------------------------------------------------------------------------
   3. asset.Sensor

   Matched on SourceColumn, which is the immutable identity of a signal in the source
   file. SensorCode is a display convenience and could change; the CSV header cannot.

   Note DV_eletric: the spelling is wrong in the source file. It is preserved exactly in
   SourceColumn, because silently renaming a source column is a data-lineage bug. The
   corrected spelling lives in SensorCode and SensorName.
   ------------------------------------------------------------------------------------- */

DECLARE @EquipmentId SMALLINT =
    (SELECT EquipmentId FROM asset.Equipment WHERE EquipmentCode = 'APU-01');

IF @EquipmentId IS NULL
    THROW 50001, 'seed.sql: equipment APU-01 is missing. Run the equipment section first.', 1;

MERGE asset.Sensor AS target
USING (VALUES
    /* --- Analogue (7) - official UCI descriptions, verbatim ------------------------- */
    ('TP2',             'TP2',             N'Compressor pressure (TP2)',              'Analogue', 'Pressure',    'bar',   CAST(-1.0 AS REAL), CAST(16.0 AS REAL),  NULL,               10,
     N'the measure of the pressure on the compressor.'),
    ('TP3',             'TP3',             N'Pneumatic panel pressure (TP3)',         'Analogue', 'Pressure',    'bar',   CAST(-1.0 AS REAL), CAST(16.0 AS REAL),  NULL,               20,
     N'the measure of the pressure generated at the pneumatic panel.'),
    ('H1',              'H1',              N'Cyclonic separator pressure drop (H1)',  'Analogue', 'Pressure',    'bar',   CAST(-1.0 AS REAL), CAST(16.0 AS REAL),  NULL,               30,
     N'the measure of the pressure generated due to pressure drop when the discharge of the cyclonic separator filter occurs.'),
    ('DV_PRESSURE',     'DV_pressure',     N'Dryer tower discharge pressure drop',    'Analogue', 'Pressure',    'bar',   CAST(-1.0 AS REAL), CAST(16.0 AS REAL),  NULL,               40,
     N'the measure of the pressure drop generated when the towers discharge air dryers; a zero reading indicates that the compressor is operating under load.'),
    ('RESERVOIRS',      'Reservoirs',      N'Reservoir downstream pressure',          'Analogue', 'Pressure',    'bar',   CAST(-1.0 AS REAL), CAST(16.0 AS REAL),  NULL,               50,
     N'the measure of the downstream pressure of the reservoirs, which should be close to the pneumatic panel pressure (TP3).'),
    ('OIL_TEMPERATURE', 'Oil_temperature', N'Compressor oil temperature',             'Analogue', 'Temperature', 'degC',  CAST(-20.0 AS REAL), CAST(150.0 AS REAL), NULL,              60,
     N'the measure of the oil temperature on the compressor.'),
    ('MOTOR_CURRENT',   'Motor_current',   N'Motor phase current',                    'Analogue', 'Current',     'A',     CAST(-1.0 AS REAL), CAST(30.0 AS REAL),  NULL,               70,
     N'the measure of the current of one phase of the three-phase motor; it presents values close to 0A - when it turns off, 4A - when working offloaded, 7A - when working under load, and 9A - when it starts working.'),

    /* --- Digital (8) - official UCI descriptions, verbatim -------------------------- */
    ('COMP',            'COMP',            N'Air intake valve signal (COMP)',         'Digital',  'ValveState',       NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,               80,
     N'the electrical signal of the air intake valve on the compressor; it is active when there is no air intake, indicating that the compressor is either turned off or operating in an offloaded state.'),
    ('DV_ELECTRIC',     'DV_eletric',      N'Compressor outlet valve signal',         'Digital',  'ValveState',       NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,               90,
     N'the electrical signal that controls the compressor outlet valve; it is active when the compressor is functioning under load and inactive when the compressor is either off or operating in an offloaded state.'),
    ('TOWERS',          'Towers',          N'Drying tower selection',                 'Digital',  'DryerTowerSelect', NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,              100,
     N'the electrical signal that defines the tower responsible for drying the air and the tower responsible for draining the humidity removed from the air; when not active, it indicates that tower one is functioning; when active, it indicates that tower two is in operation.'),
    ('MPG',             'MPG',             N'Load start signal (MPG)',                'Digital',  'StartSignal',      NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), CAST(8.2 AS REAL), 110,
     N'the electrical signal responsible for starting the compressor under load by activating the intake valve when the pressure in the air production unit (APU) falls below 8.2 bar; it activates the COMP sensor, which assumes the same behaviour as the MPG sensor.'),
    ('LPS',             'LPS',             N'Low pressure switch (LPS)',              'Digital',  'LowPressureSwitch',NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), CAST(7.0 AS REAL), 120,
     N'the electrical signal that detects and activates when the pressure drops below 7 bars.'),
    ('PRESSURE_SWITCH', 'Pressure_switch', N'Tower discharge pressure switch',        'Digital',  'DischargeSwitch',  NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,              130,
     N'the electrical signal that detects the discharge in the air-drying towers.'),
    ('OIL_LEVEL',       'Oil_level',       N'Oil level alarm',                        'Digital',  'OilLevelAlarm',    NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,              140,
     N'the electrical signal that detects the oil level on the compressor; it is active when the oil is below the expected values.'),
    ('CAUDAL_IMPULSES', 'Caudal_impulses', N'Air flow pulse counter',                 'Digital',  'FlowPulse',        NULL, CAST(0.0 AS REAL), CAST(1.0 AS REAL), NULL,              150,
     N'the electrical signal that counts the pulse outputs generated by the absolute amount of air flowing from the APU to the reservoirs.')
) AS source (SensorCode, SourceColumn, SensorName, SensorClass, MeasurementType, Unit,
             PhysicalMin, PhysicalMax, DocumentedSetpoint, DisplayOrder, Description)
    ON target.SourceColumn = source.SourceColumn
WHEN MATCHED THEN UPDATE SET
    target.SensorCode         = source.SensorCode,
    target.SensorName         = source.SensorName,
    target.SensorClass        = source.SensorClass,
    target.MeasurementType    = source.MeasurementType,
    target.Unit               = source.Unit,
    target.PhysicalMin        = source.PhysicalMin,
    target.PhysicalMax        = source.PhysicalMax,
    target.DocumentedSetpoint = source.DocumentedSetpoint,
    target.DisplayOrder       = source.DisplayOrder,
    target.Description        = source.Description
WHEN NOT MATCHED BY TARGET THEN
    INSERT (EquipmentId, SensorCode, SourceColumn, SensorName, SensorClass, MeasurementType,
            Unit, PhysicalMin, PhysicalMax, DocumentedSetpoint, DisplayOrder, Description)
    VALUES (@EquipmentId, source.SensorCode, source.SourceColumn, source.SensorName,
            source.SensorClass, source.MeasurementType, source.Unit, source.PhysicalMin,
            source.PhysicalMax, source.DocumentedSetpoint, source.DisplayOrder, source.Description);
GO

/* -------------------------------------------------------------------------------------
   4. ops.FailureEvent

   The four maintenance reports published alongside the dataset, entered verbatim.
   Dates are parsed as M/D/YYYY, which is unambiguous here because the first event is
   "4/18/2020" and there is no month 18.

   DataQualityNote records our observations about the SOURCE table's defects. Those are
   not corrections - the source values are kept exactly as published.
   ------------------------------------------------------------------------------------- */

DECLARE @Eq SMALLINT = (SELECT EquipmentId FROM asset.Equipment WHERE EquipmentCode = 'APU-01');

MERGE ops.FailureEvent AS target
USING (VALUES
    (CAST('2020-04-18T00:00:00' AS DATETIME2(3)), CAST('2020-04-18T23:59:00' AS DATETIME2(3)),
     N'Air leak', N'High stress', N'#1', NULL,
     N'Source table lists this as "#1". Profiling found 69.5% of this event''s 24-hour lead-up is frozen data (see docs/DATA_PROFILE.md §10), so the lead-up is NOT usable for pre-failure analysis.'),
    (CAST('2020-05-29T23:30:00' AS DATETIME2(3)), CAST('2020-05-30T06:00:00' AS DATETIME2(3)),
     N'Air Leak', N'High stress', N'#1', N'Maintenance on 30Apr at 12:00',
     N'Source table numbers this "#1" a second time; it is almost certainly #2. Its maintenance note is dated 30 April against a 29-30 May window. Both defects preserved as published.'),
    (CAST('2020-06-05T10:00:00' AS DATETIME2(3)), CAST('2020-06-07T14:30:00' AS DATETIME2(3)),
     N'Air Leak', N'High stress', N'#3', N'Maintenance on 8Jun at 16:00',
     N'Longest documented event at 52.5 hours. Window coverage 91.6%.'),
    (CAST('2020-07-15T14:30:00' AS DATETIME2(3)), CAST('2020-07-15T19:00:00' AS DATETIME2(3)),
     N'Air Leak', N'High stress', N'#4', N'Maintenance on 16Jul at 00:00',
     N'Shortest documented event at 4.5 hours.')
) AS source (StartTs, EndTs, FailureType, Severity, SourceReference, ReportNote, DataQualityNote)
    ON target.StartTs = source.StartTs AND target.EndTs = source.EndTs
WHEN MATCHED THEN UPDATE SET
    target.FailureType     = source.FailureType,
    target.Severity        = source.Severity,
    target.SourceReference = source.SourceReference,
    target.ReportNote      = source.ReportNote,
    target.DataQualityNote = source.DataQualityNote
WHEN NOT MATCHED BY TARGET THEN
    INSERT (EquipmentId, StartTs, EndTs, FailureType, Severity, SourceReference,
            ReportNote, DataQualityNote)
    VALUES (@Eq, source.StartTs, source.EndTs, source.FailureType, source.Severity,
            source.SourceReference, source.ReportNote, source.DataQualityNote);
GO

PRINT 'seed.sql: reference and master data are present.';
GO
