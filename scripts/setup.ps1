<#
.SYNOPSIS
    Prepare the project: verify prerequisites, create the environment, build the database.

.DESCRIPTION
    Checks every prerequisite before changing anything, then creates the virtual
    environment, the .env file, the database and the dashboard's packages.

    Safe to re-run: an existing .env is never overwritten and the SQL is idempotent.

.PARAMETER SkipPython
    Do not create the virtual environment or install Python packages.

.PARAMETER SkipDatabase
    Do not touch SQL Server.

.PARAMETER SkipDashboard
    Do not run npm install.

.PARAMETER CheckOnly
    Verify prerequisites and report, but change nothing.

.OUTPUTS
    Exit code 0 ready · 1 a step failed · 2 a prerequisite is missing · 3 partially ready

.EXAMPLE
    .\scripts\setup.ps1
.EXAMPLE
    .\scripts\setup.ps1 -CheckOnly
#>

[CmdletBinding()]
param(
    [switch] $SkipPython,
    [switch] $SkipDatabase,
    [switch] $SkipDashboard,
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'Monitor.Common.psm1') -Force

$config = Get-MonitorConfig
$logPath = Start-ScriptLog -Name 'setup' -LogDirectory $config.LogDirectory

$failures = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList
function Add-Failure { param([string] $Message) [void]$failures.Add($Message); Write-Fail $Message }
function Add-Warning { param([string] $Message) [void]$warnings.Add($Message); Write-Warn $Message }

function Get-CommandVersion {
    <#
    .SYNOPSIS
        Return a tool's version string, or $null if the tool is absent.
    .DESCRIPTION
        Errors are swallowed on purpose: "not installed" is an answer this function is
        expected to give, not an exception the caller should handle.
    #>
    param([string] $Command, [string[]] $VersionArgs = @('--version'))

    $found = Get-Command $Command -ErrorAction SilentlyContinue
    if ($null -eq $found) { return $null }
    try {
        $output = & $Command @VersionArgs 2>$null
        if ($null -eq $output) { return 'unknown version' }
        return ($output | Select-Object -First 1).ToString().Trim()
    } catch {
        return 'unknown version'
    }
}

try {
    Write-Host 'Industrial Time-Series Monitoring - setup'
    Write-Host (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
    Write-Host "Project root: $($config.Root)"

    # -- 1. prerequisites --------------------------------------------------------------
    Write-Step 'Prerequisites'

    Write-Info "PowerShell $($PSVersionTable.PSVersion)"
    if ($PSVersionTable.PSVersion.Major -lt 5) {
        Add-Failure 'PowerShell 5.1 or later is required.'
    }

    # `py` is preferred over `python`: the launcher can select a specific version, and
    # this project needs 3.12 rather than whatever is first on PATH.
    $pythonLauncher = Get-CommandVersion -Command 'py' -VersionArgs @('-3.12', '--version')
    $pythonDirect = Get-CommandVersion -Command 'python'

    $pythonCommand = $null
    $pythonArgs = @()
    if ($null -ne $pythonLauncher -and $pythonLauncher -like 'Python 3.12*') {
        $pythonCommand = 'py'
        $pythonArgs = @('-3.12')
        Write-Ok "Python: $pythonLauncher (via the py launcher)"
    } elseif ($null -ne $pythonDirect) {
        $pythonCommand = 'python'
        Write-Ok "Python: $pythonDirect"
        if ($pythonDirect -notlike 'Python 3.1[2-9]*') {
            Add-Warning "Python 3.12 is recommended. Found: $pythonDirect. Wheel availability for pyodbc and SciPy is less reliable on newer versions."
        }
    } else {
        Add-Failure 'Python not found. Install Python 3.12 and ensure it is on PATH.'
    }

    $node = Get-CommandVersion -Command 'node'
    if ($null -eq $node) {
        Add-Warning 'Node.js not found. The dashboard cannot be built; the API and pipeline are unaffected.'
    } else {
        Write-Ok "Node: $node"
    }

    $npm = Get-CommandVersion -Command 'npm'
    if ($null -eq $npm -and $null -ne $node) {
        Add-Warning 'npm not found even though Node is installed.'
    } elseif ($null -ne $npm) {
        Write-Ok "npm: $npm"
    }

    # Docker is genuinely optional here: SQL Server is installed natively on this
    # machine, and the compose file exists only for reviewers who have it the other way
    # round. Reporting its absence as a problem would be misleading.
    $docker = Get-CommandVersion -Command 'docker'
    if ($null -eq $docker) {
        Write-Info 'Docker not found. Not required - this project uses a locally installed SQL Server.'
    } else {
        Write-Ok "Docker: $docker"
    }

    if (-not $SkipDatabase) {
        $probe = Test-SqlConnection -Server $config.Server -Database 'master'
        if (-not $probe.Connected) {
            Add-Failure "Cannot reach SQL Server at $($config.Server): $($probe.Error)"
            Write-Info 'Check that the SQL Server service is running, and that DB_SERVER in .env names the right instance.'
        } else {
            Write-Ok "SQL Server: $($probe.Version) - $($probe.Edition)"
            if ($probe.Edition -like '*Express*') {
                Write-Info 'Express Edition: 10 GB per database. The full archive uses about 1 GB, so this is comfortable.'
            }
        }
    }

    $odbc = Get-OdbcDriver -Platform '64-bit' -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like '*SQL Server*' }
    if ($null -eq $odbc -or @($odbc).Count -eq 0) {
        Add-Failure 'No ODBC driver for SQL Server found. Install "ODBC Driver 18 for SQL Server".'
    } else {
        Write-Ok "ODBC drivers: $((@($odbc) | ForEach-Object { $_.Name }) -join ', ')"
    }

    if ($failures.Count -gt 0) {
        $code = Write-Summary -Title 'Setup' -ExitCode 2 `
            -Detail "$($failures.Count) prerequisite(s) missing. Nothing was changed."
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    if ($CheckOnly) {
        $checkExit = 0
        if ($warnings.Count -gt 0) { $checkExit = 3 }
        $code = Write-Summary -Title 'Setup (check only)' -ExitCode $checkExit `
            -Detail 'Prerequisites verified. Nothing was changed.'
        Stop-ScriptLog -Path $logPath
        exit $code
    }

    # -- 2. virtual environment ---------------------------------------------------------
    if (-not $SkipPython) {
        Write-Step 'Python environment'
        $venvPath = Join-Path $config.Root '.venv'
        $venvPython = Join-Path $venvPath 'Scripts\python.exe'

        if (Test-Path -LiteralPath $venvPython) {
            Write-Ok 'Virtual environment already present'
        } else {
            Write-Info "Creating $venvPath ..."
            $createArgs = $pythonArgs + @('-m', 'venv', $venvPath)
            & $pythonCommand @createArgs | Out-Host
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
                Add-Failure 'Failed to create the virtual environment.'
                $code = Write-Summary -Title 'Setup' -ExitCode 1 -Detail 'Environment creation failed.'
                Stop-ScriptLog -Path $logPath
                exit $code
            }
            Write-Ok 'Virtual environment created'
        }

        Write-Info 'Installing requirements (this can take a few minutes) ...'
        & $venvPython -m pip install --quiet --upgrade pip | Out-Host
        & $venvPython -m pip install --quiet -r (Join-Path $config.Root 'requirements.txt') | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Add-Failure 'pip install failed.'
            $code = Write-Summary -Title 'Setup' -ExitCode 1 -Detail 'Dependency installation failed.'
            Stop-ScriptLog -Path $logPath
            exit $code
        }
        Write-Ok 'Python dependencies installed'

        # Import the driver rather than trusting that pip reported success: a wheel can
        # install and still fail to load if the ODBC runtime is missing.
        & $venvPython -c "import pyodbc, pandas, fastapi" | Out-Host
        if ($LASTEXITCODE -ne 0) {
            Add-Failure 'Core packages installed but could not be imported.'
        } else {
            Write-Ok 'Core packages import cleanly'
        }
    }

    # -- 3. configuration ----------------------------------------------------------------
    Write-Step 'Configuration'
    $envPath = Join-Path $config.Root '.env'
    $examplePath = Join-Path $config.Root '.env.example'

    if (Test-Path -LiteralPath $envPath) {
        # Never overwritten. It may contain a password, and clobbering it during a
        # re-run would be a genuinely destructive act by a script meant to be safe.
        Write-Ok '.env already exists (left untouched)'
    } elseif (Test-Path -LiteralPath $examplePath) {
        Copy-Item -LiteralPath $examplePath -Destination $envPath
        Write-Ok '.env created from .env.example'
        Write-Info 'Review DB_SERVER and METROPT_RAW_CSV before ingesting.'
    } else {
        Add-Warning '.env.example is missing; configuration will fall back to defaults.'
    }

    foreach ($directory in @('logs', 'reports', 'data\raw', 'data\processed')) {
        $full = Join-Path $config.Root $directory
        if (-not (Test-Path -LiteralPath $full)) {
            New-Item -ItemType Directory -Path $full -Force | Out-Null
            Write-Ok "Created $directory\"
        }
    }

    # -- 4. database ------------------------------------------------------------------------
    if (-not $SkipDatabase) {
        Write-Step 'Database'
        $venvPython = Get-PythonExe -ProjectRoot $config.Root
        if ($null -eq $venvPython) {
            Add-Warning 'No virtual environment; skipping database creation.'
        } else {
            $dbExit = Invoke-ProjectPython -PythonExe $venvPython `
                -Arguments @('scripts/init_database.py', '--with-views') `
                -WorkingDirectory $config.Root
            if ($dbExit -ne 0) {
                Add-Failure "Database initialisation exited with code $dbExit."
            } else {
                Write-Ok "Database $($config.Database) is ready"
            }
        }
    }

    # -- 5. dashboard --------------------------------------------------------------------
    if (-not $SkipDashboard -and $null -ne $npm) {
        Write-Step 'Dashboard'
        $dashboardPath = Join-Path $config.Root 'dashboard'
        if (-not (Test-Path -LiteralPath (Join-Path $dashboardPath 'package.json'))) {
            Add-Warning 'dashboard\package.json not found; skipping.'
        } else {
            Push-Location $dashboardPath
            try {
                Write-Info 'npm install ...'
                & npm install --silent | Out-Host
                if ($LASTEXITCODE -ne 0) {
                    Add-Warning 'npm install failed. The API and pipeline are unaffected.'
                } else {
                    Write-Ok 'Dashboard dependencies installed'
                }
            } finally {
                Pop-Location
            }
        }
    }

    # -- 6. what next --------------------------------------------------------------------
    Write-Step 'Next steps'
    $csvPresent = Test-Path -LiteralPath $config.CsvPath
    if (-not $csvPresent) {
        Write-Info "1. Obtain the dataset - see data\README.md. Expected at: $($config.CsvPath)"
        Write-Info '2. .\scripts\ingest.ps1'
    } else {
        Write-Ok "Dataset present: $($config.CsvPath)"
        Write-Info '1. .\scripts\ingest.ps1          load the archive'
    }
    Write-Info '2. python scripts\run_analytics.py   compute baselines and detect anomalies'
    Write-Info '3. python -m api                     start the REST API on port 8000'
    Write-Info '4. npm --prefix dashboard run dev    start the dashboard on port 5173'
    Write-Info '5. .\scripts\health-check.ps1        confirm everything is working'

    $exitCode = 0
    $detail = 'The project is ready.'
    if ($failures.Count -gt 0) {
        $exitCode = 1
        $detail = "$($failures.Count) step(s) failed."
    } elseif ($warnings.Count -gt 0) {
        $exitCode = 3
        $detail = "$($warnings.Count) warning(s). The core pipeline is usable."
    }

    $code = Write-Summary -Title 'Setup' -ExitCode $exitCode -Detail $detail
    Stop-ScriptLog -Path $logPath
    exit $code

} catch {
    Write-Fail "Unexpected error: $($_.Exception.Message)"
    Write-Fail $_.ScriptStackTrace
    $code = Write-Summary -Title 'Setup' -ExitCode 1 -Detail 'The script itself failed.'
    Stop-ScriptLog -Path $logPath
    exit $code
}
