<#
.SYNOPSIS
    Shared helpers for the operational PowerShell scripts.

.DESCRIPTION
    Targets **Windows PowerShell 5.1**, which is what is installed on the target machine.
    That rules out several things that look normal in modern examples and are syntax
    errors here:

      * pipeline chain operators `&&` and `||`
      * the ternary `? :`, null-coalescing `??`, and null-conditional `?.`
      * `ConvertFrom-Json -AsHashtable`

    Two hard-won constraints are baked into this module:

      * **`sqlcmd` is not on PATH**, so SQL access goes through `System.Data.SqlClient`
        directly. Nothing here shells out to a tool that may not exist.
      * **Connecting and querying are separated.** A `try` wrapping both reports a broken
        query as a connection failure, which sent an early diagnosis in completely the
        wrong direction (see docs/DEV_LOG.md DL-001). `Test-SqlConnection` reports which
        of the two actually failed.

    Configuration is read from the same `.env` file the Python code uses, so the two
    halves of the system cannot drift apart.

.NOTES
    Exit codes used by every script in this folder:
        0  success
        1  failure
        2  usage or configuration error
        3  completed, but the system is degraded
#>

Set-StrictMode -Version Latest

$script:ExitOk       = 0
$script:ExitFailure  = 1
$script:ExitUsage    = 2
$script:ExitDegraded = 3

# --------------------------------------------------------------------------------------
# Paths and configuration
# --------------------------------------------------------------------------------------

function Get-ProjectRoot {
    <#
    .SYNOPSIS
        Repository root, derived from this module's own location.
    .DESCRIPTION
        Never a hard-coded path and never the caller's working directory, so the scripts
        behave identically whether run from the repo root, from scripts\, or from a
        scheduled task with an arbitrary working directory.
    #>
    [CmdletBinding()]
    param()
    return (Split-Path -Parent $PSScriptRoot)
}

function Read-DotEnv {
    <#
    .SYNOPSIS
        Parse a .env file into a hashtable.
    .DESCRIPTION
        Deliberately shares the Python configuration file rather than duplicating
        settings. Two copies of a connection string is two things to keep in step.
    #>
    [CmdletBinding()]
    param(
        [string] $Path = (Join-Path (Get-ProjectRoot) '.env')
    )

    $settings = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $settings }

    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if ($trimmed.Length -eq 0) { continue }
        if ($trimmed.StartsWith('#')) { continue }

        $separator = $trimmed.IndexOf('=')
        if ($separator -lt 1) { continue }

        $key = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()

        # Strip surrounding quotes if present, but leave inner content alone.
        if ($value.Length -ge 2) {
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        $settings[$key] = $value
    }
    return $settings
}

function Get-Setting {
    <#
    .SYNOPSIS
        Resolve one setting: process environment first, then .env, then a default.
    .DESCRIPTION
        Same precedence the Python side uses, so overriding an environment variable
        affects both halves of the system identically.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [hashtable] $DotEnv,
        [string] $Default = ''
    )

    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($fromEnv)) { return $fromEnv }

    if ($null -ne $DotEnv -and $DotEnv.ContainsKey($Name)) {
        if (-not [string]::IsNullOrWhiteSpace($DotEnv[$Name])) { return $DotEnv[$Name] }
    }
    return $Default
}

function Get-MonitorConfig {
    <#
    .SYNOPSIS
        The configuration every script needs, resolved once.
    #>
    [CmdletBinding()]
    param()

    $root = Get-ProjectRoot
    $dotEnv = Read-DotEnv

    $csvSetting = Get-Setting -Name 'METROPT_RAW_CSV' -DotEnv $dotEnv `
        -Default 'data/raw/MetroPT3(AirCompressor).csv'
    if ([System.IO.Path]::IsPathRooted($csvSetting)) {
        $csvPath = $csvSetting
    } else {
        $csvPath = Join-Path $root $csvSetting
    }

    return [pscustomobject]@{
        Root         = $root
        Server       = Get-Setting -Name 'DB_SERVER'   -DotEnv $dotEnv -Default 'localhost\SQLEXPRESS01'
        Database     = Get-Setting -Name 'DB_DATABASE' -DotEnv $dotEnv -Default 'IndustrialMonitor'
        ApiHost      = Get-Setting -Name 'API_HOST'    -DotEnv $dotEnv -Default '127.0.0.1'
        ApiPort      = Get-Setting -Name 'API_PORT'    -DotEnv $dotEnv -Default '8000'
        CsvPath      = $csvPath
        LogDirectory = Join-Path $root 'logs'
        VenvPython   = Join-Path $root '.venv\Scripts\python.exe'
        DotEnv       = $dotEnv
    }
}

# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------

function Write-Step {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Message)
    Write-Host "    [ OK ] $Message" -ForegroundColor Green
}

function Write-Info {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Message)
    Write-Host "    [INFO] $Message"
}

function Write-Warn {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Message)
    Write-Host "    [WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Message)
    # Write-Host rather than Write-Error: Write-Error emits an ErrorRecord that a caller
    # may treat as a terminating failure, and these scripts decide their own exit code.
    Write-Host "    [FAIL] $Message" -ForegroundColor Red
}

function Write-Summary {
    <#
    .SYNOPSIS
        Print the closing banner and return the exit code to use.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Title,
        [Parameter(Mandatory = $true)][int] $ExitCode,
        [string] $Detail = ''
    )

    $labels = @{ 0 = 'SUCCESS'; 1 = 'FAILED'; 2 = 'USAGE ERROR'; 3 = 'DEGRADED' }
    $label = 'UNKNOWN'
    if ($labels.ContainsKey($ExitCode)) { $label = $labels[$ExitCode] }

    $colour = 'Green'
    if ($ExitCode -eq 1) { $colour = 'Red' }
    if ($ExitCode -eq 2) { $colour = 'Red' }
    if ($ExitCode -eq 3) { $colour = 'Yellow' }

    Write-Host ''
    Write-Host ('-' * 72)
    Write-Host ("{0}: {1} (exit {2})" -f $Title, $label, $ExitCode) -ForegroundColor $colour
    if (-not [string]::IsNullOrWhiteSpace($Detail)) { Write-Host $Detail }
    Write-Host ('-' * 72)
    return $ExitCode
}

# --------------------------------------------------------------------------------------
# Transcript logging
# --------------------------------------------------------------------------------------

function Start-ScriptLog {
    <#
    .SYNOPSIS
        Begin a transcript for this run.
    .DESCRIPTION
        Returns the log path, or $null if transcripting is unavailable. A failure to open
        a log must never stop the work: an unattended run that cannot write a log file
        should still ingest the data and say so.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [Parameter(Mandatory = $true)][string] $LogDirectory
    )

    try {
        if (-not (Test-Path -LiteralPath $LogDirectory)) {
            New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
        }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $path = Join-Path $LogDirectory ("{0}-{1}.log" -f $Name, $stamp)
        Start-Transcript -Path $path -Force | Out-Null
        return $path
    } catch {
        Write-Warn "Could not start a transcript ($($_.Exception.Message)). Continuing without one."
        return $null
    }
}

function Stop-ScriptLog {
    [CmdletBinding()]
    param([string] $Path)

    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try { Stop-Transcript | Out-Null } catch { }
}

# --------------------------------------------------------------------------------------
# SQL Server
# --------------------------------------------------------------------------------------

function New-SqlConnectionString {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $Database,
        [int] $TimeoutSeconds = 10
    )
    return "Server=$Server;Database=$Database;Integrated Security=True;TrustServerCertificate=True;Connect Timeout=$TimeoutSeconds"
}

function Test-SqlConnection {
    <#
    .SYNOPSIS
        Check the database, reporting connect and query failures separately.
    .DESCRIPTION
        The separation is the point. Wrapping both in one try/catch reports a broken
        query as "cannot connect", which is how an early diagnosis went wrong (DL-001).
        Returns an object with Connected, Queried, and either the server version or the
        error that actually occurred.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $Database,
        [int] $TimeoutSeconds = 10
    )

    $result = [pscustomobject]@{
        Connected = $false
        Queried   = $false
        Version   = $null
        Edition   = $null
        Error     = $null
        Stage     = 'connect'
    }

    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = New-SqlConnectionString -Server $Server -Database $Database -TimeoutSeconds $TimeoutSeconds

    try {
        $connection.Open()
        $result.Connected = $true
    } catch {
        $result.Error = $_.Exception.Message
        return $result
    }

    $result.Stage = 'query'
    try {
        $command = $connection.CreateCommand()
        # SERVERPROPERTY returns sql_variant; CONCAT will not implicitly convert it, which
        # is the exact mistake that produced a misleading "connection failed" (DL-001).
        $command.CommandText = @"
SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)),
       CAST(SERVERPROPERTY('Edition')        AS nvarchar(128))
"@
        $reader = $command.ExecuteReader()
        if ($reader.Read()) {
            $result.Version = $reader.GetString(0)
            $result.Edition = $reader.GetString(1)
        }
        $reader.Close()
        $result.Queried = $true
    } catch {
        $result.Error = $_.Exception.Message
    } finally {
        $connection.Close()
    }

    return $result
}

function Invoke-SqlScalar {
    <#
    .SYNOPSIS
        Run a query and return its first column of its first row.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $Database,
        [Parameter(Mandatory = $true)][string] $Query,
        [int] $TimeoutSeconds = 60
    )

    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = New-SqlConnectionString -Server $Server -Database $Database
    try {
        $connection.Open()
        $command = $connection.CreateCommand()
        $command.CommandText = $Query
        $command.CommandTimeout = $TimeoutSeconds
        return $command.ExecuteScalar()
    } finally {
        $connection.Close()
    }
}

function Invoke-SqlQuery {
    <#
    .SYNOPSIS
        Run a query and return its rows as PSCustomObjects.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Server,
        [Parameter(Mandatory = $true)][string] $Database,
        [Parameter(Mandatory = $true)][string] $Query,
        [int] $TimeoutSeconds = 60
    )

    $connection = New-Object System.Data.SqlClient.SqlConnection
    $connection.ConnectionString = New-SqlConnectionString -Server $Server -Database $Database
    $rows = New-Object System.Collections.ArrayList

    try {
        $connection.Open()
        $command = $connection.CreateCommand()
        $command.CommandText = $Query
        $command.CommandTimeout = $TimeoutSeconds
        $reader = $command.ExecuteReader()

        while ($reader.Read()) {
            $row = [ordered]@{}
            for ($i = 0; $i -lt $reader.FieldCount; $i++) {
                $name = $reader.GetName($i)
                if ([string]::IsNullOrWhiteSpace($name)) { $name = "Column$i" }
                $value = $reader.GetValue($i)
                if ($value -is [System.DBNull]) { $value = $null }
                $row[$name] = $value
            }
            [void]$rows.Add([pscustomobject]$row)
        }
        $reader.Close()
    } finally {
        $connection.Close()
    }

    return $rows.ToArray()
}

# --------------------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------------------

function Get-PythonExe {
    <#
    .SYNOPSIS
        Locate the interpreter to use: the project virtual environment if present.
    .DESCRIPTION
        Falls back to a system Python only so that setup.ps1 can bootstrap. Every other
        script requires the venv, because the system interpreter will not have the
        project's dependencies.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $ProjectRoot,
        [switch] $AllowSystem
    )

    $venv = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    if (-not $AllowSystem) { return $null }

    foreach ($candidate in @('py', 'python')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($null -ne $command) { return $command.Source }
    }
    return $null
}

function Invoke-ProjectPython {
    <#
    .SYNOPSIS
        Run a Python entry point in the project and return its exit code.
    .DESCRIPTION
        Output is streamed rather than captured, so a long ingestion shows progress as it
        happens instead of appearing to hang. The child's exit code is returned unchanged:
        the Python side already defines meaningful codes and these scripts must not
        flatten them.

        Note the deliberate absence of `2>&1`. In Windows PowerShell, redirecting a native
        executable's stderr wraps each line in an ErrorRecord and sets `$?` to false even
        on a clean exit - which would make every successful run look like a failure.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $PythonExe,
        [Parameter(Mandatory = $true)][string[]] $Arguments,
        [Parameter(Mandatory = $true)][string] $WorkingDirectory
    )

    Push-Location $WorkingDirectory
    try {
        # `| Out-Host` is load-bearing, not decoration.
        #
        # In PowerShell every uncaptured value a function emits becomes part of its
        # return value. Without this, `& $PythonExe` sends the child's stdout into the
        # pipeline and the caller receives an ARRAY of [...output lines, exit code]
        # instead of an integer - so `if ($exitCode -ne 0)` compares against an array,
        # which is truthy, and a successful run reports as a failure.
        #
        # Out-Host writes straight to the console, so output still streams live (a
        # 20-minute ingestion shows progress) while the function returns only the code.
        & $PythonExe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

Export-ModuleMember -Function @(
    'Get-ProjectRoot', 'Read-DotEnv', 'Get-Setting', 'Get-MonitorConfig',
    'Write-Step', 'Write-Ok', 'Write-Info', 'Write-Warn', 'Write-Fail', 'Write-Summary',
    'Start-ScriptLog', 'Stop-ScriptLog',
    'New-SqlConnectionString', 'Test-SqlConnection', 'Invoke-SqlScalar', 'Invoke-SqlQuery',
    'Get-PythonExe', 'Invoke-ProjectPython'
)
