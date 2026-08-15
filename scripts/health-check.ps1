<#
.SYNOPSIS
    Check that the monitoring platform is working.

.DESCRIPTION
    Verifies, in order:
      1. SQL Server is reachable
      2. the schema is present and populated
      3. data has been ingested, and how recently
      4. the analytics layer has produced baselines and detections
      5. the REST API is answering

    Written to be run unattended. It distinguishes **down** from **degraded**, because
    those need different responses: a database that cannot be reached is an outage, while
    an API that is not running when nobody asked it to be is not.

.PARAMETER SkipApi
    Do not check the REST API. Useful on a machine that only runs ingestion.

.PARAMETER MaxAgeHours
    Warn if the newest reading in the archive is older than this. Defaults to 0, which
    disables the check - this project loads a fixed 2020 archive, so staleness against
    the wall clock is expected rather than a fault. Set it when pointing at live data.

.PARAMETER Quiet
    Suppress per-check output; print only the summary.

.OUTPUTS
    Exit code 0 healthy · 1 a required component is down · 2 usage error · 3 degraded

.EXAMPLE
    .\scripts\health-check.ps1
.EXAMPLE
    .\scripts\health-check.ps1 -SkipApi -MaxAgeHours 6
#>

[CmdletBinding()]
param(
    [switch] $SkipApi,
    [ValidateRange(0, 8760)]
    [int] $MaxAgeHours = 0,
    [switch] $Quiet
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$config = Get-MonitorConfig
$logPath = Start-ScriptLog -Name 'health-check' -LogDirectory $config.LogDirectory

# Findings accumulate rather than exiting early: a health check that stops at the first
# problem tells you one thing when you wanted the whole picture.
$failures = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

function Add-Failure { param([string] $Message) [void]$failures.Add($Message); if (-not $Quiet) { Write-Fail $Message } }
function Add-Warning { param([string] $Message) [void]$warnings.Add($Message); if (-not $Quiet) { Write-Warn $Message } }
function Add-Ok      { param([string] $Message) if (-not $Quiet) { Write-Ok $Message } }

try {
    if (-not $Quiet) {
        Write-Host "Health check - $($config.Server)/$($config.Database)"
        Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    }

    # -- 1. database ------------------------------------------------------------------
    if (-not $Quiet) { Write-Step 'SQL Server' }
    $probe = Test-SqlConnection -Server $config.Server -Database $config.Database

    if (-not $probe.Connected) {
        Add-Failure "Cannot connect to $($config.Server)/$($config.Database): $($probe.Error)"
        # Nothing further can be checked without a connection.
        $code = Write-Summary -Title 'Health check' -ExitCode 1 `
            -Detail 'The database is unreachable. Start the SQL Server service, or check DB_SERVER in .env.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }
    Add-Ok "Connected to $($config.Server)"

    if (-not $probe.Queried) {
        # Connected but the query failed: a real distinction, and the one DL-001 blurred.
        Add-Failure "Connected, but the server did not answer a query: $($probe.Error)"
    } else {
        Add-Ok "SQL Server $($probe.Version) - $($probe.Edition)"
    }

    # -- 2. schema --------------------------------------------------------------------
    if (-not $Quiet) { Write-Step 'Schema' }
    $requiredTables = @(
        'asset.Equipment', 'asset.Sensor', 'ref.QualityCode', 'ts.SensorReading',
        'ops.IngestionRun', 'ops.DataQualityIssue', 'ops.FailureEvent'
    )
    $presentRows = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT CONCAT(SCHEMA_NAME(schema_id), '.', name) AS FullName FROM sys.tables
"@
    $present = @($presentRows | ForEach-Object { $_.FullName })

    $missing = @()
    foreach ($table in $requiredTables) {
        if ($present -notcontains $table) { $missing += $table }
    }
    if ($missing.Count -gt 0) {
        Add-Failure "Missing table(s): $($missing -join ', '). Run scripts\setup.ps1 or scripts\init_database.py."
    } else {
        Add-Ok "All $($requiredTables.Count) required tables present ($($present.Count) total)"
    }

    $viewCount = Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT(*) FROM sys.views'
    if ([int]$viewCount -lt 10) {
        Add-Warning "Only $viewCount view(s) present; expected 10. Apply database\views.sql."
    } else {
        Add-Ok "$viewCount views present"
    }

    # -- 3. ingestion -----------------------------------------------------------------
    if (-not $Quiet) { Write-Step 'Ingested data' }
    $readingCount = 0
    if ($missing.Count -eq 0) {
        $readingCount = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query 'SELECT COUNT_BIG(*) FROM ts.SensorReading')
    }

    if ($readingCount -eq 0) {
        Add-Failure 'No readings stored. Run scripts\ingest.ps1.'
    } else {
        Add-Ok ("{0:N0} readings stored" -f $readingCount)

        $runs = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT TOP (1) IngestionRunId, Status, CompletedUtc, RowsInserted, MaxReadingTs
FROM ops.IngestionRun WHERE Status = 'SUCCEEDED' ORDER BY IngestionRunId DESC
"@
        if ($runs.Count -eq 0) {
            Add-Failure 'No successful ingestion run recorded.'
        } else {
            $run = $runs[0]
            Add-Ok "Last successful load: run #$($run.IngestionRunId) at $($run.CompletedUtc)"
            Add-Ok "Archive covers up to $($run.MaxReadingTs)"

            if ($MaxAgeHours -gt 0 -and $null -ne $run.MaxReadingTs) {
                $ageHours = ((Get-Date) - [datetime]$run.MaxReadingTs).TotalHours
                if ($ageHours -gt $MaxAgeHours) {
                    Add-Warning ("Newest reading is {0:N1} hours old, over the {1} hour limit." -f $ageHours, $MaxAgeHours)
                } else {
                    Add-Ok ("Newest reading is {0:N1} hours old" -f $ageHours)
                }
            }
        }

        # A RUNNING row that is not actually running means an interrupted load.
        $stuck = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query "SELECT COUNT(*) FROM ops.IngestionRun WHERE Status = 'RUNNING'")
        if ($stuck -gt 0) {
            Add-Warning "$stuck ingestion run(s) still marked RUNNING. If nothing is loading, a previous run was interrupted."
        }

        $rejected = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query 'SELECT COUNT_BIG(*) FROM ops.RejectedRow')
        if ($rejected -gt 0) {
            Add-Warning ("{0:N0} row(s) in quarantine. Inspect ops.RejectedRow." -f $rejected)
        } else {
            Add-Ok 'No quarantined rows'
        }
    }

    # -- 4. analytics -----------------------------------------------------------------
    if (-not $Quiet) { Write-Step 'Analytics' }
    if ($present -contains 'analytics.SensorBaseline') {
        $baselines = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query 'SELECT COUNT(*) FROM analytics.SensorBaseline')
        if ($baselines -eq 0) {
            Add-Warning 'No baselines computed. Run scripts\run_analytics.py; anomaly detection has no reference without them.'
        } else {
            Add-Ok "$baselines baseline(s) stored"
        }

        $anomalies = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
            -Query 'SELECT COUNT(*) FROM analytics.Anomaly')
        Add-Ok ("{0:N0} anomaly record(s)" -f $anomalies)
    } else {
        Add-Warning 'Analytics tables absent.'
    }

    # -- 5. API -----------------------------------------------------------------------
    if (-not $SkipApi) {
        if (-not $Quiet) { Write-Step 'REST API' }
        $url = "http://$($config.ApiHost):$($config.ApiPort)/health"
        try {
            $response = Invoke-RestMethod -Uri $url -TimeoutSec 10 -Method Get
            if ($response.status -eq 'healthy') {
                Add-Ok "API healthy at $url"
            } else {
                Add-Warning "API reports status '$($response.status)' at $url"
            }
            foreach ($component in $response.components) {
                if ($component.status -ne 'up') {
                    Add-Warning "API component '$($component.name)': $($component.status) - $($component.detail)"
                }
            }
        } catch {
            # Not a failure: the API not running is normal on an ingestion-only host.
            # Degraded is the honest verdict, and the message says how to start it.
            Add-Warning "API not responding at $url. Start it with: python -m api"
        }
    }

    # -- verdict ----------------------------------------------------------------------
    $exitCode = 0
    $detail = 'All checks passed.'
    if ($failures.Count -gt 0) {
        $exitCode = 1
        $detail = "$($failures.Count) failure(s), $($warnings.Count) warning(s)."
    } elseif ($warnings.Count -gt 0) {
        $exitCode = 3
        $detail = "$($warnings.Count) warning(s). The system is usable but not fully healthy."
    }

    $code = Write-Summary -Title 'Health check' -ExitCode $exitCode -Detail $detail
    Stop-ScriptLog -Path $logPath
    exit $code

} catch {
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Fail $_.ScriptStackTrace
    $code = Write-Summary -Title 'Health check' -ExitCode 1 -Detail 'The check itself failed.'
    Stop-ScriptLog -Path $logPath
    exit $code
}
