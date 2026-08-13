<#
.SYNOPSIS
    Installs the Mnemosyne memory provider for Hermes Agent on Windows.

.DESCRIPTION
    Windows/PowerShell port of install-mnemosyne-hermes-unix.sh.

    Differences from the Unix script that are forced by the platform:

      * Hermes' own Windows installer puts HERMES_HOME at %LOCALAPPDATA%\hermes,
        not ~/.hermes, so that is the default here too.
      * Environment variables are persisted as *User* environment variables
        (the registry equivalent of appending to ~/.profile), not to a shell rc file.
      * System packages come from winget instead of apt/dnf/pacman/brew.
      * The systemd `loginctl enable-linger` step has no Windows equivalent and
        is omitted.
      * mnemosyne resolves its database from MNEMOSYNE_DATA_DIR, falling back to
        $HERMES_HOME\mnemosyne. MNEMOSYNE_HOME alone does not move the database,
        so when a non-default MNEMOSYNE_HOME is given this script also sets
        MNEMOSYNE_DATA_DIR to keep the data where you asked for it.

.PARAMETER NoEmbeddings
    Request mnemosyne-memory without the [embeddings] extra.

    Note that as of mnemosyne-hermes 0.5.0 this no longer changes the result:
    that package declares `mnemosyne-memory[embeddings]` as a hard dependency,
    so fastembed / onnxruntime / sqlite-vec are pulled in either way. The flag
    is kept for parity with the Unix script, which has the same limitation.

.PARAMETER SkipHermesConfiguration
    Do not change Hermes provider configuration and do not restart the gateway.

.PARAMETER NonInteractive
    Never prompt; fail instead of asking. Implied when the session is not
    attached to a console.

.EXAMPLE
    .\install-mnemosyne-hermes-windows.ps1

.EXAMPLE
    .\install-mnemosyne-hermes-windows.ps1 -NoEmbeddings -SkipHermesConfiguration
#>
[CmdletBinding()]
param(
    [switch]$NoEmbeddings,
    [switch]$SkipHermesConfiguration,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "  + $Message" -ForegroundColor Green }
function Write-Note { param([string]$Message) Write-Host "  . $Message" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Message) Write-Host "  ! $Message" -ForegroundColor Yellow }

function Test-Command {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -CommandType Application, ExternalScript -ErrorAction SilentlyContinue)
}

function Get-CommandPath {
    param([Parameter(Mandatory)][string]$Name)
    $cmd = Get-Command $Name -CommandType Application, ExternalScript -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

# Native executables do not throw on failure, so every call goes through here.
# The exit code lands in $script:LastNativeExit rather than being returned, so
# that the callee's stdout stays on the console instead of being captured.
$script:LastNativeExit = 0
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$AllowFailure,
        [switch]$Quiet,
        [string]$What
    )
    if (-not $What) { $What = (Split-Path -Leaf $FilePath) }
    if ($Quiet) { & $FilePath @ArgumentList | Out-Null } else { & $FilePath @ArgumentList }
    $script:LastNativeExit = $LASTEXITCODE
    if ($script:LastNativeExit -ne 0 -and -not $AllowFailure) {
        throw "$What failed with exit code $script:LastNativeExit."
    }
}

# winget / hermes install.ps1 write the *User* PATH; pull it back into this process.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Set-PersistentEnv {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value
    )
    Set-Item -Path "Env:$Name" -Value $Value
    if ([Environment]::GetEnvironmentVariable($Name, 'User') -ne $Value) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
        Write-Ok "Persisted $Name=$Value"
    } else {
        Write-Note "$Name already set to $Value"
    }
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory)][string]$Id,
        [Parameter(Mandatory)][string]$DisplayName
    )
    if (-not (Test-Command 'winget')) {
        throw "Cannot install '$DisplayName' automatically: winget is not available. Install $DisplayName manually and rerun."
    }
    Write-Step "Installing $DisplayName via winget ($Id)..."
    # --source winget disambiguates from identically named msstore entries.
    Invoke-Native -FilePath 'winget' -AllowFailure -Quiet -What "winget install $Id" -ArgumentList @(
        'install', '--id', $Id, '--exact', '--source', 'winget', '--silent',
        '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'
    )
    # 0 = installed. -1978335189 = "no applicable upgrade" i.e. already current.
    $code = $script:LastNativeExit
    if ($code -ne 0 -and $code -ne -1978335189) {
        throw "winget could not install $DisplayName (exit code $code)."
    }
    Update-SessionPath
}

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "PowerShell 5.1 or newer is required (found $($PSVersionTable.PSVersion))."
}
if (-not $NonInteractive -and -not [Environment]::UserInteractive) { $NonInteractive = $true }

Write-Step 'Checking prerequisites...'

# curl.exe ships with Windows 10 1803+ / Server 2019+. Invoke-WebRequest is the fallback.
$curlExe = Get-CommandPath 'curl.exe'
if ($curlExe) { Write-Note "curl: $curlExe" } else { Write-Note 'curl.exe not found; using Invoke-WebRequest' }

function Get-RemoteFile {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile
    )
    if ($curlExe) {
        Invoke-Native -FilePath $curlExe -Quiet -What "curl $Uri" -ArgumentList @('-fsSL', '-o', $OutFile, $Uri)
    } else {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Uri -OutFile $OutFile -UseBasicParsing
    }
    if (-not (Test-Path -LiteralPath $OutFile)) { throw "Download of $Uri produced no file." }
}

# Long paths: mnemosyne-memory[embeddings] pulls in onnxruntime, whose deepest
# module path is ~120 characters. With LongPathsEnabled=0 the venv must sit well
# inside MAX_PATH or pip fails mid-install with a bogus "No such file" OSError.
$longPathsEnabled = $false
try {
    $lp = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name 'LongPathsEnabled' -ErrorAction Stop
    $longPathsEnabled = ($lp.LongPathsEnabled -eq 1)
} catch {
    $longPathsEnabled = $false
}
Write-Note "Win32 long paths enabled: $longPathsEnabled"

# ---------------------------------------------------------------------------
# Hermes
# ---------------------------------------------------------------------------

Update-SessionPath

if (-not (Test-Command 'hermes')) {
    Write-Step 'Hermes was not found. Running the official Hermes installer...'
    $bootstrap = Join-Path ([IO.Path]::GetTempPath()) ("hermes-install-{0}.ps1" -f [guid]::NewGuid().ToString('N'))
    try {
        Get-RemoteFile -Uri 'https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1' -OutFile $bootstrap
        # Hashtable splat, not array: array splatting would pass "-NonInteractive"
        # as a positional *value* and it would bind to install.ps1's -Branch.
        $hermesArgs = @{}
        if ($NonInteractive) { $hermesArgs['NonInteractive'] = $true }
        & $bootstrap @hermesArgs
        if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
            Write-Warn "Hermes installer returned exit code $LASTEXITCODE; continuing to probe for the hermes command."
        }
    } finally {
        Remove-Item -LiteralPath $bootstrap -Force -ErrorAction SilentlyContinue
    }
    Update-SessionPath
    # The installer adds <HERMES_HOME>\hermes-agent\bin to the User PATH, but a
    # freshly written value is not always visible yet; look there directly too.
    if (-not (Test-Command 'hermes')) {
        $candidateHome = $env:HERMES_HOME
        if (-not $candidateHome) { $candidateHome = Join-Path $env:LOCALAPPDATA 'hermes' }
        foreach ($rel in @('hermes-agent\bin', 'hermes-agent\venv\Scripts')) {
            $probe = Join-Path $candidateHome $rel
            if (Test-Path -LiteralPath (Join-Path $probe 'hermes.exe')) {
                $env:Path = "$probe;$env:Path"
                break
            }
        }
    }
}

if (-not (Test-Command 'hermes')) {
    throw 'Hermes is not on PATH. Open a new PowerShell session and rerun this script.'
}
$hermesExe = Get-CommandPath 'hermes'
Write-Ok "hermes: $hermesExe"

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$hermesHome = $env:HERMES_HOME
if (-not $hermesHome) { $hermesHome = [Environment]::GetEnvironmentVariable('HERMES_HOME', 'User') }
if (-not $hermesHome) { $hermesHome = Join-Path $env:LOCALAPPDATA 'hermes' }

$defaultMnemosyneHome = Join-Path $hermesHome 'mnemosyne'
$mnemosyneHome = $env:MNEMOSYNE_HOME
if (-not $mnemosyneHome) { $mnemosyneHome = [Environment]::GetEnvironmentVariable('MNEMOSYNE_HOME', 'User') }
if (-not $mnemosyneHome) { $mnemosyneHome = $defaultMnemosyneHome }

$mnemosyneVenv = $env:MNEMOSYNE_VENV
if (-not $mnemosyneVenv) { $mnemosyneVenv = [Environment]::GetEnvironmentVariable('MNEMOSYNE_VENV', 'User') }
if (-not $mnemosyneVenv) { $mnemosyneVenv = Join-Path $env:USERPROFILE '.mnemosyne-venv' }

New-Item -ItemType Directory -Force -Path $hermesHome, $mnemosyneHome | Out-Null

Write-Step 'Persisting environment...'
Set-PersistentEnv -Name 'HERMES_HOME' -Value $hermesHome
Set-PersistentEnv -Name 'MNEMOSYNE_HOME' -Value $mnemosyneHome
# MNEMOSYNE_HOME is only consulted for .env discovery; the database follows
# MNEMOSYNE_DATA_DIR (else $HERMES_HOME\mnemosyne). Pin it when they diverge.
if ($mnemosyneHome -ne $defaultMnemosyneHome) {
    Set-PersistentEnv -Name 'MNEMOSYNE_DATA_DIR' -Value $mnemosyneHome
}

if (-not $longPathsEnabled -and $mnemosyneVenv.Length -gt 80) {
    Write-Warn "Long paths are disabled and the venv path is $($mnemosyneVenv.Length) characters."
    Write-Warn 'If pip fails with "No such file or directory", set MNEMOSYNE_VENV to a shorter path (e.g. C:\mnemosyne-venv) and rerun.'
}

# ---------------------------------------------------------------------------
# Python / uv
# ---------------------------------------------------------------------------

Write-Step 'Resolving a Python toolchain...'

# Hermes vendors its own uv; prefer that over anything on PATH.
$uvExe = $null
$hermesUv = Join-Path $hermesHome 'bin\uv.exe'
if (Test-Path -LiteralPath $hermesUv) { $uvExe = $hermesUv } else { $uvExe = Get-CommandPath 'uv.exe' }

$pythonExe = $null
if (-not $uvExe) {
    foreach ($name in @('py.exe', 'python.exe', 'python3.exe')) {
        $candidate = Get-CommandPath $name
        if (-not $candidate) { continue }
        # The Microsoft Store stubs under WindowsApps are not real interpreters.
        if ($candidate -like '*\WindowsApps\*') { continue }
        $pythonExe = $candidate
        break
    }
    if (-not $pythonExe) {
        Install-WingetPackage -Id 'astral-sh.uv' -DisplayName 'uv'
        $uvExe = Get-CommandPath 'uv.exe'
        if (-not $uvExe) {
            Install-WingetPackage -Id 'Python.Python.3.11' -DisplayName 'Python 3.11'
            $pythonExe = Get-CommandPath 'python.exe'
        }
    }
}
if ($uvExe) { Write-Ok "uv: $uvExe" } elseif ($pythonExe) { Write-Ok "python: $pythonExe" }
if (-not $uvExe -and -not $pythonExe) { throw 'Neither uv nor a usable Python interpreter is available.' }

# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------

$venvPython = Join-Path $mnemosyneVenv 'Scripts\python.exe'
$mnemosyneHermesExe = Join-Path $mnemosyneVenv 'Scripts\mnemosyne-hermes.exe'
$mnemosyneCliExe = Join-Path $mnemosyneVenv 'Scripts\mnemosyne.exe'

# Mirror the Unix script: a venv that exists but has no working python/pip is
# rebuilt rather than patched.
if ((Test-Path -LiteralPath $mnemosyneVenv) -and (Test-Path -LiteralPath $venvPython)) {
    # Windows PowerShell 5.1 wraps a native executable's stderr in ErrorRecords as
    # soon as any stream is redirected, and $ErrorActionPreference='Stop' turns
    # those into a terminating error. Drop to 'Continue' for the probe.
    $previousEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $venvPython -c 'import pip' 2>&1 | Out-Null
    $pipProbe = $LASTEXITCODE
    $ErrorActionPreference = $previousEA
    if ($pipProbe -ne 0) {
        Write-Warn 'Existing venv has no working pip; recreating.'
        Remove-Item -LiteralPath $mnemosyneVenv -Recurse -Force
    }
} elseif (Test-Path -LiteralPath $mnemosyneVenv) {
    Write-Warn 'Existing venv has no interpreter; recreating.'
    Remove-Item -LiteralPath $mnemosyneVenv -Recurse -Force
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Creating virtual environment at $mnemosyneVenv ..."
    if ($uvExe) {
        # --seed installs pip into the uv-created venv.
        Invoke-Native -FilePath $uvExe -What 'uv venv' -ArgumentList @('venv', $mnemosyneVenv, '--python', '3.11', '--seed')
    } else {
        Invoke-Native -FilePath $pythonExe -What 'python -m venv' -ArgumentList @('-m', 'venv', $mnemosyneVenv)
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) { throw "Virtual environment creation did not produce $venvPython." }
Write-Ok "venv python: $venvPython"

if ($NoEmbeddings) {
    $package = 'mnemosyne-memory'
    # mnemosyne-hermes requires mnemosyne-memory[embeddings] outright, so pip
    # resolves the extra back in regardless of what is requested here.
    Write-Warn 'mnemosyne-hermes depends on mnemosyne-memory[embeddings]; the vector-search extras will still be installed.'
} else {
    $package = 'mnemosyne-memory[embeddings]'
}

Write-Step 'Installing packages...'
Invoke-Native -FilePath $venvPython -What 'pip install --upgrade pip' -ArgumentList @(
    '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', '--quiet', 'pip')
Write-Note "Installing $package and mnemosyne-hermes (this can take a few minutes)..."
Invoke-Native -FilePath $venvPython -What "pip install $package mnemosyne-hermes" -ArgumentList @(
    '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', $package, 'mnemosyne-hermes')

if (-not (Test-Path -LiteralPath $mnemosyneHermesExe)) { throw "mnemosyne-hermes was not installed at $mnemosyneHermesExe." }

# ---------------------------------------------------------------------------
# Register the provider with Hermes
# ---------------------------------------------------------------------------

# Wrapper mode, not symlink: creating a symlink on Windows needs either
# Developer Mode or elevation, and the Unix script already uses wrapper mode.
Write-Step 'Registering the Mnemosyne memory provider...'
$installArgs = @('install', '--mode', 'wrapper', '--python', $venvPython)
# Without --force the command aborts when the plugin directory already exists,
# which would make every re-run (i.e. every upgrade) fail. --force also refreshes
# the bundled memory-override skill, keeping a SKILL.md.bak of the previous copy.
if (Test-Path -LiteralPath (Join-Path $hermesHome 'plugins\mnemosyne')) {
    Write-Note 'Existing provider registration found; replacing it (--force).'
    $installArgs += '--force'
}
Invoke-Native -FilePath $mnemosyneHermesExe -What 'mnemosyne-hermes install' -ArgumentList $installArgs

if (-not $SkipHermesConfiguration) {
    Write-Step 'Configuring Hermes...'
    Invoke-Native -FilePath $hermesExe -What 'hermes config set memory.provider mnemosyne' -ArgumentList @(
        'config', 'set', 'memory.provider', 'mnemosyne')

    Invoke-Native -FilePath $hermesExe -AllowFailure -What 'hermes gateway restart' -ArgumentList @('gateway', 'restart')
    if ($script:LastNativeExit -ne 0) {
        Write-Warn 'Gateway restart was not completed. Run: hermes gateway restart'
    }
}

# ---------------------------------------------------------------------------
# Work around mnemosyne's Windows config encoding
# ---------------------------------------------------------------------------

# mnemosyne writes <data>\config.yaml through Python's default text encoding,
# which on Windows is the ANSI codepage, but always reads it back as UTF-8. The
# template's em dash then makes every command print
# "Failed to inspect legacy provider defaults: 'utf-8' codec can't decode...".
# Re-encoding the file as UTF-8 keeps the content and silences the warning.
$mnemosyneConfig = Join-Path $mnemosyneHome 'config.yaml'
if (Test-Path -LiteralPath $mnemosyneConfig) {
    $bytes = [IO.File]::ReadAllBytes($mnemosyneConfig)
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    $needsFix = $false
    try { [void]$strictUtf8.GetString($bytes) } catch { $needsFix = $true }
    if ($needsFix) {
        $text = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage).GetString($bytes)
        [IO.File]::WriteAllText($mnemosyneConfig, $text, (New-Object Text.UTF8Encoding($false)))
        Write-Note "Re-encoded $mnemosyneConfig as UTF-8"
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host 'Installation complete.' -ForegroundColor Green
Write-Host ("Hermes home:    {0}" -f $hermesHome)
Write-Host ("Mnemosyne data: {0}" -f $mnemosyneHome)
Write-Host ("Mnemosyne venv: {0}" -f $mnemosyneVenv)
Write-Host ''

Invoke-Native -FilePath $hermesExe -AllowFailure -What 'hermes memory status' -ArgumentList @('memory', 'status')
if (Test-Path -LiteralPath $mnemosyneCliExe) {
    Invoke-Native -FilePath $mnemosyneCliExe -AllowFailure -What 'mnemosyne stats' -ArgumentList @('stats')
}

Write-Host ''
Write-Note 'Open a new terminal so HERMES_HOME / MNEMOSYNE_HOME are visible everywhere.'
