<#
  CI check (dev-team.yml, install-windows-ps51): every dev-* profile links
  the TEAM-only Mnemosyne bank ($HermesRoot\dev-team-mnemosyne), never the
  operator's own ($HermesRoot\mnemosyne), and the team bank actually works
  end to end. Runs under Windows PowerShell 5.1 in CI, so: no
  PowerShell 7-only syntax.

  -HermesRoot      the Hermes root (parent of `hermes config path`)
  -MnemosyneCli    the mnemosyne CLI from the operator's Mnemosyne venv
  -InitProfile     a linked profile to initialise the team DB through
  -ProfileDir      'NAME=PROFILE_DIR' entries, one per profile to check
  -PrivateProfile  profiles EXPECTED to keep their own real bank (C4)

  Behaviourally tested by
  dev-team/plugin/dev-team-gate/tests/test_ci_team_bank_checks.py, which
  is why this lives in a script and not inline in the workflow (PR #8 fix
  round 4, review t_899815da C1-C4). Every path comparison goes through
  Resolve-RealPath, which follows junctions and symlinks at every
  component; Resolve-Path follows neither (C2).
#>
param(
    [Parameter(Mandatory = $true)][string]   $HermesRoot,
    [Parameter(Mandatory = $true)][string]   $MnemosyneCli,
    [Parameter(Mandatory = $true)][string]   $InitProfile,
    [Parameter(Mandatory = $true)][string[]] $ProfileDir,
    [string[]] $PrivateProfile = @()
)
$ErrorActionPreference = 'Stop'

function Fail([string] $Message) {
    [Console]::Error.WriteLine("::error::$Message")
    exit 1
}

$Seps = [char[]]@([char]92, [char]47)

function Get-DirLinkTarget([string] $Path) {
    # The immediate target of a junction or directory symlink, else $null.
    # Windows PowerShell 5.1 reports .Target as a string array.
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $null }
    if (@('Junction', 'SymbolicLink') -notcontains [string]$item.LinkType) { return $null }
    $t = $item.Target
    if ($t -is [System.Array]) { $t = $t[0] }
    if (-not $t) { return $null }
    $t = [string]$t
    $nt = [string]::Concat([char]92, '?', '?', [char]92)
    if ($t.StartsWith($nt)) { $t = $t.Substring($nt.Length) }
    return $t
}

function Resolve-RealPath([string] $Path) {
    # Walks every component from the root; wherever a prefix is a link,
    # its target replaces that prefix and the walk restarts. Bounded.
    $full = [System.IO.Path]::GetFullPath($Path)
    $hops = 0
    while ($true) {
        $root = [System.IO.Path]::GetPathRoot($full)
        $parts = @($full.Substring($root.Length).Split($Seps, [System.StringSplitOptions]::RemoveEmptyEntries))
        $cur = $root
        $restart = $null
        for ($i = 0; $i -lt $parts.Count; $i++) {
            $next = [System.IO.Path]::Combine($cur, $parts[$i])
            $target = Get-DirLinkTarget $next
            if ($target) {
                $hops++
                if ($hops -gt 40) { throw "Too many levels of links resolving $Path" }
                if (-not [System.IO.Path]::IsPathRooted($target)) {
                    $target = [System.IO.Path]::Combine($cur, $target)
                }
                $restart = $target
                foreach ($r in @($parts | Select-Object -Skip ($i + 1))) {
                    $restart = [System.IO.Path]::Combine($restart, $r)
                }
                $restart = [System.IO.Path]::GetFullPath($restart)
                break
            }
            $cur = $next
        }
        if ($null -eq $restart) {
            if ($cur -eq [System.IO.Path]::GetPathRoot($cur)) { return $cur }
            return $cur.TrimEnd($Seps)
        }
        $full = $restart
    }
}

function Test-SamePath([string] $A, [string] $B) {
    return [string]::Equals($A, $B, [System.StringComparison]::OrdinalIgnoreCase)
}
function Test-IsOrUnder([string] $Desc, [string] $Anc) {
    if (Test-SamePath $Desc $Anc) { return $true }
    $prefix = $Anc.TrimEnd($Seps) + [System.IO.Path]::DirectorySeparatorChar
    return $Desc.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}
function Test-Overlap([string] $A, [string] $B) {
    return (Test-IsOrUnder $A $B) -or (Test-IsOrUnder $B $A)
}
function Test-IsDirLink([string] $Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    return [bool]($item -and (@('Junction', 'SymbolicLink') -contains [string]$item.LinkType))
}
function Unquote-EnvPin([string] $Value) {
    # Item 4a (review r3) / operator decision (round 2): install-dev-team-
    # unix.sh and install-dev-team-windows.ps1 now write every pin
    # UNQUOTED again (the single-quoted, python-dotenv-escaped format from
    # review r3 was reverted: Mnemosyne's own .env reader never unescapes,
    # so it would read an escaped pin back corrupted). This function is
    # therefore LEGACY-ONLY: kept so a pin written by the brief window
    # where the quoted format shipped (dotenv/parser.py:25,32 escapes:
    # '\' -> '\\', "'" -> "\'") still resolves correctly here, not because
    # any pin written by the current installer can be quoted -- it
    # cannot: Get-EnvUnsafeValueReason in install-dev-team-windows.ps1 (and
    # env_value_unsafe_reason() on Unix) refuses any value that starts or
    # ends with a quote character before it ever reaches the writer. A
    # plain unquoted value (every current pin, and any pre-existing
    # legacy one) passes through unchanged.
    if ($Value.Length -ge 2 -and $Value.StartsWith("'") -and $Value.EndsWith("'")) {
        $inner = $Value.Substring(1, $Value.Length - 2)
        $sb = New-Object System.Text.StringBuilder
        $i = 0
        while ($i -lt $inner.Length) {
            $ch = $inner[$i]
            if ($ch -eq [char]92 -and ($i + 1) -lt $inner.Length) {
                $nxt = $inner[$i + 1]
                if ($nxt -eq "'" -or $nxt -eq [char]92) {
                    [void]$sb.Append($nxt)
                    $i += 2
                    continue
                }
            }
            [void]$sb.Append($ch)
            $i += 1
        }
        return $sb.ToString()
    }
    return $Value
}
function Get-EnvPin([string] $Dir, [string] $Name) {
    $envFile = Join-Path $Dir '.env'
    if (-not (Test-Path -LiteralPath $envFile)) { return '' }
    $val = ''
    # The installer writes .env as BOM-less UTF-8 (Set-EnvVarPin). Under
    # Windows PowerShell 5.1 (the ps51 job) Get-Content with no -Encoding
    # decodes that as ANSI and mangles a non-ASCII pin (PR #9 review,
    # section 3); ReadAllLines with UTF8 also honours a BOM if present.
    foreach ($line in [System.IO.File]::ReadAllLines($envFile, [System.Text.Encoding]::UTF8)) {
        if ($line.StartsWith("$Name=")) { $val = $line.Substring($Name.Length + 1) }
    }
    return Unquote-EnvPin $val
}

# 1. The team bank is a directory of its own.
$teamPath = Join-Path $HermesRoot 'dev-team-mnemosyne'
if (-not (Test-Path -LiteralPath $teamPath -PathType Container)) { Fail "team bank directory missing: $teamPath" }
$team = Resolve-RealPath $teamPath
$operatorPath = Join-Path $HermesRoot 'mnemosyne'
$operator = $null
if (Test-Path -LiteralPath $operatorPath -PathType Container) { $operator = Resolve-RealPath $operatorPath }

# 2. C2: the two banks never resolve to (or inside) each other.
if ($operator -and (Test-Overlap $team $operator)) {
    Fail "the team bank ($teamPath -> $team) resolves to the operator's own bank ($operator)"
}

# 3. C3/C4: every profile's mnemosyne\ is a link to the team bank, or is an
#    explicitly expected private real directory.
$entries = @()
foreach ($pair in $ProfileDir) {
    $eq = $pair.IndexOf('=')
    if ($eq -lt 1) { Fail "bad -ProfileDir entry (want NAME=DIR): $pair" }
    $entries += , @($pair.Substring(0, $eq), $pair.Substring($eq + 1))
}
$initDir = $null
foreach ($e in $entries) {
    $name = $e[0]; $dir = $e[1]
    if ($name -eq $InitProfile) { $initDir = $dir }
    $link = Join-Path $dir 'mnemosyne'
    if (-not (Test-Path -LiteralPath $link)) { Fail "$name : $link does not exist" }
    $got = Resolve-RealPath $link
    $isLink = Test-IsDirLink $link
    if ($PrivateProfile -contains $name) {
        if ($isLink) { Fail "$name : expected a private bank, but $link is a link (to $got)" }
        if (Test-Overlap $got $team) { Fail "$name : expected a private bank, but $link resolves into the team bank" }
        if ($operator -and (Test-Overlap $got $operator)) { Fail "$name : $link resolves to the operator's own bank ($operator)" }
        Write-Host "$name : private bank (expected): $got"
        continue
    }
    if ($operator -and (Test-Overlap $got $operator)) { Fail "$name : $link resolves to the operator's own bank ($operator)" }
    if (-not $isLink -or -not (Test-SamePath $got $team)) {
        Fail "$name : $link is not a link to the team bank $team (resolves to $got)"
    }
    Write-Host "$name : linked to the team bank"
}
if (-not $initDir) { Fail "-InitProfile $InitProfile is not one of the given profiles" }
if ($PrivateProfile -contains $InitProfile) { Fail "-InitProfile $InitProfile is a private profile; initialise through a linked one" }

# 4. C1: create the team DB the way a real profile would -- the mnemosyne
#    CLI with the profile as HERMES_HOME and its own .env pins -- then
#    require it under dev-team-mnemosyne\data, and the operator DB
#    untouched. Pins are checked BEFORE the CLI runs, so a pin into the
#    operator's bank fails without writing to it.
$pinData = Get-EnvPin $initDir 'MNEMOSYNE_DATA_DIR'
$pinHome = Get-EnvPin $initDir 'MNEMOSYNE_HOME'
if (-not $pinData -or -not $pinHome) { Fail "$InitProfile : .env does not pin MNEMOSYNE_DATA_DIR and MNEMOSYNE_HOME" }
if (-not (Test-SamePath (Resolve-RealPath $pinHome) $team)) {
    Fail "$InitProfile : .env pins MNEMOSYNE_HOME=$pinHome, not the team bank $team"
}
if (-not (Test-SamePath (Resolve-RealPath $pinData) ([System.IO.Path]::Combine($team, 'data')))) {
    Fail "$InitProfile : .env pins MNEMOSYNE_DATA_DIR=$pinData, not $team\data"
}

$operatorDb = [System.IO.Path]::Combine([System.IO.Path]::Combine($operatorPath, 'data'), 'mnemosyne.db')
$before = $null
if (Test-Path -LiteralPath $operatorDb) { $before = (Get-FileHash -Algorithm SHA256 -LiteralPath $operatorDb).Hash }

Write-Host "Initialising the team DB through $InitProfile ($MnemosyneCli stats)..."
$saved = @{}
foreach ($v in 'HERMES_HOME', 'MNEMOSYNE_DATA_DIR', 'MNEMOSYNE_HOME', 'MNEMOSYNE_SHARED_DB_PATH') {
    $saved[$v] = [Environment]::GetEnvironmentVariable($v, 'Process')
}
$prevEap = $ErrorActionPreference
try {
    [Environment]::SetEnvironmentVariable('HERMES_HOME', $initDir, 'Process')
    [Environment]::SetEnvironmentVariable('MNEMOSYNE_DATA_DIR', $pinData, 'Process')
    [Environment]::SetEnvironmentVariable('MNEMOSYNE_HOME', $pinHome, 'Process')
    [Environment]::SetEnvironmentVariable('MNEMOSYNE_SHARED_DB_PATH', $null, 'Process')
    # A native command's stderr (e.g. an onnxruntime warning) must not
    # become a terminating error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    & $MnemosyneCli stats | Out-Host
    $rc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
    foreach ($v in $saved.Keys) { [Environment]::SetEnvironmentVariable($v, $saved[$v], 'Process') }
}
if ($rc -ne 0) { Fail "$InitProfile : '$MnemosyneCli stats' failed (exit $rc)" }

$teamDb = [System.IO.Path]::Combine([System.IO.Path]::Combine($teamPath, 'data'), 'mnemosyne.db')
if (-not (Test-Path -LiteralPath $teamDb -PathType Leaf)) { Fail "team Mnemosyne DB was not created at $teamDb" }
$after = $null
if (Test-Path -LiteralPath $operatorDb) { $after = (Get-FileHash -Algorithm SHA256 -LiteralPath $operatorDb).Hash }
if ($before -ne $after) { Fail "the operator's DB ($operatorDb) changed while initialising the team bank" }

# 5. The DB is reachable through every linked profile's own link.
foreach ($e in $entries) {
    $name = $e[0]; $dir = $e[1]
    if ($PrivateProfile -contains $name) { continue }
    $db = [System.IO.Path]::Combine([System.IO.Path]::Combine([System.IO.Path]::Combine($dir, 'mnemosyne'), 'data'), 'mnemosyne.db')
    if (-not (Test-Path -LiteralPath $db -PathType Leaf)) { Fail "$name : team Mnemosyne DB not reachable through $dir\mnemosyne" }
}
Write-Host "Team bank OK: $team"
exit 0
