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
# Set when the caller passes --model, so one explicit model beats the table.
MODEL_EXPLICIT=""
ONLY=""
SKIP_MODEL=0
KEEP_SOUL=0
DRY_RUN=0
PREFIX="dev-"
SHARED_DATA=1
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOULS_DIR="$SCRIPT_DIR/souls"
SKILLS_DIR="$SCRIPT_DIR/skills"
PLUGIN_SRC_GATE="$SCRIPT_DIR/plugin/dev-team-gate"
FORGE_PY="$SCRIPT_DIR/forge/forge.py"
KANBAN_BUDGET_PY="$SCRIPT_DIR/scripts/kanban-budget.py"

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

# Resolve HERMES_HOME from Hermes itself rather than assuming ~/.hermes, so a
# custom HERMES_HOME keeps working.
ROOT_CONFIG="$(hermes config path)"
HERMES_ROOT="$(dirname "$ROOT_CONFIG")"
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

say "Hermes home:   $HERMES_ROOT"
say "Mnemosyne:     $PLUGIN_SRC_MNEMO"
say "dev-team-gate: $PLUGIN_SRC_GATE"
say "Model:         $( ((SKIP_MODEL)) && echo '(unchanged)' || echo "$MODEL" )"
[[ -n $PREFIX ]] && say "Name prefix:   $PREFIX"
say "Memory:        $( [[ -n $SHARED_DATA ]] && echo "shared bank at $MNEMO_HOME" || echo 'separate per profile' )"
say "Team state:    $DEV_TEAM_HOME (shared across every profile)"
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
        elif [[ -d "$profile_dir/mnemosyne" ]]; then
            # A real directory here means this profile has already
            # accumulated its own memories. Moving them is not ours to
            # decide, so say so and leave it alone rather than deleting
            # someone's history.
            say "  NOTE: $profile_dir/mnemosyne is a real directory with its own"
            say "        database. Left as-is — this profile will not share the"
            say "        team bank. Remove or merge it, then re-run, to share."
            unshared="$unshared $name"
        else
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
    run mkdir -p "$profile_dir/skills/dev-team"
    if (( DRY_RUN )); then
        for stage_dir in "$SKILLS_DIR"/dev-team-stage-*; do
            [[ -d $stage_dir ]] || continue
            say "  would install skill: dev-team/$(basename "$stage_dir")"
        done
    else
        for stage_dir in "$SKILLS_DIR"/dev-team-stage-*; do
            [[ -d $stage_dir ]] || continue
            cp -R "$stage_dir" "$profile_dir/skills/dev-team/"
        done
    fi
done

if (( DRY_RUN )); then
    say ""
    say "  would install: running-the-pipeline skill + bin/kanban-budget.py"
    say "                 into the '$PREFIX$ENTRY_PROFILE' profile"
    say "  would install: ~/.local/bin/forge"
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
cat > "$HOME/.local/bin/forge" <<EOF
#!/usr/bin/env bash
exec python3 "$FORGE_PY" "\$@"
EOF
chmod +x "$HOME/.local/bin/forge"
say ""
say "forge installed: $HOME/.local/bin/forge"

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

    if hermes -p "$name" plugins doctor dev-team-gate --ci >/dev/null 2>&1; then
        say "  $name: dev-team-gate doctor ✓"
    else
        say "  $name: dev-team-gate doctor ✗"
        failed=$((failed + 1))
    fi

    if hermes -p "$name" plugins list --enabled --plain 2>/dev/null | grep -q 'dev-team-gate'; then
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
