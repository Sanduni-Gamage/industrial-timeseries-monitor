<#
.SYNOPSIS
    Verify that every automation script returns the right exit code - including when it fails.

.DESCRIPTION
    Success paths get exercised constantly. Failure paths matter in CI and at 3am, and
    nobody runs them. This forces each one and asserts the exit code.

    Failures are forced with a nonexistent SQL instance and missing or truncated files.
    Nothing here modifies the real database.

    Contract: 0 success, 1 the work failed, 2 usage or configuration error, 3 degraded.
    2 is separate from 1 so a job can tell "the data is bad" from "the machine is not
    set up".

.PARAMETER SkipSlow
    Skip cases that touch the real database.

.OUTPUTS
    Exit code 0 all cases passed · 1 at least one case failed

.EXAMPLE
    .\scripts\Test-Automation.ps1
#>

[CmdletBinding()]
param([switch] $SkipSlow)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$root = Get-ProjectRoot
$scriptDir = $PSScriptRoot
$results = New-Object System.Collections.ArrayList

# An instance name that cannot resolve, used to force connection failures.
$deadServer = 'localhost\NO_SUCH_INSTANCE_XYZ'

function Invoke-Case {
    <#
    .SYNOPSIS
        Run one script in a child process and compare its exit code to the expectation.
    .DESCRIPTION
        A child process is essential: `exit` inside a dotted script would terminate this
        harness, and only a separate process produces a real exit code to observe.
    #>
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $Script,
        [string[]] $ScriptArgs = @(),
        [Parameter(Mandatory = $true)][int] $Expected,
        [hashtable] $EnvOverrides = @{}
    )

    $original = @{}
    foreach ($key in $EnvOverrides.Keys) {
        $original[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $EnvOverrides[$key])
    }

    try {
        $scriptPath = Join-Path $scriptDir $Script
        # The script path MUST be quoted. Start-Process joins -ArgumentList with spaces
        # and does no quoting of its own, so a path containing a space - this project
        # lives under "Prodcut Eng" - is split into two arguments and PowerShell reports
        # the useless "file does not have a '.ps1' extension", exiting -196608 before any
        # script code runs. Every case then fails identically, which looks like a broken
        # harness rather than a quoting bug.
        # Any argument containing a space needs the same treatment, or a path passed to
        # -CsvPath splits and the script fails parameter binding (exit 1) instead of
        # reaching the check under test.
        $quotedArgs = @()
        foreach ($argument in $ScriptArgs) {
            if ($argument -match '\s') { $quotedArgs += ('"{0}"' -f $argument) }
            else { $quotedArgs += $argument }
        }
        $allArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $scriptPath)) + $quotedArgs
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $allArgs `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput ([System.IO.Path]::GetTempFileName()) `
            -RedirectStandardError ([System.IO.Path]::GetTempFileName())
        $actual = $process.ExitCode
    } finally {
        foreach ($key in $EnvOverrides.Keys) {
            [Environment]::SetEnvironmentVariable($key, $original[$key])
        }
    }

    $passed = ($actual -eq $Expected)
    [void]$results.Add([pscustomobject]@{
        Name     = $Name
        Expected = $Expected
        Actual   = $actual
        Passed   = $passed
    })

    if ($passed) {
        Write-Host ("  [PASS] {0,-52} exit {1}" -f $Name, $actual) -ForegroundColor Green
    } else {
        Write-Host ("  [FAIL] {0,-52} exit {1}, expected {2}" -f $Name, $actual, $Expected) -ForegroundColor Red
    }
}

Write-Host 'Automation exit-code contract'
Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

# --------------------------------------------------------------------------------------
Write-Step 'Configuration errors must exit 2, not 1'
# --------------------------------------------------------------------------------------

Invoke-Case -Name 'ingest: source file does not exist' `
    -Script 'ingest.ps1' -ScriptArgs @('-CsvPath', (Join-Path $root 'data\raw\no-such-file.csv')) `
    -Expected 2

# A truncated download still looks like a valid file. This is the DL-006 scenario.
$truncated = Join-Path $env:TEMP 'truncated-metropt.csv'
Set-Content -LiteralPath $truncated -Value ',timestamp,TP2' -Encoding utf8
Invoke-Case -Name 'ingest: source file is truncated' `
    -Script 'ingest.ps1' -ScriptArgs @('-CsvPath', $truncated) -Expected 2

Invoke-Case -Name 'ingest: database unreachable' `
    -Script 'ingest.ps1' -Expected 2 -EnvOverrides @{ DB_SERVER = $deadServer }

Invoke-Case -Name 'validate: database unreachable' `
    -Script 'validate.ps1' -Expected 2 -EnvOverrides @{ DB_SERVER = $deadServer }

Invoke-Case -Name 'generate-report: database unreachable' `
    -Script 'generate-report.ps1' -Expected 2 -EnvOverrides @{ DB_SERVER = $deadServer }

Invoke-Case -Name 'setup: prerequisite missing (no SQL Server)' `
    -Script 'setup.ps1' -ScriptArgs @('-CheckOnly') -Expected 2 `
    -EnvOverrides @{ DB_SERVER = $deadServer }

# --------------------------------------------------------------------------------------
Write-Step 'A required component being down must exit 1'
# --------------------------------------------------------------------------------------

Invoke-Case -Name 'health-check: database unreachable' `
    -Script 'health-check.ps1' -ScriptArgs @('-SkipApi') -Expected 1 `
    -EnvOverrides @{ DB_SERVER = $deadServer }

# --------------------------------------------------------------------------------------
Write-Step 'Degraded but usable must exit 3, not 0 or 1'
# --------------------------------------------------------------------------------------

# The API is not expected to be running during this test, so health-check should report
# degraded rather than healthy or failed. If the API IS running this case is skipped
# rather than reported as a failure of the script.
$apiRunning = $false
try {
    $config = Get-MonitorConfig
    Invoke-RestMethod -Uri "http://$($config.ApiHost):$($config.ApiPort)/health" -TimeoutSec 3 | Out-Null
    $apiRunning = $true
} catch { }

if ($apiRunning) {
    Write-Warn 'The API is running, so the degraded-API case does not apply. Skipped.'
} else {
    Invoke-Case -Name 'health-check: API down, database fine' `
        -Script 'health-check.ps1' -Expected 3
}

# --------------------------------------------------------------------------------------
Write-Step 'Invalid parameters must be refused by validation'
# --------------------------------------------------------------------------------------

# PowerShell's own parameter binding rejects these before any script code runs, and the
# host exits 1. Asserted so the ValidateRange attributes cannot be silently removed.
Invoke-Case -Name 'generate-report: -AnomalyDays out of range' `
    -Script 'generate-report.ps1' -ScriptArgs @('-AnomalyDays', '9999') -Expected 1

Invoke-Case -Name 'ingest: -LimitDays out of range' `
    -Script 'ingest.ps1' -ScriptArgs @('-LimitDays', '0') -Expected 1

# --------------------------------------------------------------------------------------
if (-not $SkipSlow) {
    Write-Step 'Success paths must exit 0'
    # --------------------------------------------------------------------------------------
    Invoke-Case -Name 'health-check: healthy (API skipped)' `
        -Script 'health-check.ps1' -ScriptArgs @('-SkipApi') -Expected 0

    Invoke-Case -Name 'validate: clean archive' `
        -Script 'validate.ps1' -ScriptArgs @('-SkipReport') -Expected 0

    Invoke-Case -Name 'setup: check only' `
        -Script 'setup.ps1' -ScriptArgs @('-CheckOnly') -Expected 0
}

# --------------------------------------------------------------------------------------
$passed = @($results | Where-Object { $_.Passed }).Count
$failed = @($results | Where-Object { -not $_.Passed }).Count

Write-Host ''
Write-Host ('-' * 72)
Write-Host ("Exit-code contract: {0} passed, {1} failed" -f $passed, $failed) `
    -ForegroundColor $(if ($failed -eq 0) { 'Green' } else { 'Red' })
Write-Host ('-' * 72)

if (Test-Path -LiteralPath $truncated) { Remove-Item -LiteralPath $truncated -Force }

if ($failed -gt 0) { exit 1 }
exit 0
