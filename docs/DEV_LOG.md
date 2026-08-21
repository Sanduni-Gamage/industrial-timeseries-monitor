# Development Log

A running, unsanitised record of problems hit while building this project: what broke,
why, and what fixed it. Kept because "it worked first time" is never true, and because
the recovery is usually the interesting part in an interview.

Format: `ID | Phase | Symptom | Root cause | Fix | Lesson`

---

## Phase 1 - Dataset investigation and design

### DL-001 - `SERVERPROPERTY` returned an unusable type

- Symptom. A PowerShell connectivity probe against all three SQL Server instances
  failed with:
  `Implicit conversion from data type sql_variant to varchar is not allowed. Use the CONVERT function to run this query.`
  All three instances reported `FAIL`, which looked like a connectivity problem.
- Root cause. Not connectivity at all - the connections opened fine. `SERVERPROPERTY()`
  returns `sql_variant`, and `CONCAT()` will not implicitly convert it to a string.
- Fix. `CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) + ' | ' + CAST(...)`.
- Lesson. A `try/catch` that wraps both connect and execute will report a query
  bug as a connection failure. Worth separating the two in `health-check.ps1` so the
  script's diagnosis is actually diagnostic. Carried into the Phase 6 design.

### DL-002 - PowerShell inline command mangled by nested quoting

- Symptom. `bash: unexpected EOF while looking for matching backtick` when invoking
  `powershell -Command "..."` from the Bash tool with a script containing backticks and
  `$_`.
- Root cause. Three levels of quoting (Bash → `powershell -Command` → PowerShell
  string), with the PowerShell escape character (backtick) also being Bash's command
  substitution character.
- Fix. Write the PowerShell to a `.ps1` file and run it with
  `powershell -NoProfile -ExecutionPolicy Bypass -File script.ps1`.
- Lesson. All PowerShell in this project lives in files, never in inline `-Command`
  strings. This also makes the scripts lintable by `Invoke-ScriptAnalyzer` in CI.

### DL-003 - `/tmp` path invisible to the Windows Python interpreter

- Symptom. `curl -o /tmp/uci791.json` succeeded and `ls /tmp/uci791.json` listed it,
  but `python` immediately raised `FileNotFoundError` for the same path.
- Root cause. Git Bash maps `/tmp` to a Windows temp directory internally; the
  native Windows Python interpreter resolves `/tmp` literally as `D:\tmp`, which does
  not exist. Two different filesystem views of the same string.
- Fix. Use explicit Windows-style paths for anything crossing the Bash/Python
  boundary.
- Lesson. Directly justifies the "no absolute or shell-specific paths in code" rule:
  every path in this project comes from configuration and is resolved with `pathlib.Path`.

### DL-004 - UCI download URL advertises no size

- Symptom. `curl -I` returned `200 OK` with no `Content-Length`, and a byte-range
  request (`-r 0-0`) was ignored - so the archive size could not be determined before
  downloading.
- Root cause. UCI generates the ZIP on demand and streams it with chunked
  transfer-encoding. There is nothing to report a length from.
- Fix. None available server-side. The download is performed with a progress
  indicator and its true size, SHA-256 and row count are recorded in the ingestion
  manifest afterwards.
- Lesson. Never assume an upstream source will tell you how big a payload is before
  you fetch it. The ingestion pipeline reads the CSV in chunks rather than
  `pd.read_csv()` in one shot, precisely so that an unexpectedly large file degrades
  throughput instead of exhausting memory.

### DL-005 - Source documentation contradicts itself on sampling rate

- Symptom. The UCI page states 1 Hz in one section and 0.1 Hz in another; neither is
  consistent with 1,516,948 rows over a ~212-day span (which averages ~12 s/row).
- Root cause. Unknown - a documentation defect at the source.
- Fix. Not fixable by reading. Measured empirically in `docs/DATA_PROFILE.md`; the
  gap-detection threshold is derived from the measured modal interval, not from
  either documented figure.
- Lesson. The classic industrial-data trap. Metadata describes what someone
  believed the system did; the archive records what it actually did. Always profile.

### DL-006 - Download aborted at 9 MB with a connection reset

- Symptom. `curl: (56) Recv failure: Connection was reset` after ~11.7 MB of a
  208 MB archive. `curl` exited non-zero but left a partial file on disk that
  `file` still identified as a valid ZIP - silent corruption waiting to happen.
- Root cause. UCI streams a generated ZIP; the connection dropped mid-stream.
  Because there is no `Content-Length` (see DL-004), nothing downstream could tell
  the truncated file from a complete one by size alone.
- Fix. Re-fetch with `--retry 6 --retry-all-errors --retry-delay 5` plus a stall
  detector (`--speed-limit 1024 --speed-time 60`) so a hung transfer is aborted and
  retried rather than sitting until timeout. Completed at 218,381,995 bytes.
- Lesson. A partial download can look valid. Integrity is now verified by
  reading the ZIP central directory and comparing the entry's declared uncompressed
  length against the extracted file, and the SHA-256 of both archive and CSV is
  recorded. `ingest.ps1` will refuse to run against a file whose hash is unknown.

### DL-007 - `ExtractToDirectory` overwrite overload missing on PowerShell 5.1

- Symptom.
  `Cannot convert argument "entryNameEncoding", with value: "True", to type "System.Text.Encoding"`.
- Root cause. The 3-argument `ZipFile::ExtractToDirectory(source, dest, overwrite)`
  overload is .NET Core / .NET 5+. On .NET Framework 4.x - which Windows PowerShell 5.1
  uses - the third parameter is `entryNameEncoding`, so `$true` bound to the wrong
  parameter. Exactly the PS 5.1-vs-7 divergence flagged in the implementation plan,
  hit within the hour.
- Fix. Iterate `$zip.Entries` and call
  `[System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)`,
  which does have an overwrite flag on .NET Framework.
- Lesson. Every PowerShell snippet in this project is validated against 5.1,
  not 7.x. Added to the Phase 6 checklist, and `Invoke-ScriptAnalyzer` runs in CI with
  the 5.1 target ruleset.

### DL-008 - Frozen-block count under-reported (my bug, caught in review)

- Symptom. The profiler reported "10 blocks" of frozen data regardless of the
  actual number.
- Root cause. `block_count` was set to `len(blocks)`, but `blocks` had already been
  truncated to the top 10 for display. The summary statistic was reporting the length of
  the display list.
- Fix. Count the run boundaries before truncation (`block_count = starts.size`).
  True answer: 24 blocks, not 10.
- Lesson. Never derive a summary count from a list that was built for presentation.
  A test asserting `block_count >= len(longest_blocks)` goes into Phase 7.

### DL-009 - Global IQR fences are unusable on a duty-cycled machine

- Symptom. The first profile run produced IQR fences for `TP2` of
  `-0.020 … -0.004` bar, against an observed range of `-0.032 … 10.68` bar. Taken as an
  alarm limit, that rule would flag every moment the compressor actually runs. The fence
  for `Motor_current` came out at -5.611 A - a negative current.
- Root cause. Not a code bug - a method bug. The compressor is off 54.65% of the
  time, so each signal is bimodal. Both quartiles land inside the idle mode and the
  1.5×IQR fences never reach the operating mode. Textbook outlier statistics assume
  unimodality; industrial duty-cycled equipment violates that assumption constantly.
- Fix. Compute baselines per operating state, derived from the `Motor_current`
  bands the dataset documentation itself provides. `analytics.SensorBaseline` gained an
  `OperatingState` column.
- Lesson. The most valuable output of profiling was not a number, it was discovering
  that the default statistical method was wrong for this data. Had the pipeline been
  written first and profiled later, this would have shipped as a wall of false alarms.

### DL-010 - 3.35% of the archive is frozen, and the metadata says it is complete

- Symptom. All seven analogue signals hold bit-identical values simultaneously for
  up to 51 hours - `Motor_current` pinned at 5.575 A while `COMP` reads 0, and
  `Oil_temperature` at exactly 65.15 °C for two days.
- Root cause. A data-acquisition freeze in the source logger (last-value-hold).
  Not something this project can fix - something it must detect.
- Impact. 50,870 samples across 24 blocks. Worse, 69.5% of the 24-hour lead-up to
  documented failure event #1 is frozen, which cuts the events usable for pre-failure
  analysis from four to three.
- Fix. Change-detection check across all analogue signals; affected rows ingested
  and marked `UNCERTAIN_STALE`, excluded from baselines, shown as a separate band in
  the dashboard. Never deleted.
- Lesson. The dataset declares `has_missing_values: no`, and cell-wise that is
  true - there is not one null. Completeness is not correctness. Null checks and range
  checks both pass this data; only a change-detection check catches it. This is the
  finding the whole project is built to make.

### DL-011 - `.git` directory vanished between commands

- Symptom. A commit succeeded (`24bd7e4`, 10 files). Several commands later,
  `git status` in the same directory returned
  `fatal: not a git repository (or any of the parent directories): .git`, and `.git`
  was gone from disk. No `rm`, `git clean` or destructive command had been run.
- Root cause. Not established. Most plausible candidate is an external process
  on this machine (real-time antivirus scanning, or a folder-sync client) removing or
  quarantining the directory. The repository was brand new and contained no hooks or
  submodules. Recorded as unexplained rather than given a tidy guess.
- Fix. Re-initialised and re-committed as `a7f972d`; verified `.git` persisted and
  the working tree was clean afterwards. Nothing was lost - the commit contained only
  files still on disk.
- Lesson. Two changes to how this project works. First, repository state is
  verified after every commit, not assumed from the commit's exit code. Second, this
  is a concrete argument for pushing to a remote early: the only copy of history on one
  machine is not history. A GitHub remote is added at the start of Phase 2, before
  there is meaningful work to lose.

---

## Phase 2 - Database and ingestion

### DL-012 - Load stalled for 3.5 minutes on `RESOURCE_SEMAPHORE`

- Symptom. The full load ran at a steady ~1,850 source rows/s until roughly 16.5
  million readings, then stopped dead. The row count did not move for over three
  minutes. It looked like a hang.
- Diagnosis. `sys.dm_exec_requests` showed the session `suspended`, `wait_type =
  RESOURCE_SEMAPHORE`, `wait_time = 206,761 ms`, `blocking_session_id = 0`. That
  combination is specific: not a lock, not a blocker - the query was queued waiting for
  a memory grant. `sys.dm_exec_query_memory_grants` showed a ~62 MB request against
  a `required_memory_kb` of only 512 KB, i.e. the optimiser had picked a plan with a
  large hash or sort operator.
- Root cause. `stg.SensorReadingStage` was a heap. For the
  `INSERT ... WHERE NOT EXISTS` anti-join against a fact table that had grown past 16
  million rows, SQL Server chose a hash-based plan and asked for workspace memory it then
  had to queue for. Express caps the buffer pool at ~1.4 GB, so the query-memory pool it
  draws from is small.
- Fix. Give the staging table a clustered index on the same key as the fact table
  (`SensorId, ReadingTs`). With both sides ordered on the join key the optimiser can seek
  or merge instead of hashing, which needs almost no workspace memory. The pipeline
  already emits rows tag-major and time-ascending, so filling that index is an append at
  15 insertion points rather than a random scatter.
- Verified. The re-run measured 2,305 rows/s at the same point in the file where
  the original heap-based load was doing 1,857 rows/s - and with no stall.
- Lesson. "The process is hung" and "the process is waiting for memory it will
  eventually get" look identical from outside. The wait type is the whole diagnosis, and
  it took thirty seconds to find. It also generalises: a staging table is not
  automatically better as a heap. What matters is the shape of the query that reads it.

### DL-013 - `PRINT` with a subquery silently killed an entire batch

- Symptom. `aggregates.sql` reported
  `Subqueries are not allowed in this context. Only scalar expressions are allowed. (1046)`
  and no aggregate rows were built - the hourly and daily tables stayed empty.
- Root cause. The batch ended with
  `PRINT CONCAT('...', (SELECT COUNT_BIG(*) FROM analytics.SensorHourlyAgg))`. `PRINT`
  accepts only scalar expressions. Crucially this is a compile-time error, so SQL
  Server rejected the whole batch before executing any of it - including the
  `TRUNCATE` and the large `INSERT` above it.
- Fix. `DECLARE @HourlyBuckets BIGINT = (SELECT ...); PRINT CONCAT(..., @HourlyBuckets);`
- Lesson. T-SQL compiles a batch as a unit. A trivial mistake in a diagnostic
  `PRINT` on the last line will prevent the real work on the first line from running, and
  the error message points at the `PRINT`, not at the statement that did not happen. It
  is a good argument for keeping `GO` separators tight around expensive statements so a
  cosmetic failure cannot take real work down with it.

### DL-014 - "Exit code 4" was not a bug

- Symptom. The columnstore index build returned exit code 4 with no output at all,
  which looked like a crash inside `CREATE COLUMNSTORE INDEX` - plausible, given Express
  memory limits and 22.7 million rows.
- Root cause. The command was killed when the working session was interrupted. Not a
  database problem, not a script problem.
- Fix. Re-ran it: exit 0, all three indexes present
  (clustered 381 MB, columnstore 178 MB, time-ordered nonclustered 229 MB).
- Lesson. Recorded because the instinct was to start "fixing" a columnstore build
  that was never broken. An exit code with no diagnostic output is more often an
  environment or lifecycle problem than a logic error, and the cheapest first move is to
  run it again cleanly before changing anything.

### DL-015 - Idempotency proven, both paths

Not a failure - recorded because it is a claim worth having evidence for.

| Path | Result |
|---|---|
| Re-run, same file | SHA-256 matched run 1 → skipped in 3 s instead of 18 min |
| Re-run with `--force` | Full pass in 666.5 s, 0 rows inserted, 22,754,220 matched as already present |

The `--force` pass also confirms the DL-012 fix. It held 2,250-2,305 source rows/s
from first chunk to last, against 1,839 rows/s and a 3.5-minute stall at the same point
in the original heap-based load - 39% faster overall (666 s vs 1,098 s) with no
`RESOURCE_SEMAPHORE` wait at any point. Both runs independently reported the same
50,855 frozen scans and 331 gaps.

Two independent mechanisms, deliberately: the hash short-circuit is the fast path, and
`INSERT ... WHERE NOT EXISTS` is the correct path that still holds if the ledger is lost
or the file is renamed. Reconciliation across the whole load:
1,516,948 source rows x 15 sensors = 22,754,220 readings, and
762,825 held readings / 15 = 50,855 frozen scans, matching the independent Python
profiler's count to within the 6-sample flatline threshold.

---

## Writing the tests - three real bugs, none of which the clean dataset could reveal

Tests were pulled forward from Phase 7 for a specific reason: the MetroPT-3 file is
clean - zero nulls, zero duplicate timestamps, zero out-of-range values - so the entire
rejection and quarantine path had never executed once across two full 22.7-million-row
loads. Code that only runs when something goes wrong is exactly the code that is broken
when something goes wrong.

Feeding the real pipeline a deliberately corrupted file found three defects immediately.

### DL-016 - A truncated source file crashed instead of failing cleanly

- Symptom. `pandas.errors.EmptyDataError: No columns to parse from file`, raised out
  of `verify_header` as an unhandled traceback.
- Root cause. `verify_header` called `pd.read_csv(path, nrows=0)` without supplying
  column names, so a zero-byte file had nothing to infer a header from.
- Why it mattered more than a tidy traceback. DL-006 is precisely this scenario: the
  UCI endpoint sends no `Content-Length` and drops connections mid-stream, leaving a
  partial file that still passes a cursory look. A crash gives no ledger row, no
  diagnosis, and a stack trace an operator has to interpret.
- Fix. Catch it and raise the same `ValueError` a structurally wrong header raises,
  with a message naming the likely cause. The pipeline returns `FAILED` and points at
  `data/README.md` for the expected hashes.

### DL-017 - Reconciliation cried wolf on every quarantined row

- Symptom. A load with 3 quarantined rows reported
  `unaccounted 45` and raised a warning, when nothing at all had been lost.
- Root cause. The check computed expected readings as `rows_read x sensors`. Rows the
  pipeline had deliberately refused were still counted as expected, so every rejection
  produced a phantom shortfall of 15 readings.
- Why it mattered. This is the load's self-audit - the one number that should mean
  "the pipeline lost something". A check that fires on correct behaviour gets ignored,
  and then it is worthless on the day it is right.
- Fix. Subtract quarantined rows and missing cells from the expectation; both are
  recorded elsewhere and can be audited. What remains unaccounted is genuinely
  unexplained, so the severity was also raised from `WARNING` to `CRITICAL`.

### DL-018 - `ops.vw_DataQualitySummary` was double-counting every issue

- Symptom. A run with one 6-row flatline reported 12. A 1-row gap reported 2.
- Root cause. Two different kinds of row live in `ops.DataQualityIssue`: detail
  rows describing one specific event, and summary rows carrying the exact run total for
  an issue type. Nothing distinguished them, and the view summed `AffectedRows` across
  both.
- Impact. The shipped view was wrong, and every quality figure the Phase 3 report and
  the dashboard would have drawn from it would have been exactly double. It went
  unnoticed because on the real load the numbers still looked plausible.
- Fix. Added `ops.DataQualityIssue.IsSummary`, threaded through the validator, the
  writer and the reconciliation row; the view now takes totals from summary rows and
  counts detail rows separately. Included a guarded, idempotent, self-healing backfill for
  the 690 rows written before the column existed.
- Lesson. Two record types sharing a table with nothing to tell them apart is a design
  fault, not a query bug. The query was the symptom.

### Result

85 tests, 0.5 s for the 75 that need no database. The 10 integration tests build and drop
their own throwaway database and skip automatically when no SQL Server is reachable, so
`pytest` stays green on a clean clone and in CI.

One test of mine was wrong rather than the code: a freeze of N identical scans produces
N-1 flagged rows, because the first scan of a run is the last genuine reading and
stays `GOOD`. Worth recording because the off-by-one is easy to reintroduce.

---

## Phase 3 - Data quality and analytics

### DL-019 - `fast_executemany` rejected ISO date strings

- Symptom. `Invalid character value for cast specification (0)` when storing
  baselines. The identical string worked fine in a plain `execute`.
- Root cause. `fast_executemany` binds by a parameter type inferred once for the
  whole batch rather than letting the driver convert per row. An ISO string bound to
  `DATETIME2` fails, and the error names neither the column nor the value.
- Fix. Convert to `datetime` objects once before the batch.
- Lesson. The flag that made ingestion fast (DL-012's neighbour) also makes binding
  stricter. Worth knowing before the error appears somewhere less obvious.

### DL-020 - The anomaly unique key predated per-state detection

- Symptom. Thousands of `Violation of UNIQUE KEY constraint 'UQ_Anomaly'` on the
  first real detection run.
- Root cause. The key was `(SensorId, ReadingTs, Method)`, designed in Phase 2 before
  Phase 3 established that detection has to be per operating state. A sensor can
  legitimately be abnormal in its LOADED behaviour and normal while OFFLOADED in the same
  hour.
- Fix. Widened to include `OperatingState`, with a guarded, idempotent migration that
  inspects the constraint's actual column list.
- Lesson. The constraint was not wrong when written; the model underneath it changed.
  A unique key encodes a claim about what makes a row distinct, and that claim needs
  revisiting whenever the analysis grows a dimension.

### DL-021 - A view that could never return a row

- Symptom. `analytics.vw_AbnormalReading` returned 0 rows even after 45 baselines and
  15,000 anomalies existed.
- Root cause. It joined to `analytics.SensorBaseline` on `OperatingState = 'ALL'`.
  Phase 2 anticipated a global baseline; Phase 3 established that a global baseline is
  actively wrong on this machine, so no `'ALL'` row is ever written. The view was
  join-eliminating itself to nothing.
- Fix. Joined state-to-state against `SensorHourlyStateAgg`, and excluded degenerate
  baselines whose fences collapse onto a point.
- Lesson. An empty result is not self-evidently "no problems found". This one looked
  like healthy equipment for two phases.

### DL-022 - A health indicator that could never return to NORMAL

- Symptom. `vw_EquipmentHealth` reported CRITICAL with 7,059 critical anomalies.
- Root cause. It counted every anomaly ever detected across seven months. A machine
  that had one bad afternoon in March would show CRITICAL forever.
- Fix. Scoped to the last 24 hours - of the archive, not the wall clock. Against
  the wall clock every reading in a 2020 dataset is stale and the panel would be
  permanently empty.
- Lesson. "Health" is a statement about now. Any status that only accumulates is a
  history, and dressing a history as a status is how dashboards get ignored.

### DL-023 - The same gaps counted once per ingestion run, then over-corrected

- Symptom. The quality report claimed 664 gaps. The archive has 331.
- Root cause. Two ingestion runs each recorded 331 gap rows. `ops.vw_TimestampGap`
  had no run scoping, so re-running ingestion appeared to create new gaps in the data.
- First fix, which was worse. Scoping to the most recent `SUCCEEDED` or `PARTIAL`
  run made the report claim 2 gaps - because the most recent run was a
  `--limit-days 1` development subset covering one day.
- Actual fix. Scope to the latest `SUCCEEDED` run only. A `PARTIAL` run is by
  definition a subset and must never define the archive's quality picture.
- Lesson. Two wrong answers in a row, from the same query, in the same session. The
  useful habit was checking the number against the independently-derived figure in
  `DATA_PROFILE.md` (331) rather than accepting whatever the report printed.

### DL-024 - `scipy` DLL blocked by an Application Control policy

- Symptom. `DLL load failed while importing _sosfilt: An Application Control policy
  has blocked this file`, so Holt-Winters was unavailable - intermittently. It failed for
  the first series and succeeded for the second in the same process.
- Root cause. A machine security policy on this host, not a code defect.
- Handling. Holt-Winters is imported inside a `try` and its absence is logged and
  skipped; the naive and moving-average baselines always run. Forecasting degrades to a
  smaller comparison rather than taking the pipeline down.
- Lesson. An optional dependency should be optional at runtime, not just in
  `requirements.txt`. Recorded because it will look like a code bug to the next person.

### DL-025 - A finding I had to walk back

Not a code defect. Recorded because it is the most important thing that happened in
this phase.

Comparing the 24 hours before each failure against the February baseline, the compressor
appeared to run far more than normal before failing - physically plausible, since an air
leak forces it to work harder. It was tempting to report that as a pre-failure indicator.

Checking the same figures against every rolling 24-hour window in the archive instead
of against February alone: event #2 sits at the 64th percentile, event #3 at the 38th
- below the median. Only event #4 is unusual. The apparent pattern came from anchoring on
February, which is itself an atypically quiet month near the 12th percentile of activity.

The claim was wrong, and the only reason it was caught is that it was checked against the
whole archive rather than against the window that made it look good. Full detail in
`docs/ANALYTICS_FINDINGS.md` §5.

---

## Phase 4 - FastAPI

### DL-026 - A window frame cannot take a bound parameter

- Symptom. `Incorrect syntax near '@P1'` from the `/trends` endpoint, followed by
  `Statement(s) could not be prepared`.
- Root cause. `ROWS BETWEEN ? PRECEDING AND CURRENT ROW`. SQL Server requires the
  window-frame extent to be a literal; a parameter marker there does not compile. The
  error names the parameter, not the clause, so it reads like a syntax error in the query
  rather than a restriction on where parameters are allowed.
- Fix. Interpolate the frame size, but coerce it inside the repository -
  `max(1, min(int(window_hours), 168)) - 1` - rather than trusting the route's validation.
  Route validation already bounds it; doing it again at the data layer means the
  interpolation cannot carry anything but a number even if the repository is called
  directly. Every other value stays a bound parameter.
- Lesson. "Parameterise everything" has a genuine exception, and the safe response is
  to narrow the type at the point of interpolation rather than to relax the rule.

### DL-027 - The dashboard's main endpoint took 724 ms

- Symptom. `/summary` measured 724 ms; `/data-quality` alone was 537 ms of it.
- Root cause. The good/held reading counts scan all 22.7 million rows. Correct, and
  recomputed on every dashboard poll to produce an identical answer - those numbers only
  change when ingestion runs, which is minutes of work.
- Fix. A 60-second in-process TTL cache on that one summary. 724 ms → 30 ms p95.
- Deliberately not done. A general caching layer, which would be a second source of
  truth to keep correct. One value, one lifetime, one clear comment saying why.

### DL-028 - The error handler was untestable by default

- Symptom. `test_unhandled_error_is_500_with_a_reference` failed with the raw
  `RuntimeError` instead of asserting on a 500 response.
- Root cause. Starlette's `TestClient` re-raises server exceptions by default, which
  is helpful when debugging a handler and wrong when the handler is the thing under
  test.
- Fix. `TestClient(app, raise_server_exceptions=False)` for that test only.
- Lesson. The default made the test framework disagree with production behaviour. The
  500 path is exactly the one that must be verified, since it is the one that runs when
  something unexpected happens in front of a user.

### DL-029 - Routes that looked missing (false alarm)

Introspecting `app.routes` after `include_router` showed 8 entries instead of 14, which
looked like the routers had failed to attach. They had not: this version of FastAPI keeps
included routers as `_IncludedRouter` objects rather than flattening their routes into
the parent list.

Recorded because the instinct was to start "fixing" working code. The check that settled
it in seconds was hitting the endpoints with `TestClient` - testing the behaviour rather
than the internal representation of it.

### DL-030 - `launch.json` belongs to the working directory, not the repository

`preview_start` looks for `.claude/launch.json` under the session's primary working
directory (`D:\Projects\Prodcut Eng`), not under the project folder inside it. Fixed by
placing it there with paths relative to that root. Noted so the same five minutes are not
lost again in Phase 5 when the Vite dev server needs an entry.

---

## Phase 5 - React dashboard

### DL-031 - A case-insensitive filesystem deleted a file I had just written

- Symptom. The dashboard rendered with theme colours and component styles applied but
  no layout at all - the navigation showed as a bulleted list. `app.css` existed, was
  imported correctly, and had no errors. It was simply 2 KB instead of 6 KB, and started
  partway through.
- Root cause. Clearing out the Vite template, I ran `rm -f App.css index.css` in the
  same command that had just written `app.css`. Windows filesystems are
  case-insensitive, so `App.css` and `app.css` are the same file. My layout stylesheet
  was deleted moments after being written. Later `>>` appends silently recreated it
  containing only the sections added after the deletion.
- Fix. Rewrote `app.css` in full.
- Lesson. This is a Windows-specific class of bug worth remembering: `rm Foo.css` can
  destroy `foo.css`, and `git mv foo.ts Foo.ts` can be a no-op. It was hard to spot
  because nothing failed - no error, no missing import, just a file that was quietly
  shorter than it should have been.

### DL-032 - A y-axis starting at zero hid the data

- Symptom. The pressure chart was a nearly flat line across the top of an empty plot.
  Recharts defaults a numeric y-axis to `[0, dataMax]`; `TP3` sits between 8.5 and 9.7 bar,
  so three quarters of the plot was empty and the variation was compressed into a band a
  few pixels tall.
- Fix. A domain fitted to the data with 8% padding.
- Why that is not the "truncated axis" mistake. On a bar chart a non-zero baseline
  genuinely misleads: bar length encodes magnitude, so cutting the baseline exaggerates
  differences. On a line chart of a continuous physical quantity, zero is not a
  meaningful reference - process monitoring has plotted temperature and pressure this way
  forever, and forcing zero destroys exactly the variation an operator is looking for. The
  rule is about what the mark encodes, not about axes in general.

### DL-033 - `openapi-typescript` would not install

- Symptom. `npm error peer typescript@"^5.x" from openapi-typescript@7.13.0`. The Vite
  template had scaffolded TypeScript 6.
- Rejected fix. `--legacy-peer-deps`, which npm itself describes as "potentially
  broken" - a permanent lie in the dependency tree to solve a one-off tooling need.
- Actual fix. Generate the types with an isolated `npx --yes openapi-typescript@7` run
  and commit the output, rather than making it a project dependency at all. Types are
  generated from a file exported by `scripts/export_openapi.py`, not from a running
  server, so generation works offline, in CI, and with no database.
- Lesson. A build-time code generator does not have to live in the dependency tree of
  the thing it generates code for.

### DL-034 - A status badge asserting a verdict before it had one

The Data Quality screen showed "Needs attention" while the health check was still in
flight, because the badge fell through to its non-healthy branch whenever `data` was
absent - which includes "has not arrived yet". Reporting a problem that has not been found
is worse than showing nothing. Fixed by rendering a neutral "Checking…" state during the
initial load.

Small, and worth logging: it is the same shape of mistake as DL-022. Both came from a UI
treating "no information" as "bad news".

---

## Phase 6 - PowerShell automation

### DL-035 - Uncaptured output became a function's return value

- Symptom. `generate-report.ps1` printed
  `[WARN] Report generator exited with code [INFO] Wrote ...report.html [INFO] Readings 22,754,220 ... 0.`
  - the Python script's entire stdout embedded inside a warning about its exit code. The
  report had generated perfectly.
- Root cause. In PowerShell, every uncaptured value a function emits becomes part of
  its return value. `Invoke-ProjectPython` ran `& $PythonExe @Arguments` and then
  `return $LASTEXITCODE`, so the caller received an array of
  `[...output lines, exit code]` rather than an integer. `if ($exitCode -ne 0)` then
  compared against an array, which is truthy, so a clean run reported as a failure.
- Fix. `& $PythonExe @Arguments | Out-Host`. `Out-Host` writes straight to the console,
  so output still streams live during a 20-minute ingestion while the function returns
  only the code.
- Why it mattered more than the cosmetic message. `ingest.ps1` used the same helper
  and had the identical latent bug. It had not fired yet only because that path happened
  not to have been exercised with a failing child process. One fix, two bugs.
- Lesson. This is the PowerShell equivalent of a function accidentally returning its
  own debug logging. Anything that emits to the pipeline inside a function is part of its
  contract whether you meant it or not.

### DL-036 - A space in the project path broke every test at once

- Symptom. All twelve exit-code test cases failed with exit `-196608`. Identical
  result for success cases and failure cases, which pointed at a broken harness.
- Root cause. `Start-Process -ArgumentList` joins its array with spaces and does no
  quoting of its own. This project lives under `D:\Projects\Prodcut Eng\`, so
  `-File D:\Projects\Prodcut Eng\...\health-check.ps1` was split into two arguments and
  PowerShell reported "Processing -File 'D:\Projects\Prodcut' failed because the file
  does not have a '.ps1' extension", a message about file extensions, for a quoting bug.
- Fix. Quote the script path, and any argument containing whitespace.
- The second half. After fixing the script path, one case still failed: the same
  problem in the arguments, where a `-CsvPath` pointing at a spaced directory split and
  caused a parameter-binding error (exit 1) before the check under test could run.
- Lesson. Worth having hit here rather than in a scheduled task. The runbook's
  `schtasks` example is quoted for exactly this reason, and the fault only shows up on
  machines whose paths contain spaces - which is most of them.

### DL-037 - `.NET` composite formatting has no `>` alignment specifier

`"{2,>10}"` threw "Error formatting a string: Input string was not in a correct format".
.NET right-aligns with a positive width (`{2,10}`) and left-aligns with a negative one
(`{2,-10}`); the `>` came from other formatting languages. Small, but it took down report
generation after every query had already succeeded.

### DL-038 - PSScriptAnalyzer could not be installed non-interactively

`Install-Module PSScriptAnalyzer` hung past a ten-minute timeout, almost certainly on the
NuGet-provider or PSGallery-trust prompt, which cannot be answered from a
non-interactive session.

Not worked around locally: the linting belongs in CI, where the module can be installed
with `-Force -SkipPublisherCheck` against an already-trusted gallery. Recorded so the
next person does not spend the same ten minutes. In the meantime the scripts are verified
by `Test-Automation.ps1`, which exercises behaviour rather than style.

### Result

Five scripts plus a shared module, all on Windows PowerShell 5.1 - no `&&`, no ternary, no
null-coalescing, no `-AsHashtable`, and no dependency on `sqlcmd`, which is not on PATH.

13 exit-code cases pass, covering every failure path by forcing it: missing file,
truncated file, unreachable database, out-of-range parameter, degraded-but-usable, and the
success path of each script.

| Case | Expected | Actual |
|---|---|---|
| Source file missing / truncated | 2 | 2 |
| Database unreachable (ingest, validate, report, setup) | 2 | 2 |
| Database unreachable (health-check - a required component) | 1 | 1 |
| API down, database fine | 3 | 3 |
| Parameter out of range | 1 | 1 |
| Success paths | 0 | 0 |

---

## Phase 7 - Testing and CI

### DL-039 - The loader contradicted its own header check

- Symptom. A test feeding the reader a file with one extra column failed with
  `invalid literal for int() with base 10: '2020-02-01 00:00:00'`.
- Root cause. `iter_chunks` supplied a positional `names` list of exactly 17
  entries. Given 18 columns, pandas treats the surplus leading column as an index, every
  field shifts one place, and the timestamp lands in the integer scan-ordinal column.
- Why it mattered. `verify_header` deliberately treats an extra column as a warning,
  documented as "a source that gained a signal is a reason to look, not a reason to abort
  a load of the signals we do understand". The loader then could not read such a file at
  all. Two halves of the same contract disagreeing.
- Fix. Select columns by name via `usecols`, reading the real header first to
  discover whatever pandas called the empty first column. Verified against the real
  208 MB file: same row counts, same dtypes, 0.5 s for 250,000 rows.
- Lesson. The bug was latent because the real file has exactly 17 columns. The test
  that found it existed only because the edge-case list in the plan said "unicode headers"
  - and the unicode file happened to have an extra column.

### DL-040 - mypy found a crash no test had reached

`WindowStat.z_vs_baseline` guarded `baseline_sd` but never `baseline_mean`, then computed
`self.mean_value - self.baseline_mean` on two `float | None` values. Any (sensor, state)
pair with no stored baseline would have raised `TypeError` - precisely the pairs a new
sensor or a rare operating state produces, and precisely the ones no existing test covered
because the current data has baselines for everything it exercises.

Static typing found it in seconds. Worth recording as the concrete argument for running
mypy at all: 177 tests had not.

### DL-041 - Fixing a lint warning introduced a render loop

- Context. `oxlint` flagged `setState` called synchronously inside an effect in the
  Sensor Explorer - a legitimate warning about a cascading render.
- The fix, which was worse. Deriving the value during render instead:
  `const range = chosenRange ?? (archiveEnd ? rangeEndingAt(...) : null)`.
- What broke. That expression produces a new object every render. The derived
  object feeds `useDebounced`, whose effect depends on it, so the effect re-ran on every
  render, whose `setState` caused another render, which derived another new object. The
  loop never settled, the debounced value never stabilised, and the fetch never fired.
  The UI showed "No readings in this period" with a perfectly correct date range above it.
- Actual fix. `useMemo` on the two inputs that can genuinely change. Here that is
  load-bearing, not an optimisation: it is what gives the object a stable identity.
- Lesson. Caught only by looking at the running page after the refactor. Typecheck,
  lint and build were all green - the failure was a silent empty panel, which is exactly
  the state a dashboard is most likely to show without anyone noticing.

### DL-042 - `npm ci` blocked by a running dev server

`npm ci` deletes `node_modules` before reinstalling, and failed with
`EPERM: operation not permitted, unlink ... rolldown-binding.win32-x64-msvc.node` because
the Vite dev server was still running and holding the native binary open. Windows will not
unlink a file that is mapped by a live process.

Stopping the dev server fixed it. Noted because the error names a permissions problem
rather than the actual cause, and CI never hits it - the runner has no dev server running.

### Result

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `mypy ingestion analytics api` | clean, 31 files |
| `pytest -m "not integration"` | 168 passed (what CI runs - no database) |
| `pytest` (all) | 177 passed |
| Coverage | 69% overall; `validator.py` 96%, `errors.py` 98%, `config.py` 97% |
| `npm run typecheck` / `lint` / `build` | clean, zero warnings |
| PowerShell 5.1 parse check | 6 scripts + 1 module, all OK |
| Exit-code contract | 13/13 |

Five CI jobs, all free-tier: Python (lint, types, tests, coverage), SQL batch parsing,
dashboard (typecheck, lint, build), OpenAPI drift, and PowerShell (PSScriptAnalyzer
plus a 5.1-specific parse check on `windows-latest`).

The OpenAPI job is the one worth explaining. The dashboard's TypeScript types are
generated from the committed spec, so if a Python response model changes and the spec is
not regenerated, the frontend compiles happily against a contract the backend no longer
serves - and nothing fails until a user hits the endpoint. CI regenerates the document and
diffs it, making that drift a build failure.

Every job was run locally before being committed. A workflow file that has never been
executed is a guess.

---


---

## Phase 9 - Historian concepts in SQL Server

### DL-043 - The swinging door's guarantee is not the guarantee you get back

The one genuine algorithmic defect in this project, and it was found by fuzzing rather
than by reading.

- Context. `analytics/compression.py` implements Swinging Door Trending: from an
  anchor point, maintain the corridor of slopes that keep every subsequent reading within
  `±E`; when the corridor closes, archive the previous point. Textbook, and it passed
  every hand-written test - straight lines collapse to two points, sawtooths keep
  everything, gaps and quality changes force the door shut.
- What failed. `test_error_bound_survives_fuzzing`, on one seed out of forty: a 0.05
  deviation produced a reconstruction error of 0.0561. Not a rounding artefact - 12%
  over the bound the module's own docstring promises.
- The cause, which is subtle enough to be worth stating precisely. The swinging door
  guarantees that some straight line from the anchor stays within `E` of every point in
  the segment. Reconstruction does not draw that line. It draws the chord between the
  two archived endpoints, and the chord is pinned to those endpoints while a feasible
  corridor line is not. They are different lines. The documented worst case for chord
  reconstruction is 2E, and a signal whose noise sits just under the deadband walks
  straight into it.
- Why every real historian ships with this. SDT exists to run online, in one pass, on
  a stream, with no memory of the points it discarded. It cannot revisit a decision, so it
  cannot check its own chord. The tolerance is treated as approximate in practice.
- Fix. `_enforce_error_bound`, a refinement pass. This project compresses a complete
  archive in batch, so it can afford to check: each segment is measured against the
  chord that will actually be drawn, and any segment that exceeds `E` is split at its
  worst point and re-checked, recursively. The bound becomes true rather than
  approximately true.
- What it cost. Measured, not assumed: on the real archive the error budget used
  landed at 0.9998-1.0000 across all seven analogue sensors - the algorithm still spends
  essentially its whole allowance, so the refinement is not buying safety by being timid.
- Lesson. Six hand-written cases all passed. Forty fuzzed ones found it on the first
  run. Hand-picked tests check what the author already thought of; this was a case the
  author had not thought of, because the textbook does not mention it.

### DL-044 - A fuzz case that was too short to have a middle

Immediately after adding the fuzzer, `IndexError` on a series of three points. The
refinement pass builds `np.arange(start + 1, end)` - the interior of a segment - and then
takes `errors.argmax()`. For an adjacent pair the interior is empty, `argmax` on an empty
array raises, and the guard `if end - start < 2: continue` was missing.

The same shape of bug reached `swinging_door` itself: `count <= 2` returns everything
before the loop can index `i - 1`.

Trivial once seen. Recorded because both were found within a minute of adding the fuzzer,
by `rng.integers(3, 400)` happening to roll a 3 - which is exactly the argument for
letting a generator pick the sizes instead of picking them yourself.

### DL-045 - A ruff auto-fix that was correct and unreadable

`ruff --fix` applied SIM114 (combine `if` branches with the same body) to the two
discontinuity checks, producing:

```python
if seconds[i] - seconds[i - 1] > gap_seconds or quality is not None and quality[i] != quality[i - 1]:
```

This is correct - `and` binds tighter than `or` - and it is precisely the line that gets
misread during a later edit, because it looks like it might be parsed the other way.
Replaced with two named conditions, `crosses_gap` and `changes_quality`, plus a comment
saying why the combined form was rejected. The lint passes either way; the named version
says what the two conditions are.

Worth logging as the general point: an auto-fix is a suggestion about correctness, not
about communication, and accepting one unread is how a codebase acquires lines nobody
wants to touch.

### DL-046 - The deadband below the instrument's own resolution

Not a bug - a configuration finding that fell out of the sweep, and the kind of thing that
happens in real plants.

At 0.02% of span, `OIL_TEMPERATURE` retained 80.67% of its readings while every other
sensor compressed 2-5×. Its span is 73.65 °C, so 0.02% is a corridor of 0.0147 °C -
narrower than the instrument's own 0.025 °C quantisation step. Below sensor
resolution, consecutive readings differ by a full quantisation step or nothing, so the
door slams shut constantly and compression stores quantisation noise at full fidelity.

The lesson is the argument against a single global compression setting: the right deadband
is a property of the individual instrument, and a value chosen without reference to its
resolution silently does nothing.

### Result

| Check | Result |
|---|---|
| `ruff check .` | clean |
| `mypy ingestion analytics api` | clean, 32 files |
| `pytest` (all) | 242 passed (was 177) |
| SQL batch parse | 17 files, `historian.sql` included |
| Compression at 0.1% of span | 10,618,636 → 2,444,738 readings, 4.3× |
| Error bound | held on every sensor at every deviation; budget used 0.9998-1.0000 |
| Interpolated retrieval | 111,692 discarded readings checked, max error 0.009518 ≤ 0.009572, 0 violations |

Three historian mechanisms now exist and are measured rather than asserted: swinging-door
compression with a verified bound, `analytics.fn_ValueAt` for retrieval at an instant that
was never stored, and time-weighted aggregation alongside the simple mean - which differ
by up to 50% on sparse buckets. `docs/HISTORIAN_CONCEPTS.md` maps each one to what a
real historian does, and lists what is still absent.

---

Entries are appended as they occur.
