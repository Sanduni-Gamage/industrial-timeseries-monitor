# Data Dictionary - UCI MetroPT-3 Dataset

Every field description below is taken verbatim, or lightly reformatted, from the official
UCI Machine Learning Repository metadata for dataset ID 791, retrieved from
`https://archive.ics.uci.edu/api/dataset?id=791`. Nothing in the "Official description"
column is inferred or invented. Where this project adds an interpretation, it is separated
out under engineering notes.

## 1. Dataset identity

| Attribute | Value |
|---|---|
| Name | MetroPT-3 Dataset |
| UCI ID | 791 |
| DOI | `10.24432/C5VW3R` |
| Landing page | https://archive.ics.uci.edu/dataset/791/metropt+3+dataset |
| Creators | Narjes Davari, Bruno Veloso, Rita Ribeiro, João Gama |
| Year created | 2021 (last updated 19 Sep 2024) |
| Declared instances | 1,516,948 |
| Declared features | 15 (7 analogue + 8 digital) |
| Declared coverage | February - August 2020 |
| Declared missing values | "no" |
| Characteristics | Tabular, Multivariate, Time-Series |

### Equipment context (official)

> "It consists of multivariate time series data obtained from several analogue and
> digital sensors installed on the compressor of a train. The data span between
> February and August 2020 and includes 15 signals, such as pressures, motor
> current, oil temperature, and electrical signals of air intake valves. […] The
> data were logged at 1Hz by an onboard embedded device."

The monitored asset is the Air Production Unit (APU) of a metro train, a compressor that
supplies compressed air to the train's pneumatic systems: brakes, doors, suspension. This
project models it as a single piece of equipment carrying 15 sensor tags.

### A documented contradiction, resolved by measurement

The UCI page states the sampling rate in two places and they appear to disagree:

- `instances_represent`: "The data were logged at 1Hz by an onboard embedded device."
- `variable_info`: "1516948 data points collected at 0.1Hz from February to August 2020"

Both statements are true. They describe different things. Measured from the file (see
`DATA_PROFILE.md`):

| Measurement | Value |
|---|---|
| `index` column step between consecutive rows | exactly 10, for all 1,516,947 steps |
| `index` range | 0 … 15,169,470 |
| Modal / median timestamp interval | 10 s |
| Timestamp span | 2020-02-01 00:00:00 → 2020-09-01 03:59:50 |

The `index` column counts the original 1 Hz scans. The published file retains every tenth
one, so the acquisition rate was 1 Hz and the published rate is 0.1 Hz. The mismatch
between 15,169,471 original scans and 1,516,948 published rows is exactly the decimation
factor, which confirms the reading.

The remaining discrepancy is real missing coverage. 213 days at 10 s would be 1,841,760
rows, but only 1,516,948 are present: 331 gaps longer than 30 s, the largest 48 hours.
Overall coverage is 82.36%. The honest description is therefore 1 Hz acquisition, published
at 0.1 Hz, with 17.6% of the timeline absent.

---

## 2. Column reference

### 2.1 Identity and time columns

| Column | UCI role | UCI type | Official description | Engineering notes |
|---|---|---|---|---|
| `index` | ID | Integer | "index of data" | Row ordinal from the source export. Retained in the raw layer for traceability, but not used as a database key. |
| `timestamp` | Other | Categorical | "date time" | Stored as text in the CSV. Parsed to `datetime2(3)`. The source does not declare a timezone, so it is treated as naive local time and documented as such rather than assumed to be UTC. |

### 2.2 Analogue sensors (7), signals 1-7

Descriptions below are verbatim from the UCI `variable_info` block.

| Column | Unit | Official description |
|---|---|---|
| `TP2` | bar | "the measure of the pressure on the compressor." |
| `TP3` | bar | "the measure of the pressure generated at the pneumatic panel." |
| `H1` | bar | "the measure of the pressure generated due to pressure drop when the discharge of the cyclonic separator filter occurs." |
| `DV_pressure` | bar | "the measure of the pressure drop generated when the towers discharge air dryers; a zero reading indicates that the compressor is operating under load." |
| `Reservoirs` | bar | "the measure of the downstream pressure of the reservoirs, which should be close to the pneumatic panel pressure (TP3)." |
| `Motor_current` | A | "the measure of the current of one phase of the three-phase motor; it presents values close to 0A - when it turns off, 4A - when working offloaded, 7A - when working under load, and 9A - when it starts working." |
| `Oil_temperature` | °C | "the measure of the oil temperature on the compressor." |

On `Motor_current`: the source gives four named operating states with nominal currents
(0 A off, 4 A offloaded, 7 A under load, 9 A starting). This is the single most valuable
piece of domain documentation in the dataset. It lets us derive an interpretable
operating-state signal without inventing thresholds. The exact band edges are still
calibrated from the observed distribution and recorded in `SQL_DESIGN.md`.

On `Reservoirs` versus `TP3`: the source states these "should be close" to each other.
That is a documented physical invariant, which makes it a legitimate, non-arbitrary
cross-sensor data-quality rule rather than a guessed threshold.

### 2.3 Digital sensors (8), signals 8-15

These are electrical and logic signals. The CSV encodes them as floating-point `0.0` and
`1.0`, so UCI lists their type as "Continuous"; semantically they are binary states.

| Column | Official description |
|---|---|
| `COMP` | "the electrical signal of the air intake valve on the compressor; it is active when there is no air intake, indicating that the compressor is either turned off or operating in an offloaded state." |
| `DV_eletric` | "the electrical signal that controls the compressor outlet valve; it is active when the compressor is functioning under load and inactive when the compressor is either off or operating in an offloaded state." |
| `Towers` | "the electrical signal that defines the tower responsible for drying the air and the tower responsible for draining the humidity removed from the air; when not active, it indicates that tower one is functioning; when active, it indicates that tower two is in operation." |
| `MPG` | "the electrical signal responsible for starting the compressor under load by activating the intake valve when the pressure in the air production unit (APU) falls below 8.2 bar; it activates the COMP sensor, which assumes the same behaviour as the MPG sensor." |
| `LPS` | "the electrical signal that detects and activates when the pressure drops below 7 bars." |
| `Pressure_switch` | "the electrical signal that detects the discharge in the air-drying towers." |
| `Oil_level` | "the electrical signal that detects the oil level on the compressor; it is active when the oil is below the expected values." |
| `Caudal_impulses` | "the electrical signal that counts the pulse outputs generated by the absolute amount of air flowing from the APU to the reservoirs." |

On spelling: `DV_eletric` is misspelled in the source file; it should be "electric". The
column name is preserved exactly as-is in the raw layer, and the corrected display name
lives in the `Sensor` reference table. Silently renaming a source column is a data-lineage
bug, not a tidy-up.

On the two documented setpoints: `MPG` describes an 8.2 bar start threshold and `LPS` a
7 bar low-pressure threshold. These are manufacturer setpoints stated by the data owner,
not values we chose. They are used as documented reference lines in the dashboard and as
sanity checks in validation.

### 2.4 Signals notably absent

There is no vibration, no flow rate in engineering units, no ambient temperature, no train
speed or position, and no per-row label column. The dataset is explicitly described as
unlabeled. Failure information exists only as the separate report table below.

---

## 3. Failure reports (official)

The UCI page states: "The dataset is unlabeled, but the failure reports provided by the
company are available in the following table."

Reproduced verbatim, including its inconsistencies:

| Nr. | Start Time | End Time | Failure | Severity | Report |
|---|---|---|---|---|---|
| #1 | 4/18/2020 0:00 | 4/18/2020 23:59 | Air leak | High stress | |
| #1 | 5/29/2020 23:30 | 5/30/2020 6:00 | Air Leak | High stress | Maintenance on 30Apr at 12:00 |
| #3 | 6/5/2020 10:00 | 6/7/2020 14:30 | Air Leak | High stress | Maintenance on 8Jun at 16:00 |
| #4 | 7/15/2020 14:30 | 7/15/2020 19:00 | Air Leak | High stress | Maintenance on 16Jul at 00:00 |

### Known defects in the source table, flagged rather than silently corrected

1. Duplicate identifier. The second row is numbered `#1`; the sequence then jumps to `#3`
   and `#4`. The second event is almost certainly `#2`. We store the source label verbatim
   in `SourceReference` and assign our own surrogate key.
2. Report and date mismatch. Event 2 spans 29-30 May but its report says "Maintenance on
   30Apr at 12:00". We do not know which is wrong. Stored verbatim, flagged in
   `docs/DATA_PROFILE.md`.
3. Ambiguous date format. `4/18/2020` is unambiguously US `M/D/YYYY`, since there is no
   month 18, so all four rows are parsed that way. Documented here so the assumption is
   auditable.
4. All four events are the same failure mode: air leak, high stress. The dataset therefore
   supports one failure mode with four occurrences. Any claim of general "predictive
   maintenance accuracy" from n=4 events would be statistically unsupportable, and this
   project does not make one.

---

## 4. Citation

Required by the dataset's terms of use:

```bibtex
@inproceedings{davari2021predictive,
  title={Predictive maintenance based on anomaly detection using deep learning for
         air production unit in the railway industry},
  author={Davari, Narjes and Veloso, Bruno and Ribeiro, Rita P and Pereira, Pedro Mota
          and Gama, Jo{\~a}o},
  booktitle={2021 IEEE 8th International Conference on Data Science and Advanced
             Analytics (DSAA)},
  pages={1--10}, year={2021}, organization={IEEE}
}

@article{veloso2022metropt,
  title={The MetroPT dataset for predictive maintenance},
  author={Veloso, Bruno and Ribeiro, Rita P and Gama, Jo{\~a}o and Pereira, Pedro Mota},
  journal={Scientific Data}, volume={9}, number={1}, pages={764}, year={2022},
  publisher={Nature Publishing Group UK London}
}
```

---

## 5. Mapping to this project's database

Each of the 15 signal columns becomes one row in `asset.Sensor`, all belonging to one row
in `asset.Equipment`, the APU. Every measurement becomes one row in `ts.SensorReading`.
Rationale and trade-offs are in `docs/SQL_DESIGN.md`.

| CSV column | SensorCode | SensorClass | MeasurementType | Unit |
|---|---|---|---|---|
| `TP2` | TP2 | Analogue | Pressure | bar |
| `TP3` | TP3 | Analogue | Pressure | bar |
| `H1` | H1 | Analogue | Pressure | bar |
| `DV_pressure` | DV_PRESSURE | Analogue | Pressure | bar |
| `Reservoirs` | RESERVOIRS | Analogue | Pressure | bar |
| `Motor_current` | MOTOR_CURRENT | Analogue | Current | A |
| `Oil_temperature` | OIL_TEMPERATURE | Analogue | Temperature | degC |
| `COMP` | COMP | Digital | ValveState | - |
| `DV_eletric` | DV_ELECTRIC | Digital | ValveState | - |
| `Towers` | TOWERS | Digital | DryerTowerSelect | - |
| `MPG` | MPG | Digital | StartSignal | - |
| `LPS` | LPS | Digital | LowPressureSwitch | - |
| `Pressure_switch` | PRESSURE_SWITCH | Digital | DischargeSwitch | - |
| `Oil_level` | OIL_LEVEL | Digital | OilLevelAlarm | - |
| `Caudal_impulses` | CAUDAL_IMPULSES | Digital | FlowPulse | - |

`MeasurementType` values are our own classification, labelled as such, derived directly
from the official descriptions above rather than from guesswork about what the sensors do.
