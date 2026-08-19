<#
.SYNOPSIS
    Produce the operational reports: data quality, equipment summary, anomaly summary.

.DESCRIPTION
    Regenerates the HTML data-quality report and a plain-text operations summary.

    Every figure is queried at generation time, so a stale number is impossible.

.PARAMETER OutputDirectory
    Where to write. Defaults to reports\ in the project root.

.PARAMETER AnomalyDays
    How far back to summarise anomalies, in days from the end of the archive. Default 7.

.PARAMETER SkipHtml
    Only produce the text summary.

.PARAMETER PassThru
    Also emit the summary object to the pipeline, for a caller that wants to act on it.

.OUTPUTS
    Exit code 0 written · 1 failure · 2 usage error · 3 written, but with warnings

.EXAMPLE
    .\scripts\generate-report.ps1
.EXAMPLE
    .\scripts\generate-report.ps1 -AnomalyDays 30 -SkipHtml
#>

[CmdletBinding()]
param(
    [string] $OutputDirectory,
    [ValidateRange(1, 365)]
    [int] $AnomalyDays = 7,
    [switch] $SkipHtml,
    [switch] $PassThru
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$config = Get-MonitorConfig
$logPath = Start-ScriptLog -Name 'generate-report' -LogDirectory $config.LogDirectory

$reportDir = $OutputDirectory
if ([string]::IsNullOrWhiteSpace($reportDir)) { $reportDir = Join-Path $config.Root 'reports' }

$warnings = New-Object System.Collections.ArrayList
function Add-Warning { param([string] $Message) [void]$warnings.Add($Message); Write-Warn $Message }

try {
    Write-Host "Report generation - $($config.Server)/$($config.Database)"
    Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

    $probe = Test-SqlConnection -Server $config.Server -Database $config.Database
    if (-not $probe.Connected) {
        Write-Fail "Cannot connect to the database: $($probe.Error)"
        $code = Write-Summary -Title 'Report generation' -ExitCode 2 -Detail 'Database unreachable.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    if (-not (Test-Path -LiteralPath $reportDir)) {
        New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    }

    # -- 1. HTML data-quality report ---------------------------------------------------
    if (-not $SkipHtml) {
        Write-Step 'Data-quality report'
        $python = Get-PythonExe -ProjectRoot $config.Root
        if ($null -eq $python) {
            Add-Warning 'No virtual environment; the HTML report was not generated.'
        } else {
            $htmlPath = Join-Path $reportDir 'data_quality_report.html'
            $exitCode = Invoke-ProjectPython -PythonExe $python `
                -Arguments @('scripts/generate_quality_report.py', '--out', $htmlPath) `
                -WorkingDirectory $config.Root
            if ($exitCode -ne 0) {
                Add-Warning "Report generator exited with code $exitCode."
            } else {
                $size = (Get-Item -LiteralPath $htmlPath).Length
                Write-Ok ("{0} ({1:N0} bytes)" -f $htmlPath, $size)
            }
        }
    }

    # -- 2. equipment summary -----------------------------------------------------------
    Write-Step 'Equipment summary'
    $equipment = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT EquipmentCode, EquipmentName, SensorCount, HealthStatus,
       CriticalAnomalies, WarningAnomalies, RecordedFailures, LastReadingTs, TotalReadings
FROM asset.vw_EquipmentHealth ORDER BY EquipmentCode
"@
    foreach ($item in $equipment) {
        Write-Info ("{0,-10} {1,-10} sensors={2,-4} critical={3,-4} warning={4,-4} failures={5}" -f `
            $item.EquipmentCode, $item.HealthStatus, $item.SensorCount,
            $item.CriticalAnomalies, $item.WarningAnomalies, $item.RecordedFailures)
    }

    # -- 3. anomaly summary --------------------------------------------------------------
    Write-Step "Anomaly summary (last $AnomalyDays days of the archive)"
    # Windowed against the end of the ARCHIVE, not the wall clock. This is a 2020 dataset;
    # measured from today every reading is stale and the window would always be empty.
    $anomalyRows = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
DECLARE @End DATETIME2(3) = (SELECT MAX(ReadingTs) FROM ts.SensorReading);
SELECT a.Method, a.Severity, COUNT_BIG(*) AS N
FROM analytics.Anomaly AS a
WHERE a.ReadingTs >= DATEADD(day, -$AnomalyDays, @End)
GROUP BY a.Method, a.Severity
ORDER BY a.Method, a.Severity
"@
    $anomalyTotal = 0
    foreach ($row in $anomalyRows) {
        Write-Info ("{0,-20} {1,-10} {2,8:N0}" -f $row.Method, $row.Severity, $row.N)
        $anomalyTotal += [int64]$row.N
    }
    if ($anomalyTotal -eq 0) {
        Write-Info 'None in this window.'
    }

    $topSensors = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
DECLARE @End DATETIME2(3) = (SELECT MAX(ReadingTs) FROM ts.SensorReading);
SELECT TOP (5) s.SensorCode, COUNT_BIG(*) AS N
FROM analytics.Anomaly AS a
INNER JOIN asset.Sensor AS s ON s.SensorId = a.SensorId
WHERE a.ReadingTs >= DATEADD(day, -$AnomalyDays, @End)
GROUP BY s.SensorCode ORDER BY N DESC
"@

    # -- 4. quality figures ---------------------------------------------------------------
    Write-Step 'Data quality'
    $quality = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT COUNT_BIG(*) AS Total,
       SUM(CASE WHEN QualityCodeId = 192 THEN 1 ELSE 0 END) AS Good,
       SUM(CASE WHEN QualityCodeId = 65  THEN 1 ELSE 0 END) AS Held
FROM ts.SensorReading
"@
    $total = [int64]$quality[0].Total
    $good = [int64]$quality[0].Good
    $held = [int64]$quality[0].Held
    $goodPct = 0.0
    $heldPct = 0.0
    if ($total -gt 0) {
        $goodPct = 100.0 * $good / $total
        $heldPct = 100.0 * $held / $total
    }
    Write-Info ("Readings {0:N0} | trusted {1:N2}% | held {2:N2}%" -f $total, $goodPct, $heldPct)

    $lastRun = Invoke-SqlQuery -Server $config.Server -Database $config.Database -Query @"
SELECT TOP (1) IngestionRunId, Status, CompletedUtc, RowsInserted, MinReadingTs, MaxReadingTs
FROM ops.IngestionRun WHERE Status = 'SUCCEEDED' ORDER BY IngestionRunId DESC
"@
    $gapCount = [int](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT(*) FROM ops.vw_TimestampGap')
    $rejected = [int64](Invoke-SqlScalar -Server $config.Server -Database $config.Database `
        -Query 'SELECT COUNT_BIG(*) FROM ops.RejectedRow')

    # -- 5. write the text summary ---------------------------------------------------------
    Write-Step 'Operations summary'
    $summaryPath = Join-Path $reportDir 'operations_summary.txt'

    $lines = New-Object System.Collections.ArrayList
    [void]$lines.Add('INDUSTRIAL TIME-SERIES MONITORING - OPERATIONS SUMMARY')
    [void]$lines.Add(('=' * 72))
    [void]$lines.Add(("Generated : {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')))
    [void]$lines.Add(("Database  : {0}/{1}" -f $config.Server, $config.Database))
    [void]$lines.Add(("SQL Server: {0}" -f $probe.Version))
    [void]$lines.Add('')

    [void]$lines.Add('EQUIPMENT')
    [void]$lines.Add(('-' * 72))
    foreach ($item in $equipment) {
        [void]$lines.Add(("  {0} - {1}" -f $item.EquipmentCode, $item.EquipmentName))
        [void]$lines.Add(("    Status            : {0}" -f $item.HealthStatus))
        [void]$lines.Add(("    Sensors           : {0}" -f $item.SensorCount))
        [void]$lines.Add(("    Readings stored   : {0:N0}" -f $item.TotalReadings))
        [void]$lines.Add(("    Last reading      : {0}" -f $item.LastReadingTs))
        [void]$lines.Add(("    Active critical   : {0}" -f $item.CriticalAnomalies))
        [void]$lines.Add(("    Active warnings   : {0}" -f $item.WarningAnomalies))
        [void]$lines.Add(("    Recorded failures : {0}" -f $item.RecordedFailures))
    }
    [void]$lines.Add('')

    [void]$lines.Add("ANOMALIES - last $AnomalyDays days of the archive")
    [void]$lines.Add(('-' * 72))
    if ($anomalyTotal -eq 0) {
        [void]$lines.Add('  None.')
    } else {
        [void]$lines.Add(("  {0,-22} {1,-10} {2,10}" -f 'RULE', 'SEVERITY', 'COUNT'))
        foreach ($row in $anomalyRows) {
            [void]$lines.Add(("  {0,-22} {1,-10} {2,10:N0}" -f $row.Method, $row.Severity, $row.N))
        }
        [void]$lines.Add(("  {0,-22} {1,-10} {2,10:N0}" -f 'TOTAL', '', $anomalyTotal))
        if ($topSensors.Count -gt 0) {
            [void]$lines.Add('')
            [void]$lines.Add('  Most flagged sensors:')
            foreach ($row in $topSensors) {
                [void]$lines.Add(("    {0,-20} {1,8:N0}" -f $row.SensorCode, $row.N))
            }
        }
    }
    [void]$lines.Add('')

    [void]$lines.Add('DATA QUALITY')
    [void]$lines.Add(('-' * 72))
    [void]$lines.Add(("  Readings stored     : {0,14:N0}" -f $total))
    [void]$lines.Add(("  Trusted             : {0,13:N2}%  ({1:N0})" -f $goodPct, $good))
    [void]$lines.Add(("  Held (frozen)       : {0,13:N2}%  ({1:N0})" -f $heldPct, $held))
    [void]$lines.Add(("  Gaps in the archive : {0,14:N0}" -f $gapCount))
    [void]$lines.Add(("  Rows quarantined    : {0,14:N0}" -f $rejected))
    [void]$lines.Add('')
    [void]$lines.Add('  Held readings are values a frozen logger repeated. They are non-null and')
    [void]$lines.Add('  inside range, so a null check and a range check both pass them. They are')
    [void]$lines.Add('  stored and labelled, and excluded from every baseline and average.')
    [void]$lines.Add('')

    [void]$lines.Add('LAST SUCCESSFUL LOAD')
    [void]$lines.Add(('-' * 72))
    if ($lastRun.Count -eq 0) {
        [void]$lines.Add('  None recorded.')
    } else {
        [void]$lines.Add(("  Run           : #{0} ({1})" -f $lastRun[0].IngestionRunId, $lastRun[0].Status))
        [void]$lines.Add(("  Completed     : {0} UTC" -f $lastRun[0].CompletedUtc))
        [void]$lines.Add(("  Rows inserted : {0:N0}" -f $lastRun[0].RowsInserted))
        [void]$lines.Add(("  Archive covers: {0} to {1}" -f $lastRun[0].MinReadingTs, $lastRun[0].MaxReadingTs))
    }
    [void]$lines.Add('')
    [void]$lines.Add(('=' * 72))
    [void]$lines.Add('Source: UCI MetroPT-3 dataset (DOI 10.24432/C5VW3R). Historical archive,')
    [void]$lines.Add('not a live connection to plant equipment.')

    # -Encoding utf8 is not optional: Set-Content defaults to the system ANSI code page
    # in Windows PowerShell, which mangles any non-ASCII character.
    $lines.ToArray() | Set-Content -LiteralPath $summaryPath -Encoding utf8
    Write-Ok "$summaryPath"

    Write-Host ''
    Write-Host (Get-Content -LiteralPath $summaryPath -Encoding UTF8 | Out-String)

    $exitCode = 0
    $detail = "Reports written to $reportDir."
    if ($warnings.Count -gt 0) {
        $exitCode = 3
        $detail = "$($warnings.Count) warning(s). Text summary written; see above."
    }

    if ($PassThru) {
        [pscustomobject]@{
            TotalReadings = $total
            GoodPercent   = $goodPct
            HeldPercent   = $heldPct
            GapCount      = $gapCount
            Quarantined   = $rejected
            Anomalies     = $anomalyTotal
            SummaryPath   = $summaryPath
        }
    }

    $code = Write-Summary -Title 'Report generation' -ExitCode $exitCode -Detail $detail
    Stop-ScriptLog -Path $logPath
    exit $code

} catch {
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Fail $_.ScriptStackTrace
    $code = Write-Summary -Title 'Report generation' -ExitCode 1 -Detail 'The script itself failed.'
    Stop-ScriptLog -Path $logPath
    exit $code
}
