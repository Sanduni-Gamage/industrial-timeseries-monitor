<#
.SYNOPSIS
    Run data-quality validation and produce the quality report.

.DESCRIPTION
    Checks the stored archive rather than the source file, since validation happened
    during ingestion.

    Five checks: reconciliation (a mismatch means the pipeline lost data, which is worse
    than the data being flawed), held readings (invisible to null and range checks),
    quarantine, referential integrity, and timeline coverage.

.PARAMETER FailOnWarnings
    Treat warnings as failures. For a CI gate that should not accept a degraded archive.

.PARAMETER HeldPercentThreshold
    Warn above this percentage of held readings. Default 5; the archive measures 3.35%.

.PARAMETER SkipReport
    Run the checks but do not regenerate the HTML report.

.OUTPUTS
    Exit code 0 clean · 1 a check failed · 2 usage error · 3 warnings only

.EXAMPLE
    .\scripts\validate.ps1
.EXAMPLE
    .\scripts\validate.ps1 -FailOnWarnings
#>

[CmdletBinding()]
param(
    [switch] $FailOnWarnings,
    [ValidateRange(0, 100)]
    [double] $HeldPercentThreshold = 5.0,
    [switch] $SkipReport
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$config = Get-MonitorConfig
$logPath = Start-ScriptLog -Name 'validate' -LogDirectory $config.LogDirectory

$failures = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

function Add-Failure { param([string] $Message) [void]$failures.Add($Message); Write-Fail $Message }
function Add-Warning { param([string] $Message) [void]$warnings.Add($Message); Write-Warn $Message }

try {
    Write-Host "Data validation - $($config.Server)/$($config.Database)"
    Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

    $probe = Test-SqlConnection -Server $config.Server -Database $config.Database
    if (-not $probe.Connected) {
        Write-Fail "Cannot connect to the database: $($probe.Error)"
        $code = Write-Summary -Title 'Validation' -ExitCode 2 -Detail 'Database unreachable.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    # -- 1. is there anything to validate? --------------------------------------------
    Write-Step 'Archive'
    $total = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT_BIG(*) FROM ts.SensorReading')
    if ($total -eq 0) {
        Write-Fail 'No readings stored. Run scripts\ingest.ps1 first.'
        $code = Write-Summary -Title 'Validation' -ExitCode 2 -Detail 'Nothing to validate.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }
    Write-Ok ("{0:N0} readings stored" -f $total)

    # -- 2. reconciliation -------------------------------------------------------------
    Write-Step 'Load reconciliation'
    $recon = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT TOP (1) i.Details, i.AffectedRows, i.Severity
FROM ops.DataQualityIssue AS i
INNER JOIN ops.IngestionRun AS r ON r.IngestionRunId = i.IngestionRunId
WHERE i.IssueType = 'LOAD_RECONCILIATION' AND r.Status = 'SUCCEEDED'
ORDER BY i.IssueId DESC
"@
    if ($recon.Count -eq 0) {
        Add-Warning 'No reconciliation record found for a successful run.'
    } else {
        Write-Info $recon[0].Details
        if ([int64]$recon[0].AffectedRows -ne 0) {
            Add-Failure "Reconciliation is out by $($recon[0].AffectedRows) reading(s). The pipeline lost or duplicated data."
        } else {
            Write-Ok 'Every expected reading is accounted for'
        }
    }

    # -- 3. quality distribution --------------------------------------------------------
    Write-Step 'Quality codes'
    $quality = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT q.Code, q.Family, COUNT_BIG(*) AS N,
       CAST(100.0 * COUNT_BIG(*) / (SELECT COUNT_BIG(*) FROM ts.SensorReading) AS DECIMAL(7,4)) AS Pct
FROM ts.SensorReading AS r
INNER JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId
GROUP BY q.Code, q.Family ORDER BY N DESC
"@
    $heldPct = 0.0
    foreach ($row in $quality) {
        Write-Info ("{0,-18} {1,14:N0}  {2,7:N3}%" -f $row.Code, $row.N, $row.Pct)
        if ($row.Code -eq 'UNCERTAIN_STALE') { $heldPct = [double]$row.Pct }
        if ($row.Family -eq 'BAD') {
            Add-Warning "$($row.N) reading(s) marked $($row.Code). They are stored and excluded from statistics."
        }
    }

    if ($heldPct -gt $HeldPercentThreshold) {
        Add-Warning ("Held (frozen) readings are {0:N2}%, above the {1}% threshold." -f $heldPct, $HeldPercentThreshold)
    } elseif ($heldPct -gt 0) {
        Write-Ok ("Held readings {0:N2}%, within the {1}% threshold" -f $heldPct, $HeldPercentThreshold)
        Write-Info 'These are values a frozen logger repeated. Non-null and in range, so a null check and a range check both pass them.'
    }

    # -- 4. quarantine -------------------------------------------------------------------
    Write-Step 'Quarantine'
    $rejected = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT_BIG(*) FROM ops.RejectedRow')
    if ($rejected -gt 0) {
        Add-Warning ("{0:N0} row(s) quarantined. Nothing was deleted; inspect ops.RejectedRow." -f $rejected)
        $reasons = Invoke-SqlQuery -Server $config.Server -Database $config.Database `
            -Query 'SELECT ReasonCode, COUNT_BIG(*) AS N FROM ops.RejectedRow GROUP BY ReasonCode ORDER BY N DESC'
        foreach ($reason in $reasons) {
            Write-Info ("  {0,-28} {1,10:N0}" -f $reason.ReasonCode, $reason.N)
        }
    } else {
        Write-Ok 'No quarantined rows'
    }

    # -- 5. referential integrity ---------------------------------------------------------
    Write-Step 'Referential integrity'
    $orphanSensors = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database -Query @"
SELECT COUNT_BIG(*) FROM ts.SensorReading AS r
LEFT JOIN asset.Sensor AS s ON s.SensorId = r.SensorId WHERE s.SensorId IS NULL
"@)
    if ($orphanSensors -gt 0) { Add-Failure "$orphanSensors reading(s) reference a sensor that does not exist." }
    else { Write-Ok 'Every reading maps to a real sensor' }

    $orphanQuality = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database -Query @"
SELECT COUNT_BIG(*) FROM ts.SensorReading AS r
LEFT JOIN ref.QualityCode AS q ON q.QualityCodeId = r.QualityCodeId WHERE q.QualityCodeId IS NULL
"@)
    if ($orphanQuality -gt 0) { Add-Failure "$orphanQuality reading(s) carry an unknown quality code." }
    else { Write-Ok 'Every reading carries a known quality code' }

    $uneven = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database -Query @"
SELECT COUNT(DISTINCT n) FROM (SELECT SensorId, COUNT_BIG(*) AS n FROM ts.SensorReading GROUP BY SensorId) AS x
"@)
    if ($uneven -ne 1) {
        Add-Warning "Sensors have differing reading counts ($uneven distinct totals). Expected identical counts for a scan-based archive."
    } else {
        Write-Ok 'All sensors have identical reading counts'
    }

    # -- 6. coverage -----------------------------------------------------------------------
    Write-Step 'Coverage'
    $gaps = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT(*) FROM ops.vw_TimestampGap')
    $bounds = Invoke-SqlQuery -Server $config.Server -Database $config.Database `
        -Query 'SELECT MIN(ReadingTs) AS MinTs, MAX(ReadingTs) AS MaxTs FROM ts.SensorReading'
    $scans = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT_BIG(*) FROM analytics.ScanState')

    if ($scans -gt 0 -and $null -ne $bounds[0].MinTs) {
        $span = ([datetime]$bounds[0].MaxTs - [datetime]$bounds[0].MinTs).TotalSeconds
        $expected = ($span / 10.0) + 1
        $coverage = 100.0 * $scans / $expected
        Write-Info ("Archive: {0} to {1}" -f $bounds[0].MinTs, $bounds[0].MaxTs)
        Write-Ok ("Coverage {0:N1}% across {1:N0} gap(s)" -f $coverage, $gaps)
        if ($coverage -lt 50) {
            Add-Warning ("Coverage is only {0:N1}%. More than half the timeline has no data." -f $coverage)
        }
    } else {
        Write-Info "$gaps gap(s) recorded"
    }

    # -- 7. report ---------------------------------------------------------------------------
    if (-not $SkipReport) {
        Write-Step 'Quality report'
        $python = Get-PythonExe -ProjectRoot $config.Root
        if ($null -eq $python) {
            Add-Warning 'No virtual environment; skipping the HTML report.'
        } else {
            $reportExit = Invoke-ProjectPython -PythonExe $python `
                -Arguments @('scripts/generate_quality_report.py') -WorkingDirectory $config.Root
            if ($reportExit -ne 0) {
                Add-Warning "Report generation exited with code $reportExit."
            } else {
                Write-Ok 'reports\data_quality_report.html regenerated'
            }
        }
    }

    # -- verdict --------------------------------------------------------------------------
    $exitCode = 0
    $detail = 'All validation checks passed.'
    if ($failures.Count -gt 0) {
        $exitCode = 1
        $detail = "$($failures.Count) failure(s), $($warnings.Count) warning(s)."
    } elseif ($warnings.Count -gt 0) {
        if ($FailOnWarnings) {
            $exitCode = 1
            $detail = "$($warnings.Count) warning(s), and -FailOnWarnings was set."
        } else {
            $exitCode = 3
            $detail = "$($warnings.Count) warning(s). Data is usable; see above for what was flagged."
        }
    }

    $code = Write-Summary -Title 'Validation' -ExitCode $exitCode -Detail $detail
    Stop-ScriptLog -Path $logPath
    exit $code

} catch {
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Fail $_.ScriptStackTrace
    $code = Write-Summary -Title 'Validation' -ExitCode 1 -Detail 'The script itself failed.'
    Stop-ScriptLog -Path $logPath
    exit $code
}
