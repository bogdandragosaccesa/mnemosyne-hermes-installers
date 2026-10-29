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
    [switch]   $DryRun
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

$ScriptDir      = Split-Path -Parent $MyInvocation.MyCommand.Path
$SoulsDir       = Join-Path $ScriptDir 'souls'
$SkillsDir      = Join-Path $ScriptDir 'skills'
$PluginSrcGate  = Join-Path $ScriptDir 'plugin\dev-team-gate'
$ForgePy        = Join-Path $ScriptDir 'forge\forge.py'
$KanbanBudgetPy = Join-Path $ScriptDir 'scripts\kanban-budget.py'

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

    $existing = Get-Item -LiteralPath $LinkPath -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-Item -LiteralPath $LinkPath -Force -Recurse
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
Write-Host "Hermes home:   $HermesRoot"
Write-Host "Mnemosyne:     $PluginSrcMnemo"
Write-Host "dev-team-gate: $PluginSrcGate"
Write-Host "Model:         $modelLabel"
Write-Host "Name prefix:   $Prefix"
$memLabel = if ($SeparateMemory) { 'separate per profile' } else { "shared bank at $MnemoHome" }
Write-Host "Memory:        $memLabel"
Write-Host "Team state:    $DevTeamHome (shared across every profile)"
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
    # card itself carries, not from the completing profile's role.
    $stageSkillsDir = Join-Path $profileDir 'skills\dev-team'
    New-Item -ItemType Directory -Path $stageSkillsDir -Force | Out-Null
    foreach ($stageDir in (Get-ChildItem -LiteralPath $SkillsDir -Directory -Filter 'dev-team-stage-*' -ErrorAction SilentlyContinue)) {
        Copy-Item -LiteralPath $stageDir.FullName -Destination $stageSkillsDir -Recurse -Force
    }
}

if ($DryRun) {
    Write-Host ''
    Write-Host '  would install: running-the-pipeline skill + bin\kanban-budget.py'
    Write-Host "                 into the '$Prefix$EntryProfile' profile"
    Write-Host '  would install: forge.cmd next to hermes.exe on PATH'
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
# python at forge.py itself.
$hermesCmd = Get-Command hermes -ErrorAction Stop
$hermesDir = Split-Path -Parent $hermesCmd.Source
$forgeCmdPath = Join-Path $hermesDir 'forge.cmd'
$forgeCmdContent = "@echo off`r`npython `"$ForgePy`" %*`r`n"
Set-Content -LiteralPath $forgeCmdPath -Value $forgeCmdContent -NoNewline -Encoding ascii
Write-Host ''
Write-Host "forge installed: $forgeCmdPath"

Write-Host ''
Write-Host 'Verifying'
Write-Host ('-' * 40)
$failed = 0
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

    Invoke-Hermes '-p' $name 'plugins' 'doctor' 'dev-team-gate' '--ci' | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $name : dev-team-gate doctor ok"
    } else {
        Write-Host "  $name : dev-team-gate doctor FAILED"
        $failed++
    }

    $enabledList = Invoke-Hermes '-p' $name 'plugins' 'list' '--enabled' '--plain'
    if ($enabledList -match 'dev-team-gate') {
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
