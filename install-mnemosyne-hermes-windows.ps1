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
    Disable dense vector retrieval by setting MNEMOSYNE_NO_EMBEDDINGS=1.

    It cannot skip the download: mnemosyne-hermes declares
    `mnemosyne-memory[embeddings]` as a hard dependency, so fastembed /
    onnxruntime / sqlite-vec are pulled in either way. What it does is stop
    Mnemosyne generating vectors at runtime, leaving keyword/FTS recall only.

.PARAMETER All
    Install mnemosyne-memory[all]: adds the local LLM used for sleep
    consolidation, plus the MCP and sync extras.

    llama-cpp-python is compiled from source, so this takes several minutes.
    Measured on mnemosyne-memory 3.15.1 from PyPI (before these installers moved
    to upstream main) it added roughly 120 MB, not the ~1.5 GB upstream quotes,
    and it did not pull in sentence-transformers or torch. The GGUF model is
    fetched later, on the first sleep consolidation.

.PARAMETER DisableBuiltinMemory
    Turn off Hermes' built-in MEMORY.md / USER.md store, which upstream
    recommends once Mnemosyne is the provider so the two do not both consume
    context. Off by default because it changes what the agent remembers.

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
    [switch]$All,
    [switch]$DisableBuiltinMemory,
    [switch]$SkipHermesConfiguration,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if ($All -and $NoEmbeddings) {
    throw '-All and -NoEmbeddings contradict each other: one adds the local embedding and LLM stack, the other turns dense retrieval off. Pick one.'
}
if ($DisableBuiltinMemory -and $SkipHermesConfiguration) {
    throw '-DisableBuiltinMemory needs to write Hermes configuration, which -SkipHermesConfiguration forbids. Pick one.'
}

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

# Reads "3.11" from an interpreter, or $null if it cannot be run. Redirecting a
# native command's stderr under $ErrorActionPreference='Stop' turns ordinary
# output into a terminating error, so drop to 'Continue' for the probe.
# Windows PowerShell 5.1 strips embedded double quotes from native arguments,
# so the Python code passed to -c must use single quotes only.
function Get-PyMinorVersion {
    param([Parameter(Mandatory)][string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath)) { return $null }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $PythonPath -c 'import sys; print(''%d.%d'' % sys.version_info[:2])' 2>$null
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { return $null }
    return ([string]($out | Select-Object -First 1)).Trim()
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
    # The installer adds its bin directory to the User PATH, but a freshly
    # written value is not always visible yet; look there directly too.
    if (-not (Test-Command 'hermes')) {
        $candidateHome = $env:HERMES_HOME
        if (-not $candidateHome) { $candidateHome = Join-Path $env:LOCALAPPDATA 'hermes' }
        foreach ($rel in @('bin', 'hermes-agent\bin', 'hermes-agent\venv\Scripts')) {
            $probe = Join-Path $candidateHome $rel
            if ((Test-Path -LiteralPath (Join-Path $probe 'hermes.exe')) -or (Test-Path -LiteralPath (Join-Path $probe 'hermes.cmd'))) {
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

# Hermes vendors its own uv; prefer that over anything on PATH. Current Hermes
# keeps it under tools\uv-<version>-<platform>\ (HERMES_RUNTIME_DIR relocates
# tools\), older installs under bin\. Newest version first, compared as versions.
$uvExe = $null
$hermesTools = if ($env:HERMES_RUNTIME_DIR) { $env:HERMES_RUNTIME_DIR } else { Join-Path $hermesHome 'tools' }
$toolsUv = @(Get-ChildItem -Path (Join-Path $hermesTools 'uv-*\uv.exe') -ErrorAction SilentlyContinue |
    Sort-Object -Descending -Property {
        $parsed = $null
        if ($_.Directory.Name -match '^uv-(\d+(?:\.\d+)+)') { [void][version]::TryParse($Matches[1], [ref]$parsed) }
        $parsed
    } | Select-Object -ExpandProperty FullName)
$hermesUv = $toolsUv + @(Join-Path $hermesHome 'bin\uv.exe') |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if ($hermesUv) { $uvExe = $hermesUv } else { $uvExe = Get-CommandPath 'uv.exe' }

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

# The provider is registered in wrapper mode, so Hermes' own interpreter imports
# these packages in-process. Compiled wheels are ABI-locked to one Python minor
# version, and a mismatch fails quietly: the provider still registers and
# keyword recall still works, but embeddings never load, so memories are stored
# without vectors and semantic recall can never match them.
#
# Current Hermes runs from a managed "store" Python and reports it through its
# launcher's --print-runtime-command: a JSON argv whose first element is the
# interpreter that owns the ABI. The JSON is ASCII (Python escapes non-ASCII
# paths), so the console code page cannot mangle it. Older installs kept a venv
# inside the checkout and may not reject the flag, so the call is bounded and
# gets an empty stdin. MNEMOSYNE_HERMES_PYTHON overrides detection (not
# HERMES_PYTHON, which Hermes' own tooling sets to unrelated interpreters).
function Resolve-HermesPython {
    if ($env:MNEMOSYNE_HERMES_PYTHON) {
        if (Get-PyMinorVersion -PythonPath $env:MNEMOSYNE_HERMES_PYTHON) { return $env:MNEMOSYNE_HERMES_PYTHON }
        throw "MNEMOSYNE_HERMES_PYTHON=$($env:MNEMOSYNE_HERMES_PYTHON) is not a runnable Python interpreter. Point it at Hermes' python.exe and rerun."
    }
    $stdin = [IO.Path]::GetTempFileName()
    $stdout = [IO.Path]::GetTempFileName()
    $stderr = [IO.Path]::GetTempFileName()
    $line = $null
    try {
        $process = Start-Process -FilePath $hermesExe -ArgumentList '--print-runtime-command' -NoNewWindow -PassThru `
            -RedirectStandardInput $stdin -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        # Touching Handle keeps ExitCode readable after exit (a Start-Process quirk).
        $null = $process.Handle
        if (-not $process.WaitForExit(60000)) {
            # Native stderr under 'Stop' is a terminating error in PS 5.1, and
            # taskkill reports children that already exited on stderr.
            $previous = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try { & taskkill.exe /T /F /PID $process.Id 2>&1 | Out-Null } finally { $ErrorActionPreference = $previous }
            [void]$process.WaitForExit(5000)
            # A launcher that hangs is a current one in trouble, not an old layout:
            # adopting a leftover legacy venv here could pick the wrong Python.
            Write-Note 'hermes --print-runtime-command did not answer within 60 seconds.'
            return $null
        }
        if ($process.ExitCode -eq 0) {
            $line = Get-Content -LiteralPath $stdout | Where-Object { $_.StartsWith('[') } | Select-Object -First 1
        }
    } finally {
        Remove-Item -LiteralPath $stdin, $stdout, $stderr -Force -ErrorAction SilentlyContinue
    }
    if ($line) {
        try {
            $python = @(($line | ConvertFrom-Json))[0]
            if ($python -is [string] -and (Test-Path -LiteralPath $python -PathType Leaf)) { return $python }
        } catch {
            Write-Note "hermes --print-runtime-command returned no usable JSON: $($_.Exception.Message)"
            return $null
        }
        Write-Note "hermes --print-runtime-command named an interpreter that does not exist: $line"
        return $null
    }
    # No answer at all: an older Hermes without the flag, with its venv in the checkout.
    $legacy = Join-Path $hermesHome 'hermes-agent\venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $legacy -PathType Leaf) {
        Write-Note "hermes --print-runtime-command gave no interpreter; using the legacy venv $legacy"
        return $legacy
    }
    return $null
}
# Refuse rather than guess: a guessed version is exactly how memories end up
# stored without embeddings.
$hermesPython = Resolve-HermesPython
$hermesPyVersion = $null
if ($hermesPython) { $hermesPyVersion = Get-PyMinorVersion -PythonPath $hermesPython }
if (-not $hermesPyVersion) {
    throw ("Could not find the Python interpreter Hermes runs on (tried hermes --print-runtime-command, then " +
        "$hermesHome\hermes-agent\venv). Hermes imports Mnemosyne in-process, so the Mnemosyne environment has " +
        "to be built for Hermes' Python version; a guessed one stores every memory without an embedding. " +
        "Set MNEMOSYNE_HERMES_PYTHON to Hermes' python.exe and rerun.")
}
Write-Ok "Hermes runs Python $hermesPyVersion ($hermesPython); matching it."

# Install from the official repository's main branch rather than PyPI, which
# lags it. Both packages come from one commit: mnemosyne-hermes on main requires
# the mnemosyne-memory version that ships beside it. The commit is pinned in the
# archive URL, so a re-run picks up whatever main has moved on to. Resolved
# before the venv is touched, so a failure here leaves an existing install alone.
$mnemosyneRepo = 'mnemosyne-oss/mnemosyne'
# Returns @{ Sha; Status }: Status is the HTTP status, or $null when unreachable.
function Get-MainCommit {
    param([string]$Token)
    $headers = @{}
    if ($Token) { $headers['Authorization'] = "Bearer $Token" }
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $response = Invoke-RestMethod -Uri "https://api.github.com/repos/$mnemosyneRepo/commits/main" -Headers $headers
        return @{ Sha = [string]$response.sha; Status = 200 }
    } catch {
        $status = $null
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        Write-Note "GitHub API request failed: $($_.Exception.Message)"
        return @{ Sha = ''; Status = $status }
    }
}
$githubToken = if ($env:GITHUB_TOKEN) { $env:GITHUB_TOKEN } else { $env:GH_TOKEN }
$commit = Get-MainCommit -Token $githubToken
if ($commit.Status -eq 401 -and $githubToken) {
    # An expired token, or one for another host, must not block a public repo.
    Write-Note 'GitHub rejected the token in GITHUB_TOKEN/GH_TOKEN; retrying without it.'
    $githubToken = $null
    $commit = Get-MainCommit -Token $null
}
$mnemosyneCommit = $commit.Sha
if ($mnemosyneCommit -notmatch '^[0-9a-f]{40}$') {
    $commitStatus = $commit.Status
    $hint = if (($commitStatus -eq 403 -or $commitStatus -eq 429) -and $githubToken) {
        "GitHub answered HTTP $commitStatus to the token in GITHUB_TOKEN/GH_TOKEN (rate limit or missing access). Unset it, or wait, and rerun."
    } elseif ($commitStatus -eq 403 -or $commitStatus -eq 429) {
        'GitHub API rate limit reached (60 requests/hour per IP without a token). Wait and rerun, or set GITHUB_TOKEN and rerun.'
    } elseif ($null -eq $commitStatus) {
        'api.github.com is unreachable; check the network or proxy and rerun.'
    } elseif ($commitStatus -eq 200) {
        'api.github.com answered, but not with a commit id; a proxy or captive portal may be intercepting HTTPS. Check the network and rerun.'
    } else {
        "api.github.com answered HTTP $commitStatus; rerun later."
    }
    throw "Could not resolve the latest commit of https://github.com/$mnemosyneRepo (main). $hint The existing Mnemosyne environment was left untouched."
}
$mnemosyneArchive = "https://github.com/$mnemosyneRepo/archive/$mnemosyneCommit.zip"

function Get-InstalledVersion {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Test-Path -LiteralPath $venvPython)) { return $null }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $venvPython -c "import importlib.metadata as m; print(m.version('$Name'))" 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ([string]($out | Select-Object -First 1)).Trim()
    } finally {
        $ErrorActionPreference = $previous
    }
}
$oldVersion = Get-InstalledVersion -Name 'mnemosyne-memory'

# Mirror the Unix script: a venv that exists but has no working python/pip, or
# was built against a different Python, is rebuilt rather than patched.
if ((Test-Path -LiteralPath $mnemosyneVenv) -and (Test-Path -LiteralPath $venvPython) -and
    ((Get-PyMinorVersion -PythonPath $venvPython) -ne $hermesPyVersion)) {
    Write-Warn "Existing venv was built against Python $(Get-PyMinorVersion -PythonPath $venvPython), not $hermesPyVersion; recreating."
    Remove-Item -LiteralPath $mnemosyneVenv -Recurse -Force
}
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

$venvCreated = $false
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Creating virtual environment at $mnemosyneVenv ..."
    $venvCreated = $true
    if ($uvExe) {
        # --seed installs pip into the uv-created venv.
        Invoke-Native -FilePath $uvExe -What 'uv venv' -ArgumentList @('venv', $mnemosyneVenv, '--python', $hermesPyVersion, '--seed')
    } else {
        Invoke-Native -FilePath $pythonExe -What 'python -m venv' -ArgumentList @('-m', 'venv', $mnemosyneVenv)
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) { throw "Virtual environment creation did not produce $venvPython." }
Write-Ok "venv python: $venvPython"

$venvPyVersion = Get-PyMinorVersion -PythonPath $venvPython
if ($venvPyVersion -ne $hermesPyVersion) {
    throw ("Python version mismatch: Hermes runs $hermesPyVersion but the Mnemosyne " +
        "environment is $venvPyVersion. Hermes imports Mnemosyne in-process, so compiled " +
        "wheels built for $venvPyVersion cannot load under $hermesPyVersion; memories would be " +
        "stored without embeddings. Install a Python $hermesPyVersion interpreter (or uv) and rerun.")
}

if ($All) {
    $extras = '[all]'
    Write-Note 'Installing the [all] profile; llama-cpp-python compiles from source, so this takes a few minutes.'
} elseif ($NoEmbeddings) {
    $extras = ''
    # mnemosyne-hermes requires mnemosyne-memory[embeddings] outright, so pip
    # resolves the extra back in regardless of what is requested here. The only
    # thing that actually turns dense retrieval off is Mnemosyne's own runtime
    # switch, so set that rather than leaving the flag doing nothing.
    Write-Note 'mnemosyne-hermes depends on mnemosyne-memory[embeddings]; the extras are installed regardless.'
    Set-PersistentEnv -Name 'MNEMOSYNE_NO_EMBEDDINGS' -Value '1'
    Write-Ok 'Dense vector retrieval disabled via MNEMOSYNE_NO_EMBEDDINGS=1'
} else {
    $extras = '[embeddings]'
    # The flag is declarative: omitting it has to undo a previous -NoEmbeddings
    # run, or one flagged install would leave dense retrieval off forever.
    if ([Environment]::GetEnvironmentVariable('MNEMOSYNE_NO_EMBEDDINGS', 'User')) {
        [Environment]::SetEnvironmentVariable('MNEMOSYNE_NO_EMBEDDINGS', $null, 'User')
        Remove-Item -LiteralPath 'Env:MNEMOSYNE_NO_EMBEDDINGS' -ErrorAction SilentlyContinue
        Write-Ok 'Cleared MNEMOSYNE_NO_EMBEDDINGS left by an earlier -NoEmbeddings install'
    }
}

$memorySpec = "mnemosyne-memory$extras @ $mnemosyneArchive"
$hermesSpec = "mnemosyne-hermes @ $mnemosyneArchive#subdirectory=integrations/hermes"

Write-Step "Installing packages from $mnemosyneRepo main at $($mnemosyneCommit.Substring(0, 12))..."
Invoke-Native -FilePath $venvPython -What 'pip install --upgrade pip' -ArgumentList @(
    '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', '--quiet', 'pip')
Write-Note "Installing mnemosyne-memory$extras and mnemosyne-hermes (this can take a few minutes)..."
Invoke-Native -FilePath $venvPython -What "pip install mnemosyne-memory$extras mnemosyne-hermes" -ArgumentList @(
    '-m', 'pip', 'install', '--upgrade', '--disable-pip-version-check', $memorySpec, $hermesSpec)
# pip skips a direct-URL package whose version is already installed, and main
# keeps one version string across many commits, so --upgrade alone would leave
# a re-run on the old commit. Force just these two; their dependencies were
# resolved from this commit's metadata by the call above. A venv created in
# this run already holds this commit.
if (-not $venvCreated) {
    Invoke-Native -FilePath $venvPython -What 'pip install --force-reinstall --no-deps mnemosyne-memory mnemosyne-hermes' -ArgumentList @(
        '-m', 'pip', 'install', '--force-reinstall', '--no-deps', '--disable-pip-version-check', $memorySpec, $hermesSpec)
}
$newVersion = Get-InstalledVersion -Name 'mnemosyne-memory'
if ($oldVersion -and $oldVersion -ne $newVersion) {
    Write-Ok "mnemosyne-memory upgraded: $oldVersion -> $newVersion (commit $($mnemosyneCommit.Substring(0, 12)))"
} else {
    Write-Ok "mnemosyne-memory $newVersion (commit $($mnemosyneCommit.Substring(0, 12)))"
}

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

# `hermes memory status` reports "available" from the registration alone, so it
# stays green even when the embedding stack cannot load in Hermes' interpreter.
# Import it the way the wrapper does, in isolated mode (-I) as Hermes launches
# it, and report what actually happens.
if (-not $NoEmbeddings) {
    $site = Join-Path $mnemosyneVenv 'Lib\site-packages'
    function Test-EmbeddingStack {
        $previousEA = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $out = & $hermesPython -I -c "import sys; sys.path.insert(0, r'$site'); import numpy, onnxruntime" 2>&1 |
                ForEach-Object { "$_" }
            return @{ Ok = ($LASTEXITCODE -eq 0); Output = ($out -join "`n") }
        } finally {
            $ErrorActionPreference = $previousEA
        }
    }
    $embed = Test-EmbeddingStack
    # onnxruntime links against the Visual C++ runtime, which a fresh Windows does
    # not ship. Installing it is the fix for both a missing and an outdated one,
    # and the package has to match the interpreter's architecture.
    $vcRedistId = 'Microsoft.VCRedist.2015+.x64'
    $vcRedistCurrent = $false
    if (-not $embed.Ok -and $embed.Output -match 'DLL load failed') {
        # sysconfig names the interpreter's own build (win-amd64 / win-arm64 / win32);
        # platform.machine() names the CPU, which differs under emulation.
        $previousEA = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $pyPlatform = ([string](& $hermesPython -I -c 'import sysconfig; print(sysconfig.get_platform())' 2>$null |
                Select-Object -First 1)).Trim()
        } finally {
            $ErrorActionPreference = $previousEA
        }
        $vcRedistId = switch ($pyPlatform) {
            'win-arm64' { 'Microsoft.VCRedist.2015+.arm64' }
            'win32' { 'Microsoft.VCRedist.2015+.x86' }
            default { 'Microsoft.VCRedist.2015+.x64' }
        }
        Write-Note "onnxruntime cannot load its DLLs; installing the Visual C++ runtime it links against ($vcRedistId). This may ask for elevation."
        try {
            Install-WingetPackage -Id $vcRedistId -DisplayName "the Microsoft Visual C++ runtime ($pyPlatform)"
            $vcRedistCurrent = ($script:LastNativeExit -eq -1978335189)
            $embed = Test-EmbeddingStack
        } catch {
            Write-Warn "Could not install the Visual C++ runtime: $($_.Exception.Message)"
        }
    }
    if ($embed.Ok) {
        Write-Ok "Verified: Hermes' interpreter can load the Mnemosyne embedding stack."
    } else {
        Write-Warn 'Hermes cannot import numpy/onnxruntime from the Mnemosyne environment.'
        Write-Warn '  Memories will be stored without embeddings; semantic recall will not match.'
        $reason = @($embed.Output -split "`n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
        if ($reason) { Write-Warn "  Python reported: $($reason.Trim())" }
        if ($vcRedistCurrent) {
            Write-Warn '  The Visual C++ runtime is already current, so the cause lies elsewhere;'
            Write-Warn '  see docs/troubleshooting.md.'
        } else {
            Write-Warn '  On Windows this is usually a missing or outdated Visual C++ runtime:'
            Write-Warn "    winget install --id $vcRedistId"
        }
    }
}

if (-not $SkipHermesConfiguration) {
    Write-Step 'Configuring Hermes...'
    Invoke-Native -FilePath $hermesExe -What 'hermes config set memory.provider mnemosyne' -ArgumentList @(
        'config', 'set', 'memory.provider', 'mnemosyne')

    # Upstream recommends turning the built-in MEMORY.md / USER.md store off once
    # Mnemosyne is the provider, so the two do not both consume context. Opt-in,
    # because it changes what the agent remembers rather than how it is stored.
    if ($DisableBuiltinMemory) {
        foreach ($key in @('memory.memory_enabled', 'memory.user_profile_enabled')) {
            Invoke-Native -FilePath $hermesExe -Quiet -What "hermes config set $key false" -ArgumentList @(
                'config', 'set', $key, 'false')
        }
        Write-Ok 'Built-in MEMORY.md / USER.md injection disabled; Mnemosyne is now the only store.'
    }

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
# Mnemosyne writes the file the first time it runs, which is *after* this point
# on a fresh install -- so this has to be callable again once the verification
# commands below have actually created it, or the fix silently does nothing.
$mnemosyneConfig = Join-Path $mnemosyneHome 'config.yaml'
function Repair-MnemosyneConfigEncoding {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $bytes = [IO.File]::ReadAllBytes($Path)
    $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
    try { [void]$strictUtf8.GetString($bytes); return } catch { }
    $text = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage).GetString($bytes)
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
    Write-Note "Re-encoded $Path as UTF-8"
}
Repair-MnemosyneConfigEncoding -Path $mnemosyneConfig

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

# Those two commands are what create config.yaml on a fresh install, in the ANSI
# codepage. Re-encode now that it exists, otherwise every later Mnemosyne
# command prints "'utf-8' codec can't decode byte 0x97".
Repair-MnemosyneConfigEncoding -Path $mnemosyneConfig

Write-Host ''
Write-Note 'Open a new terminal so HERMES_HOME / MNEMOSYNE_HOME are visible everywhere.'
