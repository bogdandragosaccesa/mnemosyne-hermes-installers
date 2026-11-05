<#
.SYNOPSIS
    Creates the eight dev-team profiles on an existing Hermes install and
    points each one at Mnemosyne and the dev-team-gate plugin.

.DESCRIPTION
    Windows/PowerShell port of install-dev-team-unix.sh.

    Everything here is idempotent: re-running upgrades the SOUL, the model,
    the plugin link and the skills in place rather than erroring out.

    Symlinks need Developer Mode or elevation on Windows, so every directory
    link here (the Mnemosyne plugin, the Mnemosyne data store, dev-team-gate,
    the shared dev-team home) is a directory *junction* instead, which needs
    no special privilege. Where a junction cannot be created either, the
    script falls back to copying the source directory and prints a warning -
    a copy works, but it will not track later upgrades to the source, so the
    affected profile needs this script re-run after any such upgrade.

.PARAMETER Model
    Set this one model on every profile, overriding the per-role tier table
    (frontier for pm, architect and reviewer; mid for the rest). Must be a
    real model id, never a tier word like "mid".

.PARAMETER Only
    Create only these profiles instead of all eight. Names are the bare role
    names, unaffected by -Prefix.

.PARAMETER Prefix
    Prepend a prefix to every profile name (default: dev-), e.g. -Prefix t2-
    creates t2-qa. Use to coexist with another team.

.PARAMETER SkipModel
    Leave each profile's model at whatever it inherited from the root
    profile.

.PARAMETER KeepSoul
    Do not overwrite an existing SOUL.md.

.PARAMETER SeparateMemory
    Give each profile its own Mnemosyne database instead of sharing the root
    one. Roles then cannot read each other's notes, so the board no longer
    hands work between them.

.PARAMETER DryRun
    Print what would happen and change nothing.

.PARAMETER TierFast
    deciders tier table entry for the fast tier, as PROVIDER:MODEL (e.g.
    anthropic:claude-haiku-4-5). Default: $FastModel split on its first '/'.
    Every -Tier* model must be a chat model an agent can run on; a
    decisions-only model is refused.

.PARAMETER TierStandard
    deciders tier table entry for the standard tier. Default: $MidModel
    split on its first '/'.

.PARAMETER TierDeep
    deciders tier table entry for the deep tier. Default: $FrontierModel
    split on its first '/'.

.PARAMETER ForgeTokenSource
    Refused outright: Hermes' command secret source is POSIX-only (it needs
    /bin/sh), so this flag would record devops profile config that Hermes
    running on Windows can never read. Rather than silently writing a key
    nothing will ever use -- a failure that would otherwise only surface
    much later as a pr-ci card blocking with "no forge token", disconnected
    from its install-time cause -- this installer exits 2 and writes
    nothing. Use the Unix installer's --forge-token-source on a POSIX
    Hermes host instead, or configure secrets.command.command /
    secrets.command.enabled on the devops profile by hand from there.

.EXAMPLE
    .\install-dev-team-windows.ps1

.EXAMPLE
    .\install-dev-team-windows.ps1 -Only qa,reviewer -SkipModel
#>

[CmdletBinding()]
param(
    [string]   $Model = 'anthropic/claude-sonnet-5',
    [string[]] $Only,
    [string]   $Prefix = 'dev-',
    [switch]   $SkipModel,
    [switch]   $KeepSoul,
    [switch]   $SeparateMemory,
    [string]   $TeamMemory,
    [switch]   $AdoptTeamMemory,
    [switch]   $DryRun,
    [string]   $TierFast,
    [string]   $TierStandard,
    [string]   $TierDeep,
    [string]   $ForgeTokenSource
)

$ErrorActionPreference = 'Stop'

# Tiers used when -Model is not given. pm, architect and reviewer hold
# refusal/one-way-door authority or author the plan every later stage is
# checked against, so they get the frontier tier. The rest work against a
# plan or a finding someone else already wrote, which is where the mid tier
# earns its keep. Fable models are deliberately absent and must not be
# added: their cost is out of all proportion to the work these profiles do.
$FrontierModel = 'anthropic/claude-opus-5'
$MidModel      = 'anthropic/claude-sonnet-5'
# Default for the deciders tier table's fast tier only (no profile uses it).
# Must be a chat model an agent worker can run on: never a decisions-only
# model such as deciders' own tier/decide models (see DECISION_MODELS).
$FastModel     = 'anthropic/claude-haiku-4-5'
$ModelExplicit = $PSBoundParameters.ContainsKey('Model')
# The profile a human talks to. It gets the pipeline-orchestration skill and
# the budget script; every other profile is a worker the dispatcher spawns.
$EntryProfile = 'pm'

if ($SkipModel -and $ModelExplicit) {
    [Console]::Error.WriteLine('-SkipModel and -Model contradict each other: one sets the model, the other leaves it alone. Pick one.')
    exit 2
}

if (-not $Prefix -or $Prefix -notmatch '^[a-zA-Z0-9_-]+$') {
    [Console]::Error.WriteLine('-Prefix must be non-empty and contain only letters, digits, hyphen and underscore.')
    exit 2
}

function Split-TierPM {
    <#
      Splits "$Value" on its FIRST ':' into a two-element array
      (provider, model). Exits 2, naming $FlagName, when there is no ':'
      or when either half is empty - same contract as the Unix
      installer's --tier-* flags ("must be PROVIDER:MODEL with both
      non-empty").
    #>
    param([string] $Value, [string] $FlagName)
    if ($Value -notmatch ':') {
        [Console]::Error.WriteLine("$FlagName must be PROVIDER:MODEL (got '$Value')")
        exit 2
    }
    $idx = $Value.IndexOf(':')
    $provider = $Value.Substring(0, $idx)
    $model = $Value.Substring($idx + 1)
    if (-not $provider -or -not $model) {
        [Console]::Error.WriteLine("$FlagName must be PROVIDER:MODEL with both non-empty (got '$Value')")
        exit 2
    }
    return @($provider, $model)
}

function Split-ProviderModel {
    <# Splits "$Value" (e.g. $MidModel) on its first '/' into (provider, model). #>
    param([string] $Value)
    $idx = $Value.IndexOf('/')
    if ($idx -lt 0) { return @($Value, '') }
    return @($Value.Substring(0, $idx), $Value.Substring($idx + 1))
}

# deciders tier table. -TierFast/-TierStandard/-TierDeep default from
# $FastModel/$MidModel/$FrontierModel, split on their first '/'.
if ($PSBoundParameters.ContainsKey('TierFast')) {
    $parts = Split-TierPM $TierFast '-TierFast'
    $TierFastProvider = $parts[0]; $TierFastModel = $parts[1]
} else {
    $parts = Split-ProviderModel $FastModel
    $TierFastProvider = $parts[0]; $TierFastModel = $parts[1]
}

if ($PSBoundParameters.ContainsKey('TierStandard')) {
    $parts = Split-TierPM $TierStandard '-TierStandard'
    $TierStandardProvider = $parts[0]; $TierStandardModel = $parts[1]
} else {
    $parts = Split-ProviderModel $MidModel
    $TierStandardProvider = $parts[0]; $TierStandardModel = $parts[1]
}

if ($PSBoundParameters.ContainsKey('TierDeep')) {
    $parts = Split-TierPM $TierDeep '-TierDeep'
    $TierDeepProvider = $parts[0]; $TierDeepModel = $parts[1]
} else {
    $parts = Split-ProviderModel $FrontierModel
    $TierDeepProvider = $parts[0]; $TierDeepModel = $parts[1]
}

$ScriptDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$SoulsDir       = Join-Path $ScriptDir 'souls'
$SkillsDir      = Join-Path $ScriptDir 'skills'
$PluginSrcGate  = Join-Path $ScriptDir 'plugin\dev-team-gate'
$ForgePy        = Join-Path $ScriptDir 'forge\forge.py'
$KanbanBudgetPy = Join-Path $ScriptDir 'scripts\kanban-budget.py'
$DecidersPy       = Join-Path $ScriptDir 'deciders\deciders.py'
$DecidersSkillDir = Join-Path $SkillsDir 'dev-team-deciders'
# The .env pin checker (dev-team\scripts\env_pin_check.py): it decides
# every MNEMOSYNE_HOME / MNEMOSYNE_DATA_DIR pin this installer writes
# (Get-EnvPinCheckerReason / Assert-EnvPinSafe, below).
$EnvPinCheckPy    = Join-Path (Join-Path $ScriptDir 'scripts') 'env_pin_check.py'
# The shared terminal.env_passthrough list merge, used by -ForgeTokenSource
# (Set-ForgeTokenSource, below). One implementation for both installers.
$EnvPassthroughMergePy = Join-Path (Join-Path $ScriptDir 'scripts') 'env_passthrough_merge.py'
# -ForgeTokenSource: the variable the recorded command is contracted to print,
# and the role whose profile holds it. The PROFILE name is always built as
# "$Prefix$ForgeTokenRole", never hardcoded, so a -Prefix t2- install
# configures t2-devops.
$ForgeTokenEnvVar = 'DEV_TEAM_GH_TOKEN'
$ForgeTokenRole   = 'devops'

# -ForgeTokenSource is refused unconditionally on this (Windows) installer:
# Hermes' `command` secret source only ever resolves on POSIX (it shells out
# to /bin/sh), so no shape of this flag would ever do useful work here. Exit
# 2 before anything is written -- see .PARAMETER ForgeTokenSource above for
# why refusal, not a silent no-op, is the right failure mode. Set-ForgeTokenSource
# below is therefore unreachable; its body is kept correct (two leaf keys,
# same contract as the Unix installer) in case this refusal is ever lifted.
if ($ForgeTokenSource) {
    [Console]::Error.WriteLine("-ForgeTokenSource is refused on Windows: Hermes' command secret source is POSIX-only, so this flag would record devops profile config Hermes can never read here. Nothing was written. Use the Unix installer's --forge-token-source from a POSIX Hermes host instead.")
    exit 2
}

# Refuse a decisions-only model in the tier table before anything is
# written: an agent worker on a card routed to it could not run at all. The
# list is deciders_core.py's one-line DECISION_MODELS tuple, so it has one
# owner; a dated variant ("<id>-20261027") is refused too. Two Join-Paths
# rather than one backslash path, so the check also runs under pwsh on Linux.
$DecidersCorePy = Join-Path (Join-Path $ScriptDir 'deciders') 'deciders_core.py'
$DecisionModels = @()
if (Test-Path -LiteralPath $DecidersCorePy) {
    $line = Get-Content -LiteralPath $DecidersCorePy | Where-Object { $_ -like 'DECISION_MODELS = (*' } | Select-Object -First 1
    if ($line) { $DecisionModels = @([regex]::Matches($line, '"([^"]*)"') | ForEach-Object { $_.Groups[1].Value }) }
}
if ($DecisionModels.Count -eq 0) {
    [Console]::Error.WriteLine("cannot read DECISION_MODELS from $DecidersCorePy")
    exit 2
}
function Assert-NotDecisionModel {
    param([string] $FlagName, [string] $Provider, [string] $ModelId)
    # Strip an OpenRouter ":variant" suffix, e.g. "<provider>/<model>:nitro"
    # (OpenRouter's variant syntax), before comparing, same as
    # deciders_core.py's _is_decision_model.
    $bareModel = $ModelId.Split(':')[0]
    foreach ($m in $DecisionModels) {
        foreach ($candidate in @($bareModel, "$Provider/$bareModel")) {
            $isMatch = $false
            if ($m.EndsWith('*')) {
                # A family prefix entry, e.g. "perplexity/pplx-decider-*".
                $prefix = $m.Substring(0, $m.Length - 1)
                $isMatch = $candidate.StartsWith($prefix)
            } else {
                $isMatch = ($candidate -eq $m -or $candidate.StartsWith("$m-"))
            }
            if ($isMatch) {
                [Console]::Error.WriteLine("$FlagName $Provider`:$ModelId is a decisions-only model (it only answers /api/alpha/decisions); an agent worker cannot run on it. Pick a chat model, e.g. anthropic:claude-haiku-4-5.")
                exit 2
            }
        }
    }
}
Assert-NotDecisionModel '-TierFast' $TierFastProvider $TierFastModel
Assert-NotDecisionModel '-TierStandard' $TierStandardProvider $TierStandardModel
Assert-NotDecisionModel '-TierDeep' $TierDeepProvider $TierDeepModel
# I3: -Model sets every profile's own model; it must be refused the same
# way a decisions-only tier-table entry is, or an agent worker ends up
# unable to run at all.
$ModelParts = Split-ProviderModel $Model
Assert-NotDecisionModel '-Model' $ModelParts[0] $ModelParts[1]

# Profile name -> the description the kanban decomposer routes tasks on.
# Ordered so the output prints in a stable order. Kept in lockstep with the
# NAMES/DESCS arrays in install-dev-team-unix.sh.
$Profiles = [ordered]@{
    'pm'         = 'dev-team pipeline entry point: intake, decomposition, budget discipline, the attention ledger, stopping at a ready PR.'
    'architect'  = 'Architecture and planning: boundaries, one-way doors, and the plan card every implement/validate/review stage is checked against.'
    'uiux'       = 'UI/UX: interaction and visual design for implement cards that touch a user-facing surface.'
    'backend-db' = 'Backend and data: services, schemas, migrations and data-layer implement cards.'
    'frontend'   = 'Frontend: client-side implement cards against the architecture''s contracts.'
    'qa'         = 'QA: validate stage - exercises the change, writes testable evidence the review stage checks.'
    'reviewer'   = 'Review: judges a diff against the plan card, approves or sends back with findings. Holds refusal authority.'
    'devops'     = 'Devops: repository onboarding, the forge project record, pr-ci stage - pipelines and CI, never merge or deploy.'
}

if (-not (Test-Path -LiteralPath $PluginSrcGate)) { Write-Error "Missing $PluginSrcGate"; exit 1 }
if (-not (Test-Path -LiteralPath $ForgePy))       { Write-Error "Missing $ForgePy"; exit 1 }
if (-not (Test-Path -LiteralPath $DecidersPy))       { Write-Error "Missing $DecidersPy"; exit 1 }
if (-not (Test-Path -LiteralPath $DecidersSkillDir)) { Write-Error "Missing $DecidersSkillDir"; exit 1 }
foreach ($role in $Profiles.Keys) {
    $soul = Join-Path $SoulsDir "SOUL-$role.md"
    if (-not (Test-Path -LiteralPath $soul)) { Write-Error "Missing $soul"; exit 1 }
}

# Fail on a typo in -Only rather than silently creating nothing.
if ($Only) {
    $unknown = $Only | Where-Object { $_ -and ($Profiles.Keys -notcontains $_) }
    if ($unknown) {
        [Console]::Error.WriteLine("Unknown profile(s) in -Only: $($unknown -join ', ')`nValid: $($Profiles.Keys -join ' ')")
        exit 2
    }
}

function Test-Selected([string] $Name) {
    if (-not $Only) { return $true }
    return $Only -contains $Name
}

function Set-ForgeTokenSource {
    <#
      -ForgeTokenSource: record the operator's token command on the devops
      profile and pass $ForgeTokenEnvVar through to that profile's terminal.

      Unreachable on this (Windows) installer -- the unconditional refusal
      above exits before this function is ever called. Kept correct so a
      lifted refusal does not resurrect the wrong-shape bug defect A fixed
      on the Unix side.

      Secrecy contract: only the command STRING is stored. This installer
      itself never executes COMMAND, so no token value is fetched, printed
      or written here - not to a config, not to the console, not under
      -DryRun.

      Exactly three keys are touched. `hermes config set` merges into the
      existing config, so nothing else in it is clobbered. CommandSource
      requires a MAPPING with two leaf keys - secrets.command.command and
      secrets.command.enabled; `enabled` defaults to false, so writing only
      the parent key (or only .command) would leave the source disabled.
      Writing the two leaf keys is idempotent even over an already-correct
      mapping. `terminal.env_passthrough` is a list, so it is read first and
      MERGED by dev-team\scripts\env_passthrough_merge.py, which appends the
      variable only when absent - keeping a re-run a no-op and never
      dropping a name the profile already passes through.
    #>
    param([string] $Name)

    if ($DryRun) {
        Write-Host "  would run:  hermes -p $Name config set secrets.command.command $ForgeTokenSource"
        Write-Host "  would run:  hermes -p $Name config set secrets.command.enabled true"
        Write-Host "  would add:  $ForgeTokenEnvVar to $Name's terminal.env_passthrough"
        return
    }
    if (-not (Test-Path -LiteralPath $EnvPassthroughMergePy)) {
        Write-Error "Missing $EnvPassthroughMergePy, so $Name's terminal.env_passthrough cannot be merged."
        exit 1
    }
    if (-not $WrapperPython) {
        Write-Error "No Python was resolved, so $Name's terminal.env_passthrough cannot be merged."
        exit 1
    }

    $out = Invoke-Hermes '-p' $Name 'config' 'set' 'secrets.command.command' $ForgeTokenSource
    if ($LASTEXITCODE -ne 0) { Write-Error "Setting secrets.command.command on '$Name' failed: $out"; exit 1 }
    $out = Invoke-Hermes '-p' $Name 'config' 'set' 'secrets.command.enabled' 'true'
    if ($LASTEXITCODE -ne 0) { Write-Error "Setting secrets.command.enabled on '$Name' failed: $out"; exit 1 }

    # An unset key exits non-zero; the merge treats empty input as an empty
    # list. Invoke-Hermes folds stderr in, so take the last non-empty line.
    $raw = Invoke-Hermes '-p' $Name 'config' 'get' 'terminal.env_passthrough' '--json'
    $current = if ($LASTEXITCODE -eq 0) { Get-HermesLastLine $raw } else { '' }

    $previousEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $merged = ($current | & $WrapperPython '-I' $EnvPassthroughMergePy $ForgeTokenEnvVar 2>&1 | Out-String).Trim()
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousEA
    }
    if ($code -ne 0 -or -not $merged) {
        Write-Error "Cannot merge $Name's terminal.env_passthrough: $merged"
        exit 1
    }

    $out = Invoke-Hermes '-p' $Name 'config' 'set' 'terminal.env_passthrough' $merged
    if ($LASTEXITCODE -ne 0) { Write-Error "Setting terminal.env_passthrough on '$Name' failed: $out"; exit 1 }
    Write-Host "  forge token: secrets.command.command and secrets.command.enabled set, $ForgeTokenEnvVar passed through"
}

# -ForgeTokenSource only has a target when the devops profile is in this run.
# Say so rather than silently doing nothing.
if ($ForgeTokenSource -and -not (Test-Selected $ForgeTokenRole)) {
    [Console]::Error.WriteLine("-ForgeTokenSource was given but -Only does not include '$ForgeTokenRole', so there is no profile to configure; nothing was written for it.")
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

function Get-HermesLastLine {
    <#
      Extracts the meaningful value from Invoke-Hermes output. On a
      brand-new profile's first invocation, Hermes prints one-time
      dependency-setup lines ("completing source-update dependencies...",
      "Resolving Python dependencies"...) to the same stream before the
      actual value, so the raw output is not reliably a single line. The
      real value (a path, a config value, ...) is always the last
      non-empty line, so take that instead of the whole blob.
    #>
    param([string] $RawOutput)
    $lines = $RawOutput -split "`r?`n" | Where-Object { $_.Trim() }
    return ($lines | Select-Object -Last 1).Trim()
}

function New-DirLink {
    <#
      Creates $LinkPath as a junction pointing at $Target, overwriting
      whatever is already there. Falls back to a recursive copy with a
      printed warning when a junction cannot be created (e.g. across
      volumes, or on a filesystem without junction support). Returns
      'junction' or 'copy'.
    #>
    param([string] $LinkPath, [string] $Target, [string] $Label)

    $existing = Get-Item -LiteralPath $LinkPath -Force -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.LinkType) {
            # A junction or symlink: delete the link itself, never what it
            # points at. Windows PowerShell 5.1's `Remove-Item -Recurse` on a
            # junction walks INTO the target and deletes its contents - the
            # shared Mnemosyne bank, or the plugin source. Directory.Delete
            # without the recursive flag removes only the reparse point.
            [System.IO.Directory]::Delete($LinkPath)
        } elseif ($existing.PSIsContainer) {
            # A real directory (an earlier copy fallback, or what `profile
            # create --clone` just made). Callers only get here for a
            # directory this run owns.
            Remove-Item -LiteralPath $LinkPath -Force -Recurse
        } else {
            Remove-Item -LiteralPath $LinkPath -Force
        }
    }

    try {
        New-Item -ItemType Junction -Path $LinkPath -Target $Target -ErrorAction Stop | Out-Null
        return 'junction'
    } catch {
        Write-Warning "Could not create a junction for $Label at $LinkPath -> $Target. Copying instead; a copy will not track later changes to the source, so re-run this script after the source changes."
        Copy-Item -LiteralPath $Target -Destination $LinkPath -Recurse -Force
        return 'copy'
    }
}

function Test-DirHasData {
    <#
      True when $Path exists and contains at least one entry. Used to tell
      a freshly created, empty team-home directory apart from one that
      still holds live data (T4 migration).
    #>
    param([string] $Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { return $false }
    return [bool](Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Get-NormalizedPath {
    <#
      Normalizes a filesystem path for comparison: resolves it to a full
      path (so relative segments and case-insensitive drive letters don't
      cause a false mismatch), strips a trailing backslash, and strips the
      '\??\' NT-namespace prefix some junction targets carry (the Win32
      path APIs put it there; the string a junction's .Target reports can
      come back with or without it depending on how it was created).
      Gives parity with the Unix installer's `readlink -f` comparison
      (review finding #3): without this, a junction that already points at
      the correct root could be read as a different, old location, making
      every re-run exit 2.
    #>
    param([string] $Path)
    if (-not $Path) { return $Path }
    $ntPrefix = [string]::Concat([char]92, '?', '?', [char]92)
    $p = $Path
    if ($p.StartsWith($ntPrefix)) { $p = $p.Substring($ntPrefix.Length) }
    try { $p = [System.IO.Path]::GetFullPath($p) } catch { }
    return $p.TrimEnd([char]92)
}

function Test-IsAncestorOrEqualPath {
    <# True iff $Desc (normalized) equals $Anc (normalized) or is nested
       inside it. Mirrors the Unix installer's _is_ancestor_or_equal. #>
    param([string] $Anc, [string] $Desc)
    if (-not $Anc -or -not $Desc) { return $false }
    $a = (Get-NormalizedPath $Anc)
    $d = (Get-NormalizedPath $Desc)
    if (-not $a -or -not $d) { return $false }
    if ($d -ieq $a) { return $true }
    return $d.StartsWith("$a$([char]92)", [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-AllProfileDirs {
    <#
      N3 (review): the refusal in Get-UnsafeOldHomeReason must cover every
      directory under $HermesRoot\profiles, not only the dev-team-prefixed
      ones this installer's own role scan discovers -- otherwise a
      candidate equal to some other, unrelated profile directory slips
      through and "migrating" it would move that whole profile. Mirrors
      the Unix installer's "$HERMES_ROOT"/profiles/*/ glob.
    #>
    param([string] $HermesRoot)
    $profilesRoot = Join-Path $HermesRoot 'profiles'
    if (-not (Test-Path -LiteralPath $profilesRoot)) { return @() }
    return @(Get-ChildItem -LiteralPath $profilesRoot -Directory -Force -ErrorAction SilentlyContinue |
        ForEach-Object { $_.FullName })
}

function Get-UnsafeOldHomeReason {
    <#
      D1 (review): a candidate old-home target must actually be named
      "dev-team", and must not equal, contain, or lie within/above the
      Hermes root, the team home, or any profile directory -- otherwise
      "migrating" it would move a whole profile or the Hermes root itself
      (repros F and H). Returns a non-empty reason string when the
      candidate must be refused; $null when it is safe.
    #>
    param(
        [string] $Target, [string] $HermesRoot, [string] $DevTeamHome,
        [string[]] $ProfileDirs
    )
    $base = Split-Path -Leaf (Get-NormalizedPath $Target)
    if ($base -ne 'dev-team') {
        return "its name is '$base', not 'dev-team' (migrating it would move an entire profile or other directory, not a team-state folder)"
    }
    if (Test-IsAncestorOrEqualPath -Anc $Target -Desc $HermesRoot) {
        return "it equals or contains the Hermes root ($HermesRoot)"
    }
    if (Test-IsAncestorOrEqualPath -Anc $Target -Desc $DevTeamHome) {
        return "it equals or contains the team home ($DevTeamHome)"
    }
    foreach ($pd in $ProfileDirs) {
        if (-not $pd) { continue }
        if (Test-IsAncestorOrEqualPath -Anc $Target -Desc $pd) {
            return "it equals or contains profile directory $pd"
        }
    }
    return $null
}

function Resolve-TeamHomeMigrationPlan {
    <#
      Pure decision function for T4 migration, deliberately separated from
      Invoke-Hermes/junction I/O so it can be unit tested in isolation
      (this script has no fake-hermes harness for a full run). Takes
      $DevTeamHome and a map of profile name -> that profile's raw
      dev-team junction target (or $null/absent when it has none), and
      decides what should happen:
        Outcome 'invalid'  - D1: a candidate is not a safe old-home
                 target (see Get-UnsafeOldHomeReason). Message is the
                 full stderr text to print before exit 2.
        Outcome 'conflict' - more than one location holds data: either two
                 or more distinct old targets, or one old target plus the
                 new root. Message is the full stderr text to print before
                 exit 2 (review findings #1 and #2 depend on scanning every
                 profile, not just -Only's selection, and on collecting
                 every distinct old target instead of stopping at the
                 first).
        Outcome 'move'   - exactly one old location holds data and the new
                 root does not. OldTeamHome is that location; Affected
                 Profiles lists every profile name whose junction pointed
                 there (not only the ones -Only selected).
        Outcome 'none'   - nothing to migrate.
    #>
    param(
        [string] $DevTeamHome, [hashtable] $CandidateTargets,
        [string] $HermesRoot, [string[]] $ProfileDirs,
        [switch] $DevTeamHomeIsLink
    )

    $devTeamHomeNorm = Get-NormalizedPath $DevTeamHome
    $oldTargets = [ordered]@{}   # normalized target -> {Raw; Profiles}
    foreach ($name in $CandidateTargets.Keys) {
        $target = $CandidateTargets[$name]
        if (-not $target) { continue }
        $targetNorm = Get-NormalizedPath $target
        if ($targetNorm -eq $devTeamHomeNorm) { continue }
        $reason = Get-UnsafeOldHomeReason -Target $target -HermesRoot $HermesRoot `
            -DevTeamHome $DevTeamHome -ProfileDirs $ProfileDirs
        if ($reason) {
            $label = if ($name -eq '__root_link__') { "the root dev-team link ($DevTeamHome)" } else { "$name/dev-team" }
            $msg = "Refusing to migrate: $label resolves to $target, $reason.`n" +
                   "This link looks misconfigured, not a migratable old team home.`n" +
                   "Fix it by hand (point it at a real dev-team directory, or remove the dangerous link) and re-run."
            return [ordered]@{ Outcome = 'invalid'; Message = $msg }
        }
        if (-not (Test-DirHasData $target)) { continue }
        if (-not $oldTargets.Contains($targetNorm)) {
            $oldTargets[$targetNorm] = [ordered]@{ Raw = $target; Profiles = @() }
        }
        $oldTargets[$targetNorm].Profiles += $name
    }

    # D4 (review): when the root itself is a link, any data "at" it is the
    # same physical data as whatever old-home candidate it resolves to --
    # counting it as separately populated would manufacture a false
    # conflict with itself.
    $newHasData = if ($DevTeamHomeIsLink) { $false } else { Test-DirHasData $DevTeamHome }
    $populatedCount = $oldTargets.Count + [int]$newHasData

    if ($populatedCount -gt 1) {
        $lines = @('Team home conflict: existing team state in more than one location:')
        if ($newHasData) { $lines += "  $DevTeamHome  (the correct Hermes-root location)" }
        foreach ($key in $oldTargets.Keys) {
            $entry = $oldTargets[$key]
            $shownFrom = @($entry.Profiles | ForEach-Object { if ($_ -eq '__root_link__') { 'the root dev-team link' } else { $_ } })
            $lines += "  $($entry.Raw)  (linked from: $($shownFrom -join ', '))"
        }
        $lines += 'Refusing to guess which is current. Reconcile by hand by choosing exactly one candidate below, then re-run:'
        $stamp = Get-Date -Format 'yyyyMMddTHHmmss'
        # D5 (review): one concrete, working command sequence per
        # candidate, never a placeholder, guarded so an absent root (the
        # commonest conflict shape) does not silently block the real move,
        # and naming every profile that needs relinking afterwards.
        $choices = @()
        if ($newHasData) { $choices += [ordered]@{ Label = 'the existing root location'; Path = $DevTeamHome } }
        foreach ($key in $oldTargets.Keys) {
            $entry = $oldTargets[$key]
            $shown = @($entry.Profiles | ForEach-Object { if ($_ -eq '__root_link__') { 'the root dev-team link' } else { $_ } })
            $choices += [ordered]@{ Label = "the location linked from: $($shown -join ', ')"; Path = $entry.Raw }
        }
        foreach ($choice in $choices) {
            $lines += "  To keep $($choice.Label) ($($choice.Path)):"
            $lines += "    if (Test-Path -LiteralPath '$DevTeamHome') { Rename-Item -LiteralPath '$DevTeamHome' -NewName '$DevTeamHome.bak-$stamp' }"
            if ((Get-NormalizedPath $choice.Path) -ne $devTeamHomeNorm) {
                $lines += "    Move-Item -LiteralPath '$($choice.Path)' -Destination '$DevTeamHome'"
            }
            # N2 (review r2): the chosen path no longer exists after the move,
            # so every affected profile must point at the root; the installer
            # does exactly that on a re-run.
            $lines += "    # then re-run this installer: it relinks every team profile's dev-team to '$DevTeamHome'"
        }
        return [ordered]@{ Outcome = 'conflict'; Message = ($lines -join "`n") }
    } elseif ($oldTargets.Count -eq 1) {
        $only = $oldTargets.Values | Select-Object -First 1
        return [ordered]@{ Outcome = 'move'; OldTeamHome = $only.Raw; AffectedProfiles = $only.Profiles }
    } else {
        return [ordered]@{ Outcome = 'none' }
    }
}

function Invoke-TeamHomeMigration {
    <#
      T4: a team installed before $HermesRoot climbed out of a profile may
      have every dev-* profile's dev-team junction pointing at that old,
      profile-rooted location -- which still holds the live data (project
      records, the deciders ledger, model-tiers.json). Moves it into
      $DevTeamHome when that is safe (empty or absent there), otherwise
      refuses with exit 2 and the exact command to reconcile by hand -
      never silently picks one side when both hold data.

      D3 (review): moves (renames) the directory first, then leaves a
      junction at $OldTeamHome pointing at $DevTeamHome, so every old path
      -- including a junction outside the scanned profile set -- keeps
      resolving with zero window where a concurrent write lands nowhere.
      Only after that is a backup copy made. $OldTeamHome and $DevTeamHome
      must be on the same volume for Move-Item to be an atomic rename;
      refuses with exit 2 and a manual procedure otherwise.

      Returns an ordered hashtable {Choice; Why} describing what happened,
      for the dry-run report and the completion record.
    #>
    param([string] $DevTeamHome, [string] $OldTeamHome, [switch] $DryRun, [switch] $DevTeamHomeIsLink)

    if (-not $OldTeamHome) {
        return [ordered]@{
            Choice = 'none'
            Why    = 'no pre-existing profile-rooted team home with data was found'
        }
    }
    # Defense in depth: Resolve-TeamHomeMigrationPlan already refuses a
    # genuine conflict before this is ever called, but a caller that
    # skips the plan (or the root link's own data, when it is not itself
    # the link) must still never silently pick a side.
    if (-not $DevTeamHomeIsLink -and (Test-DirHasData $DevTeamHome)) {
        $stamp = Get-Date -Format 'yyyyMMddTHHmmss'
        [Console]::Error.WriteLine('Team home conflict: existing team state at both:')
        [Console]::Error.WriteLine("  $OldTeamHome  (where dev-* profiles currently link)")
        [Console]::Error.WriteLine("  $DevTeamHome  (the correct Hermes-root location)")
        [Console]::Error.WriteLine('Refusing to guess which is current. Reconcile by hand, then re-run, e.g.:')
        [Console]::Error.WriteLine("  if (Test-Path -LiteralPath '$DevTeamHome') { Rename-Item -LiteralPath '$DevTeamHome' -NewName '$DevTeamHome.bak-$stamp' }")
        [Console]::Error.WriteLine("  Move-Item -LiteralPath '$OldTeamHome' -Destination '$DevTeamHome'")
        exit 2
    }
    # Review finding #4: 'what moved' must name the top-level entries, not
    # only the source/destination paths, in both the dry run and the real
    # run.
    $entryNames = @()
    if (Test-Path -LiteralPath $OldTeamHome -PathType Container) {
        $entryNames = @(Get-ChildItem -LiteralPath $OldTeamHome -Force -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Name | Sort-Object)
    }
    $backup = "$OldTeamHome.pre-root-migration-$(Get-Date -Format 'yyyyMMddTHHmmss')"
    if ($DryRun) {
        Write-Host "Would migrate team state: $OldTeamHome -> $DevTeamHome"
        Write-Host "  (backup would be kept at $backup)"
        Write-Host '  entries that would move:'
        foreach ($entryName in $entryNames) { Write-Host "    $entryName" }
    } else {
        $oldRoot = [System.IO.Path]::GetPathRoot((Get-NormalizedPath $OldTeamHome))
        $newRootProbe = if (Test-Path -LiteralPath $DevTeamHome) { $DevTeamHome } else { Split-Path -Parent $DevTeamHome }
        $newRoot = [System.IO.Path]::GetPathRoot((Get-NormalizedPath $newRootProbe))
        if ($oldRoot -and $newRoot -and ($oldRoot -ine $newRoot)) {
            [Console]::Error.WriteLine("Refusing to migrate: $OldTeamHome and $DevTeamHome are on different volumes, so an atomic move is not possible.")
            [Console]::Error.WriteLine('Reconcile by hand:')
            [Console]::Error.WriteLine("  New-Item -ItemType Directory -Path '$DevTeamHome' -Force")
            [Console]::Error.WriteLine("  Copy-Item -Path (Join-Path '$OldTeamHome' '*') -Destination '$DevTeamHome' -Recurse -Force")
            [Console]::Error.WriteLine("  Rename-Item -LiteralPath '$OldTeamHome' -NewName (Split-Path -Leaf '$backup')")
            [Console]::Error.WriteLine("  New-Item -ItemType Junction -Path '$OldTeamHome' -Target '$DevTeamHome'")
            exit 2
        }
        # D4: if the root is itself a junction, remove it first so the move
        # lands a real directory there instead of writing through the link.
        if ($DevTeamHomeIsLink -and (Get-Item -LiteralPath $DevTeamHome -Force -ErrorAction SilentlyContinue)) {
            [System.IO.Directory]::Delete($DevTeamHome)
        }
        $DevTeamHomeParent = Split-Path -Parent $DevTeamHome
        if ($DevTeamHomeParent -and -not (Test-Path -LiteralPath $DevTeamHomeParent)) {
            New-Item -ItemType Directory -Path $DevTeamHomeParent -Force | Out-Null
        }
        # N1 (review r2): Move-Item into an existing folder moves the source
        # INSIDE it. The plan only gets here when the root holds no data;
        # a non-recursive Directory.Delete refuses a non-empty folder.
        $rootItem = Get-Item -LiteralPath $DevTeamHome -Force -ErrorAction SilentlyContinue
        if ($rootItem -and $rootItem.PSIsContainer -and -not $rootItem.LinkType) {
            [System.IO.Directory]::Delete($DevTeamHome)
        }
        Move-Item -LiteralPath $OldTeamHome -Destination $DevTeamHome
        New-DirLink -LinkPath $OldTeamHome -Target $DevTeamHome -Label 'the old team-home path' | Out-Null
        New-Item -ItemType Directory -Path $backup -Force | Out-Null
        Copy-Item -Path (Join-Path $DevTeamHome '*') -Destination $backup -Recurse -Force
        Write-Host "Migrated team state: $OldTeamHome -> $DevTeamHome"
        Write-Host "  (backup kept at $backup)"
        Write-Host '  entries moved:'
        foreach ($entryName in $entryNames) { Write-Host "    $entryName" }
    }
    return [ordered]@{
        Choice = 'move'
        Why    = "dev-* profiles' dev-team links point at $OldTeamHome, which holds the live team's data; $DevTeamHome is empty or absent, so it is safe to move the data there without discarding anything"
    }
}

if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    Write-Error @'
hermes not found on PATH.
Install Hermes and Mnemosyne first: ..\..\install-mnemosyne-hermes-windows.ps1
If it is installed, add its directory to PATH.
'@
    exit 1
}

# Resolve HERMES_HOME from Hermes itself rather than assuming
# %LOCALAPPDATA%\hermes, so a custom HERMES_HOME keeps working.
$RootConfig = Get-HermesLastLine (Invoke-Hermes 'config' 'path')
if ($LASTEXITCODE -ne 0) { Write-Error "hermes config path failed: $RootConfig"; exit 1 }
$HermesRoot     = Split-Path -Parent $RootConfig
# T4: 'hermes config path' returns the ACTIVE PROFILE's config.yaml
# (...\profiles\<name>\config.yaml) when this installer itself is run under
# a named profile -- so $HermesRoot above would be that profile's own
# directory, not the true Hermes root. Climb out of profiles\<name> exactly
# the way kanban-budget.py's hermes_root() already does, so the team home
# below lands at the Hermes root regardless of which profile ran this.
if ((Split-Path -Leaf (Split-Path -Parent $HermesRoot)) -eq 'profiles') {
    $HermesRoot = Split-Path -Parent (Split-Path -Parent $HermesRoot)
}
# The wrappers name an absolute python.exe. A bare `python` resolves, on a
# machine without a system Python, to the Microsoft Store alias stub in
# WindowsApps, which prints 'Python was not found' and exits 9009 (seen in
# CI on both runners). Prefer the interpreter Hermes itself installed, then
# the py launcher's Python 3, then a real python.exe on PATH.
# Resolved here, before anything is written: the same interpreter runs
# dev-team\scripts\env_pin_check.py on every .env pin (Assert-EnvPinSafe),
# and a wrapper path cmd.exe cannot read is refused before any profile is
# touched (Assert-CmdWrapperPathsAscii).
function Test-WrapperPythonEmbeddable {
    <# M3 (round 2 review): a candidate from the py launcher or PATH that
       cannot be embedded in forge.cmd/deciders.cmd (not ASCII, and under
       no %ENVVAR% prefix this installer can rewrite) must not be chosen at
       all -- Resolve-WrapperPython falls back to the next source instead,
       rather than committing to a path Assert-CmdWrapperPathsAscii will
       later have to hard-refuse (M3's regression: main picked a usable
       PATH Python in that situation; this installer used to refuse the
       whole install instead). Probes with Resolve-CmdEmbeddablePath's own
       -NoRefuse mode, so this is the exact same rule, not a reimplementation
       of it. Hermes' own interpreter (toolsDir, above) is NEVER probed here
       -- it stays strict, per D6: it is always under %LOCALAPPDATA% in
       practice, and steering away from it toward "a different, unrelated
       installation" because of a representability quirk is exactly the
       silent-wrong-Python outcome D6 forbids. #>
    param([string] $Path)
    if (-not $Path) { return $false }
    # N2 (round-7 review): this used to carry its OWN printable-ASCII fast
    # path -- a SECOND copy of Resolve-CmdEmbeddablePath's rule that this
    # function's docstring claims to mirror exactly via the -NoRefuse probe
    # below. Round 6 (F1) tried to keep the copies in agreement by gating
    # them identically; that still left the copy here skipping the 8.3
    # long-form canonicalisation the real function does. An 8.3 SHORT name
    # is printable ASCII BY CONSTRUCTION, so a short-form candidate (e.g.
    # "D:\PYTHN~1\python.exe" for a non-ASCII "D:\Python\python.exe")
    # answered $true here without ever reaching the probe, Resolve-
    # WrapperPython committed to it, and the later real call (made WITHOUT
    # -NoRefuse, from Assert-CmdWrapperPathsAscii) refused the whole
    # install with exit 2 instead of falling back to the next source --
    # reinstating the exact M3 regression this function exists to prevent.
    # Two copies of one rule drift apart; one copy cannot. The probe is
    # now the ONLY gate, so this function is the real rule by construction
    # and pays one GetLongPathName call for the privilege.
    $prefixes = Get-CmdPrefixCandidates -HermesRoot $HermesRoot
    $probe = Resolve-CmdEmbeddablePath -Label 'probe' -Path $Path -Prefixes $prefixes -NoRefuse
    return [bool]$probe
}
function Resolve-WrapperPython {
    <# D6 (review follow-up, pr10-review.md section 4): candidates in that
       order, and the FIRST ONE THAT EXISTS wins -- no preference for an
       ASCII-representable path over a non-ASCII one. Whether the winning
       path can be embedded in forge.cmd/deciders.cmd (and, if not,
       whether it can be rewritten to an %ENVVAR% form) is a separate
       concern decided afterwards by Assert-CmdWrapperPathsAscii; letting
       path-representability steer WHICH interpreter is chosen here would
       mean a Hermes Python under a non-ASCII %LOCALAPPDATA% (a non-ASCII
       Windows user name) silently loses to a different, unrelated
       installation -- exactly the "silently runs a different Python"
       outcome this installer must never produce. No usable form at all is
       a hard failure (exit 2 in Assert-CmdWrapperPathsAscii), not a
       fallback to another interpreter.
       M3 (round 2 review): that "no fallback" rule above is about Hermes'
       OWN interpreter (toolsDir) only. For the py launcher and PATH
       sources it was a regression: a non-ASCII, non-embeddable py -3
       result used to make the WHOLE install exit 2 even when PATH had a
       perfectly usable ASCII (or otherwise embeddable) Python right
       behind it -- main (pre-D1..D9) picked that PATH Python and
       installed fine. Each of those two sources now falls back to the
       next one when its own candidate exists but can't be embedded;
       Hermes' own interpreter is still never skipped for that reason. #>
    $pyCandidate = $null
    # N3 (round-7 review): true when $pyCandidate is the py launcher's own
    # choice AND it was declined because of a '%', '"' or '!' in its path
    # rather than because it is non-ASCII. Only that case warrants a
    # warning when a later source wins: 1.3.1 used the launcher's Python
    # for such a path, and the operator must not silently get a different
    # interpreter. The non-ASCII fallback is already documented behaviour.
    $pyCandidateUnsafeChar = $false
    $toolsDir = Join-Path $env:LOCALAPPDATA 'hermes\tools'
    if (Test-Path -LiteralPath $toolsDir) {
        $found = Get-ChildItem -LiteralPath $toolsDir -Directory -Filter 'python-3*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' } |
            Where-Object { Test-Path -LiteralPath $_ } |
            Select-Object -First 1
        if ($found) { return $found }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # 'Continue' locally: under 'Stop', PS 5.1 turns a native command's
        # redirected stderr (py with no Python 3 installed) into a terminating
        # error, which would abort the install instead of falling through.
        $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            # D7 (review): ask py -3 to print sys.executable as an
            # ASCII-only hex encoding of its raw UTF-16LE code units.
            # Hex is 7-bit clean, so the console's OEM/ANSI code page
            # never touches it -- under Windows PowerShell 5.1 a bare
            # `print(sys.executable)` is decoded through that code page,
            # which mangles a non-ASCII path (a non-ASCII Windows user
            # name) before PowerShell ever sees it -- the reviewer's
            # repro -- making an existing interpreter look like "no
            # Python 3 found". This -c program is also kept free of
            # double quotes (single-quoted PS string; the doubled '' is
            # PS's own escape for an embedded single quote), because
            # PS 5.1's native-argument quoting of a literal '"' is
            # unreliable.
            # C2 (review round 1): 'surrogatepass' keeps a lone surrogate
            # intact THROUGH sys.executable.encode() on the Python side,
            # but that is not the whole trip -- the PowerShell side below
            # decodes the hex with [Text.Encoding]::Unicode, and that
            # encoding uses U+FFFD REPLACEMENT CHARACTER fallback for an
            # unpaired surrogate (.NET docs: Encoding.Unicode "Remarks",
            # https://learn.microsoft.com/en-us/dotnet/api/system.text.encoding.unicode#remarks),
            # so a lone surrogate does NOT round-trip end to end. That is
            # fine here: the decoded path then simply fails Test-Path (a
            # replacement character is not a character any real NTFS
            # path contains), and falls through to the normal "not
            # found" handling below -- it fails safe, it just isn't the
            # exact original path in a report-only variable nothing else
            # trusts.
            # m3 (round 2 review): py -3 can print something other than
            # the hex line before it (a deprecation notice, a launcher
            # warning), and an earlier version joined EVERY line of $hex
            # into one string before matching -- a single stray line made
            # the whole thing fail the hex regex, giving "No Python 3
            # found" even though the launcher DID report a path (exactly
            # the misleading message this change set out to remove). Use
            # the LAST non-empty line: the -c program's only output is the
            # final `print(...)`, so whatever else py -3 writes to stdout
            # comes before it.
            $hex = & py -3 -c 'import sys; print(sys.executable.encode(''utf-16-le'',''surrogatepass'').hex())' 2>$null
        } finally { $ErrorActionPreference = $savedEap }
        if ($LASTEXITCODE -eq 0 -and $hex) {
            $hexLines = @(@($hex) | Where-Object { $_ -and ([string]$_).Trim() })
            $hexTrimmed = if ($hexLines.Count -gt 0) { ([string]$hexLines[-1]).Trim() } else { '' }
            # No [Convert]::FromHexString: that overload is .NET 5+ only
            # and this must also run under PowerShell 5.1 (.NET Framework).
            if ($hexTrimmed -match '^([0-9a-f]{4})+$') {
                $bytes = New-Object byte[] ($hexTrimmed.Length / 2)
                for ($i = 0; $i -lt $bytes.Length; $i++) {
                    $bytes[$i] = [Convert]::ToByte($hexTrimmed.Substring($i * 2, 2), 16)
                }
                $decoded = [Text.Encoding]::Unicode.GetString($bytes)
                # D8 (review): remember what the launcher reported even if
                # it turns out not to exist, so the caller's "nothing
                # found" message can say so accurately instead of
                # claiming no Python 3 exists when one was in fact
                # reported (just not at a path that currently resolves).
                $script:WrapperPythonLauncherPath = $decoded
                # M3 (round 2 review): a py -3 result that EXISTS but can't
                # be embedded in forge.cmd/deciders.cmd must not win OUTRIGHT
                # over a PATH candidate that can -- that was the regression
                # (main picked the usable PATH Python; this installer forced
                # a hard refusal instead). But if PATH has nothing better
                # either, still resolve to this one rather than $null: it IS
                # a real Python 3 ("no Python 3 found" would be false), and
                # Assert-CmdWrapperPathsAscii's own refusal message (which
                # already suggests an ASCII-only install path) is the
                # correct place for that failure, not a fabricated "nothing
                # found at all".
                if (Test-Path -LiteralPath $decoded) {
                    if (Test-WrapperPythonEmbeddable $decoded) { return $decoded }
                    $pyCandidate = $decoded
                    $pyCandidateUnsafeChar = Test-ContainsUnsafeCmdEmbedChar $decoded
                }
            }
        }
    }
    $onPathCmd = Get-Command python -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } |
        Select-Object -First 1
    if ($onPathCmd) {
        if (Test-WrapperPythonEmbeddable $onPathCmd.Source) {
            # N3 (round-7 review): the py launcher's own Python is being
            # passed over right here. Say so when the reason is a '%', '"'
            # or '!' in its path -- 1.3.1 used that interpreter, so this is
            # a change of behaviour the operator has to be able to see,
            # and it is actionable (PY_PYTHON/py.ini, or move the Python).
            if ($pyCandidate -and $pyCandidateUnsafeChar) {
                Write-Warning "The py launcher reported a Python 3 at $(Format-EnvPinForDisplay $pyCandidate), whose path contains a '%', '`"' or '!' character that cannot be safely embedded in forge.cmd/deciders.cmd; using $(Format-EnvPinForDisplay $onPathCmd.Source) from PATH instead. To use the py launcher's Python, move it onto a path without those characters, or point the launcher at a different Python 3 with the PY_PYTHON environment variable or a py.ini file (https://docs.python.org/3/using/windows.html#customization)."
            }
            return $onPathCmd.Source
        }
        if (-not $pyCandidate) { $pyCandidate = $onPathCmd.Source }
    }
    if ($pyCandidate) { return $pyCandidate }
    return $null
}
function Format-EnvPinForDisplay {
    <# $Value with every UTF-16 code unit outside printable ASCII shown as
       \uXXXX (so a lone surrogate shows too): a refusal never puts a raw
       control or line-break character on the console, and reads the same
       under any console code page. #>
    param([string] $Value)
    $sb = New-Object System.Text.StringBuilder
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int]$ch
        if ($code -ge 0x20 -and $code -le 0x7E) { [void]$sb.Append($ch) }
        else { [void]$sb.Append(('\u{0:X4}' -f $code)) }
    }
    return $sb.ToString()
}
function Test-AsciiOrdinalRange {
    <# S2 (review round 1): PowerShell's `-match` is CASE-INSENSITIVE by
       default, and .NET's case folding treats some non-ASCII code points
       as equal to an ASCII letter -- U+0130 (LATIN CAPITAL LETTER I WITH
       DOT ABOVE, i.e. Turkish "I") folds to 'i', and U+212A (KELVIN SIGN)
       folds to 'k' -- so `-match '[^\x20-\x7E]'` reported both as "already
       ASCII" and let them through unrewritten, and the same bug was in
       the Assert-WrapperTextAscii defence meant to catch exactly this.
       Under Windows PowerShell 5.1 (.NET Framework) that folding is also
       CULTURE-SENSITIVE, so the exact set of affected characters would
       depend on the machine's locale. This helper never folds case and
       never uses the regex engine: a plain per-character ordinal ([int]
       cast of each [char]) comparison against [$Min, $Max], so the result
       is the same on every machine and every .NET runtime. #>
    param([string] $Value, [int] $Min, [int] $Max)
    foreach ($ch in $Value.ToCharArray()) {
        $code = [int]$ch
        if ($code -lt $Min -or $code -gt $Max) { return $false }
    }
    return $true
}
function Test-ContainsUnsafeCmdEmbedChar {
    <# THE gate (D2, round 7): the single rule deciding whether a path may
       be embedded in forge.cmd/deciders.cmd LITERALLY -- unrewritten,
       unguarded, exactly as 1.3.1 wrote it -- or must instead be rewritten
       to a %ENVVAR% form or refused outright. Safe when this returns
       $false, not safe when it returns $true. It has exactly ONE DECIDING
       call site, in Resolve-CmdEmbeddablePath's fast path;
       Test-WrapperPythonEmbeddable reaches the same rule through that
       function's -NoRefuse probe rather than asking again. That is
       deliberate: this file has now produced three separate findings
       (round-6 F1, round-7 N1 and N2) that were all the same bug -- two
       copies of this rule drifting apart -- so a second DECIDING call site
       is a defect, not an optimisation, and
       test_D2_the_literal_embed_rule_has_exactly_one_deciding_call_site
       fails if
       one appears.
       WHICH characters: cmd.exe's full metacharacter set is
       '& ^ % ! ( ) " < >', and an earlier version of this gate
       (Test-ContainsCmdShellMetachar, A3 round 5, removed in round 7)
       excluded all of them from the literal embed. That was too wide.
       Every embed site wraps the path in double quotes -- the wrapper's
       run line, and the guard's echo via Get-CmdGuardBlock, which also
       doubles '%' -- and inside double quotes cmd.exe treats '&', '^', '('
       and ')' as literal text (cmd's own Remarks: those characters "must
       be preceded by ... quotation marks" to be literal; see also
       Get-CmdGuardBlock's S1 comment and
       test_guard_echo_pins_quoted_doubled_percent_bytes_for_ampersand_remainder).
       Only three are genuinely unsafe inside the quotes:
         '%' -- starts another expansion even inside quotes;
         '"' -- closes the quoting outright;
         '!' -- consumed by `!var!` under `cmd /v:on` (delayed expansion),
                which these wrappers do not enable but a caller's shell
                might.
       '<' and '>' need no entry: they are illegal in a Windows file name,
       so they can never occur in a path this function is asked about.
       '^' is a special case rather than an exclusion: it IS safe in the
       quoted literal embed, EXCEPT that cmd's delayed-expansion pass
       strips carets inside quotes on a line that also contains '!' (which
       `%*` can supply from a caller's argument). Excluding it here would
       force caret paths into the %ENVVAR% rewrite and reintroduce N1 for
       them; instead Get-CmdGuardBlock emits
       `setlocal DisableDelayedExpansion` when a literal embed contains
       '^' (D1), which closes that hole without touching the embed.
       A plain per-character scan, not `-match`: same reason as
       Test-AsciiOrdinalRange -- these are punctuation, so .NET's
       culture-sensitive case folding cannot apply, but a bare loop keeps
       this function independent of the regex engine entirely, matching
       every other classification helper in this file. #>
    param([string] $Value)
    foreach ($ch in $Value.ToCharArray()) {
        if ('%!"'.IndexOf($ch) -ge 0) { return $true }
    }
    return $false
}
function Get-LongPathName {
    <# Round 2 (review): prefix matching in Resolve-CmdEmbeddablePath compares
       $Path against LOCALAPPDATA/APPDATA/USERPROFILE/ProgramFiles. On a CI
       (or any) machine where the process's own path arrives through an 8.3
       short name (e.g. "C:\Users\RUNNER~1\...") while those env vars hold the
       canonical LONG form (e.g. "C:\Users\runneradmin\..."), an ordinal
       string compare of the two forms never matches even though they denote
       the same directory -- the install would wrongly refuse (or, worse,
       silently embed a short-name path a later run under the long name can't
       prefix-rewrite). Canonicalize to the long form on BOTH sides before
       comparing. GetLongPathName is a thin kernel32 call: it only inspects
       the path string and the filesystem's own 8.3<->long mapping, nothing
       mutates or is created. Off Windows, or when the path does not exist
       (GetLongPathName requires the path to resolve), the input is returned
       unchanged -- never a failure, since a non-existent path can't have a
       short name to begin with and an unresolvable call means "nothing to
       canonicalize", not "refuse". #>
    param([string] $Path)
    if (-not $Path) { return $Path }
    try {
        if (-not ([System.Management.Automation.PSTypeName]'DevTeamNative.PathNative').Type) {
            Add-Type -Namespace DevTeamNative -Name PathNative -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern uint GetLongPathName(string lpszShortPath, System.Text.StringBuilder lpszLongPath, uint cchBuffer);
'@ -ErrorAction Stop
        }
        $sb = New-Object System.Text.StringBuilder 1024
        $len = [DevTeamNative.PathNative]::GetLongPathName($Path, $sb, [uint32]$sb.Capacity)
        if ($len -gt 0 -and $len -lt [uint32]$sb.Capacity) { return $sb.ToString() }
    } catch {
        # Not Windows, Add-Type unavailable, or the call failed: fall through
        # and use the path exactly as given.
    }
    return $Path
}
function Get-PersistedEnvVar {
    <# Thin wrapper around [Environment]::GetEnvironmentVariable(Name,
       Scope) so Get-CmdPrefixCandidates's registry read can be
       substituted in a test harness (the static .NET call itself cannot
       be mocked). Off Windows (this suite also runs under Linux pwsh)
       the registry-backed overload throws; callers treat that the same
       as "nothing persisted there". #>
    param([string] $Name, [string] $Scope)
    try {
        return [Environment]::GetEnvironmentVariable($Name, $Scope)
    } catch {
        return $null
    }
}
function Get-CmdPrefixCandidates {
    <# D2 (review follow-up): prefix candidates to rewrite a non-ASCII path
       into an %ENVVAR% form a .cmd file can embed verbatim (ASCII) and
       cmd.exe expands from its own (UTF-16) environment at run time.
       Read from THIS PROCESS's environment for LOCALAPPDATA/APPDATA/
       USERPROFILE/ProgramW6432: Windows seeds those into every new
       process from the user's profile, so a later cmd.exe run by the same
       user sees the same values this installer saw. These are TIER 1.
       m2 (round 2 review): %ProgramFiles% is always ASCII on disk (so it
       can never actually match a non-ASCII remainder on its own), and
       under a 32-bit cmd.exe (WOW64) it is REDIRECTED to
       "...\Program Files (x86)" -- a different, narrower directory than
       the one this (64-bit PowerShell) process resolved
       (https://learn.microsoft.com/en-us/windows/win32/winprog64/wow64-implementation-details),
       so a wrapper written against %ProgramFiles% here could expand to the
       wrong folder under a 32-bit cmd.exe later. %ProgramW6432% always
       names the true 64-bit Program Files regardless of the caller's
       bitness, and is only ever ABSENT on a 32-bit Windows OS -- where
       there is no WOW64 redirection to begin with, so plain %ProgramFiles%
       is safe there and used as the fallback.
       HERMES_ROOT is different: it is not one of those profile-seeded
       variables, so it is only used when PERSISTED -- read directly from
       the registry (HKCU or HKLM 'Environment', via the User/Machine
       target, bypassing this process's own environment) -- AND equal to
       the $HermesRoot this installer actually resolved. A process-only
       HERMES_ROOT (set for this invocation alone, e.g. by a CI step or a
       parent script) would expand to nothing in a cmd.exe spawned later,
       silently breaking the wrapper, so it is never used as a prefix.
       M2 (round 2 review): even when persisted and matching, HERMES_ROOT
       is TIER 2 (see Resolve-CmdEmbeddablePath) -- Hermes itself never
       defines or reads HERMES_ROOT (it uses HERMES_HOME), so this is a
       variable only a user's own shell profile or a CI step happens to
       set; it must never outrank an OS profile variable just because it
       is textually longer, or a user who changes/unsets it later silently
       repoints the wrapper at a different Python. It is tried only when no
       tier-1 variable matches at all. #>
    param([string] $HermesRoot)
    $cands = New-Object System.Collections.Generic.List[object]
    foreach ($name in @('LOCALAPPDATA', 'APPDATA', 'USERPROFILE')) {
        $val = [Environment]::GetEnvironmentVariable($name)
        if ($val) { $cands.Add([pscustomobject]@{ Name = $name; Value = $val; Tier = 1 }) }
    }
    $programFiles64 = [Environment]::GetEnvironmentVariable('ProgramW6432')
    if ($programFiles64) {
        $cands.Add([pscustomobject]@{ Name = 'ProgramW6432'; Value = $programFiles64; Tier = 1 })
    } else {
        $programFiles = [Environment]::GetEnvironmentVariable('ProgramFiles')
        if ($programFiles) { $cands.Add([pscustomobject]@{ Name = 'ProgramFiles'; Value = $programFiles; Tier = 1 }) }
    }
    if ($HermesRoot) {
        $trimmedRoot = $HermesRoot.TrimEnd('\')
        # C1 (review round 1): a user-scope value, when non-empty, is the
        # one a later cmd.exe process actually sees -- it overrides a
        # machine-scope value of the same name (the exception is PATH,
        # which this isn't). The old loop accepted a matching Machine
        # value even when a DIFFERENT User value was also set, which
        # would make the rewritten wrapper expand %HERMES_ROOT% to the
        # wrong path at run time. Compute the effective value the same
        # way cmd.exe would, and only use it as a prefix if THAT value
        # equals $HermesRoot.
        $userVal = Get-PersistedEnvVar 'HERMES_ROOT' 'User'
        $machineVal = Get-PersistedEnvVar 'HERMES_ROOT' 'Machine'
        $effective = if ($userVal) { $userVal } else { $machineVal }
        if ($effective -and $effective.TrimEnd('\').Equals($trimmedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $cands.Add([pscustomobject]@{ Name = 'HERMES_ROOT'; Value = $effective; Tier = 2 })
        }
    }
    return $cands.ToArray()
}
function Resolve-CmdEmbeddablePath {
    <# D1-D2, as re-drawn by N1/D2 (round 7): a path that is already
       printable ASCII AND contains none of the three characters that are
       unsafe inside the wrapper's double quotes ('%', '"', '!' --
       Test-ContainsUnsafeCmdEmbedChar, the single gate) is returned
       literally, unrewritten (Rewritten = $false) -- byte-identical to the
       pre-D1..D9 wrapper for every such install, including one whose path
       contains '&', '^', '(' or ')' (those are literal inside quotes, so
       1.3.1's literal embed was always correct for them; see N1 in
       pr12-review-r2.md). The ONE exception to byte-identical is a
       literal embed containing '^', which additionally gets a
       `setlocal DisableDelayedExpansion` line (D1 -- see
       Get-CmdGuardBlock).
       Everything else -- non-ASCII, or ASCII containing '%', '"' or '!' --
       tries the %NAME%\<remainder> rewrite: it is
       rewritten only when it sits under one of $Prefixes at a
       path-separator boundary (an exact match, or the next character is
       '\' -- so e.g. prefix "C:\Users\Drag" never matches
       "C:\Users\Dragos2\..."), matched case-insensitively (OrdinalIgnoreCase
       -- NTFS paths are case-insensitive), and only when the remaining,
       rewritten part is itself printable ASCII with no '%' (would start
       another, unintended expansion) and no '"' (would break out of the
       wrapper's quoting) -- '&','^','(',')' ARE legal in this remainder,
       since it is always embedded inside double quotes (see
       Get-CmdGuardBlock's S1 comment). Among every prefix that qualifies,
       the lowest TIER wins first (see Get-CmdPrefixCandidates -- M2, round
       2 review: HERMES_ROOT is tier 2, every OS profile variable is tier
       1, so HERMES_ROOT is only ever used when no OS variable matches,
       never because it happens to be textually longer), then the LONGEST
       one within that tier, so e.g. LOCALAPPDATA beats USERPROFILE when a
       path sits under both.
       A path with no qualifying prefix is a hard refusal
       (exit 2, nothing written) -- the caller never falls back to a
       different, possibly wrong, interpreter or script -- UNLESS
       -NoRefuse is given,
       in which case it returns $null instead: used by
       Test-WrapperPythonEmbeddable to PROBE embeddability for the py
       launcher/PATH fallback (M3) without the exit-2 side effect.
       Round 6's separate "fall back to a literal embed rather than
       refuse" branch after the prefix loop is gone (D2): since the fast
       path above now asks that exact question of that exact value, the
       branch could never fire, and a second copy of the gate is how
       round-6 F1, N1 and N2 all happened. #>
    param([string] $Label, [string] $Path, [object[]] $Prefixes, [switch] $NoRefuse)
    # F1 (round 3 review): an 8.3 SHORT name is printable ASCII BY
    # CONSTRUCTION (that is the whole point of the 8.3 fallback form) even
    # when the LONG form -- the real directory name -- is not, e.g.
    # "C:\DEV-TE~1\DRGO~1\AppData\Local\Python\python.exe" for a checkout
    # under a non-ASCII user name. Round 2's canonicalization ran only
    # AFTER this ASCII check, so a short-named $Path took the "already
    # ASCII, embed literally" fast path before Get-LongPathName ever saw
    # it, silently embedding the short name verbatim in forge.cmd/
    # deciders.cmd instead of either matching a prefix or refusing. '~' is
    # the one character common to every 8.3 short component
    # (https://learn.microsoft.com/windows/win32/fileio/naming-a-file#short-vs-long-names)
    # and never appears in the long form of the same path, so testing for
    # it is a safe, cheap gate: only pay for a GetLongPathName call when
    # $Path could possibly be a short name, and an ordinary long path (no
    # '~') is still routed through the ASCII check completely unchanged --
    # byte-identical to before this fix.
    $asciiCheckPath = $Path
    if ($Path -and $Path.Contains('~')) {
        $asciiCheckPath = Get-LongPathName $Path
    }
    # N1 (round-7 review) / D2: printable-ASCII alone is not enough to
    # embed $Path literally -- but neither is "contains a cmd.exe
    # metacharacter" enough to forbid it. Every embed site wraps the path
    # in double quotes (the run line, and the guard's echo via
    # Get-CmdGuardBlock), and inside double quotes cmd.exe treats '&',
    # '^', '(' and ')' as literal text. Only '%' (starts another
    # expansion even inside quotes), '"' (closes the quoting outright)
    # and '!' (consumed under `cmd /v:on`) are genuinely unsafe, which is
    # exactly what Test-ContainsUnsafeCmdEmbedChar decides.
    # The A3 round-5 fix gated this on the WIDER
    # Test-ContainsCmdShellMetachar set, which pushed an all-ASCII path
    # containing only the quote-safe characters (e.g.
    # "C:\Users\bob\src (copy)\..." or "...\Programs\R&D\python.exe")
    # into the %ENVVAR% rewrite the moment it happened to sit under a
    # profile folder: it gained variable guards, an exit 1 under a
    # trimmed environment, and followed %USERPROFILE% to whichever user
    # ran the wrapper. 1.3.1 embedded all of those literally, and the
    # round-6 fallback below already said in comments that they are safe
    # literally -- so the wider gate bought nothing and cost the
    # byte-identical guarantee.
    # D2: this is now the ONE call site of that rule. The round-6
    # literal-embed fallback that used to sit after the prefix loop asked
    # the identical question of the identical value, so after this change
    # it could never fire -- two copies of one rule that only looked
    # independent (N1 and N2 are both that bug). It is gone; a path either
    # takes this fast path or is rewritten or is refused.
    if ((Test-AsciiOrdinalRange $asciiCheckPath 0x20 0x7E) -and
            -not (Test-ContainsUnsafeCmdEmbedChar $asciiCheckPath)) {
        return [ordered]@{ Embed = $Path; Rewritten = $false; PrefixName = $null }
    }
    # Round 2 (review): canonicalize to the LONG path form on BOTH sides
    # before any prefix comparison -- see Get-LongPathName. $Path itself is
    # canonicalized too (not just the prefixes) so an 8.3-short $Path still
    # matches a long-form LOCALAPPDATA/USERPROFILE/etc, and the computed
    # remainder is relative to the long form the %ENVVAR% will actually
    # expand to at run time. Reuse the canonicalization done above when
    # $Path contained '~' instead of calling GetLongPathName twice.
    $longPath = if ($Path -and $Path.Contains('~')) { $asciiCheckPath } else { Get-LongPathName $Path }
    $bestName = $null; $bestTier = [int]::MaxValue; $bestLen = -1; $bestRemainder = $null
    foreach ($p in $Prefixes) {
        $val = (Get-LongPathName ([string]$p.Value)).TrimEnd('\')
        if (-not $val -or $longPath.Length -lt $val.Length) { continue }
        $prefixPart = $longPath.Substring(0, $val.Length)
        if (-not $prefixPart.Equals($val, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $rest = $longPath.Substring($val.Length)
        if ($rest.Length -gt 0 -and $rest[0] -ne '\') { continue }   # separator-boundary check
        $remainder = $rest.TrimStart('\')
        if (-not (Test-AsciiOrdinalRange $remainder 0x20 0x7E)) { continue }
        # Only '%' (would start another expansion) and '"' (would break
        # the wrapper's quoting) are excluded here -- other cmd.exe
        # metacharacters such as '&' are LEGAL in this remainder: the
        # embed is always wrapped in double quotes at every use site
        # (the run line, and the guard's echo via Get-CmdGuardBlock,
        # which also doubles '%'), which cmd.exe treats as literal text
        # for '&','^','(',')' -- see Get-CmdGuardBlock's S1 comment and
        # test_guard_echo_pins_quoted_doubled_percent_bytes_for_ampersand_remainder.
        # N1 (round 7) only changes the ASCII FAST-PATH check above
        # (now Test-ContainsUnsafeCmdEmbedChar, not the wider A3 round-5
        # metachar set), so a path like
        # "...\R&D (x)\AppData\Local\..." now takes that literal fast
        # path instead of reaching here at all; this remainder check is
        # unchanged, and still governs what a REWRITTEN remainder (a
        # non-ASCII or '%'/'"'/'!'-bearing path under a prefix) may
        # contain.
        if ($remainder.Contains('%') -or $remainder.Contains('"')) { continue }
        $tier = if ($null -ne $p.Tier) { [int]$p.Tier } else { 1 }
        if ($tier -lt $bestTier -or ($tier -eq $bestTier -and $val.Length -gt $bestLen)) {
            $bestTier = $tier; $bestLen = $val.Length; $bestName = $p.Name; $bestRemainder = $remainder
        }
    }
    if ($bestName) {
        $sep = if ($bestRemainder) { '\' } else { '' }
        return [ordered]@{ Embed = "%$bestName%$sep$bestRemainder"; Rewritten = $true; PrefixName = $bestName }
    }
    # D2 (round-7): the round-6 "fall back to a literal embed instead of
    # refusing" block used to sit here, asking
    # (Test-AsciiOrdinalRange ... -and -not Test-ContainsUnsafeCmdEmbedChar ...)
    # of the same $asciiCheckPath the fast path above now asks. Anything
    # that reaches this line has already failed that exact test, so the
    # block was dead code -- and a second copy of the gate that a future
    # edit could drift from the first, which is precisely how N1 and N2
    # (and round-6's F1) happened. One gate, one call site: a path that
    # matched no prefix and is not literal-embeddable is refused here.
    if ($NoRefuse) { return $null }
    $names = ($Prefixes | ForEach-Object { $_.Name }) -join ', '
    if (-not $names) { $names = '(none available in this environment)' }
    # F6 (this review): $Path may be an 8.3 SHORT name (printable ASCII by
    # construction, e.g. "C:\DEV-TE~1\DRGO~1\..."), which is why it never
    # took the early-return fast path above -- $asciiCheckPath/$longPath,
    # the canonicalized LONG form, is what actually failed the ASCII test
    # and the prefix match, and is the real reason for this refusal.
    # Displaying the original (short) $Path here would show an
    # already-ASCII string while calling it "not printable ASCII", which
    # is both misleading and not the value any prefix was actually
    # compared against. Show $longPath instead, so the message always
    # names the real, non-ASCII (or non-prefixed) directory.
    [Console]::Error.WriteLine("Refusing to write forge.cmd/deciders.cmd: the path of $Label, $(Format-EnvPinForDisplay $longPath), is not printable ASCII, or contains a '%', '`"' or '!' character that cannot be safely embedded, and is not under any of the folders whose environment variable cmd.exe can expand ($names). Move the dev-team checkout, and/or the Python that runs it, onto an ASCII-only path with none of those characters (e.g. Python installed under C:\Python312) or under one of those folders, then re-run. If the py launcher is what picked this path, you can instead point it at a different Python 3 with the PY_PYTHON environment variable or a py.ini file (https://docs.python.org/3/using/windows.html#customization).")
    exit 2
}
function Assert-WrapperTextAscii {
    <# D9 (review): defence in depth -- after prefix rewriting, assert the
       WHOLE generated wrapper text is still plain ASCII before it is
       written. Should be unreachable given Resolve-CmdEmbeddablePath's own
       checks; a failure here means that invariant broke elsewhere (e.g. a
       marker or comment introduced non-ASCII text), and writing a
       silently corrupted wrapper is worse than refusing. #>
    param([string] $Label, [string] $Text)
    if (-not (Test-AsciiOrdinalRange $Text 0x00 0x7F)) {
        [Console]::Error.WriteLine("Internal error: the generated $Label wrapper is not plain ASCII after path-prefix rewriting; refusing to write it. This should be unreachable -- please report this as a bug.")
        exit 2
    }
}
function Get-CmdGuardBlock {
    <# D4 (review): for each (Label, Embed) pair whose Embed was REWRITTEN
       (non-ASCII, expanded via an %ENVVAR% prefix -- see
       Resolve-CmdEmbeddablePath), returns:
         - Guards: one `if not exist "<embed>" goto <label>` line per
           rewritten path, meant to go BEFORE the wrapper's run line;
         - Tail: the matching `:<label>` / message / `exit /b 1` blocks,
           meant to go AFTER it, so the run line's own `exit /b
           %ERRORLEVEL%` is reached on the success path and these blocks
           are never fallen into.
       A path that was embedded literally (ASCII, Rewritten = $false) gets
       no guard at all -- D1: an all-ASCII install's wrapper stays
       byte-identical to before this change.
       goto + label, not a parenthesized `( ... )` block: an expanded
       value can contain ')' (e.g. "C:\Program Files (x86)\..." under a
       32-bit cmd.exe), which would close a `( ... )` block early and
       corrupt the rest of the wrapper.
       D5: this guard only proves the %ENVVAR%-expanded path EXISTS; it
       cannot tell whose interpreter it is. See Assert-CmdWrapperPathsAscii
       for why that residual risk (another Windows user's same-named
       Hermes-managed Python) is accepted rather than further mitigated.
       M1 (round 2 review): an UNSET %VAR% expands to nothing in a batch
       file, so `if not exist "%VAR%\tools\python.exe" ...` becomes
       `if not exist "\tools\python.exe" ...` -- a path on the CURRENT
       DRIVE's root, which a non-admin user can create folders under by
       default. A wrapper started with a trimmed environment (a sandbox, a
       service, a gateway that strips profile variables) would then have
       its guard check "\tools\python.exe" instead of refusing, and a
       planted python.exe there would run. Fix: emit `if not defined NAME
       goto <label>` (the `if` doc: "defined <variable> ... true condition
       if *variable* is defined") immediately before each `if not exist`,
       going to the SAME label -- the failure message already covers
       "wasn't found", which is equally true of "the variable that names
       it isn't set".
       m1 (round 2 review, C6 follow-up): under `cmd /v:on` (delayed
       expansion), a literal '!' in the EXPANDED %VAR% value is consumed
       by cmd's own `!var!` syntax in BOTH the guard and the run line
       before either is evaluated -- e.g. a remainder "a!b!c" is read as
       "ac" in both places, so the guard can approve a different path than
       the one actually run. `setlocal DisableDelayedExpansion` (the
       setlocal doc: applies "regardless of the setting before") emitted
       once, alongside the guard lines, fixes this.
       D1 (round 7, the review's unverified '^' nit): the same pass that
       eats '!' also STRIPS CARETS inside quotes on any line it processes,
       so a LITERAL (unrewritten) embed containing '^' -- which N1
       deliberately keeps literal, because pushing caret paths into the
       %ENVVAR% rewrite would hand them N1's guards-and-exit-1 regression
       -- can be altered the same way when a caller's argument arrives
       through `%*` carrying a '!'. The wrapper would then run a path that
       is not the one embedded. So setlocal is emitted when there is at
       least one rewritten path OR at least one literal embed containing
       '^'. An all-ASCII install with no caret anywhere still gets no
       guards and no setlocal, so its wrapper stays byte-identical to
       1.3.1's -- that exception, and only that one, is what the CHANGELOG
       now states. #>
    param([string] $Prefix, [object[]] $Pairs)
    $guards = ''
    $tail = ''
    $i = 0
    $literalCaret = $false
    foreach ($pair in $Pairs) {
        if (-not $pair.Embed.Rewritten) {
            # D1: a literal embed gets no guard, but a '^' in it still
            # needs delayed expansion off (see the D1 note above).
            if ($pair.Embed.Embed -and $pair.Embed.Embed.Contains('^')) { $literalCaret = $true }
            continue
        }
        $gotoLabel = ":${Prefix}_missing_$i"
        $guards += "if not defined $($pair.Embed.PrefixName) goto $gotoLabel`r`n"
        $guards += "if not exist `"$($pair.Embed.Embed)`" goto $gotoLabel`r`n"
        # S1 (review round 1): cmd.exe expands every %VAR% in a line
        # BEFORE it parses that line, so an earlier version of this echo
        # printed the EXPANDED path unquoted -- a '&' in it (legal in a
        # file name, and a plain ASCII remainder such as "R&D" has one)
        # split the line and ran the fragments after it as commands, on
        # the very path meant to fail closed. D4 also asked for the
        # UNEXPANDED form in the message, not the expanded one. Fix:
        # double every '%' in the rewritten Embed (cmd.exe prints "%%" as
        # a literal '%' -- see 'for /?' for the %% idiom, used here to
        # SUPPRESS expansion rather than to loop) and wrap the whole
        # thing in double quotes. Inside double quotes, '&', '^', '(' and
        # ')' are literal (cmd's own Remarks: those characters "must be
        # preceded by ... quotation marks" to be literal), and the
        # remainder can never contain '"' (Resolve-CmdEmbeddablePath
        # rejects that), so the quotes can't be closed early.
        $displayEmbed = $pair.Embed.Embed.Replace('%', '%%')
        $tail += "$gotoLabel`r`n" +
                 "echo dev-team: $($pair.Label) was not found at `"$displayEmbed`" 1>&2`r`n" +
                 "echo This happens when this wrapper runs under a different Windows user than installed it, or after the Python moved -- re-run install-dev-team-windows.ps1 as this user. 1>&2`r`n" +
                 "exit /b 1`r`n"
        $i++
    }
    if ($guards -or $literalCaret) { $guards = "setlocal DisableDelayedExpansion`r`n" + $guards }
    return [ordered]@{ Guards = $guards; Tail = $tail }
}
function Assert-CmdWrapperPathsAscii {
    <# PR #9 review section 3: forge.cmd and deciders.cmd embed absolute
       paths ($WrapperPython, $ForgePy, $DecidersPy), and used to be
       written with -Encoding ascii, which turns C:\Users\<non-ASCII name>
       into '?'. Decision: REFUSE a non-ASCII path (exit 2, before
       anything is written), because no encoding is provably read back by
       cmd.exe on every machine:
         - cmd.exe reads a batch file as 8-bit text in the console's code
           page, not as Unicode: a UTF-16 batch file stops at its first NUL
           byte, and the fix is to save it as ANSI (Raymond Chen, Microsoft,
           "Diagnosing why your batch file prints a garbage character, one
           character, and nothing more", The Old New Thing, 2021-07-26,
           https://devblogs.microsoft.com/oldnewthing/20210726-00/?p=105483).
           So UTF-8 bytes are misread under any OEM/ANSI code page, and the
           ANSI code page of the installing machine is not the one of every
           console that later runs the wrapper.
         - `chcp 65001` inside the wrapper: chcp "Changes the active console
           code page" (https://learn.microsoft.com/windows-server/
           administration/windows-commands/chcp) -- the CALLER's console,
           left changed for whatever runs in it next -- and no Microsoft
           documentation states that cmd.exe decodes the rest of a running
           batch file in the new code page.
         - the 8.3 short name: short-name creation can be disabled per
           volume, and a file created while it was disabled has none
           (https://learn.microsoft.com/windows-server/administration/
           windows-commands/fsutil-8dot3name), so it may not exist.
       Never write a silently corrupted wrapper.

       D1-D5 (review follow-up, pr10-review.md section 4): a flat refusal
       made an install impossible for a user whose only Python is Hermes's
       own and whose checkout is under a non-ASCII %LOCALAPPDATA%/
       %USERPROFILE% -- the common case for a non-ASCII Windows user name.
       Decision: rewrite such a path to "%ENVVAR%\<remainder>" instead of
       refusing it outright (see Resolve-CmdEmbeddablePath for the exact
       rule and Get-CmdPrefixCandidates for where %ENVVAR% comes from) --
       cmd.exe expands %ENVVAR% from its own UTF-16 environment at run
       time, so the .cmd file on disk stays plain ASCII while the value
       that actually reaches python.exe is the real, non-ASCII one. Only
       REFUSE (still exit 2, nothing written) when no prefix a later
       cmd.exe can expand covers the path.
       D5: another Windows user's %LOCALAPPDATA%\hermes\tools\python-3.x\
       python.exe can exist and be THAT OTHER user's Hermes Python, not
       the installing user's one -- if a forge.cmd/deciders.cmd written
       under user A is somehow run under user B, who happens to have a
       same-named interpreter installed. Accepted rather than further
       mitigated, because: (a) both are Hermes-managed CPython builds laid
       down by the same installer code under the same naming scheme, so
       any behaviour difference is at most a CPython patch-level one (the
       `python-3.x` directory name pins the minor version, e.g.
       "python-3.12", so it is never a cross-minor-version mismatch); (b)
       forge.py/deciders.py are stdlib-only by design (no venv, no
       site-packages dependency), so there is nothing version-sensitive a
       different user's interpreter could get wrong; (c) the D4 guard
       below at least verifies the %ENVVAR%-expanded path EXISTS before
       running it, catching the more likely failure (moved/uninstalled
       Python) even though it cannot tell whose interpreter answered. This
       is the same class of residual risk the original "prefer the
       interpreter Hermes itself installed" choice already accepted for
       the single-user case -- "a valid Hermes Python ran this", just not
       necessarily the one this exact installer run resolved.
       C5 (review round 1): the same reasoning applies when forge.py or
       deciders.py itself is rewritten under %USERPROFILE% rather than
       %LOCALAPPDATA% -- under user B, "%USERPROFILE%\...\forge.py" also
       resolves into B's OWN checkout at that same relative path, so this
       is still "a valid install ran this", never some unrelated file.
       And a SYSTEM/service account that ends up running the wrapper gets
       its own %LOCALAPPDATA% under ...\systemprofile\AppData\Local,
       which almost never has a matching hermes\tools\python-3.x there,
       so the D4 guard fails closed (exit 1, no silent run) rather than
       accidentally picking up a real user's interpreter.
       C6 (review round 1) / m1 (round 2 review, fixed): if DelayedExpansion
       is enabled for cmd.exe (registry default, or the caller invoking
       this wrapper via `cmd /v:on`), a literal '!' inside an EXPANDED
       %ENVVAR% value is consumed by cmd's `!var!` syntax instead of
       being left alone, and '!' is a legal Windows user-name/path
       character. This is not new in this change -- it already affects
       any literal, non-rewritten ASCII path with '!' in v1.3.1. F4 (round
       3 review): this paragraph used to say DelayedExpansion was left as
       a known issue rather than adding `setlocal DisableDelayedExpansion`
       -- that was stale even when round 2 landed: Get-CmdGuardBlock DOES
       emit `setlocal DisableDelayedExpansion` (see its m1 note), and ONLY
       when there is at least one rewritten path, so an all-ASCII
       wrapper's bytes are unaffected. #>
    $prefixes = Get-CmdPrefixCandidates -HermesRoot $HermesRoot
    $embeds = [ordered]@{}
    foreach ($pair in @(
        @{ Key = 'WrapperPython'; Label = 'the wrapper Python'; Path = $WrapperPython },
        @{ Key = 'ForgePy';       Label = 'forge.py';            Path = $ForgePy },
        @{ Key = 'DecidersPy';    Label = 'deciders.py';         Path = $DecidersPy })) {
        $embeds[$pair.Key] = Resolve-CmdEmbeddablePath -Label $pair.Label -Path $pair.Path -Prefixes $prefixes
        # Round 2 (review): defence in depth -- Resolve-CmdEmbeddablePath
        # itself never returns an empty Embed (it either embeds the literal
        # path, embeds a non-empty "%NAME%\<remainder>", or exits 2), but a
        # caller that skips Assert-CmdWrapperPathsAscii (or a future bug in
        # that function) must never reach Set-Content with an empty embed --
        # cmd.exe would run `"" "<script>"`, i.e. no interpreter at all,
        # silently. Refuse instead of writing a corrupt wrapper.
        if (-not $embeds[$pair.Key].Embed) {
            [Console]::Error.WriteLine("Internal error: the embed for $($pair.Label) resolved empty; refusing to write forge.cmd/deciders.cmd rather than write a wrapper with no interpreter path. This should be unreachable -- please report this as a bug.")
            exit 2
        }
    }
    return $embeds
}
$script:WrapperPythonLauncherPath = $null
$WrapperPython = Resolve-WrapperPython
if (-not $WrapperPython) {
    if ($script:WrapperPythonLauncherPath) {
        # D8 (review): the py launcher DID report a Python 3 -- just not at
        # a path that currently resolves (wrong environment for this
        # account, or it moved) -- so this must not say "no Python 3
        # found", which would be inaccurate and send the user down the
        # wrong troubleshooting path.
        [Console]::Error.WriteLine("The py launcher reported a Python 3 at $(Format-EnvPinForDisplay $script:WrapperPythonLauncherPath), which does not exist (looked in $env:LOCALAPPDATA\hermes\tools, the py launcher, and PATH outside WindowsApps). Install Hermes or Python 3, or fix the py launcher's registration (py --list-paths), then re-run.")
    } else {
        [Console]::Error.WriteLine("No Python 3 found for the forge and deciders wrappers and the .env pin checker (looked in $env:LOCALAPPDATA\hermes\tools, the py launcher, and PATH outside WindowsApps). Install Hermes or Python 3, then re-run.")
    }
    exit 1
}
$CmdPaths = Assert-CmdWrapperPathsAscii
$WrapperPythonCmd = $CmdPaths.WrapperPython
$ForgePyCmd       = $CmdPaths.ForgePy
$DecidersPyCmd    = $CmdPaths.DecidersPy
Write-Host "wrapper python: $WrapperPython"

$PluginSrcMnemo = Join-Path $HermesRoot 'plugins\mnemosyne'
# The team's shared Mnemosyne bank: a dedicated directory, never the
# operator's own bank ($HermesRoot\mnemosyne, or whatever MNEMOSYNE_HOME
# points at -- that store is never linked into, read from, or written to by
# this installer). -TeamMemory overrides the location.
function Get-TeamMemoryLinkTarget {
    <#
      For one path component: $null if it does not exist or is not a
      link; otherwise the link's immediate target (a symlink OR a junction,
      live or dangling). Uses the reparse-point attribute so a dangling link
      is still seen as a link, then .NET 6+'s FileSystemInfo.LinkTarget
      (PowerShell 7) with Get-Item's .Target as the Windows PowerShell 5.1
      fallback.
    #>
    param([string] $Path)
    $fi = New-Object System.IO.DirectoryInfo $Path
    $attr = [int]$fi.Attributes          # -1 when nothing is there at all
    if ($attr -eq -1) { return $null }
    if (($attr -band [int][System.IO.FileAttributes]::ReparsePoint) -eq 0) { return $null }
    $t = $null
    if ($fi.PSObject.Properties['LinkTarget']) { $t = $fi.LinkTarget }
    if (-not $t) {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($item -and $item.Target) { $t = $item.Target }
    }
    if ($t -is [System.Array]) { $t = $t[0] }
    if (-not $t) { return $null }
    $ntPrefix = [string]::Concat([char]92, '?', '?', [char]92)
    if ($t.StartsWith($ntPrefix)) { $t = $t.Substring($ntPrefix.Length) }
    return $t
}
function Resolve-TeamMemoryRealPath {
    <#
      Resolves $Path to a canonical absolute path (realpath -m semantics).
      GetFullPath first (Windows' own lexical '.'/'..'/separator rules, and
      a relative value becomes absolute -- r2 finding #3). Then every
      component is walked from the root: wherever an existing prefix is a
      symlink or junction -- at ANY depth, not just the nearest existing
      ancestor (r3 review finding #2), and even when dangling -- its target
      replaces that prefix and the walk restarts on target + remaining
      components. Bounded to 40 hops: a link cycle throws (the caller
      refuses with exit 2) instead of spinning or returning a half-resolved
      path that could dodge the containment check.
    #>
    param([string] $Path)
    if (-not $Path) { return $Path }
    # Get-NormalizedPath trims a trailing backslash, which turns a bare
    # drive root 'C:\' into the drive-RELATIVE 'C:'; put it back.
    $fixRoot = { param($p) if ($p -match '^[A-Za-z]:$') { "$p\" } else { $p } }
    $full = & $fixRoot (Get-NormalizedPath $Path)
    $seps = [char[]]@([char]92, [char]47)
    $hops = 0
    while ($true) {
        $root = [System.IO.Path]::GetPathRoot($full)
        if (-not $root) { return $full }
        $parts = @($full.Substring($root.Length).Split($seps, [System.StringSplitOptions]::RemoveEmptyEntries))
        $cur = $root
        $restart = $null
        for ($i = 0; $i -lt $parts.Count; $i++) {
            $next = [System.IO.Path]::Combine($cur, $parts[$i])
            $target = Get-TeamMemoryLinkTarget $next
            if ($target) {
                $hops++
                if ($hops -gt 40) {
                    throw "Too many levels of symbolic links resolving: $Path"
                }
                if (-not [System.IO.Path]::IsPathRooted($target)) {
                    $target = [System.IO.Path]::Combine($cur, $target)
                }
                $rest = @($target) + @($parts | Select-Object -Skip ($i + 1))
                $restart = & $fixRoot (Get-NormalizedPath ([System.IO.Path]::Combine([string[]]$rest)))
                break
            }
            $cur = $next
        }
        if ($null -eq $restart) { break }
        $full = $restart
    }
    if ($cur -eq [System.IO.Path]::GetPathRoot($cur)) { return $cur }
    return (Get-NormalizedPath $cur)
}
function Test-SameFileIdentity {
    <#
      True if $A and $B denote the same real file-system object, by
      identity rather than by path string (review D4: 8.3 short names and
      `subst` drives both give a path a second name with no symlink or
      junction anywhere in it, so Resolve-TeamMemoryRealPath's
      link-resolution walk cannot see it).

      On Windows: volume serial number + file index (nFileIndexHigh/Low
      from GetFileInformationByHandle), the same identity pair
      GetFinalPathNameByHandle-based approaches rely on, via a tiny P/Invoke
      helper added once per session.

      On the non-Windows pwsh this test suite actually runs under (and on
      real PowerShell 7+ on Linux/macOS), the equivalent identity is
      device + inode, read with `stat`, so a bind mount -- the same class
      of alias the Unix installer's identity_overlap() guards against --
      is caught the same way here.
    #>
    param([string] $A, [string] $B)
    if (-not (Test-Path -LiteralPath $A) -or -not (Test-Path -LiteralPath $B)) { return $false }
    if ($IsWindows) {
        if (-not ([System.Management.Automation.PSTypeName]'DevTeamInstaller.NativeFileId').Type) {
            Add-Type -Namespace DevTeamInstaller -Name NativeFileId -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern System.IntPtr CreateFile(string lpFileName, uint dwDesiredAccess, uint dwShareMode,
    System.IntPtr lpSecurityAttributes, uint dwCreationDisposition, uint dwFlagsAndAttributes, System.IntPtr hTemplateFile);
[StructLayout(LayoutKind.Sequential)]
public struct BY_HANDLE_FILE_INFORMATION {
    public uint FileAttributes;
    public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
    public uint VolumeSerialNumber;
    public uint FileSizeHigh;
    public uint FileSizeLow;
    public uint NumberOfLinks;
    public uint FileIndexHigh;
    public uint FileIndexLow;
}
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool GetFileInformationByHandle(System.IntPtr hFile, out BY_HANDLE_FILE_INFORMATION lpFileInformation);
[DllImport("kernel32.dll", SetLastError = true)]
public static extern bool CloseHandle(System.IntPtr hObject);
'@
        }
        $getId = {
            param($p)
            $h = [DevTeamInstaller.NativeFileId]::CreateFile($p, 0, 7, [System.IntPtr]::Zero, 3, 0x02000000, [System.IntPtr]::Zero)
            if ($h -eq [System.IntPtr]::new(-1)) { return $null }
            try {
                $info = New-Object DevTeamInstaller.NativeFileId+BY_HANDLE_FILE_INFORMATION
                if (-not [DevTeamInstaller.NativeFileId]::GetFileInformationByHandle($h, [ref]$info)) { return $null }
                return "$($info.VolumeSerialNumber):$($info.FileIndexHigh):$($info.FileIndexLow)"
            } finally { [void][DevTeamInstaller.NativeFileId]::CloseHandle($h) }
        }
        $ida = & $getId $A
        $idb = & $getId $B
        if (-not $ida -or -not $idb) { return $false }
        return $ida -eq $idb
    }
    try {
        $sa = & stat -c '%d:%i' -- $A 2>$null
        $sb = & stat -c '%d:%i' -- $B 2>$null
    } catch { return $false }
    if (-not $sa -or -not $sb) { return $false }
    return $sa -eq $sb
}
function Test-IdentityOverlap {
    <# Either direction: true if $A or $B sits inside (or is) the other,
       by filesystem identity rather than path string -- checked at every
       EXISTING ancestor of the candidate against the other side's full
       path (never against the other side's own ancestors: that would
       match the shared Hermes root every path lives under). #>
    param([string] $Candidate, [string] $Outer)
    if (-not $Outer -or -not (Test-Path -LiteralPath $Outer)) { return $false }
    $probe = $Candidate
    while ($true) {
        if ($probe -and (Test-Path -LiteralPath $probe)) {
            if (Test-SameFileIdentity $probe $Outer) { return $true }
        }
        $parent = Split-Path -Parent $probe
        if (-not $parent -or $parent -eq $probe) { break }
        $probe = $parent
    }
    return $false
}
function Test-IdentityOverlapEither {
    param([string] $A, [string] $B)
    return (Test-IdentityOverlap -Candidate $A -Outer $B) -or (Test-IdentityOverlap -Candidate $B -Outer $A)
}
function Test-PathIsOrUnder {
    <# True if $Desc (normalized) equals $Anc or is nested inside it. #>
    param([string] $Anc, [string] $Desc)
    if (-not $Anc -or -not $Desc) { return $false }
    if ($Desc -ieq $Anc) { return $true }
    $sep = [System.IO.Path]::DirectorySeparatorChar
    return $Desc.StartsWith("$Anc$sep", [System.StringComparison]::OrdinalIgnoreCase)
}
function Get-EnvUnsafeValueReason {
    <# A PowerShell-side SUBSET of dev-team\scripts\env_pin_check.py. The
       checker (Get-EnvPinCheckerReason, below) is the authority: every pin
       goes through Assert-EnvPinSafe, which runs this first -- for the
       specific messages -- and then the checker. Every rule here is also
       a checker rule; the checker refuses strictly more: every
       str.splitlines() boundary (U+000B, U+000C, U+001C-U+001E, U+0085,
       U+2028, U+2029) anywhere, the Python-isspace() characters .NET's \s
       lacks (U+001C-U+001F) at either end or before '#', unassigned code
       points, and anything its transcribed readers read back differently.

       The pin format is unquoted KEY=value (Set-EnvVarPin, below) -- NOT
       python-dotenv's single-quote-escaped format, which does not round-
       trip through Mnemosyne's own .env reader (mnemosyne mcp_server.py
       _load_dotenv, :832 `v.strip().strip("'\"")`, which never unescapes
       `\\` or `\'`). Refused here:
         - an unpaired UTF-16 surrogate -- it has no UTF-8 encoding:
           Set-EnvVarPin's UTF8Encoding($false) would silently write U+FFFD
           instead (and outside Windows .NET hands the checker U+FFFD too,
           so there only this rule sees it);
         - a newline (CR or LF) -- would split into more than one .env line;
         - leading/trailing whitespace -- BOTH readers strip it, so what is
           read back is not the value pinned (nor the link target):
           python-dotenv 1.2.2 consumes whitespace after '=' (parser.py:24
           `_equal_sign` = `(=[^\S\r\n]*)`, read at :157) and rstrip()s an
           unquoted value (parser.py:123-125 parse_unquoted_value), and
           Mnemosyne's reader does `v.strip()` (mcp_server.py:832);
         - a leading/trailing quote character (' or ") -- Mnemosyne's
           `.strip("'\"")` removes any number of quote characters from each
           end, paired or not, while python-dotenv parses a leading quote
           as the start of a quoted value; neither gives the value back;
         - whitespace immediately followed by '#' -- python-dotenv's parser
           cuts the value there (parser.py:125 `\s+#.*`, Unicode-aware, so
           NBSP counts: `/t/x #y` becomes `/t/x`); Mnemosyne's reader does
           not;
         - a literal '${' -- python-dotenv (via env_loader's
           _resolve_dotenv_assignments) expands `${VAR}` in an unquoted
           value (review r3 round-2 coverage finding: `'C:\a\${HOME}\b'`
           comes back as `'C:\a\/home/<user>\b'` there), while
           Mnemosyne's reader does not expand it at all and returns the
           value verbatim; the two readers would disagree.
       .NET's \s, used below, is [\f\n\r\t\v\x85\p{Z}]: it covers NBSP but
       not U+001C-U+001F, and is only tested at the ends and before '#'.
       Returns $null when $Value is safe, otherwise a human-readable
       reason. #>
    param([string] $Value)
    try { [void](New-Object System.Text.UTF8Encoding($false, $true)).GetByteCount($Value) }
    catch { return 'contains an unpaired UTF-16 surrogate (it has no UTF-8 encoding)' }
    if ($Value -match "`r|`n") { return 'contains a newline' }
    if ($Value -match '^\s' -or $Value -match '\s$') { return 'starts or ends with whitespace' }
    if ($Value.Length -gt 0) {
        $first = $Value.Substring(0, 1)
        $last = $Value.Substring($Value.Length - 1, 1)
        if ($first -eq "'" -or $first -eq '"' -or $last -eq "'" -or $last -eq '"') {
            return 'starts or ends with a quote character'
        }
    }
    if ($Value -match '\s#') { return "contains whitespace immediately followed by '#'" }
    if ($Value.Contains('${')) { return 'contains a literal ${' }
    return $null
}
function Get-EnvPinCheckerReason {
    <# Runs dev-team\scripts\env_pin_check.py KEY with $WrapperPython. The
       value travels ONLY in $env:DEV_TEAM_ENV_CHECK_VALUE -- a UTF-16
       environment block on Windows, so it arrives exact, with no 5.1 argv
       quoting rules involved -- and the variable is removed afterwards.
       Exit 0: safe, $null. Exit 1: unsafe, one ASCII reason line on stdout.
       Anything else -- exit 2 (internal error), any other code, no Python,
       no checker -- fails closed: a reason is returned, so the caller
       refuses. #>
    param([string] $Key, [string] $Value)
    if (-not $WrapperPython) { return 'could not be checked: no Python was resolved to run env_pin_check.py' }
    if (-not $EnvPinCheckPy -or -not (Test-Path -LiteralPath $EnvPinCheckPy)) {
        return "could not be checked: env_pin_check.py is missing ($EnvPinCheckPy)"
    }
    $savedEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $rc = $null
    $out = @()
    try {
        $env:DEV_TEAM_ENV_CHECK_VALUE = $Value
        $out = @(& $WrapperPython -I $EnvPinCheckPy $Key 2>&1 | ForEach-Object { "$_" })
        $rc = $LASTEXITCODE
    } catch {
        $out = @($_.Exception.Message)
    } finally {
        Remove-Item -LiteralPath Env:DEV_TEAM_ENV_CHECK_VALUE -ErrorAction SilentlyContinue
        $ErrorActionPreference = $savedEap
    }
    $detail = (@($out | Where-Object { $_ }) -join ' ').Trim()
    if ($rc -eq 0) { return $null }
    if ($rc -eq 1 -and $detail) { return "is refused by env_pin_check.py: $(Format-EnvPinForDisplay $detail)" }
    if ($null -eq $rc) { $rc = 'not run' }
    return "could not be checked: env_pin_check.py exited $rc ($(Format-EnvPinForDisplay $detail))"
}
function Assert-EnvPinSafe {
    <# The one gate every .env pin passes, on the exact KEY and value: the
       PowerShell subset, then the checker. Unsafe, or not checkable:
       exit 2, naming the pin. #>
    param([string] $Key, [string] $Value, [string] $Subject)
    $reason = Get-EnvUnsafeValueReason $Value
    if (-not $reason) { $reason = Get-EnvPinCheckerReason -Key $Key -Value $Value }
    if ($reason) {
        [Console]::Error.WriteLine("$Subject`: refusing to pin $Key=$(Format-EnvPinForDisplay $Value): the value $reason; it would not read back identically from .env.")
        exit 2
    }
}
$OperatorMnemoHome = Join-Path $HermesRoot 'mnemosyne'
$rawTeamMemoryHome = if ($TeamMemory) { $TeamMemory } else { Join-Path $HermesRoot 'dev-team-mnemosyne' }
# F4a (review r3 minor #4, round 2 operator decision): refuse, rather than
# silently mis-pin, a -TeamMemory value that python-dotenv's real parser and
# Mnemosyne's own naive .env reader would not both read back identically.
# Same rule as the Unix installer (quote_env_value was reverted there to
# this same unquoted-with-refusal rule per the same decision). Checked on
# the raw argument, before path resolution, so this fires even when the
# value can't be resolved to a real path at all.
$_teamMemoryUnsafeReason = Get-EnvUnsafeValueReason $rawTeamMemoryHome
if ($_teamMemoryUnsafeReason) {
    [Console]::Error.WriteLine("-TeamMemory ($(Format-EnvPinForDisplay $rawTeamMemoryHome)) $_teamMemoryUnsafeReason; it would not read back identically from .env. Pick a path without that.")
    exit 2
}
# Every path below is canonical (no '.', '..', repeated separator, link or
# junction anywhere on it): the value checked is the value later passed to
# New-Item / New-DirLink.
try {
    $TeamMemoryHome = Resolve-TeamMemoryRealPath $rawTeamMemoryHome
    $_operatorHomeReal = Resolve-TeamMemoryRealPath $OperatorMnemoHome
    $_mnemosyneHomeReal = if ($env:MNEMOSYNE_HOME) { Resolve-TeamMemoryRealPath $env:MNEMOSYNE_HOME } else { $null }
    # D1: MNEMOSYNE_DATA_DIR is the variable that actually decides the data
    # directory the installed Mnemosyne package uses (mnemosyne/cli.py:20-23,
    # mnemosyne/core/beam.py:358-366 both check it before anything else).
    # The Windows Mnemosyne installer persists it as a User-scope
    # environment variable whenever the bank was relocated with
    # MNEMOSYNE_HOME (install-mnemosyne-hermes-windows.ps1:302-304), so a
    # new process picks it up even without the live session variable --
    # check both.
    $_mnemosyneDataDirRaw = if ($env:MNEMOSYNE_DATA_DIR) { $env:MNEMOSYNE_DATA_DIR }
        else { [Environment]::GetEnvironmentVariable('MNEMOSYNE_DATA_DIR', 'User') }
    $_mnemosyneDataDirReal = if ($_mnemosyneDataDirRaw) { Resolve-TeamMemoryRealPath $_mnemosyneDataDirRaw } else { $null }
    $HermesRootProfilesReal = Resolve-TeamMemoryRealPath (Join-Path $HermesRoot 'profiles')
    $HermesRootDevTeamReal  = Resolve-TeamMemoryRealPath (Join-Path $HermesRoot 'dev-team')
} catch {
    [Console]::Error.WriteLine("-TeamMemory: cannot resolve the path: $($_.Exception.Message)")
    exit 2
}
# F4a round-3 (reviewer repro): the refusal above (line 888) only checked
# the RAW -TeamMemory argument. A symlink/junction whose target contains
# an unsafe shape, or a relative argument resolved under a cwd/
# $HermesRoot that contains one, passed that check untouched and was
# pinned anyway -- Set-EnvVarPin writes the RESOLVED $TeamMemoryHome
# (used below at the Set-MnemosyneDataDirPin call sites), not the raw
# string. Check the resolved value too, right after the resolve block
# and before any side effect (New-Item, Set-EnvVarPin, etc. all happen
# later). Also check $HermesRootProfilesReal: every profile-local pin
# (Set-MnemosyneDataDirPin -ProfileDir $profileDir/-DataDir ...) derives
# its path from $HermesRoot\profiles, so an unsafe $HermesRoot pins an
# unsafe value even when -TeamMemory itself is perfectly safe.
$_teamMemoryHomeUnsafeReason = Get-EnvUnsafeValueReason $TeamMemoryHome
if ($_teamMemoryHomeUnsafeReason) {
    [Console]::Error.WriteLine("-TeamMemory resolves to a path that $_teamMemoryHomeUnsafeReason; it would not read back identically from .env. Pick a path (or, if it's a link, a link target) without that.")
    exit 2
}
$_hermesRootProfilesUnsafeReason = Get-EnvUnsafeValueReason $HermesRootProfilesReal
if ($_hermesRootProfilesUnsafeReason) {
    [Console]::Error.WriteLine("`$HermesRoot\profiles ($HermesRootProfilesReal) $_hermesRootProfilesUnsafeReason; profile-local Mnemosyne pins derived from it would not read back identically from .env.")
    exit 2
}
# A3: the checker (env_pin_check.py, authoritative) on every value this run
# will pin, with its KEY, before any side effect: the team bank's two pins,
# and each selected profile's own-bank pins (written when a profile keeps or
# is given a private bank). Set-MnemosyneDataDirPin / Set-EnvVarPin check the
# exact written values again.
Assert-EnvPinSafe 'MNEMOSYNE_HOME' $TeamMemoryHome '-TeamMemory (the team bank)'
Assert-EnvPinSafe 'MNEMOSYNE_DATA_DIR' (Join-Path $TeamMemoryHome 'data') '-TeamMemory (the team bank data directory)'
if ($Profiles) {
    foreach ($role in $Profiles.Keys) {
        if (-not (Test-Selected $role)) { continue }
        $_ownBank = Join-Path (Join-Path $HermesRootProfilesReal "$Prefix$role") 'mnemosyne'
        Assert-EnvPinSafe 'MNEMOSYNE_HOME' $_ownBank "$Prefix$role (its own Mnemosyne bank)"
        Assert-EnvPinSafe 'MNEMOSYNE_DATA_DIR' (Join-Path $_ownBank 'data') "$Prefix$role (its own Mnemosyne data directory)"
    }
}
$_teamHomeReal = $TeamMemoryHome
function Test-TouchesOperatorBank {
    <# True if $Target (canonical) is, sits inside, or contains any
       operator location -- the operator bank, MNEMOSYNE_HOME, or
       MNEMOSYNE_DATA_DIR, whichever are set -- by path containment or by
       filesystem identity (D4). #>
    param([string] $Target)
    foreach ($loc in @($_operatorHomeReal, $_mnemosyneHomeReal, $_mnemosyneDataDirReal)) {
        if (-not $loc) { continue }
        if ((Test-PathIsOrUnder -Anc $loc -Desc $Target) -or (Test-PathIsOrUnder -Anc $Target -Desc $loc) -or
            (Test-IdentityOverlapEither $Target $loc)) {
            return $true
        }
    }
    return $false
}
$_teamMemoryRefusal = $null
$_refusalPairs = @(
    @{ Name = "the operator's own bank ($OperatorMnemoHome)"; Real = $_operatorHomeReal },
    @{ Name = "MNEMOSYNE_HOME ($($env:MNEMOSYNE_HOME))"; Real = $_mnemosyneHomeReal },
    @{ Name = "MNEMOSYNE_DATA_DIR ($_mnemosyneDataDirRaw)"; Real = $_mnemosyneDataDirReal }
)
foreach ($pair in $_refusalPairs) {
    if (-not $pair.Real) { continue }
    if ((Test-PathIsOrUnder -Anc $pair.Real -Desc $_teamHomeReal) -or
        (Test-PathIsOrUnder -Anc $_teamHomeReal -Desc $pair.Real) -or
        (Test-IdentityOverlapEither $_teamHomeReal $pair.Real)) {
        $_teamMemoryRefusal = $pair.Name
        break
    }
}
# D5: -TeamMemory under $HermesRoot\profiles or $HermesRoot\dev-team breaks
# every profile's memory (review repro: pointed at a profile's own
# mnemosyne\, the link ends up pointing at itself).
if (-not $_teamMemoryRefusal) {
    if ((Test-PathIsOrUnder -Anc $HermesRootProfilesReal -Desc $_teamHomeReal) -or
        (Test-IdentityOverlap -Candidate $_teamHomeReal -Outer $HermesRootProfilesReal)) {
        $_teamMemoryRefusal = "a Hermes profile directory ($(Join-Path $HermesRoot 'profiles'))"
    } elseif ((Test-PathIsOrUnder -Anc $HermesRootDevTeamReal -Desc $_teamHomeReal) -or
              (Test-IdentityOverlap -Candidate $_teamHomeReal -Outer $HermesRootDevTeamReal)) {
        $_teamMemoryRefusal = "the dev-team state directory ($(Join-Path $HermesRoot 'dev-team'))"
    }
}
if ($_teamMemoryRefusal) {
    [Console]::Error.WriteLine("-TeamMemory must not point at, be nested in, or contain $_teamMemoryRefusal.")
    exit 2
}
# F1 (review r3): also pin MNEMOSYNE_HOME alongside MNEMOSYNE_DATA_DIR.
# MNEMOSYNE_DATA_DIR only steers the CLI (mnemosyne/cli.py:20-23) and
# call-time modules (core/banks.py, core/triples.py, core/canonical.py,
# core/media.py); it does NOT steer the MCP shared-surface DB or
# federation, which both read MNEMOSYNE_HOME directly
# (mnemosyne/mcp_tools.py:167-173, core/fleet_census.py:149-155,
# core/federation_handshake.py:263-267, mcp_server.py:799). An inherited
# MNEMOSYNE_HOME therefore still leaked the operator's shared-surface DB
# path into a dev-team profile even after MNEMOSYNE_DATA_DIR was pinned.
# D1: pin this profile's own Mnemosyne data directory into its own .env
# (see Set-MnemosyneDataDirPin below, called once per profile) so an
# inherited MNEMOSYNE_DATA_DIR from the launching shell, the gateway, or
# the Windows User-scope environment cannot redirect it away from the
# bank this installer just linked. hermes_cli/env_loader.py:502-562
# (load_hermes_dotenv) loads a profile's own .env with override=True
# specifically because "a profile's .env overrides stale shell exports".
function Set-EnvVarPin {
    <# Rewrite a single VAR=value line in a profile's .env, preserving
       every other line byte-exact, via a read/compare/write that goes
       through [IO.File]::ReadAllLines/WriteAllLines with an explicit
       UTF8Encoding($false) -- never Get-Content/Set-Content/Add-Content
       with no -Encoding, and never a regex substitution built from the
       value itself (so a bank path containing regex-special characters
       is written verbatim -- same concern F4 raised for the Unix sed
       version).
       On Windows PowerShell 5.1 (the CI target) those cmdlets default to
       the ANSI code page with no -Encoding: that both garbles a
       non-ASCII bank path on write (review r3 minor #1 -- a non-ASCII
       path would turn into '?' bytes, Hermes would then open a fresh,
       empty DB there) and misreads an already-non-ASCII existing line (a
       `--clone`d operator .env) back into memory before this function
       rewrites the file. 5.1 also has no `utf8NoBOM` value for
       -Encoding, so even an explicit `-Encoding UTF8` there would add a
       BOM; Hermes' own loader tolerates a UTF-8 BOM
       (hermes_cli/env_loader.py opens with `encoding="utf-8-sig"`), but
       writing none is the simpler invariant and matches what Hermes
       itself writes. [IO.File]::ReadAllLines auto-detects a BOM already
       on disk and otherwise falls back to the UTF-8 encoding passed in,
       so a pre-existing BOM-less UTF-8 file (what this function always
       writes) decodes correctly either way.

       Accepted lossy case (round-1 review coverage #4): ReadAllLines /
       WriteAllLines normalise line terminators to Environment.NewLine
       and drop an EOF trailing-newline difference, and any byte that is
       not valid UTF-8 in a pre-existing .env (e.g. an ANSI file left by
       an older install run under PS 5.1) decodes as U+FFFD and is
       rewritten lossy. Line *content* stays byte-exact for valid UTF-8,
       which is all item 1 (above) required; a non-UTF-8 existing file is
       an already-broken precondition this function does not try to
       repair. #>
    param([string] $ProfileDir, [string] $VarName, [string] $Value)
    # F4a defense-in-depth (round-3 reviewer finding), A3: the preflight
    # above checks the values this run is expected to pin. Re-check the
    # exact value actually being written here too -- the PowerShell subset
    # and env_pin_check.py, with this KEY -- so no call site, present or
    # future, can pin an unsafe value by constructing it some other way.
    Assert-EnvPinSafe $VarName $Value "$ProfileDir\.env"
    New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
    $envFile = Join-Path $ProfileDir '.env'
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $line = "$VarName=$Value"
    $prefix = "$VarName="
    if (Test-Path -LiteralPath $envFile) {
        $existingLines = [System.IO.File]::ReadAllLines($envFile, [System.Text.Encoding]::UTF8)
        $found = $false
        $newLines = @($existingLines | ForEach-Object {
            if ($_.StartsWith($prefix)) { $found = $true; $line } else { $_ }
        })
        if (-not $found) { $newLines += $line }
        [System.IO.File]::WriteAllLines($envFile, $newLines, $utf8NoBom)
    } else {
        [System.IO.File]::WriteAllLines($envFile, @($line), $utf8NoBom)
    }
}
function Set-MnemosyneDataDirPin {
    param([string] $ProfileDir, [string] $DataDir)
    # F1: DataDir is always "<bank>\data" at every call site, so its
    # parent is exactly the bank root MNEMOSYNE_HOME must name.
    $homeDir = Split-Path -Parent $DataDir
    # A3: before the dry-run branch, so -DryRun reports the refusal the real
    # run would hit.
    Assert-EnvPinSafe 'MNEMOSYNE_DATA_DIR' $DataDir "$ProfileDir\.env"
    Assert-EnvPinSafe 'MNEMOSYNE_HOME' $homeDir "$ProfileDir\.env"
    if ($DryRun) {
        Write-Host "  would pin in .env: MNEMOSYNE_DATA_DIR=$DataDir"
        Write-Host "  would pin in .env: MNEMOSYNE_HOME=$homeDir"
        return
    }
    Set-EnvVarPin -ProfileDir $ProfileDir -VarName 'MNEMOSYNE_DATA_DIR' -Value $DataDir
    Set-EnvVarPin -ProfileDir $ProfileDir -VarName 'MNEMOSYNE_HOME' -Value $homeDir
}
# Team state (forge project records, team clones) must be visible to every
# role: devops writes the project record at onboarding, pr-ci reads it to
# open the PR, and the guard reads it from every other stage to verify the
# default branch before letting a push through. Shared unconditionally - this
# is pipeline state, not personal memory, so -SeparateMemory does not affect
# it.
$DevTeamHome = Join-Path $HermesRoot 'dev-team'

if (-not (Test-Path -LiteralPath $PluginSrcMnemo)) {
    Write-Error @"
Mnemosyne plugin not found at $PluginSrcMnemo.
Run ..\..\install-mnemosyne-hermes-windows.ps1 first.
"@
    exit 1
}

# The provider must be active on the root profile before profiles are cloned
# from it: `hermes profile create --clone` copies config.yaml as a file, so
# whatever memory settings are in place at clone time are what each profile
# gets. The design requires the installer to refuse outright rather than
# create profiles with no working memory.
$Provider = Get-HermesLastLine (Invoke-Hermes 'config' 'get' 'memory.provider')
if ($Provider -ne 'mnemosyne') {
    Write-Error @"
memory.provider on the root profile is '$Provider', not 'mnemosyne'.
Run ..\..\install-mnemosyne-hermes-windows.ps1 -DisableBuiltinMemory first,
so the profiles clone a config that already points at Mnemosyne.
"@
    exit 1
}

foreach ($key in @('memory.memory_enabled', 'memory.user_profile_enabled')) {
    $val = Get-HermesLastLine (Invoke-Hermes 'config' 'get' $key)
    if ($val -and $val.ToLowerInvariant() -ne 'false') {
        Write-Warning @"
$key is '$val' on the root profile. Cloned profiles will inherit it and run
Hermes' built-in MEMORY.md / USER.md store alongside Mnemosyne. Re-run the main
installer with -DisableBuiltinMemory to turn it off.
"@
    }
}

$modelLabel = if ($SkipModel) { '(unchanged)' } else { $Model }

# forge.cmd goes next to hermes.exe. Another tool may already own the name
# `forge` there: never overwrite a file this installer did not write.
# Checked before any profile is touched, so a refusal leaves nothing half
# installed.
$ForgeMarker  = 'REM dev-team forge wrapper (install-dev-team-windows.ps1)'
$hermesCmd    = Get-Command hermes -ErrorAction Stop
$hermesDir    = Split-Path -Parent $hermesCmd.Source
$forgeCmdPath = Join-Path $hermesDir 'forge.cmd'
if (Test-Path -LiteralPath $forgeCmdPath) {
    $currentForge = Get-Content -LiteralPath $forgeCmdPath -Raw -ErrorAction SilentlyContinue
    $ours = $currentForge -and (
        $currentForge.Contains($ForgeMarker) -or
        ($currentForge -match '(?m)^python "[^"]*\\dev-team\\forge\\forge\.py" %\*'))
    if (-not $ours) {
        [Console]::Error.WriteLine("$forgeCmdPath already exists and was not written by this installer.`nRefusing to overwrite another tool's 'forge'. Move it aside, then re-run.")
        exit 1
    }
}
# deciders.cmd goes beside forge.cmd, same guard.
$DecidersMarker  = 'REM dev-team deciders wrapper (install-dev-team-windows.ps1)'
$decidersCmdPath = Join-Path $hermesDir 'deciders.cmd'
if (Test-Path -LiteralPath $decidersCmdPath) {
    $currentDeciders = Get-Content -LiteralPath $decidersCmdPath -Raw -ErrorAction SilentlyContinue
    $oursDeciders = $currentDeciders -and (
        $currentDeciders.Contains($DecidersMarker) -or
        ($currentDeciders -match '(?m)^python "[^"]*\\dev-team\\deciders\\deciders\.py" %\*'))
    if (-not $oursDeciders) {
        [Console]::Error.WriteLine("$decidersCmdPath already exists and was not written by this installer.`nRefusing to overwrite another tool's 'deciders'. Move it aside, then re-run.")
        exit 1
    }
}
# T4 migration: scan every dev-* profile, not just the ones -Only selected.
# -Only only limits which profiles this run *provisions*; a profile left
# out by -Only can still have a live dev-team junction, and renaming the
# old location out from under it would leave that junction dangling
# (review finding #1). Resolve-TeamHomeMigrationPlan collects every
# *distinct* populated old location instead of stopping at the first, so a
# second, unrelated populated home is reported rather than silently
# orphaned (review finding #2), and compares targets normalized (review
# finding #3), so a junction that already points at the correct root is
# never mistaken for an old one.
$CandidateTargets = @{}
# N3 (review): pass every directory under $HermesRoot\profiles to
# Get-UnsafeOldHomeReason, not only the $Prefix$role ones this loop scans
# for live dev-team junctions -- matching the Unix installer's
# profiles/*/ glob so a candidate equal to any other profile directory
# (dev-team-prefixed or not) is refused.
$ProfileDirs = Get-AllProfileDirs -HermesRoot $HermesRoot
foreach ($role in $Profiles.Keys) {
    $candidateName = "$Prefix$role"
    Invoke-Hermes 'profile' 'show' $candidateName | Out-Null
    if ($LASTEXITCODE -ne 0) { continue }
    $candidateDir = Split-Path -Parent (Get-HermesLastLine (Invoke-Hermes '-p' $candidateName 'config' 'path'))
    $candidateLink = Join-Path $candidateDir 'dev-team'
    $linkItem = Get-Item -LiteralPath $candidateLink -Force -ErrorAction SilentlyContinue
    if (-not $linkItem -or -not $linkItem.LinkType) { continue }
    $candidateTarget = $linkItem.Target | Select-Object -First 1
    if (-not $candidateTarget) { continue }
    $CandidateTargets[$candidateName] = $candidateTarget
}

# D4 (review): the root dev-team path itself may be a junction into one of
# the profiles -- its target is then also an old-home candidate, and
# $DevTeamHomeIsLink tells Resolve-TeamHomeMigrationPlan/Invoke-TeamHome
# Migration not to double-count or write through that link.
$DevTeamHomeItem = Get-Item -LiteralPath $DevTeamHome -Force -ErrorAction SilentlyContinue
$DevTeamHomeIsLink = [bool]($DevTeamHomeItem -and $DevTeamHomeItem.LinkType)
if ($DevTeamHomeIsLink) {
    $rootLinkTarget = $DevTeamHomeItem.Target | Select-Object -First 1
    if ($rootLinkTarget) { $CandidateTargets['__root_link__'] = $rootLinkTarget }
}

$Plan = Resolve-TeamHomeMigrationPlan -DevTeamHome $DevTeamHome -CandidateTargets $CandidateTargets `
    -HermesRoot $HermesRoot -ProfileDirs $ProfileDirs -DevTeamHomeIsLink:$DevTeamHomeIsLink
if ($Plan.Outcome -eq 'invalid' -or $Plan.Outcome -eq 'conflict') {
    [Console]::Error.WriteLine($Plan.Message)
    exit 2
}

$AffectedProfileNames = @()
if ($Plan.Outcome -eq 'move') {
    $AffectedProfileNames = $Plan.AffectedProfiles
    $MigrationResult = Invoke-TeamHomeMigration -DevTeamHome $DevTeamHome -OldTeamHome $Plan.OldTeamHome -DryRun:$DryRun -DevTeamHomeIsLink:$DevTeamHomeIsLink
} else {
    $MigrationResult = Invoke-TeamHomeMigration -DevTeamHome $DevTeamHome -OldTeamHome $null -DryRun:$DryRun -DevTeamHomeIsLink:$DevTeamHomeIsLink
}

if ($MigrationResult.Choice -eq 'move' -and $AffectedProfileNames.Count -gt 0) {
    # Every profile that pointed at the old home, not only the ones -Only
    # selected (review finding #1): the old location was just renamed out
    # from under them, so leaving their junction alone would make it
    # dangling. The D3 junction above already keeps it resolving, but a
    # direct relink shortens the chain.
    foreach ($affectedName in $AffectedProfileNames) {
        if ($affectedName -eq '__root_link__') { continue }
        if ($DryRun) {
            Write-Host "  would relink: $affectedName/dev-team -> $DevTeamHome"
            continue
        }
        $affectedDir = Split-Path -Parent (Get-HermesLastLine (Invoke-Hermes '-p' $affectedName 'config' 'path'))
        $affectedLink = Join-Path $affectedDir 'dev-team'
        New-DirLink -LinkPath $affectedLink -Target $DevTeamHome -Label 'the shared dev-team home' | Out-Null
        Write-Host "  relinked: $affectedName/dev-team -> $DevTeamHome"
    }
}

Write-Host "Hermes home:   $HermesRoot"
Write-Host "Mnemosyne:     $PluginSrcMnemo"
Write-Host "dev-team-gate: $PluginSrcGate"
Write-Host "Model:         $modelLabel"
Write-Host "Name prefix:   $Prefix"
$memLabel = if ($SeparateMemory) { 'separate per profile' } else { "team bank at $TeamMemoryHome" }
Write-Host "Memory:        $memLabel"
Write-Host "Team state:    $DevTeamHome (shared across every profile)"
Write-Host ''

# deciders tier table: $DevTeamHome\model-tiers.json is the same file
# $HERMES_HOME\dev-team\model-tiers.json resolves to in every installed
# profile, via the dev-team junction each profile gets below (step 7).
$ModelTiersPath = Join-Path $DevTeamHome 'model-tiers.json'
$ModelTiers = [ordered]@{}
if ($TierFastProvider) { $ModelTiers['fast'] = [ordered]@{ provider = $TierFastProvider; model = $TierFastModel } }
$ModelTiers['standard'] = [ordered]@{ provider = $TierStandardProvider; model = $TierStandardModel }
$ModelTiers['deep']     = [ordered]@{ provider = $TierDeepProvider; model = $TierDeepModel }
$ModelTiersJson = $ModelTiers | ConvertTo-Json -Depth 4
Write-Host 'Tier table:'
$fastLabel = if ($TierFastProvider) { "$TierFastProvider`:$TierFastModel" } else { '(absent)' }
Write-Host "  fast:     $fastLabel"
Write-Host "  standard: $TierStandardProvider`:$TierStandardModel"
Write-Host "  deep:     $TierDeepProvider`:$TierDeepModel"
if ($DryRun) {
    Write-Host "  would write: $ModelTiersPath"
    ($ModelTiersJson -split "`r?`n") | ForEach-Object { Write-Host "    $_" }
} else {
    New-Item -ItemType Directory -Path $DevTeamHome -Force | Out-Null
    Set-Content -LiteralPath $ModelTiersPath -Value $ModelTiersJson -Encoding ascii
}
Write-Host ''

$created  = 0
$updated  = 0
$copied   = @()
$unshared = @()
$adopted  = @()

if (-not $SeparateMemory) {
    if ($DryRun) {
        if (-not (Test-Path -LiteralPath $TeamMemoryHome)) { Write-Host "  would create: $TeamMemoryHome" }
    } else {
        New-Item -ItemType Directory -Path $TeamMemoryHome -Force | Out-Null
    }
}
# One timestamp per run, reused for every profile -AdoptTeamMemory backs up:
# distinct profiles live in distinct directories, so a shared timestamp
# cannot collide.
$AdoptTimestamp = Get-Date -Format 'yyyyMMddHHmmss'

function Invoke-MnemosyneDataLink {
    <#
      D6: the whole per-profile Mnemosyne data-link decision, in one
      function (the same move Resolve-TeamHomeMigrationPlan made for the
      team-home junction), so it can be called -- and pwsh-tested -- the
      same way whether $DryRun is set or not, instead of duplicating the
      decision inline in both the dry-run early-exit and the real path.

      D2: canonicalises $ProfileDir and $ProfileDir\mnemosyne BEFORE any
      Move-Item/Remove-Item/New-DirLink, and refuses rather than acts if
      the profile directory resolves outside $HermesRoot\profiles, or the
      mnemosyne directory resolves to the operator's own bank, or to the
      team bank already.

      D3: an existing backup path (dir or junction) is never merged into;
      a numeric suffix is appended until the name is free.

      D1: pins MNEMOSYNE_DATA_DIR into the profile's own .env on every
      branch that leaves it either freshly linked or kept private, via
      Set-MnemosyneDataDirPin (defined above, beside the refusal logic).
    #>
    param(
        [string] $ProfileDir, [string] $Name, [bool] $Exists,
        [string] $TeamMemoryHome, [bool] $AdoptTeamMemory, [string] $AdoptTimestamp,
        [bool] $DryRun, [string] $HermesRootProfilesReal
    )
    $result = @{ Copied = $false; Adopted = $false; Unshared = $false }
    $profileDirReal = $null
    try { $profileDirReal = Resolve-TeamMemoryRealPath $ProfileDir } catch {
        [Console]::Error.WriteLine("Cannot resolve $Name's profile directory: $ProfileDir")
        exit 1
    }
    # F2 equality (review r3 minor #4): Test-PathIsOrUnder treats Desc -ieq
    # Anc as is-or-under, so it alone would accept a profile dir that
    # resolves to EXACTLY $HermesRoot\profiles (dev-qa -> profiles), not
    # just one nested under it. Refuse that explicitly too.
    if (((-not (Test-PathIsOrUnder -Anc $HermesRootProfilesReal -Desc $profileDirReal)) `
            -and (-not (Test-IdentityOverlap -Candidate $profileDirReal -Outer $HermesRootProfilesReal))) `
            -or ($profileDirReal -ieq $HermesRootProfilesReal)) {
        [Console]::Error.WriteLine("$Name's profile directory ($ProfileDir) resolves outside, or equal to, `$HermesRoot\profiles; refusing to touch its whole profile.")
        exit 1
    }
    $dataLink = Join-Path $ProfileDir 'mnemosyne'
    $existing = Get-Item -LiteralPath $dataLink -ErrorAction SilentlyContinue
    $isLink   = [bool]($existing -and $existing.LinkType)
    if ($existing -and -not $isLink -and $Exists) {
        $profileMnemoReal = $null
        try { $profileMnemoReal = Resolve-TeamMemoryRealPath $dataLink } catch {
            [Console]::Error.WriteLine("Cannot resolve $Name's mnemosyne\: $dataLink")
            exit 1
        }
        if (Test-TouchesOperatorBank $profileMnemoReal) {
            [Console]::Error.WriteLine("$Name's mnemosyne\ ($dataLink) resolves to the operator's own bank; refusing to move, remove or relink it.")
            exit 1
        }
        if (($profileMnemoReal -ieq $_teamHomeReal) -or (Test-IdentityOverlapEither $profileMnemoReal $_teamHomeReal)) {
            [Console]::Error.WriteLine("$Name's mnemosyne\ ($dataLink) already resolves to the team bank; refusing to move it.")
            exit 1
        }
        if ($AdoptTeamMemory) {
            # D3: an existing backup path (dir or junction) is never merged into.
            $backupDir = Join-Path $ProfileDir "mnemosyne.pre-team-memory-$AdoptTimestamp"
            $suffix = 0
            while (Test-Path -LiteralPath $backupDir) {
                $suffix++
                $backupDir = Join-Path $ProfileDir "mnemosyne.pre-team-memory-$AdoptTimestamp.$suffix"
            }
            if ($DryRun) {
                Write-Host "  would back up: $dataLink -> $backupDir"
                Write-Host "  would link: $dataLink -> $TeamMemoryHome"
                Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $TeamMemoryHome 'data')
                $result.Adopted = $true
                return $result
            }
            Move-Item -LiteralPath $dataLink -Destination $backupDir
            $kind = New-DirLink -LinkPath $dataLink -Target $TeamMemoryHome -Label 'the Mnemosyne data store'
            if ($kind -eq 'copy') { $result.Copied = $true }
            Write-Host "  ADOPTED: $Name's private mnemosyne\ moved to $backupDir"
            Write-Host '           (backup, not deleted) and linked to the team bank.'
            Write-Host '           Merging its old memories into the team bank is out'
            Write-Host '           of scope for this installer.'
            $result.Adopted = $true
            Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $TeamMemoryHome 'data')
            return $result
        }
        if ($DryRun) {
            Write-Host "  would leave: $dataLink (private, real directory;"
            Write-Host '               re-run with -AdoptTeamMemory to move it aside and share)'
            Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $dataLink 'data')
            $result.Unshared = $true
            return $result
        }
        Write-Host "  NOTE: $dataLink is a real directory with its own database."
        Write-Host '        Left as-is - this profile will not share the team bank.'
        Write-Host '        Re-run with -AdoptTeamMemory to move it aside (as a'
        Write-Host '        backup) and link to the team bank, or remove/merge it'
        Write-Host '        yourself, then re-run.'
        $result.Unshared = $true
        Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $dataLink 'data')
        return $result
    }
    if ($DryRun) {
        Write-Host "  would link: $dataLink -> $TeamMemoryHome"
        Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $TeamMemoryHome 'data')
        return $result
    }
    if ($isLink) {
        $prevTarget = $null
        try { $prevTarget = Resolve-TeamMemoryRealPath $dataLink } catch { }
        if ($prevTarget -and (Test-TouchesOperatorBank $prevTarget)) {
            Write-Host "  NOTE: $dataLink was linked to the operator's own bank;"
            Write-Host '        repointing to the team bank. The operator''s bank is'
            Write-Host '        untouched, but this profile loses access to memories'
            Write-Host '        only there.'
        }
    }
    $kind = New-DirLink -LinkPath $dataLink -Target $TeamMemoryHome -Label 'the Mnemosyne data store'
    if ($kind -eq 'copy') { $result.Copied = $true }
    Set-MnemosyneDataDirPin -ProfileDir $ProfileDir -DataDir (Join-Path $TeamMemoryHome 'data')
    return $result
}

foreach ($role in $Profiles.Keys) {
    if (-not (Test-Selected $role)) { continue }

    $name = "$Prefix$role"
    $desc = $Profiles[$role]
    $soul = Join-Path $SoulsDir "SOUL-$role.md"

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
    # $HermesRoot\profiles\$name - except for a profile this run just
    # created (or would create under -DryRun), where the path is always
    # predictable and resolving it via `hermes -p $name config path` would
    # just be one more unnecessary `-p $name` invocation.
    if (-not $exists) {
        $profileDir = Join-Path $HermesRoot "profiles\$name"
    } else {
        $profileDir = Split-Path -Parent (Get-HermesLastLine (Invoke-Hermes '-p' $name 'config' 'path'))
    }

    # F2 (review r3): canonicalise $profileDir itself FIRST, before SOUL,
    # the prefix file, or any link -- not just before the mnemosyne\ link
    # as D2 originally had it (that check lived inside
    # Invoke-MnemosyneDataLink, called further down). Windows is worse
    # than Unix here: step 4's New-DirLink on plugins\mnemosyne takes a
    # Remove-Item -Recurse branch when the target is a real directory,
    # which deleted the operator's actual plugin directory in the
    # reviewer's repro (profiles\dev-qa -> $HermesRoot) before the old,
    # later check ever ran.
    $profileDirReal = $null
    try { $profileDirReal = Resolve-TeamMemoryRealPath $profileDir } catch {
        [Console]::Error.WriteLine("Cannot resolve $name's profile directory: $profileDir")
        exit 1
    }
    if (((-not (Test-PathIsOrUnder -Anc $HermesRootProfilesReal -Desc $profileDirReal)) `
            -and (-not (Test-IdentityOverlap -Candidate $profileDirReal -Outer $HermesRootProfilesReal))) `
            -or ($profileDirReal -ieq $HermesRootProfilesReal)) {
        [Console]::Error.WriteLine("$name's profile directory ($profileDir) resolves outside, or equal to, `$HermesRoot\profiles; refusing to touch its whole profile.")
        exit 1
    }

    # 2. SOUL
    if ($KeepSoul -and (Test-Path -LiteralPath (Join-Path $profileDir 'SOUL.md'))) {
        Write-Host '  SOUL.md kept (-KeepSoul)'
    } elseif ($DryRun) {
        Write-Host "  would copy: SOUL-$role.md -> $profileDir\SOUL.md"
    } else {
        Copy-Item -LiteralPath $soul -Destination (Join-Path $profileDir 'SOUL.md') -Force
    }

    # 2b. The team prefix, so skills and souls can build prefixed assignee
    # names (`<prefix>qa`, never a bare `qa`, which the dispatcher would hand
    # to a different, unguarded profile). Per profile, not in the shared team
    # home: two teams with different prefixes share that directory.
    $prefixFile = Join-Path $profileDir 'dev-team-prefix'
    if ($DryRun) {
        Write-Host "  would write: $prefixFile ($Prefix)"
    } else {
        Set-Content -LiteralPath $prefixFile -Value "$Prefix`n" -NoNewline -Encoding ascii
    }

    if ($DryRun) {
        Write-Host "  would link: $profileDir\plugins\mnemosyne -> $PluginSrcMnemo"
        if (-not $SeparateMemory) {
            Invoke-MnemosyneDataLink -ProfileDir $profileDir -Name $name -Exists $exists `
                -TeamMemoryHome $TeamMemoryHome -AdoptTeamMemory:$AdoptTeamMemory -AdoptTimestamp $AdoptTimestamp `
                -DryRun:$true -HermesRootProfilesReal $HermesRootProfilesReal | Out-Null
        } else {
            # F3 (review r3): Unix's -separate-memory pins the private
            # path in .env too (install-dev-team-unix.sh's final `else`
            # branch); -SeparateMemory must do the same, or a User-scope
            # MNEMOSYNE_DATA_DIR/MNEMOSYNE_HOME still reaches a
            # "separate" profile's CLI and call-time modules.
            Set-MnemosyneDataDirPin -ProfileDir $profileDir -DataDir (Join-Path $profileDir 'mnemosyne\data')
        }
        Write-Host "  would link: $profileDir\plugins\dev-team-gate -> $PluginSrcGate"
        Write-Host "  would run:  hermes -p $name config set model.default ..."
        if ($ForgeTokenSource -and $role -eq $ForgeTokenRole) { Set-ForgeTokenSource -Name $name }
        Write-Host "  would run:  hermes -p $name plugins enable dev-team-gate --no-allow-tool-override"
        Write-Host "  would link: $profileDir\dev-team -> $DevTeamHome"
        foreach ($stageDir in (Get-ChildItem -LiteralPath $SkillsDir -Directory -Filter 'dev-team-stage-*' -ErrorAction SilentlyContinue)) {
            Write-Host "  would install skill: dev-team\$($stageDir.Name)"
        }
        Write-Host '  would install skill: dev-team\dev-team-deciders'
        continue
    }

    # 3. Mnemosyne plugin link. The gap this closes: a named profile
    # redirects HERMES_HOME to its own directory, and memory providers are
    # discovered under $HERMES_HOME\plugins\. So the Mnemosyne plugin
    # installed at the root is invisible to every named profile without
    # this link.
    #
    # This link, the Mnemosyne data link right below it, the dev-team-gate
    # plugin link and the dev-team home link all happen here, before the
    # model is set and every later `-p $name` call. `hermes profile create
    # --clone` itself (the branch above, for a freshly created profile)
    # already materializes a real `mnemosyne\` directory as part of the
    # clone, before this script gets a chance to link anything - so moving
    # these links earlier does not by itself avoid that directory; what
    # does is the `$exists` check in the data-link block below, which only
    # preserves a real directory when the profile predates this run.
    # Linking here (rather than after the model/kanban/gate calls) still
    # matters on its own: it keeps a freshly cloned profile's plugin and
    # gate wiring in place before anything else touches it.
    $pluginDir = Join-Path $profileDir 'plugins'
    New-Item -ItemType Directory -Path $pluginDir -Force | Out-Null
    $pluginLinkMnemo = Join-Path $pluginDir 'mnemosyne'
    $mnemoLinkKind = New-DirLink -LinkPath $pluginLinkMnemo -Target $PluginSrcMnemo -Label 'the Mnemosyne plugin'
    if ($mnemoLinkKind -eq 'copy') { $copied += "$name (mnemosyne plugin)" }

    # The plugin link above shares Mnemosyne's *code*. It does not share its
    # *data*: the database path is steered by MNEMOSYNE_DATA_DIR / MNEMOSYNE_HOME,
    # which this installer pins into each profile's own .env (Set-MnemosyneDataDirPin,
    # above) so a directly-launched profile beats whatever the launching shell, the
    # gateway, or the Windows User-scope environment already set
    # (hermes_cli/env_loader.py:502-562 load_hermes_dotenv, override=True at :560).
    # That pin does nothing for a profile run through a multiplex gateway -- the
    # routed profile's own .env load is skipped there (env_loader.py:535-547) -- or
    # for a managed worker, which returns before reading any .env at all
    # (env_loader.py:513-515; agent/managed_worker.py:139-143). Point each profile's
    # Mnemosyne data root at the team's dedicated bank, unless -SeparateMemory asked
    # for isolation. The operator's own bank ($HermesRoot\mnemosyne) is never a
    # candidate here.
    if (-not $SeparateMemory) {
        Invoke-MnemosyneDataLink -ProfileDir $profileDir -Name $name -Exists $exists `
            -TeamMemoryHome $TeamMemoryHome -AdoptTeamMemory:$AdoptTeamMemory -AdoptTimestamp $AdoptTimestamp `
            -DryRun:$false -HermesRootProfilesReal $HermesRootProfilesReal | ForEach-Object {
                if ($_.Copied)   { $copied   += "$name (mnemosyne data)" }
                if ($_.Adopted)  { $adopted  += $name }
                if ($_.Unshared) { $unshared += $name }
            }
    } else {
        # F3 (review r3): see the DryRun branch above for why.
        Set-MnemosyneDataDirPin -ProfileDir $profileDir -DataDir (Join-Path $profileDir 'mnemosyne\data')
    }

    # 4. dev-team-gate link (enabled in step 6, after the model is set).
    # Linked the same way Mnemosyne is, for the same reason: it must be in
    # place before any `-p $name` call, including the `plugins enable` call
    # that references it.
    $pluginLinkGate = Join-Path $pluginDir 'dev-team-gate'
    $gateLinkKind = New-DirLink -LinkPath $pluginLinkGate -Target $PluginSrcGate -Label 'dev-team-gate'
    if ($gateLinkKind -eq 'copy') { $copied += "$name (dev-team-gate)" }

    # Team state (forge project records, team clones): same gap as
    # Mnemosyne's data link, same fix, always shared (see $DevTeamHome
    # above).
    New-Item -ItemType Directory -Path $DevTeamHome -Force | Out-Null
    $teamDataLink = Join-Path $profileDir 'dev-team'
    $existingTeam = Get-Item -LiteralPath $teamDataLink -ErrorAction SilentlyContinue
    $isTeamLink   = $existingTeam -and $existingTeam.LinkType
    if ($existingTeam -and -not $isTeamLink -and $exists) {
        Write-Host "  NOTE: $teamDataLink is a real directory. Left as-is -"
        Write-Host '        this profile will not share the team''s forge records.'
    } else {
        $kind = New-DirLink -LinkPath $teamDataLink -Target $DevTeamHome -Label 'the shared dev-team home'
        if ($kind -eq 'copy') { $copied += "$name (dev-team home)" }
    }

    # 5. model per role: pm, architect and reviewer get the frontier tier;
    # the rest get the mid tier. -Model overrides the whole table,
    # -SkipModel leaves it alone. This is the first `-p $name` call for a
    # freshly cloned profile, now safe because every directory link above
    # is already in place.
    if (-not $SkipModel) {
        if ($ModelExplicit) {
            $roleModel = $Model
        } else {
            $roleModel = switch ($role) {
                'pm'        { $FrontierModel }
                'architect' { $FrontierModel }
                'reviewer'  { $FrontierModel }
                default     { $MidModel }
            }
        }
        $out = Invoke-Hermes '-p' $name 'config' 'set' 'model.default' $roleModel
        if ($LASTEXITCODE -ne 0) { Write-Error "Setting model on '$name' failed: $out"; exit 1 }
    }

    # 5b. -ForgeTokenSource, devops only: the pr-ci stage is devops' work, so
    # that is the one profile that needs the forge credential. The profile
    # name is "$Prefix$ForgeTokenRole", derived here, so a -Prefix t2- install
    # configures t2-devops.
    if ($ForgeTokenSource -and $role -eq $ForgeTokenRole) {
        Set-ForgeTokenSource -Name $name
    }

    # 6. Kanban is how this team actually works: the pm profile writes
    # cards, the dispatcher spawns the assignee, and a worker raises cards
    # for siblings. Workers inherit their profile's *CLI* toolsets at
    # dispatch time, and the kanban toolset ships disabled by default - so
    # without this, every profile silently lacks kanban_create/kanban_link
    # and cannot delegate onward.
    $out = Invoke-Hermes '-p' $name 'tools' 'enable' 'kanban' '--platform' 'cli'
    if ($LASTEXITCODE -ne 0) { Write-Error "Enabling the kanban toolset on '$name' failed: $out"; exit 1 }

    # 7. dev-team-gate enable: the plugin that enforces the pipeline does
    # nothing until it is in `plugins.enabled`, unlike Mnemosyne.
    # --no-allow-tool-override: this plugin declares no tool overrides, so
    # the confirmation prompt would never fire, but passing it keeps the
    # script non-interactive regardless.
    $out = Invoke-Hermes '-p' $name 'plugins' 'enable' 'dev-team-gate' '--no-allow-tool-override'
    if ($LASTEXITCODE -ne 0) { Write-Error "Enabling dev-team-gate on '$name' failed: $out"; exit 1 }

    # 8. Pipeline stage skills: every dev-team-stage-* skill, for every
    # role, since the guard identifies a card's stage from the skill the
    # card itself carries, not from the completing profile's role. Plus
    # dev-team-deciders, beside the stage skills, for every profile - the
    # design asks that every role be able to decide/extract/summarize.
    $stageSkillsDir = Join-Path $profileDir 'skills\dev-team'
    New-Item -ItemType Directory -Path $stageSkillsDir -Force | Out-Null
    foreach ($stageDir in (Get-ChildItem -LiteralPath $SkillsDir -Directory -Filter 'dev-team-stage-*' -ErrorAction SilentlyContinue)) {
        Copy-Item -LiteralPath $stageDir.FullName -Destination $stageSkillsDir -Recurse -Force
    }
    Copy-Item -LiteralPath $DecidersSkillDir -Destination $stageSkillsDir -Recurse -Force
}

if ($DryRun) {
    Write-Host ''
    Write-Host '  would install: running-the-pipeline skill + bin\kanban-budget.py'
    Write-Host "                 into the '$Prefix$EntryProfile' profile"
    Write-Host '  would install: forge.cmd next to hermes.exe on PATH'
    Write-Host '  would install: deciders.cmd next to hermes.exe on PATH'
    Write-Host ''
    Write-Host "OpenRouter key for deciders: set a user environment variable, e.g.:"
    Write-Host "  setx OPENROUTER_DECIDERS_KEY <key>"
    Write-Host 'Dry run: nothing was changed.'
    exit 0
}

# pm additionally gets the orchestration skill (how to intake, decompose,
# budget-check and report) and the budget script, because kanban has no
# native cost tracking. The other roles receive a card and do one job.
if (Test-Selected $EntryProfile) {
    $entryDir = Split-Path -Parent (Get-HermesLastLine (Invoke-Hermes '-p' "$Prefix$EntryProfile" 'config' 'path'))
    Write-Host ''
    Write-Host "Entry point: $Prefix$EntryProfile"
    $pipelineSkillSrc = Join-Path $SkillsDir 'running-the-pipeline'
    if (Test-Path -LiteralPath $pipelineSkillSrc) {
        $entrySkillsDir = Join-Path $entryDir 'skills'
        New-Item -ItemType Directory -Path $entrySkillsDir -Force | Out-Null
        Copy-Item -LiteralPath $pipelineSkillSrc -Destination $entrySkillsDir -Recurse -Force
        Write-Host '  skill:  running-the-pipeline'
    }
    if (Test-Path -LiteralPath $KanbanBudgetPy) {
        $entryBinDir = Join-Path $entryDir 'bin'
        New-Item -ItemType Directory -Path $entryBinDir -Force | Out-Null
        Copy-Item -LiteralPath $KanbanBudgetPy -Destination $entryBinDir -Force
        Write-Host '  script: bin\kanban-budget.py'
    }
}

# forge on a PATH the gateway sees. On Windows that is the same directory
# hermes.exe itself resolves from on PATH - the lesson from the graft plugin
# work, carried over from the Unix installer's ~/.local/bin/forge. A thin
# .cmd wrapper so every profile's own `python` need not be found separately
# - the forge directory holds its own imports (adapters, forge_core) that
# resolve off the script's own path, so the wrapper only needs to point
# the resolved python at forge.py itself.
$ForgeEmbedPairs = @(
    [ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd },
    [ordered]@{ Label = 'forge.py'; Embed = $ForgePyCmd })
$ForgeGuard = Get-CmdGuardBlock -Prefix 'dt_forge' -Pairs $ForgeEmbedPairs
$forgeCmdContent = "@echo off`r`n$ForgeMarker`r`n$($ForgeGuard.Guards)`"$($WrapperPythonCmd.Embed)`" `"$($ForgePyCmd.Embed)`" %*`r`nexit /b %ERRORLEVEL%`r`n$($ForgeGuard.Tail)"
Assert-WrapperTextAscii 'forge.cmd' $forgeCmdContent
Set-Content -LiteralPath $forgeCmdPath -Value $forgeCmdContent -NoNewline -Encoding ascii
Write-Host ''
Write-Host "forge installed: $forgeCmdPath"

# deciders.cmd on the same PATH, same wrapper shape as forge.cmd above.
# `exit /b %ERRORLEVEL%` is explicit on purpose: without it, a caller that
# launches the .cmd as other than the last command in its own cmd.exe
# session (or under a shell that does not treat a bare batch-file
# ERRORLEVEL as its own exit code) sees exit 0 regardless of python's real
# exit code. python's own exit code is the whole point of this check.
$DecidersEmbedPairs = @(
    [ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd },
    [ordered]@{ Label = 'deciders.py'; Embed = $DecidersPyCmd })
$DecidersGuard = Get-CmdGuardBlock -Prefix 'dt_deciders' -Pairs $DecidersEmbedPairs
$decidersCmdContent = "@echo off`r`n$DecidersMarker`r`n$($DecidersGuard.Guards)`"$($WrapperPythonCmd.Embed)`" `"$($DecidersPyCmd.Embed)`" %*`r`nexit /b %ERRORLEVEL%`r`n$($DecidersGuard.Tail)"
Assert-WrapperTextAscii 'deciders.cmd' $decidersCmdContent
Set-Content -LiteralPath $decidersCmdPath -Value $decidersCmdContent -NoNewline -Encoding ascii
Write-Host "deciders installed: $decidersCmdPath"

# The OpenRouter key for deciders: this installer never reads, prompts for
# or writes it. There is no secret store on Windows, so the only note is a
# user environment variable -- never a per-profile plaintext dotfile (I5):
# one `setx` is a single copy, not eight.
Write-Host ''
Write-Host "OpenRouter key for deciders: set a user environment variable, e.g.:"
Write-Host "  setx OPENROUTER_DECIDERS_KEY <key>"

# The checks below can race the plugin reload right after `plugins enable`
# (Task 12: a first run exited 1 although the plugin was enabled). Each is
# retried a bounded number of times before it counts as a failure.
$VerifyRetries = if ($env:DEV_TEAM_VERIFY_RETRIES) { [int]$env:DEV_TEAM_VERIFY_RETRIES } else { 10 }
$VerifyDelay   = if ($env:DEV_TEAM_VERIFY_DELAY)   { [int]$env:DEV_TEAM_VERIFY_DELAY }   else { 1 }
function Test-Eventually([scriptblock] $Check) {
    for ($attempt = 1; $attempt -le $VerifyRetries; $attempt++) {
        if (& $Check) { return $true }
        if ($attempt -lt $VerifyRetries) { Start-Sleep -Seconds $VerifyDelay }
    }
    return $false
}

function Invoke-CheckedCommand {
    <#
      Runs $FilePath with $Arguments and returns @{ Code = <exit code>;
      Output = <combined stdout+stderr as one string> }, never throwing on
      a non-zero exit.

      Windows PowerShell 5.1 writes a native command's stderr as
      ErrorRecords as soon as any stream is redirected (here, folding
      stderr into stdout with 2>&1), and $ErrorActionPreference = 'Stop'
      turns those into terminating errors even though the command itself
      ran and exited normally. Drop to 'Continue' for the call so a
      failing child process is reported through its exit code and output,
      the way a verification check needs, rather than as a PowerShell
      exception that a bare try/catch collapses into an unexplained
      "FAILED". See also Invoke-Hermes above, which has the same guard for
      the same reason.
    #>
    param([string] $FilePath, [string[]] $Arguments)
    $previousEA = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = (& $FilePath @Arguments 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        # e.g. the file cannot be launched at all (missing, not executable)
        $output = $_.Exception.Message
        $code = 1
    } finally {
        $ErrorActionPreference = $previousEA
    }
    return @{ Code = $code; Output = $output }
}

function Write-CheckFailure([string] $Label, [string] $Output) {
    <# Prints the captured output of a failed check, indented, so a future
       failure explains itself instead of only saying "FAILED". #>
    Write-Host "  $Label`: FAILED"
    if ($Output) {
        ($Output -split "`r?`n") | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "    $_" }
    }
}

Write-Host ''
Write-Host 'Verifying'
Write-Host ('-' * 40)
$failed = 0

# deciders + model-tiers.json: one check, not per-profile - both are shared
# installs, not something each profile wires separately.
$decidersResult = Invoke-CheckedCommand -FilePath $decidersCmdPath -Arguments @('--help')
if ($decidersResult.Code -eq 0) {
    Write-Host '  deciders --help: ok'
} else {
    Write-CheckFailure 'deciders --help' $decidersResult.Output
    $failed++
}
try {
    Get-Content -LiteralPath $ModelTiersPath -Raw | ConvertFrom-Json | Out-Null
    Write-Host '  model-tiers.json: parses'
} catch {
    Write-Host '  model-tiers.json: FAILED to parse'
    $failed++
}

foreach ($role in $Profiles.Keys) {
    if (-not (Test-Selected $role)) { continue }
    $name = "$Prefix$role"

    $status = Invoke-Hermes '-p' $name 'memory' 'status'
    if ($status -match 'Status:\s*available') {
        Write-Host "  $name : mnemosyne available"
    } else {
        Write-Host "  $name : mnemosyne NOT available"
        $failed++
    }

    if (Test-Eventually { Invoke-Hermes '-p' $name 'plugins' 'doctor' 'dev-team-gate' '--ci' | Out-Null; $LASTEXITCODE -eq 0 }) {
        Write-Host "  $name : dev-team-gate doctor ok"
    } else {
        Write-Host "  $name : dev-team-gate doctor FAILED"
        $failed++
    }

    if (Test-Eventually { (Invoke-Hermes '-p' $name 'plugins' 'list' '--enabled' '--plain') -match 'dev-team-gate' }) {
        Write-Host "  $name : dev-team-gate enabled"
    } else {
        Write-Host "  $name : dev-team-gate NOT enabled"
        $failed++
    }
}

Write-Host ''
Write-Host "Created $created, updated $updated."

if ($copied.Count -gt 0) {
    Write-Warning @"
Could not create a junction for: $($copied -join ', ')
Each was copied instead. A copy will not track later changes to its source
(e.g. a Mnemosyne upgrade), so re-run this script for the affected profile
after the source changes.
"@
}

if ($adopted.Count -gt 0) {
    Write-Host ''
    Write-Host "Adopted into the team bank (-AdoptTeamMemory): $($adopted -join ', ')"
    Write-Host "Each profile's old private mnemosyne\ was backed up alongside it as"
    Write-Host 'mnemosyne.pre-team-memory-<timestamp> (never deleted) before linking.'
    Write-Host 'Merging those old memories into the team bank is out of scope for this'
    Write-Host 'installer.'
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
    Write-Error "$failed check(s) failed - see 'hermes -p <name> memory status' and 'hermes -p <name> plugins doctor dev-team-gate'."
    exit 1
}
Write-Host 'All profiles are wired to Mnemosyne and the dev-team-gate plugin.'
