# Data Profile - MetroPT-3 raw CSV

Generated `2026-09-09T03:01:20+00:00` by `scripts/profile_dataset.py`.
Every number below is measured from the file, not taken from documentation.

## 1. Source file

| Property | Value |
|---|---|
| File | `MetroPT3(AirCompressor).csv` |
| Size | 208.19 MB (218,300,507 bytes) |
| SHA-256 | `db30ccb4ea402e3c8bf2c99db06e288d4f2a772f6928f9dbe26a920d69793e24` |
| Rows | 1,516,948 |
| Columns | 17 |

## 2. Structure

Columns: `source_index`, `timestamp`, `TP2`, `TP3`, `H1`, `DV_pressure`, `Reservoirs`, `Oil_temperature`, `Motor_current`, `COMP`, `DV_eletric`, `Towers`, `MPG`, `LPS`, `Pressure_switch`, `Oil_level`, `Caudal_impulses`

The first column has an empty header in the source file, a pandas export artefact. It is read as `source_index` rather than allowing pandas to invent `Unnamed: 0`.

| Property | Value |
|---|---|
| `source_index` range | 0 … 15,169,470 |
| Monotonic increasing | True |
| Unique | True |
| Most common step | [('10.0', 1516947)] |

## 3. Timestamps and sampling

| Property | Value |
|---|---|
| First reading | 2020-02-01 00:00:00 |
| Last reading | 2020-09-01 03:59:50 |
| Span | 213.17 days |
| Unparseable | 0 |
| Strictly increasing | True |
| Unique | True |
| Duplicate timestamps | 0 |
| Backward steps | 0 |
| Modal interval | 10.0 s |
| Median interval | 10.0 s |
| Mean interval | 12.141 s |
| Gap threshold used | 30.0 s (3x modal) |
| Gaps detected | 331 |
| Rows if continuous | 1,841,760 |
| Actual coverage | 82.36% |
| Missing samples (est.) | 324,812 |

### Interval distribution (top values)

| Interval (s) | Count |
|---|---|
| 10.0 | 1,337,521 |
| 9.0 | 128,277 |
| 12.0 | 38,321 |
| 13.0 | 7,988 |
| 11.0 | 4,471 |
| 21.0 | 10 |
| 19.0 | 5 |
| 22.0 | 4 |
| 20.0 | 3 |
| 17.0 | 3 |

### Ten largest gaps

| Gap start | Gap end | Hours |
|---|---|---|
| 2020-04-25 01:10:51 | 2020-04-27 01:12:49 | 48.03 |
| 2020-06-27 10:53:07 | 2020-06-28 23:07:43 | 36.24 |
| 2020-02-28 23:57:08 | 2020-03-01 04:00:09 | 28.05 |
| 2020-08-04 07:42:28 | 2020-08-05 08:23:01 | 24.68 |
| 2020-05-24 00:39:23 | 2020-05-25 01:14:14 | 24.58 |
| 2020-07-07 15:24:51 | 2020-07-08 15:20:51 | 23.93 |
| 2020-08-22 19:11:44 | 2020-08-23 18:51:01 | 23.65 |
| 2020-05-10 00:31:17 | 2020-05-10 22:48:58 | 22.29 |
| 2020-06-07 14:19:39 | 2020-06-08 11:48:04 | 21.47 |
| 2020-04-01 13:15:40 | 2020-04-02 09:59:17 | 20.73 |

## 4. Completeness and duplication

- Total null cells across all columns: 0
- Fully duplicated rows (all columns): 0
- Duplicated (timestamp + all signals): 0

No null values in any column, consistent with the UCI declaration (`has_missing_values: no`). Note that this refers to cells. Missing time coverage is a separate matter, quantified in section 3.

## 5. Analogue sensors

| Sensor | Min | Max | Mean | Median | Std | P01 | P99 | Negative rows | Distinct |
|---|---|---|---|---|---|---|---|---|---|
| `TP2` | -0.032 | 10.68 | 1.368 | -0.012 | 3.251 | -0.024 | 10.35 | 1,275,474 | 5,257 |
| `TP3` | 0.73 | 10.3 | 8.985 | 8.96 | 0.6391 | 7.876 | 10.14 | 0 | 3,683 |
| `H1` | -0.036 | 10.29 | 7.568 | 8.784 | 3.333 | -0.024 | 10.11 | 237,996 | 2,665 |
| `DV_pressure` | -0.032 | 9.844 | 0.05596 | -0.02 | 0.3824 | -0.026 | 2.112 | 1,444,001 | 2,257 |
| `Reservoirs` | 0.712 | 10.3 | 8.985 | 8.96 | 0.6383 | 7.876 | 10.14 | 0 | 3,682 |
| `Oil_temperature` | 15.4 | 89.05 | 62.64 | 62.7 | 6.516 | 48.83 | 76.17 | 0 | 2,462 |
| `Motor_current` | 0.02 | 9.295 | 2.05 | 0.045 | 2.302 | 0.035 | 6.188 | 0 | 1,809 |

### Global IQR fences - and why they must not be used as alarm limits

| Sensor | P25 | P75 | IQR lower fence | IQR upper fence | Usable globally? |
|---|---|---|---|---|---|
| `TP2` | -0.014 | -0.01 | -0.02 | -0.004 | no - see below |
| `TP3` | 8.492 | 9.492 | 6.992 | 10.99 | yes |
| `H1` | 8.254 | 9.374 | 6.574 | 11.05 | no - see below |
| `DV_pressure` | -0.022 | -0.018 | -0.028 | -0.012 | no - see below |
| `Reservoirs` | 8.494 | 9.492 | 6.997 | 10.99 | yes |
| `Oil_temperature` | 57.78 | 67.25 | 43.56 | 81.46 | yes |
| `Motor_current` | 0.04 | 3.808 | -5.611 | 9.459 | yes |

Several of these fences are nonsense as alarm limits, and that is the point. `TP2` sits at roughly -0.01 bar for the ~55% of the time the compressor is unloaded and rises above 8 bar when it runs, so its distribution is bimodal: the quartiles both land in the idle mode and the fences exclude every loaded sample. A global IQR rule would flag normal operation as anomalous and miss genuine faults. `Motor_current` shows the same problem from the other side - its lower fence is a negative current, which is meaningless.

The consequence for the design is that baselines and anomaly thresholds are computed per operating state, not globally. Operating state is derived from the documented `Motor_current` bands and the `COMP` valve signal. This is recorded as a design decision in `docs/SQL_DESIGN.md`.

### Flatline (stuck-value) runs

| Sensor | Longest run (samples) | Hours | Held value | From | To |
|---|---|---|---|---|---|
| `TP2` | 18,515 | 51.43 | 8.39 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `TP3` | 18,515 | 51.43 | 8.506 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `H1` | 18,515 | 51.43 | -0.0139999999999993 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `DV_pressure` | 18,541 | 51.5 | -0.0139999999999993 | 2020-06-22 15:01:53 | 2020-06-25 05:08:35 |
| `Reservoirs` | 18,515 | 51.43 | 8.506 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `Oil_temperature` | 18,515 | 51.43 | 65.14999999999999 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `Motor_current` | 18,515 | 51.43 | 5.575000000000001 | 2020-06-22 15:06:11 | 2020-06-25 05:08:35 |
| `COMP` | 18,518 | 51.44 | 0.0 | 2020-06-22 15:05:41 | 2020-06-25 05:08:35 |
| `DV_eletric` | 17,951 | 49.86 | 1.0 | 2020-06-05 09:48:30 | 2020-06-08 13:54:18 |
| `Towers` | 466 | 1.29 | 1.0 | 2020-02-19 19:33:50 | 2020-02-20 05:04:33 |
| `MPG` | 18,518 | 51.44 | 0.0 | 2020-06-22 15:05:41 | 2020-06-25 05:08:35 |
| `LPS` | 254,011 | 705.59 | 0.0 | 2020-02-06 07:52:30 | 2020-03-11 19:21:46 |
| `Pressure_switch` | 7,275 | 20.21 | 1.0 | 2020-02-03 19:07:16 | 2020-02-04 15:08:59 |
| `Oil_level` | 233,669 | 649.08 | 1.0 | 2020-03-12 14:11:12 | 2020-04-13 18:31:09 |
| `Caudal_impulses` | 287,973 | 799.92 | 1.0 | 2020-07-22 13:04:51 | 2020-09-01 03:59:50 |

## 6. Digital sensors

| Sensor | Strictly binary? | Non-binary rows | Distinct values | Active % | Longest flat run |
|---|---|---|---|---|---|
| `COMP` | True | 0 | [0.0, 1.0] | 83.696% | 18,518 |
| `DV_eletric` | True | 0 | [0.0, 1.0] | 16.061% | 17,951 |
| `Towers` | True | 0 | [0.0, 1.0] | 91.985% | 466 |
| `MPG` | True | 0 | [0.0, 1.0] | 83.266% | 18,518 |
| `LPS` | True | 0 | [0.0, 1.0] | 0.342% | 254,011 |
| `Pressure_switch` | True | 0 | [0.0, 1.0] | 99.144% | 7,275 |
| `Oil_level` | True | 0 | [0.0, 1.0] | 90.416% | 233,669 |
| `Caudal_impulses` | True | 0 | [0.0, 1.0] | 93.711% | 287,973 |

## 7. Coverage by month

| Month | Rows |
|---|---|
| 2020-02 | 214,850 |
| 2020-03 | 230,448 |
| 2020-04 | 198,734 |
| 2020-05 | 212,800 |
| 2020-06 | 216,514 |
| 2020-07 | 222,638 |
| 2020-08 | 220,434 |
| 2020-09 | 530 |

- Distinct calendar days present: 212
- Rows expected in a fully-covered day: 8,640
- Days with under 50% coverage: 21

## 8. Documented failure windows

| # | Source ref | Start | End | Hours | Rows present | Coverage |
|---|---|---|---|---|---|---|
| 1 | `#1` | 2020-04-18 00:00 | 2020-04-18 23:59 | 23.98 | 8,657 | 100.25% |
| 2 | `#1` | 2020-05-29 23:30 | 2020-05-30 06:00 | 6.5 | 2,360 | 100.81% |
| 3 | `#3` | 2020-06-05 10:00 | 2020-06-07 14:30 | 52.5 | 17,315 | 91.61% |
| 4 | `#4` | 2020-07-15 14:30 | 2020-07-15 19:00 | 4.5 | 1,622 | 100.06% |

## 9. Cross-sensor checks

### `reservoirs_vs_tp3_abs_diff`

- rationale: UCI documentation states Reservoirs should be close to TP3.
- mean: 0.0018621020628261512
- p99: 0.006000000000000227
- max: 0.18200000000000038
- rows_over_0_5_bar: 0

### `motor_current_state_bands`

- rationale: UCI documents nominal states: ~0A off, ~4A offloaded, ~7A load, ~9A start.
- band_counts: {'off_lt_1A': 829000, 'offloaded_1_to_5A': 457356, 'under_load_5_to_8A': 230548, 'starting_ge_8A': 44}
- band_pct: {'off_lt_1A': 54.65, 'offloaded_1_to_5A': 30.15, 'under_load_5_to_8A': 15.2, 'starting_ge_8A': 0.0}

### `documented_setpoints`

- lps_active_rows: 5188
- tp3_below_7_bar_rows: 4531
- lps_active_and_tp3_below_7: 4362
- tp3_below_8_2_bar_rows: 139714
- note: Agreement between LPS activation and TP3 < 7 bar is evidence the documented setpoint matches the archived data.

### Held data inside the failure windows

Pre-failure analysis is only meaningful over data the logger was actually acquiring. Held (frozen) samples inside an event window or its lead-up must be excluded, not averaged in.

| # | Ref | Rows in event | Frozen | Rows in 24 h lead-up | Frozen |
|---|---|---|---|---|---|
| 1 | `#1` | 8,657 | 91 (1.05%) | 6,301 | 4,378 (69.48%) |
| 2 | `#1` | 2,360 | 0 (0.0%) | 7,466 | 0 (0.0%) |
| 3 | `#3` | 17,315 | 0 (0.0%) | 8,716 | 0 (0.0%) |
| 4 | `#4` | 1,622 | 0 (0.0%) | 6,185 | 0 (0.0%) |

## 10. Frozen archive blocks (stuck data acquisition)

Windows where all seven analogue signals are bit-identical to the previous scan. Oil temperature drifts, pressure ripples and motor current fluctuates in any real machine, so simultaneous freezing of all of them is not physical. It is the logger repeating its last good scan.

This is invisible to the two checks people reach for first: the cells are not null, and every held value is inside its plausible range.

- Frozen samples: 50,870 (3.353% of the file)
- Contiguous blocks found: 24 (top 10 shown)

| Start | End | Samples | Hours |
|---|---|---|---|
| 2020-06-22 15:06:11 | 2020-06-25 05:08:35 | 18,515 | 51.43 |
| 2020-05-26 09:19:26 | 2020-05-28 03:16:28 | 12,449 | 34.58 |
| 2020-04-20 04:49:07 | 2020-04-21 01:16:52 | 6,110 | 16.97 |
| 2020-06-12 02:01:46 | 2020-06-12 17:06:06 | 4,502 | 12.51 |
| 2020-04-17 09:20:33 | 2020-04-18 00:18:07 | 4,470 | 12.42 |
| 2020-07-21 13:45:02 | 2020-07-21 22:03:16 | 2,481 | 6.89 |
| 2020-07-22 06:43:34 | 2020-07-22 13:04:24 | 1,898 | 5.27 |
| 2020-04-13 18:29:10 | 2020-04-13 19:27:16 | 291 | 0.81 |
| 2020-03-11 18:29:41 | 2020-03-11 18:59:14 | 148 | 0.41 |
| 2020-02-17 06:38:04 | 2020-02-17 06:38:14 | 2 | 0.01 |

These rows are ingested, not deleted, and marked with quality code `UNCERTAIN_STALE`. Analytics excludes them from baseline statistics, and the dashboard shows them as a distinct 'held data' band rather than as normal operation. See `docs/SQL_DESIGN.md` section 3.1.

---

*Reproduce with* `python scripts/profile_dataset.py`. *Machine-readable form:* `reports/data_profile.json`.
