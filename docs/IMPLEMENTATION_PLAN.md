# Implementation Plan

## 0. Machine reconnaissance

Checked before designing anything, because the environment changes the design.

| Component | Found | Consequence for the plan |
|---|---|---|
| OS | Windows 11 Home 26200 | PowerShell automation is native, not contrived |
| PowerShell | 5.1.26100 (Windows PowerShell, not 7.x) | Scripts must avoid `&&`, `??`, ternary, `-AsHashtable`. Use `if ($?)`, explicit `$null -eq`. Verified in every script. |
| Python | 3.14.7 (default) and 3.12 | Build the venv on 3.12 - pyodbc / SciPy / statsmodels wheel coverage is reliable there; 3.14 is still patchy for the scientific stack |
| Node | v24.19.0, npm 12.0.2 | Vite 7 + React 19 fine |
| Git | 2.54.0 | Repo initialised |
| Docker | not installed, WSL not installed | Docker cannot be the primary path. `docker-compose.yml` becomes the alternative for reviewers, and must be documented as untested-on-this-machine |
| SQL Server | 2025 Express 17.0.1000.7, 3 running instances (`MSSQLSERVER`, `SQLEXPRESS`, `SQLEXPRESS01`), all reachable via Windows auth | Primary path. 10 GB per-DB limit drives the storage design |
| ODBC | Driver 18 and 17 for SQL Server | `pyodbc` works; Driver 18 requires `TrustServerCertificate=yes` for local dev |
| `sqlcmd` | not on PATH | PowerShell scripts must talk to SQL via `System.Data.SqlClient`, not `sqlcmd`. Already proven to work. |
| VS Code | installed at `D:\Softwares\Microsoft VS Code`, `code` on PATH | Recommended editor |
| Visual Studio | not installed (only a stale VS 10.0 folder) | Not used |
| Free space (D:) | ~222 GB | Dataset + database fit comfortably |

## 1. Phases and exit criteria

Each phase ends with a demonstration and a review point. Nothing from a completed
phase is rewritten later without asking first.

### Phase 1 - Dataset investigation and design  
- [x] Retrieve authoritative dataset metadata from the UCI API (not from memory)
- [x] `docs/DATA_DICTIONARY.md` - every column, verbatim source descriptions
- [x] `docs/ARCHITECTURE.md` - layers, decisions AD-1…AD-6, Mermaid diagrams
- [x] `docs/SQL_DESIGN.md` - proposed schema, indexes, views, query list
- [x] `docs/IMPLEMENTATION_PLAN.md`, `docs/DEV_LOG.md`
- [x] Acquire the CSV - 208 MB, SHA-256 verified
- [x] `profile_dataset.py` → `docs/DATA_PROFILE.md`
- [x] Every `[TBC-profile]` marker in `SQL_DESIGN.md` resolved by measurement

Exit criterion met. The 1 Hz vs 0.1 Hz contradiction is settled: the `index` column
steps by exactly 10 for all 1,516,947 steps, so acquisition was 1 Hz and the published
file keeps every tenth scan. Coverage is 82.36%. No threshold anywhere in the design is
still a guess.

### Phase 2 - Database and ingestion  
- [x] `schema.sql` (6 namespaces, 13 tables), `seed.sql`, `indexes.sql`, `aggregates.sql`,
      `views.sql` - all idempotent and re-runnable
- [x] `ingestion/` package: `config.py` (Pydantic settings), `db.py`, `loader.py`,
      `validator.py`, `transformer.py`, `writer.py`, `pipeline.py`, `__main__.py`
- [x] `scripts/init_database.py`, `scripts/run_queries.py`
- [x] 11 demonstration queries in `database/queries/`, all executed and timed
- [x] 10 views, all smoke-tested (26 ms worst case)

Exit criterion met. Measured:

| Check | Result |
|---|---|
| Full load | 1,516,948 source rows → 22,754,220 readings = exactly 15x |
| Duration | 1,097.8 s (18.3 min) at 20,727 readings/s |
| Re-run, same file | SHA-256 short-circuit, 3 s, nothing to do |
| Re-run, `--force` | 0 rows inserted, all 22,754,220 matched as present |
| Rows quarantined | 0 |
| Timestamp range | 2020-02-01 00:00:00 → 2020-09-01 03:59:50 (exact match to source) |
| Gaps recorded | 331 (matches the independent profiler exactly) |
| Held readings | 762,825 = 50,855 scans x 15 (profiler said 50,870; the 6-sample threshold explains the difference) |
| Database size | 1,032 MB - about 10% of the Express cap |

Problems hit and fixed: a `RESOURCE_SEMAPHORE` stall from a heap staging table
(DL-012), a `PRINT` subquery that silently killed a whole batch (DL-013), and one
false alarm (DL-014).

### Phase 3 - Data quality and analytics  
- [x] `analytics/operating_state.py` - every scan classified from the documented current
      bands; `analytics.ScanState` (1.5M rows). Distribution matches the Phase 1 profiler
      exactly: 829,000 / 457,356 / 230,548 / 44
- [x] `analytics/statistics.py` - 45 baselines per (sensor, state) over February 2020
- [x] `analytics/anomaly_detection.py` - adaptive MAD, fixed-baseline IQR, documented
      setpoint, all with ISA-18.2 persistence
- [x] `analytics/trends.py`, `analytics/failure_analysis.py`, `analytics/forecasting.py`
- [x] `reports/data_quality_report.html` (Jinja2, self-contained, no external assets)
- [x] `scripts/run_analytics.py`, `scripts/generate_quality_report.py`
- [x] `docs/ANALYTICS_FINDINGS.md`
- [x] 37 analytics tests; 122 tests total

Exit criterion met - every severity boundary traces to a baseline row or a documented
setpoint. Nothing is a literal in code.

| Result | |
|---|---|
| Naive per-state IQR, per reading | 1,553,181 alarms - ~7,286/day, ~50x the manageable rate |
| `ADAPTIVE_MAD` (trailing 7d, same state, MAD, 3-bucket on-delay) | 2,656 - 12.5/day |
| `BASELINE_IQR` (hourly + persistence) | 12,416 - 58.3/day |
| `SETPOINT` (documented 7 bar) | 124 episodes - 0.6/day |
| Reduction | 583x, judged against EEMUA 191 / ISA-18.2 guidance |
| Anomaly enrichment before documented failures | `ADAPTIVE_MAD` 11.5x; fixed baseline 1.9x |
| Full pipeline runtime | 189 s |

Negative results reported rather than buried: a fixed baseline is a poor detector because
February is atypical (28.7% running vs a 45.3% archive median), and the apparent
duty-cycle pre-failure signal survives in only one of three usable events (§5 of
`ANALYTICS_FINDINGS.md`). Six defects found and fixed - DL-019…DL-024.

### Phase 4 - FastAPI 
- [x] 14 documented endpoints across 4 tags; OpenAPI at `/openapi.json`, Swagger UI at
      `/docs`, ReDoc at `/redoc`
- [x] `api/models/schemas.py` - response contracts; `api/services/repository.py` - all
      SQL in one mockable seam; `api/routes/` - thin routers
- [x] RFC 7807 `application/problem+json` for every failure, with stable `type` URIs
- [x] Request-id and response-time headers on every response
- [x] `scripts/benchmark_api.py`; 46 API tests, all running without a database

Exit criterion met.

| Endpoint | p95 | Budget |
|---|---|---|
| `/health` | 10.0 ms | 400 |
| `/equipment`, `/sensors` | 4.5-7.0 ms | 100 |
| `/readings` (raw / hourly / daily) | 9.0-17.0 ms | 300 |
| `/trends` state-aware | 13.5 ms | 400 |
| `/anomalies` paginated | 46.9 ms | 400 |
| `/summary` (dashboard poll) | 30.3 ms | 500 |

Status codes verified for valid, not-found and invalid-range input: 404 for unknown
sensor or equipment, 422 for a reversed or over-wide window and for every out-of-bounds
parameter, 503 when the database is unreachable, 500 with a log reference for anything
unexpected. A test asserts that a database error carrying a server name, account and SQL
text leaks none of it to the client.

Design decisions worth naming: endpoints are synchronous `def` so FastAPI runs blocking
`pyodbc` in a worker thread rather than stalling the event loop; `resolution=auto` picks
raw/hourly/daily from the window width so a six-month request cannot ask for 1.8 million
points; any series hitting a cap returns `truncated: true` rather than being silently
trimmed. Five defects found and fixed - DL-026…DL-030.

### Phase 5 - React dashboard 
- [x] Vite 8 + React 19 + TypeScript, Recharts, React Router
- [x] Types generated from the API's own OpenAPI document - a changed Python response
      model breaks the TypeScript build rather than rendering `undefined`
- [x] Five screens: Overview, Sensor explorer, Equipment health, Anomalies, Data quality
- [x] `Panel` renders all four states - loading, error, empty, loaded - once, so no screen
      can forget one
- [x] Light and dark themes, each defined from tokens rather than inverted

Exit criterion met: the Overview answers "is this machine healthy, and what changed?"
in one call and in plain language - status with a written reason, what needs attention,
current values, trust in the data - with no statistical vocabulary anywhere on screen.

Accessibility and UX:

| | |
|---|---|
| Colour palette | Validated with a CVD checker, not chosen by eye - lightness band, chroma floor, adjacent-pair separation and contrast, in both modes |
| Status | Never colour alone: every badge carries a glyph and a word |
| Charts | One y-axis always; solid hairline gridlines; legend whenever >1 series; crosshair tooltip; labelled setpoint reference lines |
| Keyboard | Skip link, visible focus rings, all controls reachable |
| Terminology | "Flagged reading", "expected range", "how far outside" - never z-score, MAD or isolation forest |
| Defaults | Time ranges anchor to the end of the archive, not the wall clock, so the default view is never empty |
| Responsive | Reflows to a single column; wide tables scroll inside their own container |

`npm run build` and `npm run typecheck` both clean. Four defects found and fixed -
DL-031…DL-034, including a case-insensitive filesystem silently deleting a stylesheet.

### Phase 6 - PowerShell automation 
- [x] `Monitor.Common.psm1` - shared config, logging, SQL and Python helpers
- [x] `setup.ps1`, `ingest.ps1`, `validate.ps1`, `health-check.ps1`, `generate-report.ps1`
- [x] `Test-Automation.ps1` - verifies the exit-code contract by forcing failures
- [x] `docs/OPERATIONS.md` - runbook, troubleshooting, scheduling, recovery

All target Windows PowerShell 5.1: no `&&`, no ternary, no `??`, no `-AsHashtable`,
and no dependency on `sqlcmd`, which is not on PATH - SQL goes through
`System.Data.SqlClient` directly. Every script uses `[CmdletBinding()]`, validated
`param()` blocks, `$ErrorActionPreference = 'Stop'`, `try/catch/finally`, and writes a
timestamped transcript to `logs\`.

Exit criterion met - verified by forcing every failure path, not by assertion:

| Case | Expected | Actual |
|---|---|---|
| Source file missing | 2 | 2 |
| Source file truncated (the DL-006 scenario) | 2 | 2 |
| Database unreachable - ingest / validate / report / setup | 2 | 2 |
| Database unreachable - health-check (required component) | 1 | 1 |
| API down, database fine | 3 | 3 |
| Parameter out of range | 1 | 1 |
| Success paths | 0 | 0 |

13 cases, 13 passing. Configuration errors exit 2 rather than 1 on purpose: a CI job
must be able to tell "the data is bad" from "the machine is not set up". Degraded exits 3
rather than 1, because an API that is not running on an ingestion-only host is not a
failure - collapsing it into 1 teaches people to ignore red builds.

Four defects found and fixed - DL-035…DL-038, including uncaptured output silently
becoming a function's return value, and a space in the project path breaking every test
at once.

### Phase 7 - Testing and CI 
- [x] `ruff` and `mypy` clean across 31 source files
- [x] 177 tests - 168 of them need no database, which is what CI runs
- [x] Edge cases: empty file, single row, all-identical values, out-of-order rows,
      non-binary digital values, BOM and unicode headers, leap day, and both DST cases
- [x] Hand-computed fixtures for the MAD and IQR maths, worked out in the docstrings so a
      test cannot pass merely by agreeing with the implementation
- [x] `.github/workflows/ci.yml` - five jobs, free tier only

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `mypy ingestion analytics api` | clean |
| `pytest -m "not integration"` | 168 passed, no database |
| `pytest` (all) | 177 passed |
| Coverage | 69% overall; `validator.py` 96%, `errors.py` 98%, `config.py` 97% |
| Dashboard typecheck / lint / build | clean, zero warnings |
| PowerShell 5.1 parse check | all scripts OK |
| Exit-code contract | 13/13 |

CI jobs: Python (lint, types, tests, coverage) · SQL batch parsing · dashboard
(typecheck, lint, build) · OpenAPI drift · PowerShell (PSScriptAnalyzer plus a
5.1-specific parse check on `windows-latest`).

The OpenAPI job catches the failure mode nothing else would: the dashboard's types are
generated from the committed spec, so a changed Python response model that is not
regenerated leaves the frontend compiling against a contract the backend no longer serves.
CI regenerates and diffs it, turning silent drift into a build failure.

Every job was run locally before being committed - a workflow that has never executed
is a guess. Four defects found - DL-039…DL-042 - including a loader that contradicted its
own header check, a crash mypy found that 177 tests had not, and a lint fix that
introduced a render loop.

### Phase 8 - Documentation and polish 
- [x] `README.md` - problem framing, architecture diagram, measured results, example SQL,
      exact run commands, design decisions, honest limitations, future improvements
- [x] `docs/OPERATIONS.md` - runbook (delivered in Phase 6)
- [x] `docs/CV_POSITIONING.md` - approved wording, six interview stories, and the claims
      that must never be made
- [x] `docker-compose.yml` - SQL Server for reviewers who have Docker, clearly marked as
      untested on this machine
- [x] `docs/screenshots/` - capture guide naming each file, URL and what it should show

Exit criterion met. A reader can go from a clean clone to a running system using only
the README, and every number in it is reproducible by a command in it.

The limitations section is deliberately specific: not a historian, no live connection,
four failures of one mode with three usable, no operating context, an atypical reference
month, association rather than causation, and no CI badge because no remote exists yet.

### Phase 9 - Historian concepts in SQL Server 
Added after review. The brief asks the project to demonstrate concepts relevant to
industrial historians; Phases 1-8 covered the modelling side (tags, quality codes, a raw
archive beside an aggregate archive) but not the mechanism that actually distinguishes a
historian from a table with timestamps in it - it does not store every reading.

- [x] `analytics/compression.py` - Swinging Door Trending, the classic formulation, with
      two deliberate departures: a gap forces the door shut, and so does a quality change.
      Neither is in the textbook; both follow the rule the rest of this project already
      obeys - never draw a line through a period when nothing was measured.
- [x] `scripts/run_compression.py` - `--sweep` to characterise the trade-off curve
      (writes nothing), `--store` to archive the retained points and the measured cost.
      Exit code 3 is reserved for a violated error bound, because that is a defect in
      the implementation and not a property of the data.
- [x] `database/historian.sql` - `analytics.fn_ValueAt`, an inline TVF answering "what was
      this tag reading at 14:07:33" from the two archived points that bracket the request.
      Plus `vw_CompressionSummary` and `vw_AggregateComparison`.
- [x] `database/aggregates.sql` - time-weighted average alongside the simple mean, each
      reading weighted by how long it held, clamped at the 30 s gap threshold.
- [x] `database/schema.sql` - `ts.SensorReadingCompressed`, `analytics.CompressionStat`,
      `SensorHourlyAgg.TimeWeightedAvg`.
- [x] `tests/test_compression.py` - 65 tests, including 40 fuzzed series across four
      signal shapes. The fuzzer earned its place immediately (see below).
- [x] `docs/HISTORIAN_CONCEPTS.md` - a concept-by-concept scorecard: what is implemented,
      what it measured, and the six things a real historian has that this does not.
- [x] `scripts/init_database.py --with-historian` / `--all` applies `historian.sql` last,
      after the aggregates and views it reads.

Measured, not asserted.

| | |
|---|---|
| Compression at 0.1% of span | 10,618,636 → 2,444,738 readings (4.3×) |
| At 1.0% of span | 12.7× |
| Best / worst sensor | `DV_PRESSURE` 24.6× · `OIL_TEMPERATURE` 2.0× |
| Error budget used | 0.9998-1.0000 on every analogue sensor - tight, never exceeded |
| Interpolated retrieval | 111,692 discarded readings re-queried, max error 0.009518 vs 0.009572 allowance, 0 violations |
| Simple vs time-weighted mean | agree closely in general; differ by up to 50.08% on sparse buckets |
| Tests | 177 → 242 |

The defect worth the phase on its own. The swinging door guarantees that some line
from the anchor stays within `E`. Reconstruction draws the chord between the archived
endpoints, which is a different line, and can deviate up to 2E. Six hand-written tests
passed; the fuzzer found it on its first run (0.0561 against a 0.05 corridor). Fixed with a
refinement pass that verifies each segment against the chord actually drawn and splits it
where it fails - affordable here because this compresses a complete archive in batch,
which is exactly what a streaming historian cannot do. `DEV_LOG.md` DL-043.

Exit criterion met. Every historian mechanism claimed is implemented, measured against
the real 22.7 M-reading archive, and reproducible by a command in the README - and
`HISTORIAN_CONCEPTS.md` states in the same table what is still absent: asset framework,
store-and-forward collectors, native OPC connectivity, retention tiering, HA, and
edge-side exception reporting.

The positioning this phase supports, and its limit. The honest claim is now "I have
implemented a historian's core mechanisms and measured what they cost", not "I have used a
historian". The brief's constraint is unchanged and still binding: no claim of
professional experience with a commercial historian is made anywhere in this repository.

## 2. Working agreements

- Phase boundaries are review points. After each: what was built, what was tested, what
  broke, what is still open.
- Architectural changes get asked about first, not applied first.
- `docs/DEV_LOG.md` records every real error hit during development, with cause and
  fix - as requested. It is not a sanitised log.
- Commits are small and conventional (`feat:`, `fix:`, `docs:`, `chore:`), one logical
  change each.




