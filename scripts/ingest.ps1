<#
.SYNOPSIS
    Load the MetroPT-3 archive into SQL Server.

.DESCRIPTION
    Runs the ingestion pipeline, then reports what landed by querying the run ledger
    rather than trusting the pipeline's console output.

    Idempotent: a second run recognises the file by its SHA-256 and stops in seconds.
    -Force re-processes it anyway and still inserts nothing already present.

.PARAMETER CsvPath
    Override the source file. Defaults to METROPT_RAW_CSV from .env.

.PARAMETER LimitDays
    Ingest only the first N days. For development. The run is recorded as PARTIAL so a
    truncated load can never later be mistaken for a complete one.

.PARAMETER Force
    Re-process a file an earlier run already completed.

.PARAMETER SkipPreflight
    Skip the environment checks and go straight to the pipeline.

.OUTPUTS
    Exit code 0 success · 1 ingestion failed · 2 usage or configuration error

.EXAMPLE
    .\scripts\ingest.ps1
.EXAMPLE
    .\scripts\ingest.ps1 -LimitDays 3 -Force
#>

[CmdletBinding()]
param(
    [string] $CsvPath,
    [ValidateRange(1, 400)]
    [int] $LimitDays = 0,
    [switch] $Force,
    [switch] $SkipPreflight
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$config = Get-MonitorConfig
$logPath = Start-ScriptLog -Name 'ingest' -LogDirectory $config.LogDirectory
$started = Get-Date

try {
    Write-Host "Ingestion - $($config.Server)/$($config.Database)"
    Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

    $sourceFile = $config.CsvPath
    if (-not [string]::IsNullOrWhiteSpace($CsvPath)) { $sourceFile = $CsvPath }

    # -- preflight --------------------------------------------------------------------
    # Every one of these is a usage/configuration problem, not an ingestion failure, so
    # they exit 2. A CI job can then tell "the data is bad" from "the machine is not set
    # up", which need completely different responses.
    if (-not $SkipPreflight) {
        Write-Step 'Preflight'

        $python = Get-PythonExe -ProjectRoot $config.Root
        if ($null -eq $python) {
            Write-Fail 'No virtual environment found at .venv. Run scripts\setup.ps1 first.'
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Environment not prepared.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }
        Write-Ok "Interpreter: $python"

        if (-not (Test-Path -LiteralPath $sourceFile)) {
            Write-Fail "Source file not found: $sourceFile"
            Write-Info 'Set METROPT_RAW_CSV in .env, or pass -CsvPath. See data\README.md for how to obtain it.'
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Source file missing.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }

        $file = Get-Item -LiteralPath $sourceFile
        Write-Ok ("Source: {0} ({1:N1} MB)" -f $file.Name, ($file.Length / 1MB))

        # A truncated download is the specific failure this guards against: the UCI
        # endpoint sends no Content-Length and drops connections mid-stream, leaving a
        # file that still looks valid. See docs/DEV_LOG.md DL-006.
        if ($file.Length -lt 1MB) {
            Write-Fail "Source file is only $($file.Length) bytes. That is almost certainly a truncated download."
            Write-Info 'Verify it against the hashes in data\README.md.'
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Source file looks truncated.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }

        $probe = Test-SqlConnection -Server $config.Server -Database $config.Database
        if (-not $probe.Connected) {
            Write-Fail "Cannot connect to $($config.Server)/$($config.Database): $($probe.Error)"
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Database unreachable.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }
        Write-Ok "Database reachable ($($probe.Version))"

        $sensorCount = 0
        try {
            $sensorCount = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
                -Query 'SELECT COUNT(*) FROM asset.Sensor WHERE IsActive = 1')
        } catch {
            Write-Fail "The schema is not present: $($_.Exception.Message)"
            Write-Info 'Run scripts\setup.ps1 to create it.'
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Schema missing.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }
        if ($sensorCount -eq 0) {
            Write-Fail 'No active sensors configured. Ingestion is data-driven and has nothing to map columns to.'
            $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'Seed data missing.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }
        Write-Ok "$sensorCount sensors configured"
    }

    $python = Get-PythonExe -ProjectRoot $config.Root
    if ($null -eq $python) {
        $code = Write-Summary -Title 'Ingestion' -ExitCode 2 -Detail 'No virtual environment.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    $before = 0
    try {
        $before = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query 'SELECT COUNT_BIG(*) FROM ts.SensorReading')
    } catch { }

    # -- run ---------------------------------------------------------------------------
    Write-Step 'Running the ingestion pipeline'

    $arguments = @('-m', 'ingestion', '--csv', $sourceFile)
    if ($LimitDays -gt 0) { $arguments += @('--limit-days', "$LimitDays") }
    if ($Force) { $arguments += '--force' }

    Write-Info "python $($arguments -join ' ')"
    Write-Host ''

    $pythonExit = Invoke-ProjectPython -PythonExe $python -Arguments $arguments -WorkingDirectory $config.Root

    # -- verify ------------------------------------------------------------------------
    # The ledger is the source of truth, not the console output. A pipeline that printed
    # success and stored nothing would pass a check that only read stdout.
    Write-Step 'Verifying against the run ledger'

    $after = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT_BIG(*) FROM ts.SensorReading')

    $ledger = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT TOP (1) IngestionRunId, Status, RowsRead, RowsInserted, RowsSkippedDup,
       RowsRejected, MinReadingTs, MaxReadingTs,
       DATEDIFF(second, StartedUtc, CompletedUtc) AS Seconds
FROM ops.IngestionRun ORDER BY IngestionRunId DESC
"@

    if ($ledger.Count -gt 0) {
        $run = $ledger[0]
        Write-Info "Run #$($run.IngestionRunId): $($run.Status)"
        Write-Info ("Source rows    : {0:N0}" -f $run.RowsRead)
        Write-Info ("Inserted       : {0:N0}" -f $run.RowsInserted)
        Write-Info ("Already present: {0:N0}" -f $run.RowsSkippedDup)
        Write-Info ("Quarantined    : {0:N0}" -f $run.RowsRejected)
        Write-Info "Covers         : $($run.MinReadingTs) to $($run.MaxReadingTs)"
        if ($null -ne $run.Seconds) { Write-Info "Pipeline took  : $($run.Seconds) s" }
    }

    Write-Info ("Readings before: {0:N0}" -f $before)
    Write-Info ("Readings after : {0:N0}" -f $after)

    $elapsed = ((Get-Date) - $started).TotalSeconds

    if ($pythonExit -ne 0) {
        Write-Fail "The ingestion pipeline exited with code $pythonExit."
        $code = Write-Summary -Title 'Ingestion' -ExitCode 1 `
            -Detail "Pipeline failed. See the transcript at $logPath."
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    if ($after -eq 0) {
        Write-Fail 'The pipeline reported success but the archive is empty.'
        $code = Write-Summary -Title 'Ingestion' -ExitCode 1 -Detail 'Nothing was stored.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    if ($after -eq $before) {
        Write-Ok 'Nothing new to load; the archive already contains this data.'
    } else {
        Write-Ok ("{0:N0} new reading(s) stored" -f ($after - $before))
    }

    $code = Write-Summary -Title 'Ingestion' -ExitCode 0 `
        -Detail ("{0:N0} readings in the archive. Elapsed {1:N1} s." -f $after, $elapsed)
    Stop-ScriptLog -Path $logPath
    exit $code

} catch {
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Fail $_.ScriptStackTrace
    $code = Write-Summary -Title 'Ingestion' -ExitCode 1 -Detail 'The script itself failed.'
    Stop-ScriptLog -Path $logPath
    exit $code
}
