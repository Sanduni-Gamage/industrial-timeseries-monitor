# Historian Concepts

What separates a process historian from a table with timestamps in it, which of those
properties this project implements, and which it does not.

This project does not use a historian. There is no PI System, IP.21, Wonderware, Canary,
InfluxDB or TimescaleDB anywhere in it. The store is Microsoft SQL Server. What follows is
an implementation of historian concepts on top of a general-purpose RDBMS, with each one
measured rather than asserted.

---

## 1. The defining property: compression

A historian does not store every reading. It stores only readings that a straight line
cannot reconstruct within a stated tolerance, and it guarantees that tolerance. Plant
deployments commonly retain 1-10% of raw samples.

This is implemented as Swinging Door Trending in
[`analytics/compression.py`](../analytics/compression.py), the classic formulation.

### The algorithm

From the last archived point `(t0, y0)` with deviation `E`, a candidate straight line must
pass within `±E` of every reading since. Each subsequent point `(ti, yi)` constrains the
slope:

```
s >= (yi - E - y0) / (ti - t0)      it must not dip below the point's lower bound
s <= (yi + E - y0) / (ti - t0)      nor rise above its upper bound
```

Track the tightest lower and upper bounds seen. While they still overlap, some line covers
every point and none of them need storing: the "door" is open. When the lower bound exceeds
the upper, no such line exists, the door closes, and the previous point is archived.

### Measured on the real archive

10,618,636 analogue readings, deviation set as a percentage of each sensor's observed span,
which is the unit an engineer actually configures:

| Deviation | Readings retained | Ratio | Kept |
|---|---|---|---|
| 0.02% | 4,761,015 | 2.2× | 44.84% |
| 0.05% | 3,090,944 | 3.4× | 29.11% |
| 0.10% | 2,444,738 | 4.3× | 23.02% |
| 0.25% | 1,641,588 | 6.5× | 15.46% |
| 0.50% | 1,256,259 | 8.5× | 11.83% |
| 1.00% | 833,713 | 12.7× | 7.85% |

Per sensor at 0.1%, where the spread is the interesting part:

| Sensor | Ratio | Kept | Max error | Budget used |
|---|---|---|---|---|
| `DV_PRESSURE` | 24.6× | 4.06% | 0.009875 bar | 0.9999 |
| `H1` | 7.7× | 13.03% | 0.010324 bar | 1.0000 |
| `TP2` | 7.3× | 13.78% | 0.010706 bar | 0.9998 |
| `RESERVOIRS` | 4.9× | 20.31% | 0.009587 bar | 0.9999 |
| `TP3` | 4.9× | 20.36% | 0.009572 bar | 1.0000 |
| `MOTOR_CURRENT` | 2.5× | 40.48% | 0.009274 A | 0.9998 |
| `OIL_TEMPERATURE` | 2.0× | 49.13% | 0.073648 °C | 1.0000 |

"Budget used" is the guarantee made visible: max error divided by the allowance. Every
sensor sits at 0.9998-1.0000, meaning the algorithm uses essentially all of its tolerance
and none of it exceeds. Tight rather than conservative. Anything above 1.0 would mean the
implementation is wrong.

The 12× spread between sensors has a physical explanation. `DV_PRESSURE` sits flat near
-0.02 bar for most of its life, so long straight runs collapse to two points.
`OIL_TEMPERATURE` carries noise comparable to its deadband, so almost every reading
genuinely differs. Compression ratio is a property of the signal, not of the algorithm,
which is why a single global setting is the wrong way to configure a plant.

One finding worth knowing. At 0.02% of span, `OIL_TEMPERATURE` retains 80.67%. Its span is
73.65 °C, so 0.02% is a 0.0147 °C corridor, narrower than the instrument's own 0.025 °C
quantisation step. Setting a deadband below sensor resolution defeats compression entirely
and stores quantisation noise. That is a real configuration mistake made in real plants.

### Two deliberate departures from textbook SDT

1. A gap forces the door shut. Drawing a line across a 48-hour hole would invent a
   measurement, and every other part of this project refuses to interpolate across a gap.
2. A quality change forces the door shut. Held (frozen) data must never be spanned by a
   line drawn from a genuine reading, because the reconstruction would look like real
   measurement.

### And one correction the textbook does not mention

The swinging door guarantees that some line from the anchor stays within `E`. It does not
guarantee that the line actually drawn at reconstruction time, the chord between the two
archived endpoints, is that line. The chord is pinned to the endpoints; a feasible corridor
line is not. The documented worst case for chord reconstruction is 2E, and a test with
noise just under the deadband reproduced it: a 0.05 corridor produced a 0.0561 error.

Historians accept that, because SDT runs online in one pass over a stream and never
revisits a decision. This project compresses a complete archive in batch, so it can afford
to verify its own work. Each segment is checked against the chord and split at its worst
point until it complies. The result is a bound that is true rather than approximately true.
See [`DEV_LOG.md`](DEV_LOG.md) DL-043.

---

## 2. Interpolated retrieval

You do not ask a historian "give me the rows between A and B". You ask "what was this tag
reading at 14:07:33", and it answers from the two archived points that bracket the request,
whether or not it stored anything at that instant.

`analytics.fn_ValueAt(@SensorId, @At, @GapSeconds)` in
[`database/historian.sql`](../database/historian.sql). An inline table-valued function, so
SQL Server folds it into the calling plan rather than invoking it row by row.

### Verified end to end

Taking 111,692 `TP3` readings the compressor discarded, asking `fn_ValueAt` for those exact
instants, and comparing to what was actually measured:

| | |
|---|---|
| Readings checked | 111,692 |
| Deviation allowance | 0.009572 bar |
| Max reconstruction error | 0.009518 bar |
| Mean reconstruction error | 0.003407 bar |
| Exceeding the bound | 0 |

The database returns values for readings it never stored, and every one is inside the
stated tolerance.

### Three behaviours worth stating

- Linear between bracketing points, the same reconstruction the error bound was verified
  against.
- Nothing returned across a gap. No row, rather than a line through a period when nothing
  was recorded.
- Digital tags step, they do not ramp. A valve is open or shut, and interpolating it to
  0.63 is meaningless.

---

## 3. Time-weighted aggregation

A historian weights each reading by how long it held, not by how many samples arrived. The
two differ whenever sampling is irregular, and this archive has 9-13 s jitter and 331 gaps.
The simple mean is the one that is quietly wrong, because it over-weights whatever the
logger happened to sample densely.

Computed alongside the simple mean in
[`database/aggregates.sql`](../database/aggregates.sql), compared in
`analytics.vw_AggregateComparison`.

### How much does it actually matter?

Across 66,240 hourly buckets, usually very little, and occasionally a great deal:

| Sensor | Mean abs. difference | Max abs. difference | Buckets >1% apart |
|---|---|---|---|
| `OIL_TEMPERATURE` | 0.0045 °C | 1.0966 °C | 4 |
| `TP2` | 0.0037 bar | 0.6159 bar | 129 |
| `H1` | 0.0037 bar | 0.5644 bar | 36 |
| `MOTOR_CURRENT` | 0.0031 A | 0.3765 A | 107 |

The divergence concentrates exactly where you would expect, in sparse buckets, where uneven
sampling has the most leverage:

| Sensor | Bucket | Samples | Simple mean | Time-weighted | Difference |
|---|---|---|---|---|---|
| `TP2` | 2020-03-21 01:00 | 25 | 1.1622 | 1.7443 | +50.08% |
| `TP2` | 2020-05-25 20:00 | 25 | 1.7898 | 2.3352 | +30.48% |
| `TP2` | 2020-06-09 20:00 | 22 | 2.1424 | 2.7583 | +28.75% |
| `H1` | 2020-05-17 11:00 | 22 | 3.0118 | 3.5762 | +18.74% |

A 50% difference on a real bucket. Not an academic distinction.

### The clamp, and why it is not optional

Each reading's weight is the time until the next one, clamped at the 30 s gap threshold.
Without the clamp, the single reading before a 48-hour gap would carry 48 hours of weight
and completely dominate its bucket. Clamping is what a historian does under a "maximum
interval" setting; the alternative is a number that says more about the outage than about
the machine.

---

## Concept-by-concept scorecard

| Historian concept | Here? | Where |
|---|---|---|
| Tag-based data model | yes | `asset.Sensor` plus a narrow fact table |
| Quality codes on every value | yes | `ref.QualityCode`, OPC DA convention (Good 192 / Uncertain 64 / Bad 0) |
| Raw archive plus aggregate archive | yes | `ts.SensorReading` plus `SensorHourlyAgg` / `SensorDailyAgg` |
| Deadband / swinging-door compression | yes | `analytics/compression.py`, 4.3× measured |
| Interpolated retrieval at any instant | yes | `analytics.fn_ValueAt` |
| Time-weighted aggregation | yes | `SensorHourlyAgg.TimeWeightedAvg` |
| Time-window retrieval optimised by key | yes | Clustered `(SensorId, ReadingTs)` |
| Gap awareness, never interpolate across | yes | `ops.vw_TimestampGap`, enforced in compression and retrieval |
| Data lineage and audit trail | yes | `ops.IngestionRun`, `ops.RejectedRow` |
| Asset hierarchy and templates (PI AF) | no | One equipment row; no hierarchy, no templates |
| Native OPC UA / field-protocol collectors | no | Loads a CSV. Listed under Future improvements |
| Store-and-forward buffering at the collector | no | No collector exists to buffer |
| Automatic retention tiering | no | Nothing ages out |
| Sub-second ingest at 100k+ tag scale | no | 15 tags at 0.1 Hz; ~20,700 readings/s on load |
| Redundant collectors, high availability | no | Single node |
| Exception reporting at the collector | no | Compression runs in batch, not at the edge |

---

## What this does and does not qualify as

It does demonstrate that the concepts are understood well enough to implement, measure, and
find the non-obvious failure mode in. The 2E chord problem is not something you meet by
reading about SDT.

It does not substitute for having run a historian in production. Configuring compression
per tag across thousands of points, sizing archives, managing collector failover and
rationalising alarms with operators are operational skills this project cannot provide.

The honest sentence:

> "I haven't used a commercial historian. I implemented its core mechanisms on SQL Server,
> swinging-door compression, interpolated retrieval and time-weighted aggregation, and
> measured them on 22.7 million real readings, so I understand what those systems do and
> what they cost."

---

## Reproducing the numbers

```powershell
# The trade-off curve across deviations. Writes nothing.
.venv\Scripts\python.exe scripts\run_compression.py --sweep

# Compress at 0.1% of span and store the result plus its measured cost.
.venv\Scripts\python.exe scripts\run_compression.py --deviation-pct 0.1 --store

# The tests, including 40 fuzzed series that must all respect the bound.
.venv\Scripts\python.exe -m pytest tests\test_compression.py -q
```

```sql
-- What compression cost and what it bought.
SELECT * FROM analytics.vw_CompressionSummary ORDER BY CompressionRatio DESC;

-- Where the simple and time-weighted means disagree most.
SELECT TOP (20) * FROM analytics.vw_AggregateComparison
WHERE SampleCount < 200 ORDER BY ABS(Difference) DESC;

-- A value at an instant that was never stored.
SELECT * FROM analytics.fn_ValueAt(15, '2020-06-05T10:17:23', 30);
```
