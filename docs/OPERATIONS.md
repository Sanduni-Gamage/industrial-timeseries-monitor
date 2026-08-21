# Operations Runbook

How to run, check and recover this system. Written for someone who did not build it.

---

## 1. The five scripts

All live in `scripts\` and target Windows PowerShell 5.1. Run them from anywhere. Each
resolves the project root from its own location, never from the working directory.

| Script | What it does | Typical use |
|---|---|---|
| `setup.ps1` | Verifies prerequisites, creates the venv, installs dependencies, builds the database | Once, on a new machine |
| `ingest.ps1` | Loads the archive into SQL Server | After obtaining or updating the source file |
| `validate.ps1` | Checks the stored archive and regenerates the quality report | After every load; as a CI gate |
| `health-check.ps1` | Confirms database, schema, data, analytics and API are working | Monitoring; before and after a change |
| `generate-report.ps1` | Produces the HTML quality report and a text operations summary | Daily or on demand |

`Test-Automation.ps1` verifies the exit-code contract below by forcing failures.

---

## 2. Exit codes

Every script uses the same four, so a scheduler or CI job can branch on them without
reading output.

| Code | Meaning | What to do |
|---|---|---|
| 0 | Success | Nothing |
| 1 | The work failed | Investigate. Something that should have worked did not. |
| 2 | Usage or configuration error | Fix the environment. The work was never attempted. |
| 3 | Completed, but degraded | Usable. Read the warnings and decide whether to act. |

2 is separate from 1 because a job needs to distinguish "the data is bad" from "the machine
is not set up". A missing dataset, an unreachable database or an absent virtual environment
are all category 2: nothing was attempted, and no amount of retrying will help until a
human changes something.

3 exists because an API that is not running on an ingestion-only host is not a failure.
Collapsing that into 1 trains people to ignore red builds.

Verify the contract at any time:

```powershell
.\scripts\Test-Automation.ps1
```

It forces every failure path, including a missing file, a truncated file, a dead database
and an out-of-range parameter, then asserts the resulting code. 13 cases.

---

## 3. First-time setup

```powershell
.\scripts\setup.ps1 -CheckOnly    # verify prerequisites, change nothing
.\scripts\setup.ps1               # then actually set up
```

Prerequisites checked: PowerShell 5.1+, Python 3.12, Node, SQL Server, an ODBC driver.
Docker is reported as absent and fine, since this project uses a locally installed SQL
Server.

`setup.ps1` never overwrites an existing `.env`. It may contain a password, and
clobbering it on a re-run would be destructive behaviour from a script advertised as
safe.

Then obtain the dataset (see `data\README.md`) and:

```powershell
.\scripts\ingest.ps1
.venv\Scripts\python.exe scripts\run_analytics.py
.\scripts\health-check.ps1
```

---

## 4. Routine operations

### Load data

```powershell
.\scripts\ingest.ps1                    # full archive
.\scripts\ingest.ps1 -LimitDays 3       # development subset, recorded as PARTIAL
.\scripts\ingest.ps1 -Force             # re-process a file already loaded
```

Loading is idempotent. Running it twice is safe: the second run recognises the file by
its SHA-256 and stops in about three seconds. `-Force` skips that shortcut and re-checks
every row; it still inserts nothing that is already present.

The script verifies the result against `ops.IngestionRun` rather than trusting the
pipeline's console output. A pipeline that printed success and stored nothing would pass a
check that only read stdout.

### Validate

```powershell
.\scripts\validate.ps1                  # checks + HTML report
.\scripts\validate.ps1 -FailOnWarnings  # CI gate: warnings become failures
.\scripts\validate.ps1 -SkipReport      # checks only, faster
```

### Report

```powershell
.\scripts\generate-report.ps1
.\scripts\generate-report.ps1 -AnomalyDays 30 -SkipHtml
```

Writes `reports\data_quality_report.html` and `reports\operations_summary.txt`.

### Check health

```powershell
.\scripts\health-check.ps1
.\scripts\health-check.ps1 -SkipApi     # on a host that only runs ingestion
.\scripts\health-check.ps1 -Quiet       # summary only, for a scheduled task
```

---

## 5. Logging

Every script writes a timestamped transcript to `logs\`, e.g.
`logs\ingest-20260908-143552.log`.

A failure to open a log never stops the work. An unattended run that cannot write a log
file should still ingest the data and say so.

---

## 6. When something is wrong

### "Cannot connect to the database"

1. Is the service running? `Get-Service MSSQL*`
2. Does `DB_SERVER` in `.env` name the right instance? This machine has three.
3. Is an ODBC driver installed? `Get-OdbcDriver -Platform 64-bit`

`health-check.ps1` distinguishes "could not connect" from "connected but the query failed".
That distinction matters. An early diagnosis went badly wrong because a `try` block wrapped
both and reported a broken query as a connection failure (`DEV_LOG.md` DL-001).

### "Source file not found" or "looks truncated"

The UCI endpoint sends no `Content-Length` and drops connections mid-stream, so a partial
download still looks like a valid file (DL-006). Verify against the SHA-256 in
`data\README.md` and re-download with the retry flags documented there.

### An ingestion run is stuck on `RUNNING`

A ledger row left `RUNNING` means a load was interrupted. The data already committed is
valid, because loading is chunked and idempotent, so just re-run `ingest.ps1`. It will skip
what is present and continue. Close the stale row by hand if the audit trail matters:

```sql
UPDATE ops.IngestionRun SET Status = 'FAILED', CompletedUtc = SYSUTCDATETIME(),
       Notes = 'Interrupted; superseded by a later run.'
WHERE IngestionRunId = <id>;
```

### Ingestion is slow or appears to hang

If throughput collapses, check what the database is waiting on:

```sql
SELECT session_id, status, wait_type, wait_time, blocking_session_id
FROM sys.dm_exec_requests WHERE session_id > 50;
```

`RESOURCE_SEMAPHORE` with no blocking session means the query is queued for a memory grant,
not deadlocked, and it will proceed. This happened once and cost 3.5 minutes; the fix was a
clustered index on the staging table (DL-012). "Hung" and "waiting for memory" look
identical from outside, and the wait type is the whole diagnosis.

### The dashboard shows no data

1. Is the API running? `python -m api`, then check `http://127.0.0.1:8000/health`
2. Is the archive loaded? `.\scripts\health-check.ps1 -SkipApi`
3. Time ranges anchor to the end of the archive, not today. This is 2020 data, so a window
   measured from the wall clock is always empty.

### Validation reports held (frozen) readings

Expected. 3.35% of this archive is a logger repeating its last scan. Those readings are
stored, labelled `UNCERTAIN_STALE`, and excluded from every baseline and average. They are
reported rather than deleted so the record stays auditable.

---

## 7. Scheduling

`schtasks` with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` and an absolute,
quoted script path:

```powershell
schtasks /Create /TN "APU health check" /SC HOURLY `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"D:\path\to\scripts\health-check.ps1\" -Quiet"
```

The quoting is not optional. This project lives under a path containing a space, and an
unquoted path is split into two arguments. That produced an exit code of `-196608` and the
misleading message "the file does not have a '.ps1' extension" (DL-036).

---

## 8. Recovering from scratch

Everything is reproducible from the source file. Nothing is hand-edited.

```powershell
.venv\Scripts\python.exe scripts\init_database.py --drop-first --confirm-drop
.\scripts\ingest.ps1
.venv\Scripts\python.exe scripts\run_analytics.py
.\scripts\validate.ps1
.\scripts\health-check.ps1
```

Roughly 20 minutes end to end, most of it the 22.7-million-row load.

`--drop-first` requires `--confirm-drop`. A single flag that destroys a 20-minute load is
too easy to type by accident.
