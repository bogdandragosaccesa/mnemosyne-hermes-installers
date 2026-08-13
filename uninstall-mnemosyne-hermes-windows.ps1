<#
.SYNOPSIS
    Completely removes Mnemosyne (and optionally Hermes Agent) from Windows.

.DESCRIPTION
    Reverses install-mnemosyne-hermes-windows.ps1. By default this is a total
    removal, including the memory database -- pass -KeepData to preserve it.

    Removed by default:
      * Hermes provider registration:  <HERMES_HOME>\plugins\mnemosyne
                                       <HERMES_HOME>\plugins\hermes-mnemosyne  (legacy)
                                       <HERMES_HOME>\profiles\*\plugins\mnemosyne
      * Bundled skill:                 <HERMES_HOME>\skills\memory\mnemosyne-memory-override
      * The memory.provider setting in <HERMES_HOME>\config.yaml and in every profile config
      * The Mnemosyne virtual environment (default ~\.mnemosyne-venv)
      * The Mnemosyne data directory (default <HERMES_HOME>\mnemosyne)
      * User environment variables MNEMOSYNE_HOME / MNEMOSYNE_DATA_DIR / MNEMOSYNE_VENV
      * mnemosyne packages bootstrapped into Hermes' own venv, if any

    With -IncludeHermes it additionally removes Hermes Agent itself: the whole
    HERMES_HOME tree (which contains Hermes' vendored git, node and uv), the
    HERMES_HOME / HERMES_GIT_BASH_PATH user variables, and Hermes' PATH entry.

.PARAMETER KeepData
    Keep the Mnemosyne data directory (database, config.yaml, blobs).

.PARAMETER IncludeHermes
    Also remove Hermes Agent itself.

.PARAMETER DryRun
    Print the removal plan without changing anything.

.PARAMETER Force
    Do not ask for confirmation.

.PARAMETER NonInteractive
    Never prompt. Requires -Force to actually remove anything.

.EXAMPLE
    .\uninstall-mnemosyne-hermes-windows.ps1 -DryRun

.EXAMPLE
    .\uninstall-mnemosyne-hermes-windows.ps1 -Force

.EXAMPLE
    .\uninstall-mnemosyne-hermes-windows.ps1 -IncludeHermes -Force
#>
[CmdletBinding()]
param(
    [switch]$KeepData,
    [switch]$IncludeHermes,
    [switch]$DryRun,
    [switch]$Force,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Step   { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok     { param([string]$Message) Write-Host "  - $Message" -ForegroundColor Green }
function Write-Note   { param([string]$Message) Write-Host "  . $Message" -ForegroundColor DarkGray }
function Write-Warn   { param([string]$Message) Write-Host "  ! $Message" -ForegroundColor Yellow }

$script:Failures = @()
function Add-Failure { param([string]$Message) $script:Failures += $Message; Write-Warn $Message }

# Windows PowerShell 5.1 wraps every stderr line of a native executable in an
# ErrorRecord as soon as its error stream is redirected, which $ErrorActionPreference
# = 'Stop' then turns into a terminating error even when the exit code is 0.
# Every native call in this script goes through one of these two helpers.
function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $FilePath @ArgumentList 2>&1 | ForEach-Object { [string]$_ }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    return [pscustomobject]@{ ExitCode = $code; Output = @($output) }
}

function Invoke-NativeQuiet {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )
    return (Invoke-NativeCapture -FilePath $FilePath -ArgumentList $ArgumentList).ExitCode
}

if (-not $NonInteractive -and -not [Environment]::UserInteractive) { $NonInteractive = $true }

# ---------------------------------------------------------------------------
# Resolve the same locations the installer used
# ---------------------------------------------------------------------------

function Get-EnvValue {
    param([Parameter(Mandatory)][string]$Name)
    $v = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($Name, 'User') }
    if (-not $v) { $v = [Environment]::GetEnvironmentVariable($Name, 'Machine') }
    return $v
}

$hermesHome = Get-EnvValue 'HERMES_HOME'
if (-not $hermesHome) { $hermesHome = Join-Path $env:LOCALAPPDATA 'hermes' }

$dataDir = Get-EnvValue 'MNEMOSYNE_DATA_DIR'
if (-not $dataDir) {
    $dataDir = Get-EnvValue 'MNEMOSYNE_HOME'
    if (-not $dataDir) { $dataDir = Join-Path $hermesHome 'mnemosyne' }
}

$mnemosyneVenv = Get-EnvValue 'MNEMOSYNE_VENV'
if (-not $mnemosyneVenv) { $mnemosyneVenv = Join-Path $env:USERPROFILE '.mnemosyne-venv' }

$hermesExe = Join-Path $hermesHome 'hermes-agent\bin\hermes.exe'
if (-not (Test-Path -LiteralPath $hermesExe)) {
    $fallback = Get-Command 'hermes' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($fallback) { $hermesExe = $fallback.Source } else { $hermesExe = $null }
}
$hermesVenvPython = Join-Path $hermesHome 'hermes-agent\venv\Scripts\python.exe'
$mnemosyneHermesExe = Join-Path $mnemosyneVenv 'Scripts\mnemosyne-hermes.exe'

# Directories that get deleted outright.
$targets = New-Object System.Collections.Generic.List[object]
function Add-Target {
    param([string]$Path, [string]$Label)
    if ($Path -and (Test-Path -LiteralPath $Path)) {
        $targets.Add([pscustomobject]@{ Path = $Path; Label = $Label })
    }
}

Add-Target (Join-Path $hermesHome 'plugins\mnemosyne')                          'provider registration'
Add-Target (Join-Path $hermesHome 'plugins\hermes-mnemosyne')                   'legacy provider registration'
Add-Target (Join-Path $hermesHome 'skills\memory\mnemosyne-memory-override')    'bundled memory skill'

$profilesDir = Join-Path $hermesHome 'profiles'
if (Test-Path -LiteralPath $profilesDir) {
    foreach ($profileDir in (Get-ChildItem -LiteralPath $profilesDir -Directory -ErrorAction SilentlyContinue)) {
        Add-Target (Join-Path $profileDir.FullName 'plugins\mnemosyne') "profile '$($profileDir.Name)' registration"
    }
}

Add-Target $mnemosyneVenv 'virtual environment'
if (-not $KeepData) { Add-Target $dataDir 'data directory (memory database)' }

$envVarsToClear = @('MNEMOSYNE_HOME', 'MNEMOSYNE_DATA_DIR', 'MNEMOSYNE_VENV')
if ($IncludeHermes) {
    $envVarsToClear += @('HERMES_HOME', 'HERMES_GIT_BASH_PATH')
}

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

Write-Step 'Removal plan'
Write-Host ("  Hermes home:    {0}" -f $hermesHome)
Write-Host ("  Mnemosyne data: {0}{1}" -f $dataDir, $(if ($KeepData) { '  (kept)' } else { '' }))
Write-Host ("  Mnemosyne venv: {0}" -f $mnemosyneVenv)
Write-Host ''

if ($targets.Count -eq 0) {
    Write-Note 'No Mnemosyne files found.'
} else {
    foreach ($t in $targets) { Write-Host ("  remove  {0}   [{1}]" -f $t.Path, $t.Label) }
}
foreach ($n in $envVarsToClear) {
    if ([Environment]::GetEnvironmentVariable($n, 'User')) { Write-Host ("  unset   user environment variable {0}" -f $n) }
}
if ($hermesExe -and -not $IncludeHermes) { Write-Host '  unset   memory.provider in Hermes configuration' }
if ($IncludeHermes) { Write-Host ("  remove  {0}   [entire Hermes installation]" -f $hermesHome) }
Write-Host ''

if ($DryRun) {
    Write-Note 'Dry run: nothing was changed.'
    return
}

if (-not $Force) {
    if ($NonInteractive) {
        throw 'Refusing to remove anything without confirmation. Re-run with -Force (or use -DryRun to preview).'
    }
    $answer = Read-Host 'Proceed with removal? [y/N]'
    if ($answer -notmatch '^(y|yes)$') {
        Write-Note 'Aborted.'
        return
    }
}

# ---------------------------------------------------------------------------
# Stop anything holding the files open
# ---------------------------------------------------------------------------

Write-Step 'Stopping running processes...'

if ($hermesExe -and (Test-Path -LiteralPath $hermesExe)) {
    if ((Invoke-NativeQuiet -FilePath $hermesExe -ArgumentList @('gateway', 'stop')) -eq 0) {
        Write-Ok 'Gateway stopped'
    } else {
        Write-Note 'Gateway was not running'
    }
}

# A wrapper-mode plugin is imported by Hermes' own interpreter, so the lock can
# be held by python.exe under HERMES_HOME as well as by anything in the venv.
$lockRoots = @($mnemosyneVenv)
if ($IncludeHermes) { $lockRoots += $hermesHome }
foreach ($root in $lockRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $prefix = (Resolve-Path -LiteralPath $root).Path
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -ne $PID -and $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object {
            Write-Note ("Stopping {0} (PID {1})" -f $_.ProcessName, $_.Id)
            try { Stop-Process -Id $_.Id -Force -ErrorAction Stop } catch { Write-Warn "Could not stop PID $($_.Id): $($_.Exception.Message)" }
        }
}

# ---------------------------------------------------------------------------
# Deregister from Hermes
# ---------------------------------------------------------------------------

if (-not $IncludeHermes) {
    Write-Step 'Deregistering from Hermes...'

    # Best effort: the package's own uninstall/cleanup also handles layouts from
    # older releases that this script does not know about.
    if (Test-Path -LiteralPath $mnemosyneHermesExe) {
        Invoke-NativeQuiet -FilePath $mnemosyneHermesExe -ArgumentList @('uninstall') | Out-Null
        Invoke-NativeQuiet -FilePath $mnemosyneHermesExe -ArgumentList @('cleanup') | Out-Null
        Write-Ok 'Ran mnemosyne-hermes uninstall + cleanup'
    }

    # `mnemosyne-hermes cleanup` only rewrites config.yaml when `provider` is the
    # first key under `memory:`. Hermes writes it last, so unset it explicitly.
    if ($hermesExe -and (Test-Path -LiteralPath $hermesExe)) {
        $get = Invoke-NativeCapture -FilePath $hermesExe -ArgumentList @('config', 'get', 'memory.provider')
        if (($get.Output -join "`n") -match 'mnemosyne') {
            if ((Invoke-NativeQuiet -FilePath $hermesExe -ArgumentList @('config', 'unset', 'memory.provider')) -eq 0) {
                Write-Ok 'Unset memory.provider'
            } else {
                Add-Failure 'Could not unset memory.provider; edit config.yaml manually.'
            }
        } else {
            Write-Note 'memory.provider is not set to mnemosyne'
        }
    }

    # Profile configs are separate files that `hermes config` does not touch.
    if (Test-Path -LiteralPath $profilesDir) {
        foreach ($cfg in (Get-ChildItem -LiteralPath $profilesDir -Directory -ErrorAction SilentlyContinue |
                          ForEach-Object { Join-Path $_.FullName 'config.yaml' } |
                          Where-Object { Test-Path -LiteralPath $_ })) {
            $text = Get-Content -LiteralPath $cfg -Raw
            $new = [regex]::Replace($text, '(?m)^(\s*)provider:\s*mnemosyne\s*$', '$1# provider: mnemosyne (removed by uninstaller)')
            if ($new -ne $text) {
                Set-Content -LiteralPath $cfg -Value $new -Encoding UTF8
                Write-Ok "Unset memory.provider in $cfg"
            }
        }
    }

    # The installer's bootstrap step may have injected mnemosyne into Hermes' venv.
    if (Test-Path -LiteralPath $hermesVenvPython) {
        $list = Invoke-NativeCapture -FilePath $hermesVenvPython -ArgumentList @(
            '-m', 'pip', 'list', '--disable-pip-version-check', '--format=freeze')
        if ($list.ExitCode -eq 0) {
            $found = @($list.Output | Where-Object { $_ -match '^mnemosyne' } | ForEach-Object { ($_ -split '==')[0] })
            if ($found.Count -gt 0) {
                Invoke-NativeQuiet -FilePath $hermesVenvPython -ArgumentList (@(
                    '-m', 'pip', 'uninstall', '--yes', '--disable-pip-version-check') + $found) | Out-Null
                Write-Ok ("Removed from Hermes venv: {0}" -f ($found -join ', '))
            } else {
                Write-Note 'Hermes venv has no mnemosyne packages'
            }
        } else {
            # Hermes' venv is created by uv without --seed, so it often has no pip.
            Write-Note 'Hermes venv has no usable pip; nothing to uninstall there'
        }
    }
}

# ---------------------------------------------------------------------------
# Delete files
# ---------------------------------------------------------------------------

function Remove-TreeHard {
    param([Parameter(Mandatory)][string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path)) { return $true }
    # Antivirus and just-terminated processes keep transient handles; a couple of
    # retries turns a spurious failure into a clean removal.
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Attributes -band [IO.FileAttributes]::ReadOnly } |
                ForEach-Object { $_.Attributes = $_.Attributes -band (-bnot [IO.FileAttributes]::ReadOnly) }
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            Write-Ok ("Removed {0}   [{1}]" -f $Path, $Label)
            return $true
        } catch {
            if ($attempt -eq 3) {
                Add-Failure ("Could not remove {0}: {1}" -f $Path, $_.Exception.Message)
                return $false
            }
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

Write-Step 'Removing files...'
foreach ($t in $targets) { [void](Remove-TreeHard -Path $t.Path -Label $t.Label) }

# Prune the parent dirs the installer created, but only while they are empty.
foreach ($parent in @((Join-Path $hermesHome 'skills\memory'), (Join-Path $hermesHome 'plugins'))) {
    if ((Test-Path -LiteralPath $parent) -and -not (Get-ChildItem -LiteralPath $parent -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $parent -Force -ErrorAction SilentlyContinue
        Write-Note "Removed empty $parent"
    }
}

if ($IncludeHermes) {
    Write-Step 'Removing Hermes Agent...'
    [void](Remove-TreeHard -Path $hermesHome -Label 'Hermes installation')

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath) {
        $kept = @($userPath -split ';' | Where-Object {
            $_ -and -not $_.TrimEnd('\').StartsWith($hermesHome.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
        })
        $newPath = $kept -join ';'
        if ($newPath -ne $userPath) {
            [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
            Write-Ok 'Removed Hermes entries from the user PATH'
        }
    }
}

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

Write-Step 'Clearing environment variables...'
foreach ($n in $envVarsToClear) {
    if ([Environment]::GetEnvironmentVariable($n, 'User')) {
        [Environment]::SetEnvironmentVariable($n, $null, 'User')
        Write-Ok "Unset user variable $n"
    }
    Remove-Item -LiteralPath "Env:$n" -ErrorAction SilentlyContinue
}

# ---------------------------------------------------------------------------
# Restart the gateway if Hermes is staying
# ---------------------------------------------------------------------------

if (-not $IncludeHermes -and $hermesExe -and (Test-Path -LiteralPath $hermesExe)) {
    Write-Step 'Restarting the Hermes gateway...'
    if ((Invoke-NativeQuiet -FilePath $hermesExe -ArgumentList @('gateway', 'restart')) -eq 0) {
        Write-Ok 'Gateway restarted'
    } else {
        Write-Warn 'Gateway did not restart. Run: hermes gateway restart'
    }
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

Write-Step 'Verifying...'
$leftovers = @()
foreach ($p in @(
    (Join-Path $hermesHome 'plugins\mnemosyne'),
    (Join-Path $hermesHome 'plugins\hermes-mnemosyne'),
    (Join-Path $hermesHome 'skills\memory\mnemosyne-memory-override'),
    $mnemosyneVenv
)) {
    if (Test-Path -LiteralPath $p) { $leftovers += $p }
}
if (-not $KeepData -and (Test-Path -LiteralPath $dataDir)) { $leftovers += $dataDir }
if ($IncludeHermes -and (Test-Path -LiteralPath $hermesHome)) { $leftovers += $hermesHome }
foreach ($n in $envVarsToClear) {
    if ([Environment]::GetEnvironmentVariable($n, 'User')) { $leftovers += "user variable $n" }
}

Write-Host ''
if ($leftovers.Count -eq 0 -and $script:Failures.Count -eq 0) {
    Write-Host 'Uninstall complete. Nothing left behind.' -ForegroundColor Green
} else {
    Write-Host 'Uninstall finished with leftovers:' -ForegroundColor Yellow
    foreach ($l in $leftovers) { Write-Host "  still present: $l" -ForegroundColor Yellow }
    foreach ($f in $script:Failures) { Write-Host "  $f" -ForegroundColor Yellow }
    Write-Host '  Close every terminal and app using these paths, then re-run.' -ForegroundColor Yellow
}

Write-Host ''
Write-Note 'Tooling installed system-wide via winget is left alone. To remove it too:'
Write-Note '  winget uninstall --id astral-sh.uv'
if ($IncludeHermes) {
    Write-Note '  winget uninstall --id BurntSushi.ripgrep.MSVC'
    Write-Note '  winget uninstall --id Gyan.FFmpeg'
}
Write-Note 'Open a new terminal so the cleared environment variables take effect.'

if ($leftovers.Count -gt 0 -or $script:Failures.Count -gt 0) { exit 1 }
