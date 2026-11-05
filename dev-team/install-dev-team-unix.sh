#!/usr/bin/env bash
set -Eeuo pipefail

# Creates the eight dev-team profiles on an existing Hermes install, points
# each one at Mnemosyne, wires the dev-team-gate plugin and the pipeline
# stage skills, and installs the vendor-neutral `forge` CLI. Everything here
# is idempotent: re-running upgrades the SOUL, the model, the plugin link and
# the skills in place rather than erroring out.
#
# Modelled on old/outsourcing-team/install-outsourcing-team-unix.sh.

MODEL="anthropic/claude-sonnet-5"
# Tiers used when --model is not given. pm, architect and reviewer hold
# refusal/one-way-door authority or author the plan every later stage is
# checked against, so they get the frontier tier. The rest work against a
# plan or a finding someone else already wrote, which is where the mid tier
# earns its keep. Fable models are deliberately absent and must not be
# added: their cost is out of all proportion to the work these profiles do.
FRONTIER_MODEL="anthropic/claude-opus-5"
MID_MODEL="anthropic/claude-sonnet-5"
# Default for the deciders tier table's fast tier only (no profile uses it).
# Must be a chat model an agent worker can run on: never a decisions-only
# model such as deciders' own tier/decide models (see DECISION_MODELS).
FAST_MODEL="anthropic/claude-haiku-4-5"
# Set when the caller passes --model, so one explicit model beats the table.
MODEL_EXPLICIT=""
ONLY=""
SKIP_MODEL=0
KEEP_SOUL=0
DRY_RUN=0
PREFIX="dev-"
SHARED_DATA=1
# Override for the team bank directory; defaults to
# $HERMES_ROOT/dev-team-mnemosyne when empty. Never the operator's own bank.
TEAM_MEMORY=""
ADOPT_TEAM_MEMORY=0
# deciders tier table: PROVIDER:MODEL per tier, raw --tier-* values before
# the colon split below. Unset unless the caller passes the flag (each tier
# then defaults from FAST_MODEL/MID_MODEL/FRONTIER_MODEL).
TIER_FAST_PM=""
TIER_STANDARD_PM=""
TIER_DEEP_PM=""
# --forge-token-source: the operator's command that prints
# DEV_TEAM_GH_TOKEN=<value>. Only the command STRING is ever stored, and a
# COMMAND containing a literal token shape is refused outright at parse
# time below. See configure_forge_token_source() for what Hermes itself
# does with the command once the source is enabled.
FORGE_TOKEN_SOURCE=""
# The variable the recorded command is contracted to print, and the one the
# devops profile's terminal must pass through.
FORGE_TOKEN_ENV_VAR="DEV_TEAM_GH_TOKEN"
# The role whose profile holds the forge credential: pr-ci is devops' stage.
# The PROFILE name is derived as "$PREFIX$FORGE_TOKEN_ROLE" at use, never
# hardcoded, so a --prefix t2- install writes t2-devops.
FORGE_TOKEN_ROLE="devops"
# The profile a human talks to. It gets the pipeline-orchestration skill and
# the budget script; every other profile is a worker the dispatcher spawns.
ENTRY_PROFILE="pm"

usage() { cat <<'EOF'
Usage: install-dev-team-unix.sh [options]
  --model MODEL      Set this one model on every profile, overriding the
                     per-role tier table (frontier for pm, architect and
                     reviewer; mid for the rest). Must be a real model id,
                     never a tier word like "mid".
  --only a,b,c       Create only these profiles instead of all eight
  --prefix PREFIX    Prepend PREFIX to every profile name (default: dev-),
                     e.g. --prefix t2- creates t2-qa. Use to coexist with
                     another team.
  --skip-model       Leave each profile's model at whatever it inherited
  --keep-soul        Do not overwrite an existing SOUL.md
  --separate-memory  Give each profile its own Mnemosyne database instead of
                     sharing the root one. Roles then cannot read each
                     other's notes, so the board no longer hands work
                     between them.
  --team-memory DIR  Use DIR as the team's shared Mnemosyne bank instead of
                     the default $HERMES_ROOT/dev-team-mnemosyne. Created if
                     missing. Never the operator's own bank
                     ($HERMES_ROOT/mnemosyne) -- refused if it resolves there.
  --adopt-team-memory
                     For each existing profile that already has a real,
                     private mnemosyne/ directory: move it to
                     mnemosyne.pre-team-memory-<timestamp> (a backup, never
                     deleted), then link it to the team bank. Without this
                     flag such a directory is left alone. Either way, this
                     installer never merges old private memories into the
                     team bank -- that is on the operator.
  --tier-fast P:M     deciders tier table entry for the fast tier, as
                     PROVIDER:MODEL (e.g. anthropic:claude-haiku-4-5).
                     Default: $FAST_MODEL split on its first '/'.
                     Every --tier-* model must be a chat model an agent
                     can run on; a decisions-only model is refused.
  --tier-standard P:M deciders tier table entry for the standard tier.
                     Default: $MID_MODEL split on its first '/'.
  --tier-deep P:M     deciders tier table entry for the deep tier.
                     Default: $FRONTIER_MODEL split on its first '/'.
  --forge-token-source COMMAND
                     Record COMMAND as the devops profile's secrets.command
                     helper. COMMAND must print a line
                     DEV_TEAM_GH_TOKEN=<value>; pr-ci reads that variable to
                     push and to talk to the forge. Only the COMMAND STRING
                     is stored, and a COMMAND that itself contains a
                     literal token shape is refused (nothing written). This
                     installer itself never executes COMMAND -- but once
                     the source is enabled, Hermes runs it at the start of
                     every later `hermes -p <prefix>devops ...` call this
                     install makes, discarding the helper's stderr and
                     never printing the value it returns. Writes exactly
                     two leaf keys on the <prefix>devops profile --
                     secrets.command.command and secrets.command.enabled --
                     plus DEV_TEAM_GH_TOKEN appended to
                     terminal.env_passthrough. Re-running with the same
                     COMMAND changes nothing, even over an already-correct
                     mapping; a different COMMAND replaces only
                     secrets.command.command.
  --dry-run          Print what would happen and change nothing
  -h, --help         Show help

Profiles: pm architect uiux backend-db frontend qa reviewer devops
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)  MODEL="${2:-}";  [[ -n $MODEL  ]] || { echo "--model needs a value"  >&2; exit 2; }; MODEL_EXPLICIT=1; shift 2 ;;
        --only)   ONLY="${2:-}";   [[ -n $ONLY   ]] || { echo "--only needs a value"   >&2; exit 2; }; shift 2 ;;
        --prefix) PREFIX="${2:-}"; [[ -n $PREFIX ]] || { echo "--prefix needs a value" >&2; exit 2; }; shift 2 ;;
        --skip-model) SKIP_MODEL=1; shift ;;
        --keep-soul)  KEEP_SOUL=1;  shift ;;
        --separate-memory) SHARED_DATA=""; shift ;;
        --team-memory)
            TEAM_MEMORY="${2:-}"
            [[ -n $TEAM_MEMORY ]] || { echo "--team-memory needs a value" >&2; exit 2; }
            shift 2 ;;
        --adopt-team-memory) ADOPT_TEAM_MEMORY=1; shift ;;
        --tier-fast)
            TIER_FAST_PM="${2:-}"
            [[ -n $TIER_FAST_PM ]] || { echo "--tier-fast needs a value" >&2; exit 2; }
            [[ $TIER_FAST_PM == *:* ]] || { echo "--tier-fast must be PROVIDER:MODEL (got '$TIER_FAST_PM')" >&2; exit 2; }
            shift 2 ;;
        --tier-standard)
            TIER_STANDARD_PM="${2:-}"
            [[ -n $TIER_STANDARD_PM ]] || { echo "--tier-standard needs a value" >&2; exit 2; }
            [[ $TIER_STANDARD_PM == *:* ]] || { echo "--tier-standard must be PROVIDER:MODEL (got '$TIER_STANDARD_PM')" >&2; exit 2; }
            shift 2 ;;
        --tier-deep)
            TIER_DEEP_PM="${2:-}"
            [[ -n $TIER_DEEP_PM ]] || { echo "--tier-deep needs a value" >&2; exit 2; }
            [[ $TIER_DEEP_PM == *:* ]] || { echo "--tier-deep must be PROVIDER:MODEL (got '$TIER_DEEP_PM')" >&2; exit 2; }
            shift 2 ;;
        --forge-token-source)
            FORGE_TOKEN_SOURCE="${2:-}"
            [[ -n $FORGE_TOKEN_SOURCE ]] || { echo "--forge-token-source needs a value" >&2; exit 2; }
            if printf '%s' "$FORGE_TOKEN_SOURCE" | grep -qiE 'github_pat_[A-Za-z0-9_]{8,}|gh[pours]_[A-Za-z0-9]{8,}'; then
                echo "--forge-token-source must not contain a literal token value; pass a COMMAND that prints one instead. Refusing -- nothing written." >&2
                exit 2
            fi
            shift 2 ;;
        --dry-run)    DRY_RUN=1;    shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if (( SKIP_MODEL )) && [[ -n $MODEL_EXPLICIT ]]; then
    echo '--skip-model and --model contradict each other: one sets the model, the' >&2
    echo 'other leaves it alone. Pick one.' >&2
    exit 2
fi

# A prefix ending in a character Hermes will not accept in a profile name
# turns into a confusing failure eleven lines later, so check it here.
if [[ -n $PREFIX && ! $PREFIX =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "--prefix must contain only letters, digits, hyphen and underscore." >&2
    exit 2
fi

# deciders tier table: --tier-fast/standard/deep were already validated as
# PROVIDER:MODEL above. Each tier defaults from FAST_MODEL/MID_MODEL/
# FRONTIER_MODEL, split on its first '/'.
if [[ -n $TIER_FAST_PM ]]; then
    TIER_FAST_PROVIDER="${TIER_FAST_PM%%:*}"
    TIER_FAST_MODEL="${TIER_FAST_PM#*:}"
    # M8: "anthropic:" (empty model, but still has a colon) must be
    # refused, not silently written as {"provider": "anthropic", "model": ""}.
    [[ -n $TIER_FAST_PROVIDER && -n $TIER_FAST_MODEL ]] || { echo "--tier-fast must be PROVIDER:MODEL with both non-empty (got '$TIER_FAST_PM')" >&2; exit 2; }
else
    TIER_FAST_PROVIDER="${FAST_MODEL%%/*}"
    TIER_FAST_MODEL="${FAST_MODEL#*/}"
fi

if [[ -n $TIER_STANDARD_PM ]]; then
    TIER_STANDARD_PROVIDER="${TIER_STANDARD_PM%%:*}"
    TIER_STANDARD_MODEL="${TIER_STANDARD_PM#*:}"
    [[ -n $TIER_STANDARD_PROVIDER && -n $TIER_STANDARD_MODEL ]] || { echo "--tier-standard must be PROVIDER:MODEL with both non-empty (got '$TIER_STANDARD_PM')" >&2; exit 2; }
else
    TIER_STANDARD_PROVIDER="${MID_MODEL%%/*}"
    TIER_STANDARD_MODEL="${MID_MODEL#*/}"
fi

if [[ -n $TIER_DEEP_PM ]]; then
    TIER_DEEP_PROVIDER="${TIER_DEEP_PM%%:*}"
    TIER_DEEP_MODEL="${TIER_DEEP_PM#*:}"
    [[ -n $TIER_DEEP_PROVIDER && -n $TIER_DEEP_MODEL ]] || { echo "--tier-deep must be PROVIDER:MODEL with both non-empty (got '$TIER_DEEP_PM')" >&2; exit 2; }
else
    TIER_DEEP_PROVIDER="${FRONTIER_MODEL%%/*}"
    TIER_DEEP_MODEL="${FRONTIER_MODEL#*/}"
fi

# Minimal JSON string escaping (backslash, then quote) — provider/model
# values are plain model ids, but this keeps model-tiers.json valid even if
# an operator passes something unusual.
json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# Builds the model-tiers.json body. Any tier may be absent (fast, with no
# --tier-fast). Format matches the pm's `deciders tier` reader:
# {"fast": {"provider": ..., "model": ...}, "standard": {...}, "deep": {...}}.
build_model_tiers_json() {
    local body="" sep=""
    if [[ -n $TIER_FAST_PROVIDER ]]; then
        body="${body}${sep}  \"fast\": {\"provider\": \"$(json_escape "$TIER_FAST_PROVIDER")\", \"model\": \"$(json_escape "$TIER_FAST_MODEL")\"}"
        sep=$',\n'
    fi
    if [[ -n $TIER_STANDARD_PROVIDER ]]; then
        body="${body}${sep}  \"standard\": {\"provider\": \"$(json_escape "$TIER_STANDARD_PROVIDER")\", \"model\": \"$(json_escape "$TIER_STANDARD_MODEL")\"}"
        sep=$',\n'
    fi
    if [[ -n $TIER_DEEP_PROVIDER ]]; then
        body="${body}${sep}  \"deep\": {\"provider\": \"$(json_escape "$TIER_DEEP_PROVIDER")\", \"model\": \"$(json_escape "$TIER_DEEP_MODEL")\"}"
        sep=$',\n'
    fi
    printf '{\n%s\n}\n' "$body"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOULS_DIR="$SCRIPT_DIR/souls"
SKILLS_DIR="$SCRIPT_DIR/skills"
PLUGIN_SRC_GATE="$SCRIPT_DIR/plugin/dev-team-gate"
FORGE_PY="$SCRIPT_DIR/forge/forge.py"
KANBAN_BUDGET_PY="$SCRIPT_DIR/scripts/kanban-budget.py"
DECIDERS_PY="$SCRIPT_DIR/deciders/deciders.py"
DECIDERS_SKILL_DIR="$SKILLS_DIR/dev-team-deciders"

# Refuse a decisions-only model in the tier table before anything is
# written: an agent worker on a card routed to it could not run at all. The
# list is deciders_core.py's one-line DECISION_MODELS tuple, so it has one
# owner; a dated variant ("<id>-20261027") is refused too.
DECIDERS_CORE_PY="$SCRIPT_DIR/deciders/deciders_core.py"
DECISION_MODELS_LIST="$(grep -m1 '^DECISION_MODELS = (' "$DECIDERS_CORE_PY" 2>/dev/null | grep -o '"[^"]*"' | tr -d '"' || true)"
if [[ -z $DECISION_MODELS_LIST ]]; then
    echo "cannot read DECISION_MODELS from $DECIDERS_CORE_PY" >&2
    exit 2
fi
refuse_decision_model() {  # flag provider model
    local flag="$1" full="$2/$3" bare="$3" m prefix
    # Strip an OpenRouter ":variant" suffix (e.g. "liquid/d1.1:nitro")
    # before comparing, same as deciders_core.py's _is_decision_model.
    bare="${bare%%:*}"
    full="${full%%:*}"
    while IFS= read -r m; do
        [[ -n $m ]] || continue
        if [[ $m == *'*' ]]; then
            # A family prefix entry, e.g. "perplexity/pplx-decider-*":
            # quoting "$m" in a case pattern disables globbing, so match
            # the prefix explicitly instead.
            prefix="${m%\*}"
            case "$bare" in "$prefix"*) ;; *) case "$full" in "$prefix"*) ;; *) continue ;; esac ;; esac
        else
            case "$bare" in "$m"|"$m"-*) ;; *) case "$full" in "$m"|"$m"-*) ;; *) continue ;; esac ;; esac
        fi
        echo "$flag ${4:-$2:$3} is a decisions-only model (it only answers /api/alpha/decisions);" >&2
        echo "an agent worker cannot run on it. Pick a chat model, e.g. anthropic:claude-haiku-4-5." >&2
        exit 2
    done <<< "$DECISION_MODELS_LIST"
}
refuse_decision_model --tier-fast "$TIER_FAST_PROVIDER" "$TIER_FAST_MODEL"
refuse_decision_model --tier-standard "$TIER_STANDARD_PROVIDER" "$TIER_STANDARD_MODEL"
refuse_decision_model --tier-deep "$TIER_DEEP_PROVIDER" "$TIER_DEEP_MODEL"
# I3: --model sets every profile's own model; it must be refused the same
# way a decisions-only tier-table entry is, or an agent worker ends up
# unable to run at all.
refuse_decision_model --model "${MODEL%%/*}" "${MODEL#*/}" "$MODEL"

# Profile name, then the description the kanban decomposer routes tasks on.
# Parallel arrays rather than an associative array: bash 3.2 (stock macOS)
# has no associative-array support, and this script has to run there.
NAMES=(pm architect uiux backend-db frontend qa reviewer devops)
DESCS=(
    "dev-team pipeline entry point: intake, decomposition, budget discipline, the attention ledger, stopping at a ready PR."
    "Architecture and planning: boundaries, one-way doors, and the plan card every implement/validate/review stage is checked against."
    "UI/UX: interaction and visual design for implement cards that touch a user-facing surface."
    "Backend and data: services, schemas, migrations and data-layer implement cards."
    "Frontend: client-side implement cards against the architecture's contracts."
    "QA: validate stage — exercises the change, writes testable evidence the review stage checks."
    "Review: judges a diff against the plan card, approves or sends back with findings. Holds refusal authority."
    "Devops: repository onboarding, the forge project record, pr-ci stage — pipelines and CI, never merge or deploy."
)

say()  { printf '%s\n' "$*"; }
run()  { if (( DRY_RUN )); then printf '  would run: %s\n' "$*"; else "$@"; fi; }

selected() {
    [[ -z $ONLY ]] && return 0
    # Comma-separated match, anchored so `qa` cannot match `qa-lead`.
    # Matched on the bare role name, not the prefixed profile name, so --only
    # stays the same whether or not --prefix is in play.
    case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

# --forge-token-source: record the operator's token command on the devops
# profile and pass $FORGE_TOKEN_ENV_VAR through to that profile's terminal.
#
# Secrecy contract: only the command STRING is stored, and a COMMAND that
# itself contains a literal token shape is refused before anything is
# written (see the --forge-token-source parse-time check above, which reuses
# forge_core.py's github_pat_/gh[pours]_ patterns). This installer itself
# never executes COMMAND. But once the source is enabled, Hermes runs it at
# the start of every later `hermes -p <prefix>devops ...` call this install
# makes -- once per `hermes -p <prefix>devops` process, with an empty key
# cache -- and discards the helper's stderr and never prints the value it
# returns. So no token value is fetched, printed or written by THIS script:
# not to a config, not to stdout, not under --dry-run.
#
# Exactly three keys are touched. `hermes config set` merges into the
# existing config, so nothing else in it is clobbered. CommandSource
# requires a MAPPING with two leaf keys -- secrets.command.command and
# secrets.command.enabled; `enabled` defaults to false, and writing the
# parent key as a plain string is itself refused once it already holds a
# correct mapping ("it must be a mapping, got a string"). Writing the two
# leaf keys is idempotent even over an already-correct mapping: re-running
# with the same COMMAND writes the same two values (a no-op), and a
# different COMMAND replaces only secrets.command.command.
# `terminal.env_passthrough` is a list, so it is read first and MERGED --
# env_passthrough_merge.py appends the variable only when absent, which keeps
# a re-run a no-op and never drops a name the profile already passes through.
configure_forge_token_source() {  # profile_name
    local name="$1" current merged
    if (( DRY_RUN )); then
        say "  would run: hermes -p $name config set secrets.command.command $FORGE_TOKEN_SOURCE"
        say "  would run: hermes -p $name config set secrets.command.enabled true"
        say "  would add $FORGE_TOKEN_ENV_VAR to $name's terminal.env_passthrough"
        return 0
    fi
    if [[ ! -f $ENV_PASSTHROUGH_MERGE_PY ]]; then
        echo "Missing $ENV_PASSTHROUGH_MERGE_PY, so $name's terminal.env_passthrough cannot be merged. Refusing." >&2
        exit 1
    fi
    hermes -p "$name" config set secrets.command.command "$FORGE_TOKEN_SOURCE" >/dev/null || {
        echo "Setting secrets.command.command on '$name' failed." >&2
        exit 1
    }
    hermes -p "$name" config set secrets.command.enabled true >/dev/null || {
        echo "Setting secrets.command.enabled on '$name' failed." >&2
        exit 1
    }
    # An unset key exits non-zero and prints nothing; the merge treats empty
    # input as an empty list. stderr is dropped: `config get` prefixes its
    # output with secret-helper progress lines.
    current="$(hermes -p "$name" config get terminal.env_passthrough --json 2>/dev/null || true)"
    merged="$(printf '%s' "$current" | python3 -I "$ENV_PASSTHROUGH_MERGE_PY" "$FORGE_TOKEN_ENV_VAR")" || {
        echo "Cannot merge $name's terminal.env_passthrough." >&2
        exit 1
    }
    hermes -p "$name" config set terminal.env_passthrough "$merged" >/dev/null || {
        echo "Setting terminal.env_passthrough on '$name' failed." >&2
        exit 1
    }
    say "  forge token: secrets.command.command and secrets.command.enabled set, $FORGE_TOKEN_ENV_VAR passed through"
}

# --forge-token-source only has a target when the devops profile is in this
# run. Say so rather than silently doing nothing.
if [[ -n $FORGE_TOKEN_SOURCE ]] && ! selected "$FORGE_TOKEN_ROLE"; then
    echo "--forge-token-source was given but --only does not include '$FORGE_TOKEN_ROLE', so there is no profile to configure; nothing was written for it." >&2
fi

command -v hermes >/dev/null 2>&1 || {
    echo "hermes not found on PATH." >&2
    echo "Install Hermes and Mnemosyne first: ../install-mnemosyne-hermes-unix.sh" >&2
    echo "If it is installed, add ~/.local/bin to PATH." >&2
    exit 1
}

# Fail on a typo in --only rather than silently creating nothing.
if [[ -n $ONLY ]]; then
    unknown=""
    IFS=',' read -r -a requested <<< "$ONLY"
    for want in "${requested[@]}"; do
        [[ -z $want ]] && continue
        found=0
        for have in "${NAMES[@]}"; do [[ $want == "$have" ]] && { found=1; break; }; done
        (( found )) || unknown="$unknown $want"
    done
    if [[ -n $unknown ]]; then
        echo "Unknown profile(s) in --only:$unknown" >&2
        echo "Valid: ${NAMES[*]}" >&2
        exit 2
    fi
fi

for f in "${NAMES[@]}"; do
    [[ -f "$SOULS_DIR/SOUL-$f.md" ]] || { echo "Missing $SOULS_DIR/SOUL-$f.md" >&2; exit 1; }
done
[[ -d $PLUGIN_SRC_GATE ]] || { echo "Missing $PLUGIN_SRC_GATE" >&2; exit 1; }
[[ -f $FORGE_PY ]] || { echo "Missing $FORGE_PY" >&2; exit 1; }
[[ -f $DECIDERS_PY ]] || { echo "Missing $DECIDERS_PY" >&2; exit 1; }
[[ -d $DECIDERS_SKILL_DIR ]] || { echo "Missing $DECIDERS_SKILL_DIR" >&2; exit 1; }

# `forge` goes to ~/.local/bin. Another tool may already own that name
# (Laravel Forge, Foundry's forge): never overwrite a file this installer did
# not write. Checked here, before any profile is touched, so a refusal never
# leaves a partial install behind.
FORGE_BIN="$HOME/.local/bin/forge"
FORGE_MARKER="# dev-team forge wrapper (install-dev-team-unix.sh)"
if [[ -e $FORGE_BIN || -L $FORGE_BIN ]]; then
    if ! grep -qF "$FORGE_MARKER" "$FORGE_BIN" 2>/dev/null \
            && ! grep -qE '^exec python3 ".*/dev-team/forge/forge\.py" "\$@"$' "$FORGE_BIN" 2>/dev/null; then
        echo "$FORGE_BIN already exists and was not written by this installer." >&2
        echo "Refusing to overwrite another tool's 'forge'. Move it aside, then re-run." >&2
        exit 1
    fi
fi
# `deciders` goes to ~/.local/bin, same guard as `forge` above.
DECIDERS_BIN="$HOME/.local/bin/deciders"
DECIDERS_MARKER="# dev-team deciders wrapper (install-dev-team-unix.sh)"
if [[ -e $DECIDERS_BIN || -L $DECIDERS_BIN ]]; then
    if ! grep -qF "$DECIDERS_MARKER" "$DECIDERS_BIN" 2>/dev/null \
            && ! grep -qE '^exec python3 ".*/dev-team/deciders/deciders\.py" "\$@"$' "$DECIDERS_BIN" 2>/dev/null; then
        echo "$DECIDERS_BIN already exists and was not written by this installer." >&2
        echo "Refusing to overwrite another tool's 'deciders'. Move it aside, then re-run." >&2
        exit 1
    fi
fi
if (( ! DRY_RUN )); then
    mkdir -p "$HOME/.local/bin"
    [[ -w "$HOME/.local/bin" ]] || { echo "$HOME/.local/bin is not writable." >&2; exit 1; }
fi

# Resolve HERMES_HOME from Hermes itself rather than assuming ~/.hermes, so a
# custom HERMES_HOME keeps working.
ROOT_CONFIG="$(hermes config path)"
HERMES_ROOT="$(dirname "$ROOT_CONFIG")"
# T4: `hermes config path` returns the ACTIVE PROFILE's config.yaml
# (…/profiles/<name>/config.yaml) when this installer itself is run under a
# named profile -- so HERMES_ROOT above would be that profile's directory,
# not the true Hermes root. Climb out of profiles/<name> exactly the way
# kanban-budget.py's hermes_root() already does, so the team home below
# lands at the Hermes root regardless of which profile ran the installer.
if [[ "$(basename "$(dirname "$HERMES_ROOT")")" == profiles ]]; then
    HERMES_ROOT="$(dirname "$(dirname "$HERMES_ROOT")")"
fi
PLUGIN_SRC_MNEMO="$HERMES_ROOT/plugins/mnemosyne"
# The team's shared Mnemosyne bank: a dedicated directory, never the
# operator's own bank ($HERMES_ROOT/mnemosyne, or whatever MNEMOSYNE_HOME
# points at -- that store is never linked into, read from, or written to by
# this installer). --team-memory overrides the location.

# Resolves $1 to a canonical absolute path with `realpath -m` semantics,
# in portable bash (macOS has no `realpath -m`). Walks the path one
# component at a time from `/`:
#   - empty components (`//`) and `.` are dropped;
#   - `..` pops the last component of the already-resolved prefix -- which
#     is always physical, so this is correct both before and after the
#     first not-yet-existing component (review r3 finding #1: `..` in the
#     missing suffix used to be appended literally, so
#     `$HERMES_ROOT/nonexist/../mnemosyne` slipped past the check);
#   - a component that is a symlink -- live OR dangling, at ANY depth -- is
#     replaced by its target and the walk continues from there (bounded to
#     40 hops, so a link cycle fails instead of spinning).
# The result has no `.`, `..`, `//` or symlink in it. It is used both for the
# containment check below AND as the literal mkdir/ln target, so the check
# and the action can never disagree. Returns 1 on a link loop.
resolve_abs_nearest() {
    local p="$1" resolved="" c target hops=0 i=0
    local -a parts tparts
    [[ "$p" == /* ]] || p="$PWD/$p"
    # fix round 3 (safety finding 2): `IFS='/' read -r -a ... <<< "$target"`
    # below relies on bash `read`, which stops at the first newline --
    # silently shortening a path or symlink target that contains one,
    # instead of failing. Refuse before any splitting, rather than resolve
    # a truncated (and possibly real, pre-existing) prefix as if it were
    # the whole path. PR #9 review section 3: this check runs AFTER the
    # $PWD join, because a relative argument run from a cwd containing a
    # newline (`--team-memory bank` from `$T/other-team-bank<LF>/w`) used
    # to pass the check and then resolve to the unrelated `$T/other-team-bank`.
    # Byte-exact ASCII test only (LF, CR): no locale-dependent class.
    if [[ "$p" == *$'\n'* || "$p" == *$'\r'* ]]; then
        printf 'Path contains a newline or carriage return, refusing to resolve: %q\n' "$p" >&2
        return 1
    fi
    IFS='/' read -r -a parts <<< "$p"
    while (( i < ${#parts[@]} )); do
        c="${parts[i]}"
        i=$((i + 1))
        case "$c" in
            ''|.) continue ;;
            ..)   resolved="${resolved%/*}"; continue ;;
        esac
        if [[ -L "$resolved/$c" ]]; then
            hops=$((hops + 1))
            if (( hops > 40 )); then
                echo "Too many levels of symbolic links resolving: $1" >&2
                return 1
            fi
            # Sentinel read (PR #9 known issue): `$(readlink ...)` strips
            # ALL trailing newlines, so a target of "$T/decoy<LF>" used to
            # come back as "$T/decoy" and resolve to that unrelated real
            # directory. Append an `x` after readlink's output so command
            # substitution has nothing to strip, then remove the `x` and
            # exactly the ONE newline readlink itself prints. Whatever
            # newline is left belongs to the target and is refused below.
            target="$(readlink -- "$resolved/$c" && printf x)" || {
                printf 'Cannot read symlink, refusing to resolve: %q\n' "$resolved/$c" >&2
                return 1
            }
            target="${target%x}"
            target="${target%$'\n'}"
            # Same newline/CR guard as above, for a target read back off
            # disk rather than passed in as an argument -- a symlink
            # target is exactly as attacker- or mistake-controlled as the
            # raw path (repro: a symlink whose target is truncated at a
            # newline to a shorter, unrelated, pre-existing directory).
            if [[ "$target" == *$'\n'* || "$target" == *$'\r'* ]]; then
                printf 'Symlink target contains a newline or carriage return, refusing to resolve: %q -> %q\n' "$resolved/$c" "$target" >&2
                return 1
            fi
            # An absolute target restarts from `/`; a relative one is
            # relative to the directory holding the link ($resolved).
            [[ "$target" == /* ]] && resolved=""
            IFS='/' read -r -a tparts <<< "$target"
            # Splice: the link's target components, then whatever of the
            # original path is still unprocessed. Built with a loop so an
            # empty tail is safe under `set -u` on bash 3.2 (macOS).
            while (( i < ${#parts[@]} )); do
                tparts[${#tparts[@]}]="${parts[i]}"
                i=$((i + 1))
            done
            parts=("${tparts[@]}")
            i=0
        else
            resolved="$resolved/$c"
        fi
    done
    printf '%s' "${resolved:-/}"
}

# True if $2 (already resolved) is exactly $1 or nested under it. Called
# with the arguments swapped at each use site, so both "team is inside
# operator" and "operator is inside team" are caught (review finding #2).
path_is_or_under() {
    local outer="${1%/}" inner="${2%/}"
    [[ "$inner" == "$outer" || "$inner" == "$outer"/* ]]
}

# True if some EXISTING ancestor of $1 (the candidate, walked from itself
# up to `/`) is filesystem-identical to $2 (the outer location, which must
# itself exist). Catches a bind mount: `mount --bind $operator $alias`
# gives $alias a name with no symlink in it anywhere, so
# resolve_abs_nearest's symlink-only walk cannot see that it is really the
# operator's bank (review D4). Deliberately does NOT walk $2's ancestors
# too -- doing that would make every path under the same Hermes root match
# every other one, since they all eventually share the root itself as a
# common existing ancestor.
identity_overlap() {
    local candidate="${1%/}" outer="${2%/}" probe
    [[ -n $outer && -e $outer ]] || return 1
    probe="$candidate"
    while :; do
        [[ -n $probe && -e $probe ]] && { [[ "$probe" -ef "$outer" ]] && return 0; }
        [[ -z $probe || $probe == / ]] && break
        probe="${probe%/*}"
        [[ -z $probe ]] && probe=/
    done
    return 1
}
# Either-direction identity overlap: true if $1 or $2 sits inside (or is)
# the other, by filesystem identity rather than path string.
identity_overlap_either() {
    identity_overlap "$1" "$2" || identity_overlap "$2" "$1"
}

# Item 4a (review r3) / operator decision (round 2 of the Stream B fix
# round): pins stay UNQUOTED, UTF-8, no BOM, no escaping -- see the
# set_env_var() comment below for why.
#
# PR #9 follow-up (card A2): this bash function is now only a FAST PATH
# for the obvious ASCII shapes, run on the raw --team-memory argument (or
# its default) before path resolution so the message names what the
# operator typed. It is NOT what decides safety: env_pin_check_or_die()
# below runs dev-team/scripts/env_pin_check.py on every FINAL, resolved
# value this run will pin, and that checker is authoritative. Every test
# here is a byte-exact comparison against an explicit ASCII byte list --
# no `[[:space:]]`, no `$'\u....'`, no non-ASCII `$'\x..'`, no ranges --
# so the verdict cannot depend on the locale (the reverted bash attempt,
# 830e8b7/07555b3/c8cfaa6, refused ą, Å and х because `$'\x85'` is a raw
# byte that also occurs inside their UTF-8 encodings). The whitespace list
# is Python's str.isspace() restricted to ASCII: TAB LF VT FF CR, FS GS RS
# US (0x1c-0x1f) and SPACE.
_ENV_ASCII_WS=$' \t\n\v\f\r\x1c\x1d\x1e\x1f'
env_value_unsafe_reason() {  # value -> prints a reason if unsafe, else nothing
    local v="$1"
    if [[ $v == *$'\n'* || $v == *$'\r'* ]]; then
        printf 'contains a newline'
    elif [[ $v == \'* || $v == \"* || $v == *\' || $v == *\" ]]; then
        printf 'starts or ends with a quote character'
    elif [[ $v == ["$_ENV_ASCII_WS"]* || $v == *["$_ENV_ASCII_WS"] ]]; then
        printf 'starts or ends with whitespace'
    elif [[ $v == *["$_ENV_ASCII_WS"]'#'* ]]; then
        printf "contains whitespace immediately followed by '#'"
    elif [[ $v == *'${'* ]]; then
        # fix round 3 (safety finding 1): Hermes's own .env loader
        # (hermes_cli/env_loader.py _resolve_dotenv_assignments) expands a
        # literal '${VAR}' sequence found in a pinned value; Mnemosyne's
        # own reader does not. The two readers would disagree on the bank
        # path -- same rule, same wording, as install-dev-team-windows.ps1's
        # Get-EnvUnsafeValueReason (a82da09: "contains a literal ${").
        printf 'contains a literal ${'
    fi
}

# The authoritative pin check (PR #9 review section 2(b), card A2). Runs
# dev-team/scripts/env_pin_check.py KEY with the value passed ONLY through
# the environment (DEV_TEAM_ENV_CHECK_VALUE -- the checker reads it as raw
# bytes via os.environb, so neither the locale nor argv quoting can alter
# it). Checker exit 0: safe. Exit 1: unsafe, one ASCII reason line on
# stdout. Exit 2 or anything else: internal error. Both 1 and 2 -- and a
# missing python3 or checker -- refuse with this installer's exit 2 (fail
# closed). `python3 -I` so PYTHONPATH/PYTHONHOME/user site cannot swap in
# a different module. Called directly (never inside `$(...)`), so its
# `exit 2` ends the installer.
ENV_PIN_CHECK_PY="$SCRIPT_DIR/scripts/env_pin_check.py"
# The shared terminal.env_passthrough list merge (one implementation for both
# installers; see configure_forge_token_source below).
ENV_PASSTHROUGH_MERGE_PY="$SCRIPT_DIR/scripts/env_passthrough_merge.py"
env_pin_check_or_die() {  # key value subject
    local key="$1" value="$2" subject="$3" out rc=0
    if ! command -v python3 >/dev/null 2>&1; then
        echo "$subject: python3 not found on PATH, so the $key pin cannot be checked to read back identically from .env. Refusing (install python3 and re-run)." >&2
        exit 2
    fi
    if [[ ! -f $ENV_PIN_CHECK_PY ]]; then
        echo "$subject: pin checker missing ($ENV_PIN_CHECK_PY), so the $key pin cannot be checked. Refusing." >&2
        exit 2
    fi
    out="$(DEV_TEAM_ENV_CHECK_VALUE="$value" python3 -I "$ENV_PIN_CHECK_PY" "$key")" || rc=$?
    case $rc in
        0) return 0 ;;
        1) echo "$subject would not read back identically from .env as $key: ${out:-no reason given}. Pick a path without that." >&2 ;;
        *) echo "$subject: the .env pin checker failed (exit $rc) checking $key; refusing rather than writing an unchecked pin." >&2 ;;
    esac
    exit 2
}
OPERATOR_MNEMO_HOME="$HERMES_ROOT/mnemosyne"
# Every path below is canonical (no `.`, `..`, `//`, symlink): the value
# checked is byte-for-byte the value later passed to mkdir/ln.
_resolve_or_die() {
    resolve_abs_nearest "$1" || { echo "Cannot resolve $2: $1" >&2; exit 2; }
}
_raw_team_memory="${TEAM_MEMORY:-$HERMES_ROOT/dev-team-mnemosyne}"
_team_memory_unsafe_reason="$(env_value_unsafe_reason "$_raw_team_memory")"
if [[ -n "$_team_memory_unsafe_reason" ]]; then
    echo "--team-memory $_team_memory_unsafe_reason; it would not read back identically from .env. Pick a path without that." >&2
    exit 2
fi
TEAM_MEMORY_HOME="$(_resolve_or_die "$_raw_team_memory" "--team-memory")" || exit 2
_operator_home_real="$(_resolve_or_die "$OPERATOR_MNEMO_HOME" "the operator bank")" || exit 2
_mnemosyne_home_real=""
if [[ -n "${MNEMOSYNE_HOME:-}" ]]; then
    _mnemosyne_home_real="$(_resolve_or_die "$MNEMOSYNE_HOME" "MNEMOSYNE_HOME")" || exit 2
fi
# D1: MNEMOSYNE_DATA_DIR is the variable that actually decides the data
# directory the installed Mnemosyne package uses (mnemosyne/cli.py:20-23,
# mnemosyne/core/beam.py:358-366 both check it before anything else,
# including before $HERMES_HOME/mnemosyne/data). When it is set in this
# installer's own environment it names an operator-controlled location
# exactly like MNEMOSYNE_HOME, so the refusal check below must treat it
# the same way.
_mnemosyne_data_dir_real=""
if [[ -n "${MNEMOSYNE_DATA_DIR:-}" ]]; then
    _mnemosyne_data_dir_real="$(_resolve_or_die "$MNEMOSYNE_DATA_DIR" "MNEMOSYNE_DATA_DIR")" || exit 2
fi
_team_home_real="$TEAM_MEMORY_HOME"
HERMES_ROOT_PROFILES_REAL="$(_resolve_or_die "$HERMES_ROOT/profiles" "\$HERMES_ROOT/profiles")" || exit 2
HERMES_ROOT_DEV_TEAM_REAL="$(_resolve_or_die "$HERMES_ROOT/dev-team" "\$HERMES_ROOT/dev-team")" || exit 2
# True if $1 (canonical) is, sits inside, or contains any operator
# location -- the operator bank, MNEMOSYNE_HOME, or MNEMOSYNE_DATA_DIR,
# whichever are set -- by path containment or by filesystem identity (D4).
touches_operator_bank() {
    local target="$1" loc
    for loc in "$_operator_home_real" "$_mnemosyne_home_real" "$_mnemosyne_data_dir_real"; do
        [[ -n "$loc" ]] || continue
        if path_is_or_under "$loc" "$target" || path_is_or_under "$target" "$loc" \
                || identity_overlap_either "$target" "$loc"; then
            return 0
        fi
    done
    return 1
}
_team_memory_refusal=""
_refusal_names=("the operator's own bank ($OPERATOR_MNEMO_HOME)" "MNEMOSYNE_HOME (${MNEMOSYNE_HOME:-})" "MNEMOSYNE_DATA_DIR (${MNEMOSYNE_DATA_DIR:-})")
_refusal_reals=("$_operator_home_real" "$_mnemosyne_home_real" "$_mnemosyne_data_dir_real")
for _i in "${!_refusal_reals[@]}"; do
    _loc="${_refusal_reals[$_i]}"
    [[ -n "$_loc" ]] || continue
    if path_is_or_under "$_loc" "$_team_home_real" || path_is_or_under "$_team_home_real" "$_loc" \
            || identity_overlap_either "$_team_home_real" "$_loc"; then
        _team_memory_refusal="${_refusal_names[$_i]}"
        break
    fi
done
# D5: a --team-memory under $HERMES_ROOT/profiles or $HERMES_ROOT/dev-team
# breaks every profile's memory (review repro: pointed at a profile's own
# mnemosyne/, the link ends up pointing at itself).
if [[ -z "$_team_memory_refusal" ]]; then
    if path_is_or_under "$HERMES_ROOT_PROFILES_REAL" "$_team_home_real" || identity_overlap "$_team_home_real" "$HERMES_ROOT_PROFILES_REAL"; then
        _team_memory_refusal="a Hermes profile directory ($HERMES_ROOT/profiles)"
    elif path_is_or_under "$HERMES_ROOT_DEV_TEAM_REAL" "$_team_home_real" || identity_overlap "$_team_home_real" "$HERMES_ROOT_DEV_TEAM_REAL"; then
        _team_memory_refusal="the dev-team state directory ($HERMES_ROOT/dev-team)"
    fi
fi
if [[ -n "$_team_memory_refusal" ]]; then
    echo "--team-memory must not point at, be nested in, or contain $_team_memory_refusal." >&2
    exit 2
fi

# Review round 2 of fix round 1 (safety finding): env_value_unsafe_reason()
# above runs only on the RAW --team-memory string (_raw_team_memory),
# before resolve_abs_nearest. But every .env pin below is written from a
# RESOLVED path -- TEAM_MEMORY_HOME / TEAM_MEMORY_HOME/data (pin site:
# pin_mnemosyne_data_dir, call sites further down), or
# $profile_dir/mnemosyne[/data] when --separate-memory applies, or a
# profile keeps its own private bank. Resolution -- following a symlink,
# or joining a relative path against an unsafe cwd, or a tainted
# $HERMES_ROOT -- can introduce the one byte sequence the raw check was
# written to catch, which the raw check alone never sees (repro: a
# --team-memory symlink whose TARGET contains ' #'; a relative
# --team-memory run from a cwd containing ' #'; a tainted $HERMES_ROOT
# surfacing in $HERMES_ROOT/profiles/<name>/mnemosyne). Check every value
# this run will actually pin, for every profile that will be touched,
# BEFORE the mutation loop below creates or writes to ANY profile -- not
# inside set_env_var mid-loop, where an earlier profile would already be
# on disk by the time a later one's unsafe value is caught. Message
# blames --team-memory only when the user actually passed it; otherwise
# it points at the default location (derived from $HERMES_ROOT) instead.
#
# Card A2 (PR #9 review section 2(b)): the bash fast path alone accepted
# values Mnemosyne's reader splits (U+2028, 0x1c, 0x0b, a resolved symlink
# target with U+2028) or that python-dotenv/both readers alter (U+00A0
# before '#', a trailing U+00A0 or 0x1f, non-UTF-8 bytes). Safety is now
# decided by env_pin_check_or_die (dev-team/scripts/env_pin_check.py) on
# each exact value and key pin_mnemosyne_data_dir will write; the fast
# path only gives the older, more specific messages first.
_check_pin_value_or_die() {  # key value description(no-raw-check)
    local _reason
    _reason="$(env_value_unsafe_reason "$2")"
    if [[ -n "$_reason" ]]; then
        echo "$3 $_reason; it would not read back identically from .env. Pick a path without that." >&2
        exit 2
    fi
    env_pin_check_or_die "$1" "$2" "$3"
}
if [[ -n $SHARED_DATA ]]; then
    if [[ -n "${TEAM_MEMORY:-}" ]]; then
        _team_home_subject="--team-memory's resolved path ($(printf '%q' "$_team_home_real"))"
        _team_data_subject="--team-memory's resolved data path ($(printf '%q' "$_team_home_real/data"))"
    else
        _team_home_subject="the default team memory bank path ($(printf '%q' "$_team_home_real"), under \$HERMES_ROOT)"
        _team_data_subject="the default team memory bank's data path ($(printf '%q' "$_team_home_real/data"), under \$HERMES_ROOT)"
    fi
    _check_pin_value_or_die MNEMOSYNE_HOME "$_team_home_real" "$_team_home_subject"
    _check_pin_value_or_die MNEMOSYNE_DATA_DIR "$_team_home_real/data" "$_team_data_subject"
fi
for _preflight_role in "${NAMES[@]}"; do
    selected "$_preflight_role" || continue
    _preflight_name="$PREFIX$_preflight_role"
    if hermes profile show "$_preflight_name" >/dev/null 2>&1; then
        _preflight_profile_dir="$(dirname "$(hermes -p "$_preflight_name" config path)")"
    else
        _preflight_profile_dir="$HERMES_ROOT/profiles/$_preflight_name"
    fi
    _check_pin_value_or_die MNEMOSYNE_HOME "$_preflight_profile_dir/mnemosyne" \
        "$_preflight_name's resolved mnemosyne home path (\$HERMES_ROOT/profiles/$_preflight_name/mnemosyne)"
    _check_pin_value_or_die MNEMOSYNE_DATA_DIR "$_preflight_profile_dir/mnemosyne/data" \
        "$_preflight_name's resolved mnemosyne data path (\$HERMES_ROOT/profiles/$_preflight_name/mnemosyne/data)"
done
unset _preflight_role _preflight_name _preflight_profile_dir _team_home_subject _team_data_subject

# F1 (review r3): also pin MNEMOSYNE_HOME alongside MNEMOSYNE_DATA_DIR.
# MNEMOSYNE_DATA_DIR only steers the CLI (mnemosyne/cli.py:20-23) and
# call-time modules (core/banks.py, core/triples.py, core/canonical.py,
# core/media.py); it does NOT steer the MCP shared-surface DB or
# federation, which both read MNEMOSYNE_HOME directly
# (mnemosyne/mcp_tools.py:167-173, core/fleet_census.py:149-155,
# core/federation_handshake.py:262-267; mcp_server.py:811 also reads
# MNEMOSYNE_HOME, but only to locate a .env file to auto-load, not to
# build a DB path). An inherited MNEMOSYNE_HOME therefore still leaked the
# operator's shared-surface DB path into a dev-team profile even after
# MNEMOSYNE_DATA_DIR was pinned.
# D1: pin this profile's own Mnemosyne data directory into its own .env so
# an inherited MNEMOSYNE_DATA_DIR from the launching shell or gateway
# cannot redirect it away from the bank this installer just linked.
# hermes_cli/env_loader.py:502-562 (load_hermes_dotenv) loads a profile's
# own .env with override=True specifically because "a profile's .env
# overrides stale shell exports" -- confirmed by reading that function.
# mnemosyne/cli.py:20-23 and mnemosyne/core/beam.py:358-366 both check
# MNEMOSYNE_DATA_DIR before $HERMES_HOME/mnemosyne/data (the path this
# installer's symlink otherwise relies on). Together these mean a
# profile-level .env entry is the one thing Hermes applies with priority
# over an inherited shell/gateway environment variable, so pinning both
# vars here closes the gap the operator saw live: MNEMOSYNE_DATA_DIR
# leaking the CLI/core data root, and -- per F1 above -- MNEMOSYNE_HOME
# leaking the shared-surface DB -- without having to refuse the install
# outright.
#
# F4 (review r2 nit): a bank path can legitimately contain sed-special
# bytes (`&` means "the whole match" in a sed replacement; `|` collides
# with the `s|...|...|` delimiter chosen here). Piping the path through a
# sed *pattern* silently corrupts the written value or makes sed error
# out. Rewrite a single VAR=value line with plain bash string comparison
# instead, so sed never sees the path at all.
#
# Item 4a (review r3) / operator decision (round 2): pins stay UNQUOTED,
# UTF-8, no BOM, no escaping -- KEY=value, written verbatim. This reverts
# review r3's single-quoted-with-python-dotenv-escapes format (046cadc).
# That format round-tripped through the real python-dotenv parser, but
# Mnemosyne's OWN .env reader (mcp_server.py:_load_dotenv, ~823-833:
# `v.strip().strip("'\"")`) only strips a bounding whitespace/quote
# character and never unescapes `\\` or `\'`, so it would read "it's"
# pinned as `'it\'s'` back as the literal `it\'s` -- a corrupted bank
# path. Unquoted is kept; what makes it safe is a check, not the format.
#
# What guarantees the round trip (card A2; the claim this comment used to
# make -- "unquoted values round-trip byte-exact through BOTH readers for
# every value this installer can produce" -- was false, see the PR #9
# review, pr9-review.md section 2(b) and (d)): every value reaches this
# function only after env_pin_check_or_die() accepted that exact value for
# that exact key, in the preflight above and again in
# pin_mnemosyne_data_dir() below. dev-team/scripts/env_pin_check.py
# accepts a value only if its cited TRANSCRIPTIONS of the readers all read
# back exactly {KEY: value}, from this function's LF output, alone and
# between other lines:
#   * python-dotenv 1.2.2 (dotenv/parser.py:14-182, dotenv/main.py:32-39,
#     :91-95, dotenv/variables.py:5-86), as Hermes calls it;
#   * Hermes env_loader post-processing at hermes-agent bf0203a
#     (hermes_cli/env_loader.py:295-499: sanitiser rewrite, ${} expansion,
#     credential strip; hermes_cli/config.py:2556-2566);
#   * Mnemosyne 4.0.0b6 (as named by the PR #9 review; verified against
#     the installed dist-info mnemosyne_memory-4.0.0, __version__ "4.0.0")
#     mnemosyne/mcp_server.py:_load_dotenv :793-842
#     (`splitlines()`, `v.strip().strip("'\"")`, :823-834).
# The guarantee is therefore only as good as those transcriptions:
# dev-team/scripts/tests/test_env_pin_check.py diffs them against the real
# readers when installed (3004 cases, 0 mismatches, at A1/d7187e0), and a
# reader upgrade needs that diff re-run. env_value_unsafe_reason() is only
# an ASCII fast path for clearer messages and guarantees nothing on its
# own. Do not re-quote; re-check test_env_pin_check.py and
# test_install_unix_env_pins.py before changing this format again.
set_env_var() {  # env_file var_name value
    local env_file="$1" var_name="$2" value="$3" line found=0 tmp
    local -a out_lines=()
    if [[ -f $env_file ]]; then
        while IFS= read -r line || [[ -n $line ]]; do
            if [[ $line == "$var_name="* ]]; then
                out_lines+=("$var_name=$value")
                found=1
            else
                out_lines+=("$line")
            fi
        done < "$env_file"
    fi
    if (( ! found )); then
        out_lines+=("$var_name=$value")
    fi
    tmp="$(mktemp "${env_file}.XXXXXX")"
    printf '%s\n' "${out_lines[@]}" > "$tmp"
    mv -- "$tmp" "$env_file"
}
pin_mnemosyne_data_dir() {
    local profile_dir="$1" data_dir="$2" env_file home_dir
    env_file="$profile_dir/.env"
    # F1: data_dir is always "<bank>/data" at every call site below, so
    # its dirname is exactly the bank root MNEMOSYNE_HOME must name.
    home_dir="$(dirname -- "$data_dir")"
    # Card A2: check the exact values about to be written, with their keys
    # -- not only the preflight's reconstruction of them. The preflight
    # above has already accepted these same strings, so this should never
    # fire; if it does (a pin site the preflight does not mirror), refuse
    # before this profile's .env is touched.
    env_pin_check_or_die MNEMOSYNE_DATA_DIR "$data_dir" "$(basename -- "$profile_dir")'s MNEMOSYNE_DATA_DIR pin ($(printf '%q' "$data_dir"))"
    env_pin_check_or_die MNEMOSYNE_HOME "$home_dir" "$(basename -- "$profile_dir")'s MNEMOSYNE_HOME pin ($(printf '%q' "$home_dir"))"
    if (( DRY_RUN )); then
        say "  would pin in .env: MNEMOSYNE_DATA_DIR=$data_dir"
        say "  would pin in .env: MNEMOSYNE_HOME=$home_dir"
        return
    fi
    mkdir -p "$profile_dir"
    set_env_var "$env_file" MNEMOSYNE_DATA_DIR "$data_dir"
    set_env_var "$env_file" MNEMOSYNE_HOME "$home_dir"
}
# Team state (forge project records, team clones) must be visible to every
# role: devops writes the project record at onboarding, pr-ci reads it to
# open the PR, and the guard reads it from every other stage to verify the
# default branch before letting a push through. Each profile redirects
# HERMES_HOME to its own directory, so without this link every profile would
# silently keep its own, empty `dev-team/` and the guard could never verify
# anything past main/master. Shared unconditionally — this is pipeline state,
# not personal memory, so --separate-memory does not affect it.
DEV_TEAM_HOME="$HERMES_ROOT/dev-team"

if [[ ! -d $PLUGIN_SRC_MNEMO ]]; then
    echo "Mnemosyne plugin not found at $PLUGIN_SRC_MNEMO." >&2
    echo "Run ../install-mnemosyne-hermes-unix.sh first." >&2
    exit 1
fi

# The provider must be active on the root profile before profiles are cloned
# from it: `hermes profile create --clone` copies config.yaml as a file, so
# whatever memory settings are in place at clone time are what each profile
# gets. The design requires the installer to refuse outright rather than
# create profiles with no working memory.
PROVIDER="$(hermes config get memory.provider 2>/dev/null || true)"
if [[ $PROVIDER != mnemosyne ]]; then
    echo "memory.provider on the root profile is '${PROVIDER:-unset}', not 'mnemosyne'." >&2
    echo "Run ../install-mnemosyne-hermes-unix.sh --disable-builtin-memory first," >&2
    echo "so the profiles clone a config that already points at Mnemosyne." >&2
    exit 1
fi

for key in memory.memory_enabled memory.user_profile_enabled; do
    val="$(hermes config get "$key" 2>/dev/null || true)"
    # Hermes prints Python booleans; compare case-insensitively.
    if [[ $(printf '%s' "$val" | tr '[:upper:]' '[:lower:]') != false ]]; then
        say "Warning: $key is '$val' on the root profile."
        say "         Cloned profiles will inherit it and run Hermes' built-in"
        say "         MEMORY.md / USER.md store alongside Mnemosyne. Re-run the"
        say "         main installer with --disable-builtin-memory to turn it off."
    fi
done

# T4 migration: a team installed before HERMES_ROOT climbed out of a
# profile may have every dev-* profile's dev-team symlink pointing at that
# old, profile-rooted location -- which still holds the live data (project
# records, the deciders ledger, model-tiers.json). Detect that *before*
# anything below creates or writes to $DEV_TEAM_HOME, or the distinction
# between "freshly created, empty" and "pre-existing data at the old wrong
# path" is lost for good.
#
# The scan below walks every role in NAMES, not just the ones --only
# selected: --only only limits which profiles this run *provisions*, and a
# profile left out by --only still has a live dev-team link that the move
# below would turn dangling if we only looked at the selected subset
# (review finding #1). It also keeps every *distinct* populated old
# location instead of stopping at the first, so a second, unrelated
# populated home is reported rather than silently orphaned (review finding
# #2).
resolve_existing_link() {  # path -> prints its realpath, or nothing
    [[ -L $1 ]] || return 1
    readlink -f "$1" 2>/dev/null
}
dir_has_data() {  # path -> true if it exists and is non-empty
    [[ -d $1 ]] && find "$1" -mindepth 1 -print -quit 2>/dev/null | grep -q .
}
list_top_level_entries() {  # dir -> prints one basename per line, dotfiles included
    local d="$1" entry
    for entry in "$d"/* "$d"/.[!.]* "$d"/..?*; do
        [[ -e $entry ]] || continue
        basename "$entry"
    done
}

# D1 (review): a candidate old-home link must resolve to something actually
# named "dev-team", and must not equal, contain, or lie within/above the
# Hermes root, the team home, or any profile directory -- otherwise the
# "migration" would move a whole profile or the Hermes root itself (repros
# F and H). Returns a non-empty reason string on stdout when the candidate
# must be refused; silent and empty when it is safe.
_is_ancestor_or_equal() {  # anc desc -> true if desc == anc or desc is inside anc
    local anc="${1%/}" desc="${2%/}"
    [[ "$desc" == "$anc" || "$desc" == "$anc"/* ]]
}
unsafe_old_home_reason() {  # target -> prints a reason if unsafe, else nothing
    local target="${1%/}" base pd
    base="$(basename "$target")"
    if [[ "$base" != "dev-team" ]]; then
        printf 'its basename is "%s", not "dev-team" (migrating it would move an entire profile or other directory, not a team-state folder)' "$base"
        return
    fi
    if _is_ancestor_or_equal "$target" "$HERMES_ROOT"; then
        printf 'it equals or contains the Hermes root (%s)' "$HERMES_ROOT"
        return
    fi
    if _is_ancestor_or_equal "$target" "$DEV_TEAM_HOME"; then
        printf 'it equals or contains the team home (%s)' "$DEV_TEAM_HOME"
        return
    fi
    for pd in "$HERMES_ROOT"/profiles/*/; do
        [[ -d $pd ]] || continue
        pd="${pd%/}"
        if _is_ancestor_or_equal "$target" "$pd"; then
            printf 'it equals or contains profile directory %s' "$pd"
            return
        fi
    done
}
refuse_unsafe_old_home() {  # label target reason
    local label="$1" target="$2" reason="$3"
    echo "Refusing to migrate: $label resolves to $target, $reason." >&2
    echo "This link looks misconfigured, not a migratable old team home." >&2
    echo "Fix it by hand (point it at a real dev-team directory, or remove the dangerous link) and re-run." >&2
    exit 2
}

# D3 (review): mv is only atomic (and loses nothing between steps) when OLD
# and NEW are on the same filesystem. Off it, refuse rather than silently
# falling back to a copy that can lose concurrent writes.
_fs_device_id() {  # path -> device id, or nothing if it cannot be determined
    stat -c '%d' "$1" 2>/dev/null || stat -f '%d' "$1" 2>/dev/null || true
}
same_filesystem() {  # a b -> true if both resolve to the same device
    local da db
    da="$(_fs_device_id "$1")"
    db="$(_fs_device_id "$2")"
    [[ -n $da && -n $db && $da == $db ]]
}

# D4 (review): if $DEV_TEAM_HOME itself is a symlink/junction elsewhere,
# readlink -f on it would resolve straight through to that other location --
# corrupting every "is this target already the new root?" comparison below
# into a false match. Keep the literal path as NEW_TEAM_HOME_CANON in that
# case; the link's target is instead treated as another old-home candidate.
if [[ -L $DEV_TEAM_HOME ]]; then
    NEW_TEAM_HOME_CANON="$DEV_TEAM_HOME"
else
    NEW_TEAM_HOME_CANON="$(readlink -f "$DEV_TEAM_HOME" 2>/dev/null || printf '%s' "$DEV_TEAM_HOME")"
fi

# Parallel arrays (bash 3.2 has no associative arrays): OLD_TARGETS[i] is a
# distinct populated location other than the new root; OLD_TARGET_ROLES[i]
# is a space-separated list of "$PREFIX$role" profile names whose dev-team
# link points there. "__root_link__" is a non-profile sentinel meaning
# "the root dev-team link itself", used when D4 applies.
OLD_TARGETS=()
OLD_TARGET_ROLES=()
add_old_target_role() {  # target role_name
    local tgt="$1" nm="$2" i
    for i in "${!OLD_TARGETS[@]}"; do
        if [[ "${OLD_TARGETS[$i]}" == "$tgt" ]]; then
            OLD_TARGET_ROLES[$i]="${OLD_TARGET_ROLES[$i]} $nm"
            return
        fi
    done
    OLD_TARGETS+=("$tgt")
    OLD_TARGET_ROLES+=("$nm")
}

for role in "${NAMES[@]}"; do
    name="$PREFIX$role"
    hermes profile show "$name" >/dev/null 2>&1 || continue
    pdir="$(dirname "$(hermes -p "$name" config path)")"
    target="$(resolve_existing_link "$pdir/dev-team" || true)"
    [[ -n $target ]] || continue
    [[ $target == "$NEW_TEAM_HOME_CANON" ]] && continue
    reason="$(unsafe_old_home_reason "$target")"
    [[ -n $reason ]] && refuse_unsafe_old_home "$name/dev-team" "$target" "$reason"
    dir_has_data "$target" || continue
    add_old_target_role "$target" "$name"
done

# D4: the root link itself, if it is one, is also an old-home candidate.
if [[ -L $DEV_TEAM_HOME ]]; then
    root_target="$(readlink -f "$DEV_TEAM_HOME" 2>/dev/null || true)"
    if [[ -n $root_target ]]; then
        reason="$(unsafe_old_home_reason "$root_target")"
        [[ -n $reason ]] && refuse_unsafe_old_home "the root dev-team link ($DEV_TEAM_HOME)" "$root_target" "$reason"
        dir_has_data "$root_target" && add_old_target_role "$root_target" "__root_link__"
    fi
fi

NEW_HAS_DATA=0
# D4 (review): when the root dev-team path is itself a link, any data it
# "has" is the same physical data as whatever old-home candidate it
# resolves to -- counting it as a second, independently populated location
# would manufacture a false conflict with itself. Only a *real* directory
# at the root counts as the root already having its own data.
if [[ ! -L $DEV_TEAM_HOME ]]; then
    dir_has_data "$DEV_TEAM_HOME" && NEW_HAS_DATA=1
fi
POPULATED_COUNT=${#OLD_TARGETS[@]}
(( NEW_HAS_DATA )) && POPULATED_COUNT=$((POPULATED_COUNT + 1))

# D5 (review): print one concrete, working command sequence per candidate,
# never a placeholder. Guard the backup-mv with `[ -e ... ] &&` so an absent
# root (the commonest conflict shape) does not short-circuit the real mv
# that follows it, and name every profile that needs relinking afterwards.
_display_roles() {  # space-separated roles -> user-facing text (N4, review r2)
    local r out=""
    for r in $1; do
        [[ $r == "__root_link__" ]] && r="the-root-dev-team-link"
        out+=" $r"
    done
    printf '%s' "$out"
}
_relink_cmds_for_choice() {  # chosen_path (unused: every link goes to the root)
    # N2 (review r2): after `mv <chosen> $DEV_TEAM_HOME`, the chosen path no
    # longer exists, so every affected profile -- including the ones that
    # linked to the chosen location -- must point at the root.
    local i role pdir
    for i in "${!OLD_TARGETS[@]}"; do
        for role in ${OLD_TARGET_ROLES[$i]}; do
            [[ "$role" == "__root_link__" ]] && continue
            pdir="$(dirname "$(hermes -p "$role" config path)")"
            echo "    ln -sfn \"$DEV_TEAM_HOME\" \"$pdir/dev-team\"" >&2
        done
    done
    echo "    # or simply re-run this installer: it relinks every $PREFIX* profile to $DEV_TEAM_HOME" >&2
}
_print_candidate_choice() {  # label chosen_path
    local label="$1" chosen="$2"
    echo "  To keep $label ($chosen):" >&2
    echo "    [ -e \"$DEV_TEAM_HOME\" ] && mv \"$DEV_TEAM_HOME\" \"$DEV_TEAM_HOME.bak-$(date +%Y%m%dT%H%M%S)\"" >&2
    [[ "$chosen" != "$DEV_TEAM_HOME" ]] && echo "    mv \"$chosen\" \"$DEV_TEAM_HOME\"" >&2
    _relink_cmds_for_choice "$chosen"
}

MIGRATION_CHOICE="none"
MIGRATION_WHY="no pre-existing profile-rooted team home with data was found"
if (( POPULATED_COUNT > 1 )); then
    MIGRATION_CHOICE="refuse"
    MIGRATION_WHY="more than one populated team home was found (${#OLD_TARGETS[@]} old location(s)$( ((NEW_HAS_DATA)) && echo ' plus the new root' || true )); moving any of them would silently discard the others, so refuse and let the operator reconcile by hand"
    echo "Team home conflict: existing team state in more than one location:" >&2
    if (( NEW_HAS_DATA )); then
        echo "  $DEV_TEAM_HOME  (the correct Hermes-root location)" >&2
    fi
    for i in "${!OLD_TARGETS[@]}"; do
        echo "  ${OLD_TARGETS[$i]}  (linked from:$(_display_roles "${OLD_TARGET_ROLES[$i]}"))" >&2
    done
    echo "Refusing to guess which is current. Reconcile by hand by choosing exactly one candidate below, then re-run:" >&2
    (( NEW_HAS_DATA )) && _print_candidate_choice "the existing root location" "$DEV_TEAM_HOME"
    for i in "${!OLD_TARGETS[@]}"; do
        _print_candidate_choice "the location linked from:$(_display_roles "${OLD_TARGET_ROLES[$i]}")" "${OLD_TARGETS[$i]}"
    done
    exit 2
elif (( ${#OLD_TARGETS[@]} == 1 )); then
    OLD_TEAM_HOME="${OLD_TARGETS[0]}"
    AFFECTED_PROFILES="${OLD_TARGET_ROLES[0]}"
    MIGRATION_CHOICE="move"
    MIGRATION_WHY="dev-* profiles' dev-team links ($AFFECTED_PROFILES) point at $OLD_TEAM_HOME, which holds the live team's data; $DEV_TEAM_HOME is empty or absent, so it is safe to move the data there without discarding anything"
    backup="${OLD_TEAM_HOME}.pre-root-migration-$(date +%Y%m%dT%H%M%S)"
    dest_fs_probe="$DEV_TEAM_HOME"
    [[ -e $dest_fs_probe ]] || dest_fs_probe="$HERMES_ROOT"
    if (( DRY_RUN )); then
        say "Would migrate team state: $OLD_TEAM_HOME -> $DEV_TEAM_HOME"
        say "  (backup would be kept at $backup)"
        say "  entries that would move:"
        while IFS= read -r entry; do say "    $entry"; done < <(list_top_level_entries "$OLD_TEAM_HOME" | sort -u)
        for p in $AFFECTED_PROFILES; do
            if [[ "$p" == "__root_link__" ]]; then
                say "  would replace the root dev-team link with a real directory holding the data"
            else
                say "  would relink: $p/dev-team -> $DEV_TEAM_HOME"
            fi
        done
    elif ! same_filesystem "$OLD_TEAM_HOME" "$dest_fs_probe"; then
        echo "Refusing to migrate: $OLD_TEAM_HOME and $DEV_TEAM_HOME are on different filesystems, so an atomic rename is not possible." >&2
        echo "Reconcile by hand:" >&2
        echo "  mkdir -p \"$DEV_TEAM_HOME\"" >&2
        echo "  cp -a \"$OLD_TEAM_HOME\"/. \"$DEV_TEAM_HOME\"/" >&2
        echo "  mv \"$OLD_TEAM_HOME\" \"$backup\"" >&2
        echo "  ln -s \"$DEV_TEAM_HOME\" \"$OLD_TEAM_HOME\"" >&2
        for p in $AFFECTED_PROFILES; do
            [[ "$p" == "__root_link__" ]] && continue
            ppdir="$(dirname "$(hermes -p "$p" config path)")"
            echo "  ln -sfn \"$DEV_TEAM_HOME\" \"$ppdir/dev-team\"" >&2
        done
        exit 2
    else
        entries_moved=()
        while IFS= read -r entry; do entries_moved+=("$entry"); done < <(list_top_level_entries "$OLD_TEAM_HOME" | sort -u)
        # D3 (review): rename first (atomic on one filesystem), then relink
        # OLD -> NEW immediately, so every path that resolves through OLD --
        # including a link outside the scanned $PREFIX$role set, e.g. a
        # builder's own devops/dev-team -- keeps working with zero window
        # where a concurrent write would land nowhere. Only after that is a
        # backup copy made; it is redundant-but-cheap insurance, never
        # load-bearing for correctness.
        mkdir -p "$HERMES_ROOT"
        # D4: if the root path is itself a (dangling-after-this) symlink,
        # drop it first so the rename lands a real directory there instead
        # of replacing the link target through it.
        [[ -L $DEV_TEAM_HOME ]] && rm "$DEV_TEAM_HOME"
        # N1 (review r2): an existing real root directory must go first, or
        # `mv` moves OLD *into* it (dev-team/dev-team/...). The plan only gets
        # here when the root holds no data; rmdir refuses a non-empty
        # directory, so under set -e this can never delete anything.
        [[ -d $DEV_TEAM_HOME && ! -L $DEV_TEAM_HOME ]] && rmdir "$DEV_TEAM_HOME"
        mv "$OLD_TEAM_HOME" "$DEV_TEAM_HOME"
        ln -s "$DEV_TEAM_HOME" "$OLD_TEAM_HOME"
        mkdir -p "$backup"
        cp -a "$DEV_TEAM_HOME"/. "$backup"/
        say "Migrated team state: $OLD_TEAM_HOME -> $DEV_TEAM_HOME"
        say "  (backup kept at $backup)"
        say "  entries moved:"
        for e in "${entries_moved[@]}"; do say "    $e"; done
        # Every profile that pointed at the old home, not only the ones
        # --only selected (review finding #1): the old location was just
        # renamed out from under them, so leaving their link alone would
        # make it dangling. The D3 symlink above already keeps it resolving,
        # but a direct relink shortens the chain.
        for p in $AFFECTED_PROFILES; do
            [[ "$p" == "__root_link__" ]] && continue
            ppdir="$(dirname "$(hermes -p "$p" config path)")"
            ln -sfn "$DEV_TEAM_HOME" "$ppdir/dev-team"
            say "  relinked: $ppdir/dev-team -> $DEV_TEAM_HOME"
        done
    fi
fi

say "Hermes home:   $HERMES_ROOT"
say "Mnemosyne:     $PLUGIN_SRC_MNEMO"
say "dev-team-gate: $PLUGIN_SRC_GATE"
say "Model:         $( ((SKIP_MODEL)) && echo '(unchanged)' || echo "$MODEL" )"
[[ -n $PREFIX ]] && say "Name prefix:   $PREFIX"
say "Memory:        $( [[ -n $SHARED_DATA ]] && echo "team bank at $TEAM_MEMORY_HOME" || echo 'separate per profile' )"
say "Team state:    $DEV_TEAM_HOME (shared across every profile)"
say ""

# deciders tier table: $DEV_TEAM_HOME/model-tiers.json is the same file
# $HERMES_HOME/dev-team/model-tiers.json resolves to in every installed
# profile, via the dev-team symlink each profile gets below (step 7b).
MODEL_TIERS_PATH="$DEV_TEAM_HOME/model-tiers.json"
MODEL_TIERS_JSON="$(build_model_tiers_json)"
say "Tier table:"
say "  fast:     $( [[ -n $TIER_FAST_PROVIDER ]] && echo "$TIER_FAST_PROVIDER:$TIER_FAST_MODEL" || echo '(absent)' )"
say "  standard: $TIER_STANDARD_PROVIDER:$TIER_STANDARD_MODEL"
say "  deep:     $TIER_DEEP_PROVIDER:$TIER_DEEP_MODEL"
if (( DRY_RUN )); then
    say "  would write: $MODEL_TIERS_PATH"
    printf '%s\n' "$MODEL_TIERS_JSON" | sed 's/^/    /'
else
    mkdir -p "$DEV_TEAM_HOME"
    printf '%s' "$MODEL_TIERS_JSON" > "$MODEL_TIERS_PATH"
fi
say ""

created=0; updated=0; unshared=""; adopted=""
if [[ -n $SHARED_DATA ]]; then
    if (( DRY_RUN )); then
        [[ -d $TEAM_MEMORY_HOME ]] || say "  would create: $TEAM_MEMORY_HOME"
    else
        run mkdir -p "$TEAM_MEMORY_HOME"
    fi
fi
# One timestamp per run, reused for every profile --adopt-team-memory backs
# up: distinct profiles live in distinct directories, so a shared timestamp
# cannot collide.
ADOPT_TS="$(date +%Y%m%d%H%M%S)"
for i in "${!NAMES[@]}"; do
    role="${NAMES[$i]}"
    desc="${DESCS[$i]}"
    selected "$role" || continue
    name="$PREFIX$role"

    soul="$SOULS_DIR/SOUL-$role.md"

    if hermes profile show "$name" >/dev/null 2>&1; then
        say "→ $name (exists, updating)"
        updated=$((updated + 1))
        existed=1
    else
        say "→ $name (creating)"
        run hermes profile create "$name" --clone --description "$desc"
        created=$((created + 1))
        existed=0
    fi

    # Ask Hermes where the profile actually lives instead of assuming
    # $HERMES_ROOT/profiles/$name.
    if (( DRY_RUN )); then
        profile_dir="$HERMES_ROOT/profiles/$name"
    else
        profile_dir="$(dirname "$(hermes -p "$name" config path)")"
    fi

    # F2 (review r2): canonicalise $profile_dir itself FIRST, before SOUL,
    # the prefix file, or any link -- not just before the mnemosyne/ link
    # as D2 originally had it. Refuse if it resolves outside
    # $HERMES_ROOT/profiles (review repro: profiles/dev-qa -> $HERMES_ROOT
    # turns SOUL.md/dev-team-prefix writes and the plugin-link mkdir below
    # into writes onto the Hermes root itself, before the mnemosyne-only
    # check further down ever ran).
    _profile_dir_real="$(resolve_abs_nearest "$profile_dir")" || {
        echo "Cannot resolve $name's profile directory: $profile_dir" >&2
        exit 1
    }
    # F2 (review r3, item 4b): a profile dir that resolves to EXACTLY
    # $HERMES_ROOT/profiles (not merely nested under it) must also be
    # refused -- path_is_or_under() and identity_overlap() both treat
    # "is" and "is under" as the same acceptable case, but profiles/
    # itself is never a valid profile directory: writing SOUL.md, the
    # prefix file or the plugin link there would land on the shared
    # profiles/ tree, not on a single profile.
    if [[ "$_profile_dir_real" == "$HERMES_ROOT_PROFILES_REAL" ]] \
            || { [[ -e "$HERMES_ROOT_PROFILES_REAL" ]] \
                 && [[ "$_profile_dir_real" -ef "$HERMES_ROOT_PROFILES_REAL" ]]; }; then
        echo "$name's profile directory ($profile_dir) resolves to \$HERMES_ROOT/profiles itself; refusing to touch its profile." >&2
        exit 1
    fi
    if ! path_is_or_under "$HERMES_ROOT_PROFILES_REAL" "$_profile_dir_real" \
            && ! identity_overlap "$_profile_dir_real" "$HERMES_ROOT_PROFILES_REAL"; then
        echo "$name's profile directory ($profile_dir) resolves outside \$HERMES_ROOT/profiles; refusing to touch its profile." >&2
        exit 1
    fi

    # 2. SOUL
    if (( KEEP_SOUL )) && [[ -f "$profile_dir/SOUL.md" ]]; then
        say "  SOUL.md kept (--keep-soul)"
    else
        run cp "$soul" "$profile_dir/SOUL.md"
    fi

    # 2b. The team prefix, so skills and souls can build prefixed assignee
    # names (`<prefix>qa`, never a bare `qa`, which the dispatcher would hand
    # to a different, unguarded profile). Per profile, not in the shared
    # team home: two teams with different prefixes share that directory.
    if (( DRY_RUN )); then
        say "  would write: $profile_dir/dev-team-prefix ($PREFIX)"
    else
        printf '%s\n' "$PREFIX" > "$profile_dir/dev-team-prefix"
    fi

    # 3. model per role: pm, architect and reviewer get the frontier tier;
    # the rest get the mid tier. --model overrides the whole table,
    # --skip-model leaves it alone.
    if (( ! SKIP_MODEL )); then
        if [[ -n $MODEL_EXPLICIT ]]; then
            role_model="$MODEL"
        else
            case "$role" in
                pm|architect|reviewer) role_model="$FRONTIER_MODEL" ;;
                *)                     role_model="$MID_MODEL" ;;
            esac
        fi
        run hermes -p "$name" config set model.default "$role_model"
    fi

    # 3b. --forge-token-source, devops only: the pr-ci stage is devops' work,
    # so that is the one profile that needs the forge credential. The profile
    # name is "$PREFIX$FORGE_TOKEN_ROLE", derived here, so a --prefix t2-
    # install configures t2-devops.
    if [[ -n $FORGE_TOKEN_SOURCE && $role == "$FORGE_TOKEN_ROLE" ]]; then
        configure_forge_token_source "$name"
    fi

    # 4. Mnemosyne plugin and data links. The gap this closes: a named
    # profile redirects HERMES_HOME to its own directory, and memory
    # providers are discovered under $HERMES_HOME/plugins/. So the Mnemosyne
    # plugin installed at the root is invisible to every named profile
    # without this symlink.
    run mkdir -p "$profile_dir/plugins"
    run ln -sfn "$PLUGIN_SRC_MNEMO" "$profile_dir/plugins/mnemosyne"

    # F2: the profile-dir canonicalisation check now runs at the very top
    # of this loop iteration (see above), before this plugin link and
    # before SOUL/the prefix file, so it no longer needs repeating here.
    # $_profile_dir_real from that earlier check is still valid below.

    # The plugin link above shares Mnemosyne's *code*. It does not share its
    # *data*: the database path resolves from MNEMOSYNE_DATA_DIR if set,
    # else $HERMES_HOME/mnemosyne/data (mnemosyne/cli.py:20-23,
    # mnemosyne/core/beam.py:358-366) -- and a named profile has redirected
    # HERMES_HOME. Point each profile's Mnemosyne data root at the team's
    # dedicated bank, unless --separate-memory asked for isolation. The
    # operator's own bank ($HERMES_ROOT/mnemosyne) is never a candidate here.
    #
    # Why pinning MNEMOSYNE_DATA_DIR/MNEMOSYNE_HOME into the profile's own
    # .env (above) is enough to beat an inherited shell/gateway value, and
    # when it is NOT: hermes_cli/env_loader.py:560 loads that .env with
    # override=True, so a directly-launched, non-routed profile always
    # wins over its inherited environment. But while a routed profile-home
    # override is active (a multiplex gateway turn, or a routed cron fire
    # in the desktop backend), env_loader.py:535-547 skips that .env load
    # entirely to avoid leaking one profile's credentials to another; and a
    # managed worker never reaches load_hermes_dotenv's .env read at all --
    # it returns before opening any file once worker_config_snapshot() is
    # bound (env_loader.py:513-515), because the worker inherits its
    # owner process's already-resolved environment instead.
    if [[ -n $SHARED_DATA ]]; then
        if (( DRY_RUN )); then
            if [[ -d "$profile_dir/mnemosyne" && ! -L "$profile_dir/mnemosyne" ]] && (( existed )); then
                if (( ADOPT_TEAM_MEMORY )); then
                    backup_dir="$profile_dir/mnemosyne.pre-team-memory-$ADOPT_TS"
                    if [[ -e "$backup_dir" || -L "$backup_dir" ]]; then
                        _bk_n=1
                        while [[ -e "$backup_dir.$_bk_n" || -L "$backup_dir.$_bk_n" ]]; do _bk_n=$((_bk_n + 1)); done
                        backup_dir="$backup_dir.$_bk_n"
                    fi
                    say "  would back up: $profile_dir/mnemosyne -> $backup_dir"
                    say "  would link: $profile_dir/mnemosyne -> $TEAM_MEMORY_HOME"
                    pin_mnemosyne_data_dir "$profile_dir" "$TEAM_MEMORY_HOME/data"
                else
                    say "  would leave: $profile_dir/mnemosyne (private, real directory;"
                    say "               re-run with --adopt-team-memory to move it aside and share)"
                    pin_mnemosyne_data_dir "$profile_dir" "$profile_dir/mnemosyne/data"
                fi
            else
                say "  would link: $profile_dir/mnemosyne -> $TEAM_MEMORY_HOME"
                pin_mnemosyne_data_dir "$profile_dir" "$TEAM_MEMORY_HOME/data"
            fi
        elif [[ -L "$profile_dir/mnemosyne" ]]; then
            # Re-run, already a link. If an older installer pointed it at
            # the operator's own bank -- $HERMES_ROOT/mnemosyne or
            # MNEMOSYNE_HOME, the bank itself, a path inside it, or a
            # directory containing it (r3 review finding #4) -- say so: the
            # operator loses this profile's access to those memories by
            # repointing, even though the operator bank itself is untouched.
            _prev_link_target="$(resolve_abs_nearest "$profile_dir/mnemosyne" 2>/dev/null || true)"
            if [[ -n "$_prev_link_target" ]] && touches_operator_bank "$_prev_link_target"; then
                say "  NOTE: $profile_dir/mnemosyne was linked to the operator's own bank;"
                say "        repointing to the team bank. The operator's bank is untouched,"
                say "        but this profile loses access to memories only there."
            fi
            run ln -sfn "$TEAM_MEMORY_HOME" "$profile_dir/mnemosyne"
            pin_mnemosyne_data_dir "$profile_dir" "$TEAM_MEMORY_HOME/data"
        elif [[ -d "$profile_dir/mnemosyne" ]] && (( existed )); then
            # D2: canonicalise BEFORE the mv below and refuse if it would
            # move the operator's own bank, or if it already IS the team
            # bank (nothing to move).
            _profile_mnemo_real="$(resolve_abs_nearest "$profile_dir/mnemosyne")" || {
                echo "Cannot resolve $name's mnemosyne/: $profile_dir/mnemosyne" >&2
                exit 1
            }
            if touches_operator_bank "$_profile_mnemo_real"; then
                echo "$name's mnemosyne/ ($profile_dir/mnemosyne) resolves to the operator's own bank; refusing to move, remove or relink it." >&2
                exit 1
            fi
            if [[ "$_profile_mnemo_real" == "$_team_home_real" ]] || identity_overlap_either "$_profile_mnemo_real" "$_team_home_real"; then
                echo "$name's mnemosyne/ ($profile_dir/mnemosyne) already resolves to the team bank; refusing to move it." >&2
                exit 1
            fi
            if (( ADOPT_TEAM_MEMORY )); then
                # Opt-in: back up the real private directory (never delete
                # it), then link to the team bank. Merging its contents
                # into the team bank is out of scope for this installer.
                backup_dir="$profile_dir/mnemosyne.pre-team-memory-$ADOPT_TS"
                # D3: an existing backup path (a directory or a link) must
                # never be merged into -- `mv` would land the private bank
                # INSIDE it, and if it is a link to the operator's bank
                # this installer would write into that bank. Pick a new
                # unique suffix instead.
                if [[ -e "$backup_dir" || -L "$backup_dir" ]]; then
                    _bk_n=1
                    while [[ -e "$backup_dir.$_bk_n" || -L "$backup_dir.$_bk_n" ]]; do _bk_n=$((_bk_n + 1)); done
                    backup_dir="$backup_dir.$_bk_n"
                fi
                run mv -- "$profile_dir/mnemosyne" "$backup_dir"
                run ln -sfn "$TEAM_MEMORY_HOME" "$profile_dir/mnemosyne"
                say "  ADOPTED: $name's private mnemosyne/ moved to $backup_dir"
                say "           (backup, not deleted) and linked to the team bank."
                say "           Merging its old memories into the team bank is out"
                say "           of scope for this installer."
                adopted="$adopted $name"
                pin_mnemosyne_data_dir "$profile_dir" "$TEAM_MEMORY_HOME/data"
            else
                # A real directory on a profile that existed before this run
                # means it has accumulated its own memories. Moving them is not
                # ours to decide, so say so and leave it alone rather than
                # deleting someone's history.
                say "  NOTE: $profile_dir/mnemosyne is a real directory with its own"
                say "        database. Left as-is — this profile will not share the"
                say "        team bank. Re-run with --adopt-team-memory to move it"
                say "        aside (as a backup) and link to the team bank, or"
                say "        remove/merge it yourself, then re-run."
                unshared="$unshared $name"
                pin_mnemosyne_data_dir "$profile_dir" "$profile_dir/mnemosyne/data"
            fi
        else
            # Nothing there yet, or — the case this fixes — the real
            # directory `hermes profile create --clone` made a few lines up
            # in this very run. It holds no memories of this profile's own,
            # so it is replaced by the link to the team bank.
            if [[ -d "$profile_dir/mnemosyne" ]]; then
                # D2: canonicalise BEFORE this rm -rf too.
                _profile_mnemo_real="$(resolve_abs_nearest "$profile_dir/mnemosyne")" || {
                    echo "Cannot resolve $name's mnemosyne/: $profile_dir/mnemosyne" >&2
                    exit 1
                }
                if touches_operator_bank "$_profile_mnemo_real" || [[ "$_profile_mnemo_real" == "$_team_home_real" ]] \
                        || identity_overlap_either "$_profile_mnemo_real" "$_team_home_real"; then
                    echo "$profile_dir/mnemosyne resolves to the operator's own bank or the team bank; refusing to remove it." >&2
                    exit 1
                fi
                rm -rf -- "$profile_dir/mnemosyne"
            fi
            run ln -sfn "$TEAM_MEMORY_HOME" "$profile_dir/mnemosyne"
            pin_mnemosyne_data_dir "$profile_dir" "$TEAM_MEMORY_HOME/data"
        fi
    else
        pin_mnemosyne_data_dir "$profile_dir" "$profile_dir/mnemosyne/data"
    fi

    # Team state (forge project records, team clones): same gap as
    # Mnemosyne's data link, same fix, always shared (see DEV_TEAM_HOME
    # above).
    if (( DRY_RUN )); then
        say "  would link: $profile_dir/dev-team -> $DEV_TEAM_HOME"
    else
        run mkdir -p "$DEV_TEAM_HOME"
        if [[ -L "$profile_dir/dev-team" ]]; then
            run ln -sfn "$DEV_TEAM_HOME" "$profile_dir/dev-team"
        elif [[ -d "$profile_dir/dev-team" ]]; then
            say "  NOTE: $profile_dir/dev-team is a real directory. Left as-is —"
            say "        this profile will not share the team's forge records."
        else
            run ln -sfn "$DEV_TEAM_HOME" "$profile_dir/dev-team"
        fi
    fi

    # 5. Kanban is how this team actually works: the pm profile writes
    # cards, the dispatcher spawns the assignee, and a worker raises cards
    # for siblings. Workers inherit their profile's *CLI* toolsets at
    # dispatch time, and the kanban toolset ships disabled by default — so
    # without this, every profile silently lacks kanban_create/kanban_link
    # and cannot delegate onward.
    run hermes -p "$name" tools enable kanban --platform cli

    # 6. dev-team-gate: the plugin that enforces the pipeline. Linked the
    # same way Mnemosyne is, then explicitly enabled — unlike Mnemosyne,
    # a hook plugin does nothing until it is in `plugins.enabled`.
    # --no-allow-tool-override: this plugin declares no tool overrides, so
    # the confirmation prompt would never fire, but passing it keeps the
    # script non-interactive regardless.
    run mkdir -p "$profile_dir/plugins"
    run ln -sfn "$PLUGIN_SRC_GATE" "$profile_dir/plugins/dev-team-gate"
    run hermes -p "$name" plugins enable dev-team-gate --no-allow-tool-override

    # 7. Pipeline stage skills: every dev-team-stage-* skill, for every role,
    # since the guard identifies a card's stage from the skill the card
    # itself carries, not from the completing profile's role.
    # Plus dev-team-deciders, beside the stage skills, for every profile —
    # the design asks that every role be able to decide/extract/summarize.
    run mkdir -p "$profile_dir/skills/dev-team"
    if (( DRY_RUN )); then
        for stage_dir in "$SKILLS_DIR"/dev-team-stage-*; do
            [[ -d $stage_dir ]] || continue
            say "  would install skill: dev-team/$(basename "$stage_dir")"
        done
        say "  would install skill: dev-team/dev-team-deciders"
    else
        for stage_dir in "$SKILLS_DIR"/dev-team-stage-*; do
            [[ -d $stage_dir ]] || continue
            cp -R "$stage_dir" "$profile_dir/skills/dev-team/"
        done
        cp -R "$DECIDERS_SKILL_DIR" "$profile_dir/skills/dev-team/"
    fi
done

# The OpenRouter key for deciders: this installer never reads, prompts for
# or writes it. Only print where to put it -- never a per-profile plaintext
# file (I5): a shared user environment variable is one copy, not eight.
deciders_key_note() {
    if command -v secret >/dev/null 2>&1; then
        say "OpenRouter key for deciders: store it with \`secret set openrouter/deciders\`"
    else
        say "OpenRouter key for deciders: set it as a user environment variable, e.g. in"
        say "  your shell profile: export OPENROUTER_DECIDERS_KEY=<key>"
    fi
}

if (( DRY_RUN )); then
    say ""
    say "  would install: running-the-pipeline skill + bin/kanban-budget.py"
    say "                 into the '$PREFIX$ENTRY_PROFILE' profile"
    say "  would install: ~/.local/bin/forge"
    say "  would install: ~/.local/bin/deciders"
    say ""
    deciders_key_note
    say "Dry run: nothing was changed."
    exit 0
fi

# pm additionally gets the orchestration skill (how to intake, decompose,
# budget-check and report) and the budget script, because kanban has no
# native cost tracking. The other roles receive a card and do one job.
if selected "$ENTRY_PROFILE"; then
    entry_dir="$(dirname "$(hermes -p "$PREFIX$ENTRY_PROFILE" config path)")"
    say ""
    say "Entry point: $PREFIX$ENTRY_PROFILE"
    if [[ -d "$SKILLS_DIR/running-the-pipeline" ]]; then
        mkdir -p "$entry_dir/skills"
        cp -R "$SKILLS_DIR/running-the-pipeline" "$entry_dir/skills/"
        say "  skill:  running-the-pipeline"
    fi
    if [[ -f $KANBAN_BUDGET_PY ]]; then
        mkdir -p "$entry_dir/bin"
        cp "$KANBAN_BUDGET_PY" "$entry_dir/bin/"
        chmod +x "$entry_dir/bin/kanban-budget.py"
        say "  script: bin/kanban-budget.py"
    fi
fi

# `forge` on a PATH the gateway sees: ~/.local/bin, the lesson from the graft
# plugin work. A thin wrapper so every profile's own `python3` need not be
# found separately — the forge directory holds its own imports
# (adapters, forge_core) that resolve off the script's own path, so the
# wrapper only needs to point python3 at forge.py itself.
mkdir -p "$HOME/.local/bin"
cat > "$FORGE_BIN" <<EOF
#!/usr/bin/env bash
$FORGE_MARKER
exec python3 "$FORGE_PY" "\$@"
EOF
chmod +x "$FORGE_BIN"
say ""
say "forge installed: $FORGE_BIN"

# `deciders` on the same PATH, same wrapper shape as `forge` above.
cat > "$DECIDERS_BIN" <<EOF
#!/usr/bin/env bash
$DECIDERS_MARKER
exec python3 "$DECIDERS_PY" "\$@"
EOF
chmod +x "$DECIDERS_BIN"
say "deciders installed: $DECIDERS_BIN"

# The OpenRouter key for deciders: printed by deciders_key_note (defined
# above, also shown in --dry-run).
say ""
deciders_key_note

# A check that can race the gateway's plugin reload right after `plugins
# enable` (Task 12: the first run exited 1 although the plugin was enabled).
# Retried a bounded number of times before it counts as a failure.
VERIFY_RETRIES="${DEV_TEAM_VERIFY_RETRIES:-10}"
VERIFY_DELAY="${DEV_TEAM_VERIFY_DELAY:-1}"
eventually() {
    local attempt=1
    while ! "$@"; do
        (( attempt >= VERIFY_RETRIES )) && return 1
        attempt=$((attempt + 1))
        sleep "$VERIFY_DELAY"
    done
}
gate_enabled() {
    # Captured first, not piped into `grep -q`: under pipefail an early grep
    # exit can SIGPIPE hermes and fail a check that actually matched.
    local out
    out="$(hermes -p "$1" plugins list --enabled --plain 2>/dev/null || true)"
    [[ $out == *dev-team-gate* ]]
}
gate_doctor()  { hermes -p "$1" plugins doctor dev-team-gate --ci >/dev/null 2>&1; }

say ""
say "Verifying"
say "────────────────────────────────────────"
failed=0

# deciders + model-tiers.json: one check, not per-profile — both are shared
# installs, not something each profile wires separately.
if "$DECIDERS_BIN" --help >/dev/null 2>&1; then
    say "  deciders --help: ok ✓"
else
    say "  deciders --help: FAILED ✗"
    failed=$((failed + 1))
fi
if python3 -c 'import json, sys; json.load(open(sys.argv[1]))' "$MODEL_TIERS_PATH" >/dev/null 2>&1; then
    say "  model-tiers.json: parses ✓"
else
    say "  model-tiers.json: FAILED to parse ✗"
    failed=$((failed + 1))
fi

for role in "${NAMES[@]}"; do
    selected "$role" || continue
    name="$PREFIX$role"

    status="$(hermes -p "$name" memory status 2>&1 || true)"
    if printf '%s' "$status" | grep -q 'Status:.*available'; then
        say "  $name: mnemosyne available ✓"
    else
        say "  $name: mnemosyne NOT available ✗"
        failed=$((failed + 1))
    fi

    if eventually gate_doctor "$name"; then
        say "  $name: dev-team-gate doctor ✓"
    else
        say "  $name: dev-team-gate doctor ✗"
        failed=$((failed + 1))
    fi

    if eventually gate_enabled "$name"; then
        say "  $name: dev-team-gate enabled ✓"
    else
        say "  $name: dev-team-gate NOT enabled ✗"
        failed=$((failed + 1))
    fi
done

say ""
say "Created $created, updated $updated."
if [[ -n $adopted ]]; then
    say ""
    say "Adopted into the team bank (--adopt-team-memory):$adopted"
    say "Each profile's old private mnemosyne/ was backed up alongside it as"
    say "mnemosyne.pre-team-memory-<timestamp> (never deleted) before linking."
    say "Merging those old memories into the team bank is out of scope for this"
    say "installer."
fi
if [[ -n $unshared ]]; then
    say ""
    say "These profiles kept their own Mnemosyne database and will NOT see the"
    say "team's shared memory:$unshared"
    say "Each already had a real mnemosyne/ directory, so nothing was deleted."
fi
if (( failed )); then
    say "$failed check(s) failed — see 'hermes -p <name> memory status' and"
    say "'hermes -p <name> plugins doctor dev-team-gate'."
    exit 1
fi
say "All profiles are wired to Mnemosyne and the dev-team-gate plugin."
