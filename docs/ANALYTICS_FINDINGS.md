# Analytics Findings

Everything here is measured against the full 22,754,220-reading archive. Where a result is
weak or negative, it is reported as weak or negative. Those were the more useful findings.

---

## 1. A global baseline is not just imprecise, it is wrong

The compressor duty-cycles. It is OFF for 54.65% of the archive, OFFLOADED for 30.15%,
LOADED for 15.20%. Every pressure signal is therefore bimodal, and ordinary outlier
statistics computed across all of it produce limits that look authoritative and are
useless.

Measured over the February reference window:

| Sensor | Scope | P25 | P75 | 1.5×IQR fences | Observed range |
|---|---|---|---|---|---|
| `TP2` | all states | -0.012 | -0.010 | -0.015 … -0.007 | -0.030 … 10.54 |
| `TP2` | LOADED only | 8.839 | 9.964 | 7.151 … 11.65 | -0.016 … 10.54 |
| `MOTOR_CURRENT` | all states | 0.0375 | 3.715 | -5.479 … 9.231 | 0.020 … 8.292 |
| `MOTOR_CURRENT` | OFF only | 0.0350 | 0.0375 | 0.0313 … 0.0413 | 0.020 … 0.995 |

Both failure modes are present, in opposite directions:

- `TP2` flags everything. The global fence tops out at -0.007 bar, so every moment the
  compressor actually runs, up to 10.54 bar, is an "outlier".
- `MOTOR_CURRENT` flags nothing. Its lower fence is a negative current, and its upper
  fence sits above anything the machine ever draws.

Hence `analytics.ScanState`, and hence per-operating-state baselines. Band edges come from
the four nominal currents the dataset documentation states outright (0 / 4 / 7 / 9 A). The
boundaries are the midpoints between them, not values chosen to make a result look good.

The state distribution derived in SQL matches the independent Python profiler exactly:
829,000 / 457,356 / 230,548 / 44 scans.

---

## 2. Per-state fences are still not alarm limits

Fixing the bimodality is necessary and not sufficient. Applying per-state 1.5×IQR fences
to every reading flags 1,553,181 readings, or 15.13% of the archive:

| Sensor / state | Readings | Outside 1.5×IQR | |
|---|---|---|---|
| `MOTOR_CURRENT` OFF | 805,102 | 405,935 | 50.4% |
| `OIL_TEMPERATURE` LOADED | 211,887 | 144,820 | 68.4% |
| `OIL_TEMPERATURE` OFFLOADED | 449,060 | 279,161 | 62.2% |
| All analogue | 10,262,343 | 1,553,181 | 15.1% |

That is roughly 7,300 alarms per day. EEMUA 191 and ISA-18.2 put the manageable rate at
about 150 per day per operator position, with ~300 the ceiling. A naive detector overshoots
by roughly fifty times, and an alarm list nobody can read is worse than no alarm list. It
trains people to ignore it.

Two measured causes, neither obvious.

The machine's normal behaviour drifts. Mean oil temperature under load:

| Month | Feb | Mar | Apr | May | Jun | Jul | Aug |
|---|---|---|---|---|---|---|---|
| Mean °C (LOADED) | 54.97 | 67.62 | 65.88 | 66.65 | 70.09 | 68.27 | 64.76 |

February is 10-15 °C colder than every following month. A fixed February baseline
correctly identifies a change and completely misidentifies it as a fault.

IQR fences also collapse on tightly-peaked signals. `MOTOR_CURRENT` while OFF has
P25 = 0.0350 A and P75 = 0.0375 A, so the 1.5×IQR fence is 0.01 A wide against a standard
deviation of 0.0088 A. The limit is narrower than the instrument's own scatter.

---

## 3. What actually works

Three changes, each aimed at one of the causes above:

| Change | Addresses |
|---|---|
| Compare against a trailing 7-day window of the same state, not a fixed reference | drift |
| Median and MAD, not mean and standard deviation | a developing fault inflating the statistic meant to catch it |
| Hourly evaluation with a 3-bucket persistence rule (ISA-18.2 on-delay) | chatter; a 10-second excursion is not an event |

Result:

| Detector | Alarms | Per day | Verdict |
|---|---|---|---|
| Naive per-state IQR, per reading | 1,553,181 | ~7,286 | alarm flood, ~50× over ceiling |
| `ADAPTIVE_MAD` | 2,656 | 12.5 | within EEMUA 191 target |
| `BASELINE_IQR` (hourly + persistence) | 12,381 | 58.1 | within target |
| `SETPOINT` (documented 7 bar) | 124 episodes | 0.6 | within target |

A 583× reduction, from the same data, without discarding real signal.

### The alarms are physically significant, and I checked rather than assumed

A robust z-score can be enormous simply because the denominator is tiny, so CRITICAL
outnumbering WARNING (1,959 vs 697) looked like a defect. It is not:

| Relative deviation of CRITICAL alarms from the expected band | |
|---|---|
| P10 | 6.2% |
| P50 | 30.9% |
| P90 | 10,311% |
| Under 1% of the expected value | 0.0% |

Not one CRITICAL alarm is physically trivial. No deadband floor was added, because the
measurement said it was not needed.

The largest deviations are also interpretable. `DV_PRESSURE` sat at +2.4 bar while LOADED
for twelve consecutive hours on 2020-05-13, against an expected -0.02 bar or thereabouts.
The documentation states that for this tag "a zero reading indicates that the compressor is
operating under load", so this directly contradicts documented behaviour, two weeks before
failure event #2.

---

## 4. Do the alarms relate to the documented failures?

Anomaly rate inside the four event windows (plus 24 h lead-up) against the rest of the
archive:

| Method | In windows | Elsewhere | Rate in | Rate out | Enrichment |
|---|---|---|---|---|---|
| `ADAPTIVE_MAD` | 795 | 1,861 | 4.33/h | 0.377/h | 11.5× |
| `BASELINE_IQR` | 834 | 11,547 | 4.55/h | 2.341/h | 1.9× |
| `SETPOINT` | 8 | 116 | 0.044/h | 0.024/h | 1.9× |

The adaptive detector's alarms are 11.5× more concentrated around the documented failures
than elsewhere. The fixed-baseline detector is barely better than chance, which is the
expected consequence of section 2: it spends most of its alarms on seasonal drift.

This is an association across three usable events, not a validated model. No precision,
recall or accuracy figure is quoted, because none computed from three positives of a single
failure mode would mean anything.

---

## 5. A finding I had to walk back

Comparing the 24 hours before each failure against the February baseline, the compressor
appeared to run far more than normal before failing. That is exactly what an air leak
should cause, and it was tempting to stop there:

| | Reference (Feb) | #1 | #2 | #3 | #4 |
|---|---|---|---|---|---|
| Running % (24 h before) | 28.7% | 31.0%* | 48.5% | 40.4% | 95.7% |

Then I checked those figures against every rolling 24-hour window in the archive, rather
than against February alone:

| Percentile of 24 h running-time (4,232 windows) | P5 | P25 | P50 | P75 | P90 | P95 | P99 |
|---|---|---|---|---|---|---|---|
| Running % | 27.2 | 32.3 | 45.3 | 52.6 | 63.1 | 69.4 | 97.3 |

| Event | Running % | Percentile | Verdict |
|---|---|---|---|
| #1 | 31.0%* | 19.8% | unusable, 77% held data |
| #2 | 48.5% | 64.5% | unremarkable |
| #3 | 40.4% | 38.8% | below median |
| #4 | 95.7% | 98.9% | genuinely extreme |

The signal largely disappears. Only event #4 is unusual; #2 is ordinary and #3 is below the
median. The apparent pattern came from anchoring on February, which is itself an atypically
quiet month sitting near the 12th percentile of machine activity.

Event #4 remains striking on its own terms. The compressor did not switch off once in the
12 hours before that failure, and its LOADED share rose to 70.5% in the final hour against
a 5.9% reference. That is textbook air-leak behaviour. It is also one event.

Conclusion: duty cycle is not a general pre-failure indicator in this dataset. One of three
usable events shows it. Reporting it as a predictor would have been wrong, and the only
reason it was caught is that the claim was checked against the whole archive instead of
against the window that made it look good.

### Consequence for the design

The February reference window is not representative of the archive: 28.7% running against
a 45.3% median. That single fact explains the oil-temperature drift in section 2, the 15%
naive flag rate, and why `BASELINE_IQR` enrichment is only 1.9×.

It is kept unchanged for a specific reason. It is the only substantial window that precedes
all four documented failures, so it is the only defensible failure-free reference
available. Its limitation is recorded here rather than papered over, and the primary
detector deliberately does not depend on it.

---

## 6. What was tried and rejected

| Approach | Why not |
|---|---|
| Global (all-state) baselines | Section 1: produces limits that flag everything or nothing |
| Per-reading IQR detection | Section 2: 7,286 alarms/day, ~50× the manageable rate |
| Fixed baseline as the primary detector | Section 5: February is atypical, 1.9× enrichment |
| A deadband floor on the adaptive detector | Measured as unnecessary; 0.0% of CRITICAL alarms are physically trivial |
| Reporting duty cycle as a predictor | Section 5: holds for one of three usable events |
| Any precision/recall figure | Three usable events of one failure mode cannot support one |

---

## 7. Honest limitations

- Four documented failures, three usable, one failure mode. Nothing here generalises to
  other faults, other machines, or other air-production units.
- No operating context. No train schedule, ambient temperature or load demand, so a rise
  in duty cycle cannot be separated from a busier week.
- The reference window is atypical (section 5).
- Association, not causation. The 11.5× enrichment says alarms and failures co-occur. It
  does not establish that the alarms would have been actionable in advance.
- `DV_PRESSURE` contradicting its documentation (section 3) is unexplained. It may be a
  genuine fault signature or a mislabelled tag, and this dataset cannot distinguish them.
