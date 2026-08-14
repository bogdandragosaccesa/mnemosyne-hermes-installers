#!/usr/bin/env bash
#
# Completely removes Mnemosyne (and optionally Hermes Agent) from Linux/macOS.
#
# Reverses install-mnemosyne-hermes-unix.sh. By default this is a total
# removal, including the memory database -- pass --keep-data to preserve it.
#
# Removed by default:
#   * Hermes provider registration:  <HERMES_HOME>/plugins/mnemosyne
#                                    <HERMES_HOME>/plugins/hermes-mnemosyne  (legacy)
#                                    <HERMES_HOME>/profiles/*/plugins/mnemosyne
#   * Bundled skill:                 <HERMES_HOME>/skills/memory/mnemosyne-memory-override
#   * The memory.provider setting in <HERMES_HOME>/config.yaml and in every profile config
#   * The Mnemosyne virtual environment (default ~/.mnemosyne-venv)
#   * The Mnemosyne data directory (default <HERMES_HOME>/mnemosyne)
#   * Blob storage, when MNEMOSYNE_BLOB_DIR points outside the data directory
#   * The MNEMOSYNE_HOME / MNEMOSYNE_NO_EMBEDDINGS exports the installer
#     appended to your shell rc file
#   * mnemosyne packages bootstrapped into Hermes' own venv, if any
#
# With --include-hermes it additionally removes Hermes Agent itself: the whole
# HERMES_HOME tree, the hermes launcher in ~/.local/bin, and the HERMES_HOME
# export in your shell rc file.
#
# Written for bash 3.2 so it runs on stock macOS as well as Linux.

set -Eeuo pipefail

KEEP_DATA=0
INCLUDE_HERMES=0
DRY_RUN=0
FORCE=0
NON_INTERACTIVE=0

usage() { cat <<'EOF'
Usage: uninstall-mnemosyne-hermes-unix.sh [options]
  --keep-data         Keep the Mnemosyne data directory (database, config.yaml, blobs)
  --include-hermes    Also remove Hermes Agent itself
  --dry-run           Print the removal plan and change nothing
  --force             Do not ask for confirmation
  --non-interactive   Never prompt; requires --force to remove anything
  -h, --help          Show help
EOF
}

for arg in "$@"; do
    case "$arg" in
        --keep-data) KEEP_DATA=1 ;;
        --include-hermes) INCLUDE_HERMES=1 ;;
        --dry-run) DRY_RUN=1 ;;
        --force) FORCE=1 ;;
        --non-interactive) NON_INTERACTIVE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ ! -t 0 ]]; then NON_INTERACTIVE=1; fi

if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_NOTE=$'\033[90m'; C_WARN=$'\033[33m'; C_OFF=$'\033[0m'
else
    C_STEP=''; C_OK=''; C_NOTE=''; C_WARN=''; C_OFF=''
fi
write_step() { printf '%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
write_ok()   { printf '%s  - %s%s\n' "$C_OK"   "$1" "$C_OFF"; }
write_note() { printf '%s  . %s%s\n' "$C_NOTE" "$1" "$C_OFF"; }
write_warn() { printf '%s  ! %s%s\n' "$C_WARN" "$1" "$C_OFF"; }

FAILURES=()
add_failure() { FAILURES+=("$1"); write_warn "$1"; }

# 'export FOO="..."' -> 'FOO', for readable reporting.
rc_var_name() { local v="${1%%=*}"; printf '%s' "${v#export }"; }

# ---------------------------------------------------------------------------
# Resolve the same locations the installer used
# ---------------------------------------------------------------------------

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# Mnemosyne resolves its database from MNEMOSYNE_DATA_DIR first, then
# MNEMOSYNE_HOME, then the default under HERMES_HOME.
DATA_DIR="${MNEMOSYNE_DATA_DIR:-${MNEMOSYNE_HOME:-$HERMES_HOME/mnemosyne}}"
MNEMOSYNE_VENV="${MNEMOSYNE_VENV:-$HOME/.mnemosyne-venv}"

HERMES_BIN=""
if command -v hermes >/dev/null 2>&1; then
    HERMES_BIN="$(command -v hermes)"
elif [[ -x "$HOME/.local/bin/hermes" ]]; then
    HERMES_BIN="$HOME/.local/bin/hermes"
fi

HERMES_VENV_PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"
MNEMOSYNE_HERMES="$MNEMOSYNE_VENV/bin/mnemosyne-hermes"
PROFILES_DIR="$HERMES_HOME/profiles"

# launchd agents live under the login account's home, which is not necessarily
# $HOME: profile-mode Hermes repoints HOME, but the agent stays in the real
# user's Library. Hermes resolves this from the passwd database; `eval echo ~user`
# is the portable shell equivalent.
real_home() {
    local pw_home=""
    pw_home="$(eval echo "~$(id -un)" 2>/dev/null || true)"
    if [[ -n "$pw_home" && -d "$pw_home" ]]; then printf '%s\n' "$pw_home"; else printf '%s\n' "$HOME"; fi
}

# Every gateway service artifact Hermes may have registered. The name is
# profile-scoped -- `hermes-gateway.service` / `ai.hermes.gateway.plist` for the
# default profile and a `-<profile>` suffix for the rest -- so these are matched
# by glob rather than by one fixed name, which would miss profile gateways and
# the transient `ai.hermes.gateway.restart-once` agent.
gateway_service_files() {
    local f
    case "$(uname -s)" in
        Darwin)
            for f in "$(real_home)/Library/LaunchAgents"/ai.hermes.gateway*.plist; do
                if [[ -e "$f" ]]; then printf '%s\n' "$f"; fi
            done
            ;;
        *)
            for f in "$HOME/.config/systemd/user"/hermes-gateway*.service \
                     "$HOME/.config/systemd/user/default.target.wants"/hermes-gateway*.service; do
                if [[ -e "$f" || -L "$f" ]]; then printf '%s\n' "$f"; fi
            done
            ;;
    esac
}

# Ask the platform's service manager to let go before the file is deleted.
release_gateway_service() {
    local file="$1" label
    case "$(uname -s)" in
        Darwin)
            command -v launchctl >/dev/null 2>&1 || return 0
            label="$(basename "$file" .plist)"
            launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 \
                || launchctl unload "$file" >/dev/null 2>&1 || true
            ;;
        *)
            command -v systemctl >/dev/null 2>&1 || return 0
            label="$(basename "$file")"
            systemctl --user disable --now "$label" >/dev/null 2>&1 || true
            ;;
    esac
}

# Hermes' own installer puts two different kinds of entry in ~/.local/bin:
# wrapper scripts that exec an interpreter inside HERMES_HOME (hermes,
# hermes-acp, hermes-agent) and symlinks to its vendored node (node, npm, npx).
# Both are dead weight once the tree is gone, and both have to be recognised by
# where they point rather than by name -- but only ever removed when they point
# into HERMES_HOME, so a separately installed `node` is never touched.
# Hermes links its command into get_command_link_dir(): $PREFIX/bin on Termux,
# /usr/local/bin for a root FHS install, ~/.local/bin otherwise -- and drops the
# vendored node/npm/npx symlinks in that same directory. Check all three; the
# pointer test below is what actually decides, so scanning a directory we do not
# own is harmless.
hermes_launchers() {
    local bin_dir entry target first_line
    for bin_dir in "$HOME/.local/bin" /usr/local/bin ${PREFIX:+"$PREFIX/bin"}; do
        [[ -d "$bin_dir" ]] || continue
        for entry in "$bin_dir"/*; do
            [[ -e "$entry" || -L "$entry" ]] || continue
            if [[ -L "$entry" ]]; then
                target="$(readlink "$entry" || true)"
                if [[ "$target" == "$HERMES_HOME"/* ]]; then printf '%s\n' "$entry"; fi
            elif [[ -f "$entry" ]]; then
                first_line=""
                read -r first_line < "$entry" 2>/dev/null || first_line=""
                if [[ "$first_line" == '#!'* ]] && grep -qF -- "$HERMES_HOME/" "$entry" 2>/dev/null; then
                    printf '%s\n' "$entry"
                fi
            fi
        done
    done
}

# Lines persist_env() appends in the installer. They are matched verbatim, so
# the single quotes are deliberate -- nothing here should expand.
# shellcheck disable=SC2016
RC_LINE_HERMES='export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"'
# shellcheck disable=SC2016
RC_LINE_MNEMOSYNE='export MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HOME/.hermes/mnemosyne}"'
# Written only when the installer was given --no-embeddings.
RC_LINE_NO_EMBEDDINGS='export MNEMOSYNE_NO_EMBEDDINGS=1'
RC_FILES=("$HOME/.profile" "$HOME/.zprofile" "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.bash_profile")

# Directories that get deleted outright, as parallel path/label arrays so this
# stays bash 3.2 clean.
TARGET_PATHS=()
TARGET_LABELS=()
add_target() {
    if [[ -n "$1" ]] && [[ -e "$1" ]]; then
        TARGET_PATHS+=("$1")
        TARGET_LABELS+=("$2")
    fi
}

add_target "$HERMES_HOME/plugins/mnemosyne"                       'provider registration'
add_target "$HERMES_HOME/plugins/hermes-mnemosyne"                'legacy provider registration'
add_target "$HERMES_HOME/skills/memory/mnemosyne-memory-override" 'bundled memory skill'

if [[ -d "$PROFILES_DIR" ]]; then
    for profile_dir in "$PROFILES_DIR"/*/; do
        [[ -d "$profile_dir" ]] || continue
        profile_name="$(basename "$profile_dir")"
        add_target "${profile_dir}plugins/mnemosyne" "profile '$profile_name' registration"
    done
fi

add_target "$MNEMOSYNE_VENV" 'virtual environment'
if (( ! KEEP_DATA )); then
    add_target "$DATA_DIR" 'data directory (memory database)'
    # Blob storage (content-sanitizer output) is only somewhere else when
    # MNEMOSYNE_BLOB_DIR points outside the data directory; add_target skips it
    # when it has already gone with the tree above.
    if [[ -n "${MNEMOSYNE_BLOB_DIR:-}" ]]; then
        add_target "$MNEMOSYNE_BLOB_DIR" 'blob storage'
    fi
fi

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

write_step 'Removal plan'
printf '  Hermes home:    %s\n' "$HERMES_HOME"
printf '  Mnemosyne data: %s%s\n' "$DATA_DIR" "$( ((KEEP_DATA)) && printf '  (kept)' || true )"
printf '  Mnemosyne venv: %s\n' "$MNEMOSYNE_VENV"
printf '\n'

if (( ${#TARGET_PATHS[@]} == 0 )); then
    write_note 'No Mnemosyne files found.'
else
    for i in $(seq 0 $(( ${#TARGET_PATHS[@]} - 1 ))); do
        printf '  remove  %s   [%s]\n' "${TARGET_PATHS[$i]}" "${TARGET_LABELS[$i]}"
    done
fi

rc_lines_to_strip() {
    # Echoes "file<TAB>line" for every installer-written export still present.
    local file
    for file in "${RC_FILES[@]}"; do
        [[ -f "$file" ]] || continue
        if grep -qxF -- "$RC_LINE_MNEMOSYNE" "$file"; then printf '%s\t%s\n' "$file" "$RC_LINE_MNEMOSYNE"; fi
        if grep -qxF -- "$RC_LINE_NO_EMBEDDINGS" "$file"; then printf '%s\t%s\n' "$file" "$RC_LINE_NO_EMBEDDINGS"; fi
        if (( INCLUDE_HERMES )) && grep -qxF -- "$RC_LINE_HERMES" "$file"; then printf '%s\t%s\n' "$file" "$RC_LINE_HERMES"; fi
    done
}

while IFS=$'\t' read -r rc_file rc_line; do
    [[ -n "$rc_file" ]] || continue
    printf '  unset   %s   [in %s]\n' "$(rc_var_name "$rc_line")" "$rc_file"
done <<EOF
$(rc_lines_to_strip)
EOF

if [[ -n "$HERMES_BIN" ]] && (( ! INCLUDE_HERMES )); then
    printf '  unset   memory.provider in Hermes configuration\n'
fi
if (( INCLUDE_HERMES )); then
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        printf '  remove  %s   [gateway background service]\n' "$unit"
    done <<EOF
$(gateway_service_files)
EOF
    while IFS= read -r launcher; do
        [[ -n "$launcher" ]] || continue
        printf '  remove  %s   [launcher into Hermes]\n' "$launcher"
    done <<EOF
$(hermes_launchers)
EOF
    printf '  remove  %s   [entire Hermes installation]\n' "$HERMES_HOME"
fi
printf '\n'

if (( DRY_RUN )); then
    write_note 'Dry run: nothing was changed.'
    exit 0
fi

if (( ! FORCE )); then
    if (( NON_INTERACTIVE )); then
        echo 'Refusing to remove anything without confirmation. Re-run with --force (or use --dry-run to preview).' >&2
        exit 1
    fi
    printf 'Proceed with removal? [y/N] '
    read -r answer
    case "$answer" in
        y|Y|yes|YES) ;;
        *) write_note 'Aborted.'; exit 0 ;;
    esac
fi

# ---------------------------------------------------------------------------
# Stop anything still running
# ---------------------------------------------------------------------------

write_step 'Stopping running processes...'

if [[ -n "$HERMES_BIN" ]]; then
    if "$HERMES_BIN" gateway stop >/dev/null 2>&1; then
        write_ok 'Gateway stopped'
    else
        write_note 'Gateway was not running'
    fi
fi

# Unlinking an open file works fine on Unix, so this is about not leaving a
# provider process talking to a database that is about to disappear.
stop_processes_under() {
    local root="$1" pids pid
    command -v pgrep >/dev/null 2>&1 || return 0
    [[ -e "$root" ]] || return 0
    pids="$(pgrep -f -- "$root" 2>/dev/null || true)"
    for pid in $pids; do
        if [[ "$pid" == "$$" || "$pid" == "$PPID" ]]; then continue; fi
        write_note "Stopping PID $pid"
        kill "$pid" >/dev/null 2>&1 || true
    done
    if [[ -n "$pids" ]]; then
        sleep 1
        for pid in $pids; do
            if [[ "$pid" == "$$" || "$pid" == "$PPID" ]]; then continue; fi
            if kill -0 "$pid" >/dev/null 2>&1; then kill -9 "$pid" >/dev/null 2>&1 || true; fi
        done
    fi
}

stop_processes_under "$MNEMOSYNE_VENV"
if (( INCLUDE_HERMES )); then stop_processes_under "$HERMES_HOME"; fi

# ---------------------------------------------------------------------------
# Deregister from Hermes
# ---------------------------------------------------------------------------

# Rewrites a file in place through a temp copy, preserving mode and ownership
# because the final `cat` truncates the original rather than replacing it.
rewrite_file() {
    local file="$1" tmp
    tmp="$(mktemp "${TMPDIR:-/tmp}/mnemosyne-uninstall.XXXXXX")"
    cat > "$tmp"
    if ! cmp -s "$tmp" "$file"; then
        cat "$tmp" > "$file"
        rm -f "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

if (( ! INCLUDE_HERMES )); then
    write_step 'Deregistering from Hermes...'

    # Best effort: the package's own uninstall/cleanup also handles layouts from
    # older releases that this script does not know about.
    if [[ -x "$MNEMOSYNE_HERMES" ]]; then
        "$MNEMOSYNE_HERMES" uninstall >/dev/null 2>&1 || true
        "$MNEMOSYNE_HERMES" cleanup >/dev/null 2>&1 || true
        write_ok 'Ran mnemosyne-hermes uninstall + cleanup'
    fi

    # `mnemosyne-hermes cleanup` only rewrites config.yaml when `provider` is the
    # first key under `memory:`. Hermes writes it last, so unset it explicitly.
    if [[ -n "$HERMES_BIN" ]]; then
        if "$HERMES_BIN" config get memory.provider 2>/dev/null | grep -q 'mnemosyne'; then
            if "$HERMES_BIN" config unset memory.provider >/dev/null 2>&1; then
                write_ok 'Unset memory.provider'
            else
                add_failure 'Could not unset memory.provider; edit config.yaml manually.'
            fi
        else
            write_note 'memory.provider is not set to mnemosyne'
        fi

        # --disable-builtin-memory turns Hermes' own store off. Removing the
        # provider without putting it back would leave Hermes with no memory at
        # all, so restore any key that is currently false.
        for key in memory.memory_enabled memory.user_profile_enabled; do
            if "$HERMES_BIN" config get "$key" 2>/dev/null | grep -qi 'false'; then
                if "$HERMES_BIN" config set "$key" true >/dev/null 2>&1; then
                    write_ok "Re-enabled $key (built-in memory was disabled for Mnemosyne)"
                else
                    add_failure "Could not re-enable $key; set it manually."
                fi
            fi
        done
    fi

    # Profile configs are separate files that `hermes config` does not touch.
    if [[ -d "$PROFILES_DIR" ]]; then
        for cfg in "$PROFILES_DIR"/*/config.yaml; do
            [[ -f "$cfg" ]] || continue
            if sed -E 's/^([[:space:]]*)provider:[[:space:]]*mnemosyne[[:space:]]*$/\1# provider: mnemosyne (removed by uninstaller)/' "$cfg" | rewrite_file "$cfg"; then
                write_ok "Unset memory.provider in $cfg"
            fi
        done
    fi

    # The installer's bootstrap step may have injected mnemosyne into Hermes' venv.
    if [[ -x "$HERMES_VENV_PYTHON" ]]; then
        if pkg_list="$("$HERMES_VENV_PYTHON" -m pip list --disable-pip-version-check --format=freeze 2>/dev/null)"; then
            found="$(printf '%s\n' "$pkg_list" | grep '^mnemosyne' | cut -d= -f1 || true)"
            if [[ -n "$found" ]]; then
                # shellcheck disable=SC2086
                "$HERMES_VENV_PYTHON" -m pip uninstall --yes --disable-pip-version-check $found >/dev/null 2>&1 || true
                write_ok "Removed from Hermes venv: $(printf '%s' "$found" | tr '\n' ' ')"
            else
                write_note 'Hermes venv has no mnemosyne packages'
            fi
        else
            # Hermes' venv is created by uv without --seed, so it often has no pip.
            write_note 'Hermes venv has no usable pip; nothing to uninstall there'
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Delete files
# ---------------------------------------------------------------------------

remove_tree() {
    local path="$1" label="$2"
    [[ -e "$path" ]] || return 0
    if rm -rf "$path" 2>/dev/null && [[ ! -e "$path" ]]; then
        write_ok "Removed $path   [$label]"
        return 0
    fi
    add_failure "Could not remove $path"
    return 1
}

write_step 'Removing files...'
if (( ${#TARGET_PATHS[@]} > 0 )); then
    for i in $(seq 0 $(( ${#TARGET_PATHS[@]} - 1 ))); do
        remove_tree "${TARGET_PATHS[$i]}" "${TARGET_LABELS[$i]}" || true
    done
fi

# Prune the parent dirs the installer created, but only while they are empty.
for parent in "$HERMES_HOME/skills/memory" "$HERMES_HOME/plugins"; do
    if [[ -d "$parent" ]] && rmdir "$parent" 2>/dev/null; then
        write_note "Removed empty $parent"
    fi
done

if (( INCLUDE_HERMES )); then
    write_step 'Removing Hermes Agent...'

    # Must happen while the binary still exists, or the systemd/launchd unit
    # survives as a dangling service pointing into a deleted tree.
    if [[ -n "$HERMES_BIN" ]] && [[ -x "$HERMES_BIN" ]]; then
        if "$HERMES_BIN" gateway uninstall >/dev/null 2>&1; then
            write_ok 'Removed the gateway background service'
        else
            write_note 'No gateway background service was registered'
        fi
    fi
    # Belt and braces: drop any service file the CLI left behind, on either
    # platform, releasing it from the service manager first.
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        release_gateway_service "$unit"
        if rm -f "$unit"; then write_ok "Removed $unit"; else add_failure "Could not remove $unit"; fi
    done <<EOF
$(gateway_service_files)
EOF
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi

    remove_tree "$HERMES_HOME" 'Hermes installation' || true

    while IFS= read -r launcher; do
        [[ -n "$launcher" ]] || continue
        if rm -f "$launcher"; then
            write_ok "Removed $launcher   [launcher into Hermes]"
        else
            add_failure "Could not remove $launcher"
        fi
    done <<EOF
$(hermes_launchers)
EOF
fi

# ---------------------------------------------------------------------------
# Shell rc exports
# ---------------------------------------------------------------------------

write_step 'Clearing environment exports...'
stripped_any=0
while IFS=$'\t' read -r rc_file rc_line; do
    [[ -n "$rc_file" ]] || continue
    # grep exits 1 when it selects nothing, i.e. when the rc file held only that
    # line; under `pipefail` that would mask a rewrite that did happen.
    if { grep -vxF -- "$rc_line" "$rc_file" || true; } | rewrite_file "$rc_file"; then
        write_ok "Removed $(rc_var_name "$rc_line") from $rc_file"
        stripped_any=1
    fi
done <<EOF
$(rc_lines_to_strip)
EOF
if (( ! stripped_any )); then write_note 'No installer-written exports found'; fi

# ---------------------------------------------------------------------------
# Restart the gateway if Hermes is staying
# ---------------------------------------------------------------------------

if (( ! INCLUDE_HERMES )) && [[ -n "$HERMES_BIN" ]]; then
    write_step 'Restarting the Hermes gateway...'
    if "$HERMES_BIN" gateway restart >/dev/null 2>&1; then
        write_ok 'Gateway restarted'
    else
        write_warn 'Gateway did not restart. Run: hermes gateway restart'
    fi
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

write_step 'Verifying...'
LEFTOVERS=()
for p in \
    "$HERMES_HOME/plugins/mnemosyne" \
    "$HERMES_HOME/plugins/hermes-mnemosyne" \
    "$HERMES_HOME/skills/memory/mnemosyne-memory-override" \
    "$MNEMOSYNE_VENV"
do
    if [[ -e "$p" ]]; then LEFTOVERS+=("$p"); fi
done
if (( ! KEEP_DATA )) && [[ -e "$DATA_DIR" ]]; then LEFTOVERS+=("$DATA_DIR"); fi
if (( INCLUDE_HERMES )); then
    if [[ -e "$HERMES_HOME" ]]; then LEFTOVERS+=("$HERMES_HOME"); fi
    while IFS= read -r unit; do
        [[ -n "$unit" ]] || continue
        LEFTOVERS+=("$unit")
    done <<EOF
$(gateway_service_files)
EOF
    while IFS= read -r launcher; do
        [[ -n "$launcher" ]] || continue
        LEFTOVERS+=("$launcher")
    done <<EOF
$(hermes_launchers)
EOF
fi
while IFS=$'\t' read -r rc_file rc_line; do
    [[ -n "$rc_file" ]] || continue
    LEFTOVERS+=("$(rc_var_name "$rc_line") in $rc_file")
done <<EOF
$(rc_lines_to_strip)
EOF

printf '\n'
if (( ${#LEFTOVERS[@]} == 0 )) && (( ${#FAILURES[@]} == 0 )); then
    printf '%sUninstall complete. Nothing left behind.%s\n' "$C_OK" "$C_OFF"
else
    printf '%sUninstall finished with leftovers:%s\n' "$C_WARN" "$C_OFF"
    for l in ${LEFTOVERS[@]+"${LEFTOVERS[@]}"}; do printf '%s  still present: %s%s\n' "$C_WARN" "$l" "$C_OFF"; done
    for f in ${FAILURES[@]+"${FAILURES[@]}"}; do printf '%s  %s%s\n' "$C_WARN" "$f" "$C_OFF"; done
    printf '%s  Close every shell using these paths, then re-run.%s\n' "$C_WARN" "$C_OFF"
fi

printf '\n'
write_note 'System packages installed by the installer (curl, python3-venv) are left alone.'
if (( INCLUDE_HERMES )); then
    write_note 'Systemd user lingering is left enabled; it is shared with any other user service.'
    write_note "  To turn it off:  sudo loginctl disable-linger $(id -un)"
fi
write_note 'Open a new shell so the removed exports take effect.'

if (( ${#LEFTOVERS[@]} > 0 )) || (( ${#FAILURES[@]} > 0 )); then exit 1; fi
