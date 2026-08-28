<#
.SYNOPSIS
    Creates the nine SDLC team profiles on an existing Hermes install and points
    each one at Mnemosyne.

.DESCRIPTION
    Windows/PowerShell port of install-sdlc-team-unix.sh.

    Everything here is idempotent: re-running upgrades the SOUL, the model and
    the plugin link in place rather than erroring out.

    One difference from the Unix script, forced by the platform: creating a
    symlink on Windows requires Developer Mode or elevation, so the per-profile
    link back to the Mnemosyne plugin is a directory *junction* instead. A
    junction needs no special privilege, and the plugin loader only ever reads
    through it. Where a junction cannot be created either, the script falls back
    to copying the plugin directory and says so — a copy works, but a later
    `mnemosyne-hermes install --force` upgrades only the original, so the copies
    go stale and need this script re-run.

.PARAMETER Model
    Model to set on each profile. Default: anthropic/claude-sonnet-5.

.PARAMETER Only
    Create only these profiles instead of all nine.

.PARAMETER SkipModel
    Leave each profile's model at whatever it inherited from the root profile.

.PARAMETER KeepSoul
    Do not overwrite an existing SOUL.md.

.PARAMETER DryRun
    Print what would happen and change nothing.

.EXAMPLE
    .\install-sdlc-team-windows.ps1

.EXAMPLE
    .\install-sdlc-team-windows.ps1 -Only architect,qa -SkipModel
#>

[CmdletBinding()]
param(
    [string]   $Model = 'anthropic/claude-sonnet-5',
    [string[]] $Only,
    [switch]   $SkipModel,
    [switch]   $KeepSoul,
    [switch]   $DryRun
)

$ErrorActionPreference = 'Stop'

if ($SkipModel -and $PSBoundParameters.ContainsKey('Model')) {
    Write-Error '-SkipModel and -Model contradict each other: one sets the model, the other leaves it alone. Pick one.'
    exit 2
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SoulsDir  = Join-Path $ScriptDir 'souls'

# Profile name -> the description the kanban decomposer routes tasks on.
# Ordered so the table below prints in a stable order.
$Profiles = [ordered]@{
    'architect'  = 'Architecture decisions: boundaries, contracts, data ownership, technology commitments.'
    'backend-db' = 'Backend and database engineering: services, schemas, queries, migrations, invariants.'
    'devops'     = 'DevOps and platform: pipelines, clusters, infrastructure, secrets, telemetry.'
    'frontend'   = 'Frontend engineering: accessible, resilient interfaces across devices and conditions.'
    'pm'         = 'Project management: decomposes goals into cards, sequences work, manages budget and risk.'
    'qa'         = 'QA: finds whether the thing actually works under real conditions, makes risk visible.'
    'researcher' = 'Market research specialist: gathers facts, synthesizes insights, produces structured briefs.'
    'uiux'       = 'UI/UX: defines what should exist and its shape before anything is built.'
    'writer'     = 'Versatile content writer: landing pages, emails, social posts, blog posts, scripts.'
}

function Test-Selected([string] $Name) {
    if (-not $Only) { return $true }
    return $Only -contains $Name
}

function Invoke-Hermes {
    <#
      Runs hermes and returns its stdout, with stderr folded in so a failure
      message is not lost. Callers that care about success check $LASTEXITCODE.

      Windows PowerShell writes a native command's stderr as ErrorRecords as
      soon as any stream is redirected, and $ErrorActionPreference='Stop' turns
      those into terminating errors. That would abort the script on the very
      thing this wrapper exists to observe: `hermes profile show` failing is how
      a not-yet-created profile is detected. Drop to 'Continue' for the call and
      restore it afterwards.
    #>
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $HermesArgs)
    $previousEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out  = (& hermes @HermesArgs 2>&1 | Out-String).Trim()
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEA
    }
    # Restore the exit code the caller is about to test: assigning $out above
    # does not change it, but the finally block's assignment would.
    $global:LASTEXITCODE = $code
    return $out
}

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    Write-Error @'
hermes not found on PATH.
Install Hermes and Mnemosyne first: ..\install-mnemosyne-hermes-windows.ps1
'@
    exit 1
}

# Resolve HERMES_HOME from Hermes itself rather than assuming
# %LOCALAPPDATA%\hermes, so a custom HERMES_HOME keeps working.
$RootConfig = Invoke-Hermes 'config' 'path'
if ($LASTEXITCODE -ne 0) { Write-Error "hermes config path failed: $RootConfig"; exit 1 }
$HermesRoot = Split-Path -Parent $RootConfig
$PluginSrc  = Join-Path $HermesRoot 'plugins\mnemosyne'

if (-not (Test-Path -LiteralPath $PluginSrc)) {
    Write-Error @"
Mnemosyne plugin not found at $PluginSrc.
Run ..\install-mnemosyne-hermes-windows.ps1 first.
"@
    exit 1
}

# The provider must be active on the root profile before profiles are cloned
# from it: `hermes profile create --clone` copies config.yaml as a file, so
# whatever memory settings are in place at clone time are what each profile
# gets.
$Provider = Invoke-Hermes 'config' 'get' 'memory.provider'
if ($Provider -ne 'mnemosyne') {
    Write-Error @"
memory.provider on the root profile is '$Provider', not 'mnemosyne'.
Run ..\install-mnemosyne-hermes-windows.ps1 -DisableBuiltinMemory first,
so the profiles clone a config that already points at Mnemosyne.
"@
    exit 1
}

foreach ($key in @('memory.memory_enabled', 'memory.user_profile_enabled')) {
    $val = Invoke-Hermes 'config' 'get' $key
    if ($val -and $val.ToLowerInvariant() -ne 'false') {
        Write-Warning @"
$key is '$val' on the root profile. Cloned profiles will inherit it and run
Hermes' built-in MEMORY.md / USER.md store alongside Mnemosyne. Re-run the main
installer with -DisableBuiltinMemory to turn it off.
"@
    }
}

$modelLabel = if ($SkipModel) { '(unchanged)' } else { $Model }
Write-Host "Hermes home:   $HermesRoot"
Write-Host "Mnemosyne:     $PluginSrc"
Write-Host "Model:         $modelLabel"
Write-Host ''

$created = 0
$updated = 0
$copied  = @()

foreach ($name in $Profiles.Keys) {
    if (-not (Test-Selected $name)) { continue }

    $desc = $Profiles[$name]
    $soul = Join-Path $SoulsDir "SOUL-$name.md"
    if (-not (Test-Path -LiteralPath $soul)) { Write-Error "Missing $soul"; exit 1 }

    Invoke-Hermes 'profile' 'show' $name | Out-Null
    $exists = ($LASTEXITCODE -eq 0)

    if ($exists) {
        Write-Host "-> $name (exists, updating)"
        $updated++
    } else {
        Write-Host "-> $name (creating)"
        $created++
        if (-not $DryRun) {
            $out = Invoke-Hermes 'profile' 'create' $name '--clone' '--description' $desc
            if ($LASTEXITCODE -ne 0) { Write-Error "Creating '$name' failed: $out"; exit 1 }
        } else {
            Write-Host "  would run: hermes profile create $name --clone --description ..."
        }
    }

    # Ask Hermes where the profile actually lives instead of assuming
    # $HermesRoot\profiles\$name.
    if ($DryRun -and -not $exists) {
        $profileDir = Join-Path $HermesRoot "profiles\$name"
    } else {
        $profileDir = Split-Path -Parent (Invoke-Hermes '-p' $name 'config' 'path')
    }

    if ($KeepSoul -and (Test-Path -LiteralPath (Join-Path $profileDir 'SOUL.md'))) {
        Write-Host '  SOUL.md kept (-KeepSoul)'
    } elseif ($DryRun) {
        Write-Host "  would copy: SOUL-$name.md -> $profileDir\SOUL.md"
    } else {
        Copy-Item -LiteralPath $soul -Destination (Join-Path $profileDir 'SOUL.md') -Force
    }

    if (-not $SkipModel) {
        if ($DryRun) {
            Write-Host "  would run: hermes -p $name config set model.default $Model"
        } else {
            $out = Invoke-Hermes '-p' $name 'config' 'set' 'model.default' $Model
            if ($LASTEXITCODE -ne 0) { Write-Error "Setting model on '$name' failed: $out"; exit 1 }
        }
    }

    # The gap this script exists to close. A named profile redirects
    # HERMES_HOME to its own directory, and memory providers are discovered
    # under $HERMES_HOME\plugins\. So the Mnemosyne plugin installed at the root
    # is invisible to every named profile: `memory status` reports
    # "Provider: mnemosyne" (config was cloned) next to "Plugin: NOT installed",
    # and the agent silently runs with no memory at all.
    $pluginDir  = Join-Path $profileDir 'plugins'
    $pluginLink = Join-Path $pluginDir 'mnemosyne'

    if ($DryRun) {
        Write-Host "  would link: $pluginLink -> $PluginSrc"
        continue
    }

    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
    if (Test-Path -LiteralPath $pluginLink) {
        Remove-Item -LiteralPath $pluginLink -Recurse -Force
    }

    try {
        New-Item -ItemType Junction -Path $pluginLink -Target $PluginSrc -ErrorAction Stop | Out-Null
    } catch {
        # Junctions are unavailable on some filesystems (and across volumes).
        # A copy still loads, but goes stale on the next Mnemosyne upgrade.
        Copy-Item -LiteralPath $PluginSrc -Destination $pluginLink -Recurse -Force
        $copied += $name
    }
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'Dry run: nothing was changed.'
    exit 0
}

Write-Host ''
Write-Host 'Verifying'
Write-Host ('-' * 40)
$failed = 0
foreach ($name in $Profiles.Keys) {
    if (-not (Test-Selected $name)) { continue }
    $status = Invoke-Hermes '-p' $name 'memory' 'status'
    if ($status -match 'Status:\s*available') {
        Write-Host "  $name : mnemosyne available"
    } else {
        Write-Host "  $name : mnemosyne NOT available"
        $failed++
    }
}

Write-Host ''
Write-Host "Created $created, updated $updated."

if ($copied.Count -gt 0) {
    Write-Warning @"
Could not create a junction for: $($copied -join ', ')
The plugin was copied instead. A copy works, but a later
`mnemosyne-hermes install --force` upgrades only the original, so re-run this
script after upgrading Mnemosyne.
"@
}

if ($failed -gt 0) {
    Write-Error "$failed profile(s) cannot load Mnemosyne - see 'hermes -p <name> memory status'."
    exit 1
}
Write-Host 'All profiles are wired to Mnemosyne.'
