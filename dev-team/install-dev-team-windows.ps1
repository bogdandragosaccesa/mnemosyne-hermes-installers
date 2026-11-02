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
    [switch]   $DryRun,
    [string]   $TierFast,
    [string]   $TierStandard,
    [string]   $TierDeep
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
            $lines += "  $($entry.Raw)  (linked from: $($entry.Profiles -join ' '))"
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
            $choices += [ordered]@{ Label = "the location linked from: $($entry.Profiles -join ' ')"; Path = $entry.Raw }
        }
        foreach ($choice in $choices) {
            $lines += "  To keep $($choice.Label) ($($choice.Path)):"
            $lines += "    if (Test-Path -LiteralPath '$DevTeamHome') { Rename-Item -LiteralPath '$DevTeamHome' -NewName '$DevTeamHome.bak-$stamp' }"
            if ((Get-NormalizedPath $choice.Path) -ne $devTeamHomeNorm) {
                $lines += "    Move-Item -LiteralPath '$($choice.Path)' -Destination '$DevTeamHome'"
            }
            foreach ($key in $oldTargets.Keys) {
                $entry = $oldTargets[$key]
                if ($key -eq (Get-NormalizedPath $choice.Path)) { continue }
                foreach ($profileName in $entry.Profiles) {
                    if ($profileName -eq '__root_link__') { continue }
                    $lines += "    # relink ${profileName}: New-DirLink against $($choice.Path)"
                }
            }
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
$PluginSrcMnemo = Join-Path $HermesRoot 'plugins\mnemosyne'
# The one real Mnemosyne data root, which every profile is pointed at unless
# -SeparateMemory was passed. Honour MNEMOSYNE_HOME if the store was relocated.
$MnemoHome = if ($env:MNEMOSYNE_HOME) { $env:MNEMOSYNE_HOME } else { Join-Path $HermesRoot 'mnemosyne' }
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
$ProfileDirs = @()
foreach ($role in $Profiles.Keys) {
    $candidateName = "$Prefix$role"
    Invoke-Hermes 'profile' 'show' $candidateName | Out-Null
    if ($LASTEXITCODE -ne 0) { continue }
    $candidateDir = Split-Path -Parent (Get-HermesLastLine (Invoke-Hermes '-p' $candidateName 'config' 'path'))
    $ProfileDirs += $candidateDir
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
$memLabel = if ($SeparateMemory) { 'separate per profile' } else { "shared bank at $MnemoHome" }
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
            Write-Host "  would link: $profileDir\mnemosyne -> $MnemoHome"
        }
        Write-Host "  would link: $profileDir\plugins\dev-team-gate -> $PluginSrcGate"
        Write-Host "  would run:  hermes -p $name config set model.default ..."
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
    # *data*: the database path resolves from MNEMOSYNE_HOME, which defaults
    # to $HERMES_HOME\mnemosyne - and a named profile has redirected
    # HERMES_HOME. Point each profile's Mnemosyne data root at the one real
    # store, unless -SeparateMemory asked for isolation.
    if (-not $SeparateMemory) {
        $dataLink = Join-Path $profileDir 'mnemosyne'
        $existing = Get-Item -LiteralPath $dataLink -ErrorAction SilentlyContinue
        $isLink   = $existing -and $existing.LinkType
        if ($existing -and -not $isLink -and $exists) {
            # A real directory on a profile that already existed before this
            # run means it has its own memories. Merging them is not ours to
            # decide, so leave it and say so rather than deleting somebody's
            # history.
            Write-Host "  NOTE: $dataLink is a real directory with its own database."
            Write-Host '        Left as-is - this profile will not share the team bank.'
            $unshared += $name
        } else {
            # Either already a junction, or nothing there yet, or - the case
            # this fixes - a real directory that `hermes profile create
            # --clone` itself just created a few lines up in *this very
            # run* ($exists was false for this profile at the top of the
            # loop). That directory holds no memories yet; moving the link
            # above this point in the script cannot help, because the
            # clone call is itself the first `-p $name`-equivalent
            # invocation that creates it. New-DirLink's Remove-Item +
            # New-Item replaces it safely.
            $kind = New-DirLink -LinkPath $dataLink -Target $MnemoHome -Label 'the Mnemosyne data store'
            if ($kind -eq 'copy') { $copied += "$name (mnemosyne data)" }
        }
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

# The wrappers name an absolute python.exe. A bare `python` resolves, on a
# machine without a system Python, to the Microsoft Store alias stub in
# WindowsApps, which prints 'Python was not found' and exits 9009 (seen in
# CI on both runners). Prefer the interpreter Hermes itself installed, then
# the py launcher's Python 3, then a real python.exe on PATH.
function Resolve-WrapperPython {
    $toolsDir = Join-Path $env:LOCALAPPDATA 'hermes\tools'
    if (Test-Path -LiteralPath $toolsDir) {
        $hermesPy = Get-ChildItem -LiteralPath $toolsDir -Directory -Filter 'python-3*' -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'python.exe' } |
            Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if ($hermesPy) { return $hermesPy }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        # 'Continue' locally: under 'Stop', PS 5.1 turns a native command's
        # redirected stderr (py with no Python 3 installed) into a terminating
        # error, which would abort the install instead of falling through.
        $savedEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { $launched = & py -3 -c 'import sys; print(sys.executable)' 2>$null } finally { $ErrorActionPreference = $savedEap }
        if ($LASTEXITCODE -eq 0 -and $launched -and (Test-Path -LiteralPath $launched.Trim())) { return $launched.Trim() }
    }
    $onPath = Get-Command python -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return $null
}
$WrapperPython = Resolve-WrapperPython
if (-not $WrapperPython) {
    [Console]::Error.WriteLine("No Python 3 found for the forge and deciders wrappers (looked in $env:LOCALAPPDATA\hermes\tools, the py launcher, and PATH outside WindowsApps). Install Hermes or Python 3, then re-run.")
    exit 1
}
Write-Host "wrapper python: $WrapperPython"

# forge on a PATH the gateway sees. On Windows that is the same directory
# hermes.exe itself resolves from on PATH - the lesson from the graft plugin
# work, carried over from the Unix installer's ~/.local/bin/forge. A thin
# .cmd wrapper so every profile's own `python` need not be found separately
# - the forge directory holds its own imports (adapters, forge_core) that
# resolve off the script's own path, so the wrapper only needs to point
# the resolved python at forge.py itself.
$forgeCmdContent = "@echo off`r`n$ForgeMarker`r`n`"$WrapperPython`" `"$ForgePy`" %*`r`nexit /b %ERRORLEVEL%`r`n"
Set-Content -LiteralPath $forgeCmdPath -Value $forgeCmdContent -NoNewline -Encoding ascii
Write-Host ''
Write-Host "forge installed: $forgeCmdPath"

# deciders.cmd on the same PATH, same wrapper shape as forge.cmd above.
# `exit /b %ERRORLEVEL%` is explicit on purpose: without it, a caller that
# launches the .cmd as other than the last command in its own cmd.exe
# session (or under a shell that does not treat a bare batch-file
# ERRORLEVEL as its own exit code) sees exit 0 regardless of python's real
# exit code. python's own exit code is the whole point of this check.
$decidersCmdContent = "@echo off`r`n$DecidersMarker`r`n`"$WrapperPython`" `"$DecidersPy`" %*`r`nexit /b %ERRORLEVEL%`r`n"
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
