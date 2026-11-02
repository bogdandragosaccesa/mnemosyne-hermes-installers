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
# deciders tier table: PROVIDER:MODEL per tier, raw --tier-* values before
# the colon split below. Unset unless the caller passes the flag (each tier
# then defaults from FAST_MODEL/MID_MODEL/FRONTIER_MODEL).
TIER_FAST_PM=""
TIER_STANDARD_PM=""
TIER_DEEP_PM=""
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
  --tier-fast P:M     deciders tier table entry for the fast tier, as
                     PROVIDER:MODEL (e.g. anthropic:claude-haiku-4-5).
                     Default: $FAST_MODEL split on its first '/'.
                     Every --tier-* model must be a chat model an agent
                     can run on; a decisions-only model is refused.
  --tier-standard P:M deciders tier table entry for the standard tier.
                     Default: $MID_MODEL split on its first '/'.
  --tier-deep P:M     deciders tier table entry for the deep tier.
                     Default: $FRONTIER_MODEL split on its first '/'.
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
# The one real Mnemosyne data root, which every profile will be pointed at
# unless --separate-memory was passed. Honour MNEMOSYNE_HOME if the user has
# already relocated the store.
MNEMO_HOME="${MNEMOSYNE_HOME:-$HERMES_ROOT/mnemosyne}"
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
_relink_cmds_for_choice() {  # chosen_path
    local chosen="$1" i role pdir
    for i in "${!OLD_TARGETS[@]}"; do
        [[ "${OLD_TARGETS[$i]}" == "$chosen" ]] && continue
        for role in ${OLD_TARGET_ROLES[$i]}; do
            [[ "$role" == "__root_link__" ]] && continue
            pdir="$(dirname "$(hermes -p "$role" config path)")"
            echo "    ln -sfn \"$chosen\" \"$pdir/dev-team\"" >&2
        done
    done
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
        echo "  ${OLD_TARGETS[$i]}  (linked from:${OLD_TARGET_ROLES[$i]})" >&2
    done
    echo "Refusing to guess which is current. Reconcile by hand by choosing exactly one candidate below, then re-run:" >&2
    (( NEW_HAS_DATA )) && _print_candidate_choice "the existing root location" "$DEV_TEAM_HOME"
    for i in "${!OLD_TARGETS[@]}"; do
        _print_candidate_choice "the location linked from:${OLD_TARGET_ROLES[$i]}" "${OLD_TARGETS[$i]}"
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
say "Memory:        $( [[ -n $SHARED_DATA ]] && echo "shared bank at $MNEMO_HOME" || echo 'separate per profile' )"
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

created=0; updated=0; unshared=""
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

    # 4. Mnemosyne plugin and data links. The gap this closes: a named
    # profile redirects HERMES_HOME to its own directory, and memory
    # providers are discovered under $HERMES_HOME/plugins/. So the Mnemosyne
    # plugin installed at the root is invisible to every named profile
    # without this symlink.
    run mkdir -p "$profile_dir/plugins"
    run ln -sfn "$PLUGIN_SRC_MNEMO" "$profile_dir/plugins/mnemosyne"

    # The plugin link above shares Mnemosyne's *code*. It does not share its
    # *data*: the database path resolves from MNEMOSYNE_HOME, which defaults
    # to $HERMES_HOME/mnemosyne — and a named profile has redirected
    # HERMES_HOME. Point each profile's Mnemosyne data root at the one real
    # store, unless --separate-memory asked for isolation.
    if [[ -n $SHARED_DATA ]]; then
        if (( DRY_RUN )); then
            say "  would link: $profile_dir/mnemosyne -> $MNEMO_HOME"
        elif [[ -L "$profile_dir/mnemosyne" ]]; then
            run ln -sfn "$MNEMO_HOME" "$profile_dir/mnemosyne"
        elif [[ -d "$profile_dir/mnemosyne" ]] && (( existed )); then
            # A real directory on a profile that existed before this run
            # means it has accumulated its own memories. Moving them is not
            # ours to decide, so say so and leave it alone rather than
            # deleting someone's history.
            say "  NOTE: $profile_dir/mnemosyne is a real directory with its own"
            say "        database. Left as-is — this profile will not share the"
            say "        team bank. Remove or merge it, then re-run, to share."
            unshared="$unshared $name"
        else
            # Nothing there yet, or — the case this fixes — the real
            # directory `hermes profile create --clone` made a few lines up
            # in this very run. It holds no memories of this profile's own,
            # so it is replaced by the link to the shared bank.
            if [[ -d "$profile_dir/mnemosyne" ]]; then
                if [[ "$(cd "$profile_dir/mnemosyne" && pwd -P)" == "$(cd "$MNEMO_HOME" && pwd -P)" ]]; then
                    echo "$profile_dir/mnemosyne is the shared bank itself; refusing to remove it." >&2
                    exit 1
                fi
                rm -rf -- "$profile_dir/mnemosyne"
            fi
            run ln -sfn "$MNEMO_HOME" "$profile_dir/mnemosyne"
        fi
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
