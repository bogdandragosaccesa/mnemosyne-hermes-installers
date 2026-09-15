<#
.SYNOPSIS
    Creates the eleven outsourcing delivery team profiles on an existing Hermes
    install and points each one at Mnemosyne.

.DESCRIPTION
    Windows/PowerShell port of install-outsourcing-team-unix.sh.

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
    Create only these profiles instead of all eleven. Names are the bare role
    names, unaffected by -Prefix.

.PARAMETER Prefix
    Prepend a prefix to every profile name, e.g. -Prefix os- creates os-qa-lead.
    Use this to run the team alongside another profile set without collisions.

.PARAMETER SkipModel
    Leave each profile's model at whatever it inherited from the root profile.

.PARAMETER KeepSoul
    Do not overwrite an existing SOUL.md.

.PARAMETER SeparateMemory
    Give each profile its own Mnemosyne database instead of sharing the root
    one. Roles then cannot read each other's notes, so the board no longer
    hands work between them.

.PARAMETER DryRun
    Print what would happen and change nothing.

.EXAMPLE
    .\install-outsourcing-team-windows.ps1

.EXAMPLE
    .\install-outsourcing-team-windows.ps1 -Only qa-lead,app-engineer -SkipModel
#>

[CmdletBinding()]
param(
    [string]   $Model = 'anthropic/claude-sonnet-5',
    [string[]] $Only,
    [string]   $Prefix = '',
    [switch]   $SkipModel,
    [switch]   $KeepSoul,
    [switch]   $SeparateMemory,
    [switch]   $DryRun
)

$ErrorActionPreference = 'Stop'

if ($SkipModel -and $PSBoundParameters.ContainsKey('Model')) {
    Write-Error '-SkipModel and -Model contradict each other: one sets the model, the other leaves it alone. Pick one.'
    exit 2
}

if ($Prefix -and $Prefix -notmatch '^[a-zA-Z0-9_-]+$') {
    Write-Error '-Prefix must contain only letters, digits, hyphen and underscore.'
    exit 2
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SoulsDir  = Join-Path $ScriptDir 'souls'

# Role name -> the description the kanban decomposer routes tasks on.
# Ordered so the output prints in a stable order.
$Profiles = [ordered]@{
    'engagement-lead'      = 'Engagement ownership: client relationship, commercial framing, escalation, scope boundary, exit.'
    'delivery-manager'     = 'Delivery management: decomposes outcomes into cards, sequences work, manages burn, margin and risk.'
    'business-analyst'     = 'Business analysis: elicits and traces requirements, writes testable acceptance criteria, as-is to to-be process.'
    'domain-consultant'    = 'Industry domain consulting: vertical knowledge and vocabulary for manufacturing, finance, e-commerce, healthcare, public sector.'
    'solution-architect'   = "Solution architecture: boundaries, contracts and technology commitments inside a client's existing estate."
    'app-engineer'         = "Application engineering: builds features end to end in the client's codebase, following the client's conventions."
    'integration-engineer' = 'Integration and data migration: connects unchangeable systems, profiles and moves data with reconciliation and rollback.'
    'qa-lead'              = 'QA leadership: acceptance evidence, traceability, client UAT, regression of existing behaviour, contractual exit criteria.'
    'platform-sre'         = "Platform and SRE: pipelines, environments, infrastructure and telemetry in the client's cloud, handed over operable."
    'security-compliance'  = 'Security and compliance: threat models, security review, control and evidence gaps, third-party vendor exposure.'
    'presales-writer'      = 'Presales and client communication: proposals, SOW scope language, assumptions and exclusions, status reports, handover docs.'
}

# Fail on a typo in -Only rather than silently creating nothing.
if ($Only) {
    $unknown = $Only | Where-Object { $_ -and ($Profiles.Keys -notcontains $_) }
    if ($unknown) {
        Write-Error "Unknown profile(s) in -Only: $($unknown -join ', ')`nValid: $($Profiles.Keys -join ' ')"
        exit 2
    }
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
# The one real Mnemosyne data root, which every profile is pointed at unless
# -SeparateMemory was passed. Honour MNEMOSYNE_HOME if the store was relocated.
$MnemoHome  = if ($env:MNEMOSYNE_HOME) { $env:MNEMOSYNE_HOME } else { Join-Path $HermesRoot 'mnemosyne' }

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
if ($Prefix) { Write-Host "Name prefix:   $Prefix" }
$memLabel = if ($SeparateMemory) { 'separate per profile' } else { "shared bank at $MnemoHome" }
Write-Host "Memory:        $memLabel"
Write-Host ''

$created  = 0
$updated  = 0
$copied   = @()
$unshared = @()

foreach ($role in $Profiles.Keys) {
    if (-not (Test-Selected $role)) { continue }

    $name = "$Prefix$role"
    $desc = $Profiles[$role]
    $soul = Join-Path $SoulsDir "SOUL-$role.md"
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
        Write-Host "  would copy: SOUL-$role.md -> $profileDir\SOUL.md"
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
        if (-not $SeparateMemory) {
            Write-Host "  would link: $(Join-Path $profileDir 'mnemosyne') -> $MnemoHome"
        }
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

    # The plugin link above shares Mnemosyne's *code*. It does not share its
    # *data*: the database path resolves from MNEMOSYNE_HOME, which defaults to
    # $HERMES_HOME\mnemosyne — and a named profile has redirected HERMES_HOME.
    # So every profile silently writes its own database and no role can read
    # what another recorded, which defeats a team that hands work between roles.
    # Like the plugin gap, it fails quietly: `memory status` says "available"
    # either way. Setting MNEMOSYNE_HOME in the profile's .env is not honoured
    # for this, so a junction to the one real store is the mechanism.
    if (-not $SeparateMemory) {
        $dataLink = Join-Path $profileDir 'mnemosyne'
        $existing = Get-Item -LiteralPath $dataLink -ErrorAction SilentlyContinue
        $isLink   = $existing -and $existing.LinkType
        if ($existing -and -not $isLink) {
            # A real directory means this profile already has its own memories.
            # Merging them is not ours to decide, so leave it and say so rather
            # than deleting somebody's history.
            Write-Host "  NOTE: $dataLink is a real directory with its own database."
            Write-Host '        Left as-is - this profile will not share the team bank.'
            $unshared += $name
        } else {
            if ($isLink) { Remove-Item -LiteralPath $dataLink -Force -Recurse }
            try {
                New-Item -ItemType Junction -Path $dataLink -Target $MnemoHome -ErrorAction Stop | Out-Null
            } catch {
                Write-Warning "Could not link $dataLink to $MnemoHome - $name will use its own memory database."
                $unshared += $name
            }
        }
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
foreach ($role in $Profiles.Keys) {
    if (-not (Test-Selected $role)) { continue }
    $name   = "$Prefix$role"
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

if ($unshared.Count -gt 0) {
    Write-Warning @"
These profiles kept their own Mnemosyne database and will NOT see the team's
shared memory: $($unshared -join ', ')
Nothing was deleted; remove or merge each profile's mnemosyne directory and
re-run to share the team bank.
"@
}

if ($failed -gt 0) {
    Write-Error "$failed profile(s) cannot load Mnemosyne - see 'hermes -p <name> memory status'."
    exit 1
}
Write-Host 'All profiles are wired to Mnemosyne.'
