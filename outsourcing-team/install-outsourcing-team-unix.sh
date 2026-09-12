#!/usr/bin/env bash
set -Eeuo pipefail

# Creates the eleven outsourcing delivery team profiles on an existing Hermes
# install and points each one at Mnemosyne. Everything here is idempotent:
# re-running upgrades the SOUL, the model and the plugin link in place rather
# than erroring out.

MODEL="anthropic/claude-sonnet-5"
ONLY=""
SKIP_MODEL=0
KEEP_SOUL=0
DRY_RUN=0
PREFIX=""
SHARED_DATA=1

usage() { cat <<'EOF'
Usage: install-outsourcing-team-unix.sh [options]
  --model MODEL      Model to set on each profile
                     (default: anthropic/claude-sonnet-5)
  --only a,b,c       Create only these profiles instead of all eleven
  --prefix PREFIX    Prepend PREFIX to every profile name, e.g. --prefix os-
                     creates os-qa-lead. Use to coexist with another team.
  --skip-model       Leave each profile's model at whatever it inherited
  --keep-soul        Do not overwrite an existing SOUL.md
  --separate-memory  Give each profile its own Mnemosyne database instead of
                     sharing the root one. Roles then cannot read each other's
                     notes, so the board no longer hands work between them.
  --dry-run          Print what would happen and change nothing
  -h, --help         Show help

Profiles: engagement-lead delivery-manager business-analyst domain-consultant
          solution-architect app-engineer integration-engineer qa-lead
          platform-sre security-compliance presales-writer
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)  MODEL="${2:-}";  [[ -n $MODEL  ]] || { echo "--model needs a value"  >&2; exit 2; }; shift 2 ;;
        --only)   ONLY="${2:-}";   [[ -n $ONLY   ]] || { echo "--only needs a value"   >&2; exit 2; }; shift 2 ;;
        --prefix) PREFIX="${2:-}"; [[ -n $PREFIX ]] || { echo "--prefix needs a value" >&2; exit 2; }; shift 2 ;;
        --skip-model) SKIP_MODEL=1; shift ;;
        --keep-soul)  KEEP_SOUL=1;  shift ;;
        --separate-memory) SHARED_DATA=""; shift ;;
        --dry-run)    DRY_RUN=1;    shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if (( SKIP_MODEL )) && [[ "$MODEL" != "anthropic/claude-sonnet-5" ]]; then
    echo '--skip-model and --model contradict each other: one sets the model, the' >&2
    echo 'other leaves it alone. Pick one.' >&2
    exit 2
fi

# A prefix ending in a character Hermes will not accept in a profile name turns
# into a confusing failure eleven lines later, so check it here.
if [[ -n $PREFIX && ! $PREFIX =~ ^[a-zA-Z0-9_-]+$ ]]; then
    echo "--prefix must contain only letters, digits, hyphen and underscore." >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOULS_DIR="$SCRIPT_DIR/souls"

# Profile name, then the description the kanban decomposer routes tasks on.
# Parallel arrays rather than an associative array: bash 3.2 (stock macOS) has
# no `declare -A`, and this script has to run there.
NAMES=(engagement-lead delivery-manager business-analyst domain-consultant
       solution-architect app-engineer integration-engineer qa-lead
       platform-sre security-compliance presales-writer)
DESCS=(
    "Engagement ownership: client relationship, commercial framing, escalation, scope boundary, exit."
    "Delivery management: decomposes outcomes into cards, sequences work, manages burn, margin and risk."
    "Business analysis: elicits and traces requirements, writes testable acceptance criteria, as-is to to-be process."
    "Industry domain consulting: vertical knowledge and vocabulary for manufacturing, finance, e-commerce, healthcare, public sector."
    "Solution architecture: boundaries, contracts and technology commitments inside a client's existing estate."
    "Application engineering: builds features end to end in the client's codebase, following the client's conventions."
    "Integration and data migration: connects unchangeable systems, profiles and moves data with reconciliation and rollback."
    "QA leadership: acceptance evidence, traceability, client UAT, regression of existing behaviour, contractual exit criteria."
    "Platform and SRE: pipelines, environments, infrastructure and telemetry in the client's cloud, handed over operable."
    "Security and compliance: threat models, security review, control and evidence gaps, third-party vendor exposure."
    "Presales and client communication: proposals, SOW scope language, assumptions and exclusions, status reports, handover docs."
)

say()  { printf '%s\n' "$*"; }
run()  { if (( DRY_RUN )); then printf '  would run: %s\n' "$*"; else "$@"; fi; }

selected() {
    [[ -z $ONLY ]] && return 0
    # Comma-separated match, anchored so `qa-lead` cannot match `qa-leader`.
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

# Resolve HERMES_HOME from Hermes itself rather than assuming ~/.hermes, so a
# custom HERMES_HOME keeps working.
ROOT_CONFIG="$(hermes config path)"
HERMES_ROOT="$(dirname "$ROOT_CONFIG")"
PLUGIN_SRC="$HERMES_ROOT/plugins/mnemosyne"
# The one real Mnemosyne data root, which every profile will be pointed at
# unless --separate-memory was passed. Honour MNEMOSYNE_HOME if the user has
# already relocated the store.
MNEMO_HOME="${MNEMOSYNE_HOME:-$HERMES_ROOT/mnemosyne}"

if [[ ! -d $PLUGIN_SRC ]]; then
    echo "Mnemosyne plugin not found at $PLUGIN_SRC." >&2
    echo "Run ../install-mnemosyne-hermes-unix.sh first." >&2
    exit 1
fi

# The provider must be active on the root profile before profiles are cloned
# from it: `hermes profile create --clone` copies config.yaml as a file, so
# whatever memory settings are in place at clone time are what each profile
# gets.
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

say "Hermes home:   $HERMES_ROOT"
say "Mnemosyne:     $PLUGIN_SRC"
say "Model:         $( ((SKIP_MODEL)) && echo '(unchanged)' || echo "$MODEL" )"
[[ -n $PREFIX ]] && say "Name prefix:   $PREFIX"
say "Memory:        $( [[ -n $SHARED_DATA ]] && echo "shared bank at $MNEMO_HOME" || echo 'separate per profile' )"
say ""

created=0; updated=0; unshared=""
for i in "${!NAMES[@]}"; do
    role="${NAMES[$i]}"
    desc="${DESCS[$i]}"
    selected "$role" || continue
    name="$PREFIX$role"

    soul="$SOULS_DIR/SOUL-$role.md"
    [[ -f $soul ]] || { echo "Missing $soul" >&2; exit 1; }

    if hermes profile show "$name" >/dev/null 2>&1; then
        say "→ $name (exists, updating)"
        updated=$((updated + 1))
    else
        say "→ $name (creating)"
        run hermes profile create "$name" --clone --description "$desc"
        created=$((created + 1))
    fi

    # Ask Hermes where the profile actually lives instead of assuming
    # $HERMES_ROOT/profiles/$name.
    if (( DRY_RUN )); then
        profile_dir="$HERMES_ROOT/profiles/$name"
    else
        profile_dir="$(dirname "$(hermes -p "$name" config path)")"
    fi

    if (( KEEP_SOUL )) && [[ -f "$profile_dir/SOUL.md" ]]; then
        say "  SOUL.md kept (--keep-soul)"
    else
        run cp "$soul" "$profile_dir/SOUL.md"
    fi

    (( SKIP_MODEL )) || run hermes -p "$name" config set model.default "$MODEL"

    # The gap this script exists to close. A named profile redirects
    # HERMES_HOME to its own directory, and memory providers are discovered
    # under $HERMES_HOME/plugins/. So the Mnemosyne plugin installed at the
    # root is invisible to every named profile: `memory status` reports
    # "Provider: mnemosyne" (config was cloned) next to "Plugin: NOT
    # installed", and the agent silently runs with no memory at all. A symlink
    # per profile points them back at the one real install.
    run mkdir -p "$profile_dir/plugins"
    run ln -sfn "$PLUGIN_SRC" "$profile_dir/plugins/mnemosyne"

    # The plugin link above shares Mnemosyne's *code*. It does not share its
    # *data*: the database path resolves from MNEMOSYNE_HOME, which defaults to
    # $HERMES_HOME/mnemosyne — and a named profile has redirected HERMES_HOME.
    # So every profile silently writes its own database, and a role cannot read
    # what another role recorded. That defeats the point of a team that hands
    # work between roles, and like the plugin gap it fails quietly: `memory
    # status` reports "available" either way. Point each profile's Mnemosyne
    # data root at the one real store. (MNEMOSYNE_HOME in the profile's .env is
    # not honoured for this, so the link is the mechanism.)
    if [[ -n $SHARED_DATA ]]; then
        if (( DRY_RUN )); then
            say "  would link: $profile_dir/mnemosyne -> $MNEMO_HOME"
        elif [[ -L "$profile_dir/mnemosyne" ]]; then
            run ln -sfn "$MNEMO_HOME" "$profile_dir/mnemosyne"
        elif [[ -d "$profile_dir/mnemosyne" ]]; then
            # A real directory here means this profile has already accumulated
            # its own memories. Moving them is not ours to decide, so say so
            # and leave it alone rather than deleting someone's history.
            say "  NOTE: $profile_dir/mnemosyne is a real directory with its own"
            say "        database. Left as-is — this profile will not share the"
            say "        team bank. Remove or merge it, then re-run, to share."
            unshared="$unshared $name"
        else
            run ln -sfn "$MNEMO_HOME" "$profile_dir/mnemosyne"
        fi
    fi
done

if (( DRY_RUN )); then
    say ""
    say "Dry run: nothing was changed."
    exit 0
fi

say ""
say "Verifying"
say "────────────────────────────────────────"
failed=0
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
    say "$failed profile(s) cannot load Mnemosyne — see 'hermes -p <name> memory status'."
    exit 1
fi
say "All profiles are wired to Mnemosyne."
