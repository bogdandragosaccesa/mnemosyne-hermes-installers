#!/usr/bin/env bash
set -Eeuo pipefail

NO_EMBEDDINGS=0
SKIP_HERMES_CONFIGURATION=0
DISABLE_BUILTIN_MEMORY=0
ALL_EXTRAS=0
usage() { cat <<'EOF'
Usage: install-mnemosyne-hermes-unix.sh [options]
  --no-embeddings              Disable dense vector retrieval (sets MNEMOSYNE_NO_EMBEDDINGS=1)
  --all                        Install mnemosyne-memory[all]: adds the local LLM used for
                               sleep consolidation, plus the MCP and sync extras.
                               llama-cpp-python is compiled from source, so expect a few
                               minutes. The GGUF model downloads later, on first sleep.
  --disable-builtin-memory     Turn off Hermes' built-in MEMORY.md / USER.md store, which
                               upstream recommends once Mnemosyne is the provider
  --skip-hermes-configuration  Do not change Hermes provider configuration
  -h, --help                   Show help
EOF
}
for arg in "$@"; do
    case "$arg" in
        --no-embeddings) NO_EMBEDDINGS=1 ;;
        --all) ALL_EXTRAS=1 ;;
        --disable-builtin-memory) DISABLE_BUILTIN_MEMORY=1 ;;
        --skip-hermes-configuration) SKIP_HERMES_CONFIGURATION=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done
if (( ALL_EXTRAS && NO_EMBEDDINGS )); then
    echo '--all and --no-embeddings contradict each other: one adds the local embedding and' >&2
    echo 'LLM stack, the other turns dense retrieval off. Pick one.' >&2
    exit 2
fi
if (( DISABLE_BUILTIN_MEMORY && SKIP_HERMES_CONFIGURATION )); then
    echo '--disable-builtin-memory needs to write Hermes configuration, which' >&2
    echo '--skip-hermes-configuration forbids. Pick one.' >&2
    exit 2
fi
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
# `timeout` is coreutils. Stock macOS has neither, and Homebrew's coreutils
# installs it as `gtimeout`, so looking only for `timeout` silently drops the
# bound on exactly the platform that needs it most. Unbounded is the last resort.
run_bounded() {
    local secs="$1"; shift
    if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"
    elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"
    else "$@"; fi
}
install_system_package() {
    local package="$1"
    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update && as_root apt-get install -y "$package"
    elif command -v dnf >/dev/null 2>&1; then as_root dnf install -y "$package"
    elif command -v yum >/dev/null 2>&1; then as_root yum install -y "$package"
    elif command -v pacman >/dev/null 2>&1; then as_root pacman -Sy --noconfirm "$package"
    elif command -v brew >/dev/null 2>&1; then brew install "$package"
    else echo "Cannot install '$package' automatically; install it and rerun." >&2; exit 1; fi
}
command -v curl >/dev/null 2>&1 || install_system_package curl
if ! command -v hermes >/dev/null 2>&1; then
    # Hermes' installer clones its repository and refuses to start without git,
    # which minimal images (containers, cloud server images) do not ship.
    command -v git >/dev/null 2>&1 || install_system_package git
    echo 'Hermes was not found. Running the official Hermes installer...'
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
    export PATH="$HOME/.local/bin:$HOME/.local/share/uv:$PATH"
    hash -r
fi
command -v hermes >/dev/null 2>&1 || { echo 'Hermes is not in PATH; start a new shell and rerun.' >&2; exit 1; }
if command -v apt-get >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then
    PY_MAJOR_MINOR="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then install_system_package "python${PY_MAJOR_MINOR}-venv"; fi
fi
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HERMES_HOME/mnemosyne}"
MNEMOSYNE_VENV="${MNEMOSYNE_VENV:-$HOME/.mnemosyne-venv}"
export HERMES_HOME MNEMOSYNE_HOME MNEMOSYNE_VENV
if (( NO_EMBEDDINGS )); then export MNEMOSYNE_NO_EMBEDDINGS=1; fi
mkdir -p "$HERMES_HOME" "$MNEMOSYNE_HOME"
# The single quotes are deliberate: these lines are appended to the rc file
# verbatim, so the parameter expansions run when a future shell starts.
# shellcheck disable=SC2016
persist_env() {
    local rc_file="$1"; touch "$rc_file"
    grep -qxF 'export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' "$rc_file" || echo 'export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' >> "$rc_file"
    grep -qxF 'export MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HOME/.hermes/mnemosyne}"' "$rc_file" || echo 'export MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HOME/.hermes/mnemosyne}"' >> "$rc_file"
    # pip installs the embedding extras either way -- mnemosyne-hermes depends on
    # them outright -- so the only thing that actually turns dense retrieval off
    # is Mnemosyne's own MNEMOSYNE_NO_EMBEDDINGS switch, read at runtime.
    if (( NO_EMBEDDINGS )); then
        grep -qxF 'export MNEMOSYNE_NO_EMBEDDINGS=1' "$rc_file" || echo 'export MNEMOSYNE_NO_EMBEDDINGS=1' >> "$rc_file"
    elif grep -qxF 'export MNEMOSYNE_NO_EMBEDDINGS=1' "$rc_file"; then
        # The flag is declarative: omitting it has to undo a previous
        # --no-embeddings run, or one flagged install disables dense retrieval
        # forever. Rewrite through a temp file so mode and ownership survive.
        local tmp
        tmp="$(mktemp "${TMPDIR:-/tmp}/mnemosyne-rc.XXXXXX")"
        grep -vxF 'export MNEMOSYNE_NO_EMBEDDINGS=1' "$rc_file" > "$tmp" || true
        cat "$tmp" > "$rc_file"
        rm -f "$tmp"
        unset MNEMOSYNE_NO_EMBEDDINGS
        echo "Cleared MNEMOSYNE_NO_EMBEDDINGS left by an earlier --no-embeddings install"
    fi
}
case "$(uname -s)" in
    Darwin) persist_env "$HOME/.zprofile" ;;
    Linux) persist_env "$HOME/.profile" ;;
    *) echo "Unsupported operating system: $(uname -s)" >&2; exit 1 ;;
esac
VENV_PYTHON="$MNEMOSYNE_VENV/bin/python"
MNEMOSYNE_HERMES="$MNEMOSYNE_VENV/bin/mnemosyne-hermes"
MNEMOSYNE_CLI="$MNEMOSYNE_VENV/bin/mnemosyne"
py_minor_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }

# Current Hermes runs from a managed "store" Python and reports it through its
# launcher's --print-runtime-command: a JSON argv whose first element is the
# interpreter that owns the ABI. Older installs kept a venv inside the checkout
# and may not know the flag. MNEMOSYNE_HERMES_PYTHON overrides detection (not
# HERMES_PYTHON, which Hermes' own tooling sets to unrelated interpreters).
# Notices go to stderr: the function's stdout is the result.
resolve_hermes_python() {
    local raw out python rc=0
    if [[ -n "${MNEMOSYNE_HERMES_PYTHON:-}" ]]; then
        if [[ -x "$MNEMOSYNE_HERMES_PYTHON" && -n "$(py_minor_version "$MNEMOSYNE_HERMES_PYTHON")" ]]; then
            printf '%s\n' "$MNEMOSYNE_HERMES_PYTHON"; return 0
        fi
        echo "ERROR: MNEMOSYNE_HERMES_PYTHON=$MNEMOSYNE_HERMES_PYTHON is not a runnable Python interpreter." >&2
        return 1
    fi
    raw="$(run_bounded 60 hermes --print-runtime-command </dev/null 2>/dev/null)" || rc=$?
    if (( rc == 124 )); then
        # A launcher that hangs is a current one in trouble, not an old layout:
        # adopting a leftover legacy venv here could pick the wrong Python.
        echo 'hermes --print-runtime-command did not answer within 60 seconds.' >&2
        return 1
    fi
    out="$(printf '%s\n' "$raw" | grep '^\[' | head -n 1 || true)"
    if [[ -n "$out" ]]; then
        # sed reads a plain first element; an escaped one (quote, backslash,
        # non-ASCII) needs a real JSON decoder.
        python="$(printf '%s\n' "$out" | sed -n 's/^\["\([^"\\]*\)".*/\1/p')"
        if [[ -z "$python" ]] && command -v python3 >/dev/null 2>&1; then
            python="$(printf '%s\n' "$out" | python3 -c 'import json, sys; print(json.loads(sys.stdin.read())[0])' 2>/dev/null || true)"
        fi
        if [[ -n "$python" && -x "$python" ]]; then printf '%s\n' "$python"; return 0; fi
        echo "hermes --print-runtime-command named an interpreter that is not runnable: $out" >&2
        return 1
    fi
    # No answer at all: an older Hermes without the flag, with its venv in the checkout.
    python="$HERMES_HOME/hermes-agent/venv/bin/python"
    if [[ -x "$python" ]]; then
        echo "Note: hermes --print-runtime-command gave no interpreter; using the legacy venv $python" >&2
        printf '%s\n' "$python"; return 0
    fi
    return 1
}

# The provider is registered in wrapper mode, so Hermes' own interpreter imports
# the packages out of this venv's site-packages. Compiled wheels (numpy,
# onnxruntime) are ABI-locked to one Python minor version, so a venv built
# against a different one imports far enough to look healthy while silently
# losing embeddings. Match Hermes' interpreter, and refuse to proceed if we
# cannot: guessing a version is exactly how that failure happens.
HERMES_PYTHON="$(resolve_hermes_python || true)"
HERMES_PY_VERSION=""
if [[ -n "$HERMES_PYTHON" ]]; then HERMES_PY_VERSION="$(py_minor_version "$HERMES_PYTHON")"; fi
if [[ -z "$HERMES_PY_VERSION" ]]; then
    if [[ -z "${MNEMOSYNE_HERMES_PYTHON:-}" ]]; then
        cat >&2 <<EOF
ERROR: could not find the Python interpreter Hermes runs on.
  Tried: hermes --print-runtime-command, then $HERMES_HOME/hermes-agent/venv/bin/python
EOF
    fi
    cat >&2 <<EOF
Hermes imports Mnemosyne in-process, so the Mnemosyne environment has to be built for
Hermes' Python version; a guessed one stores every memory without an embedding.
Point MNEMOSYNE_HERMES_PYTHON at Hermes' interpreter and re-run, for example:
  MNEMOSYNE_HERMES_PYTHON=/path/to/python3 $0
EOF
    exit 1
fi
echo "Hermes runs Python $HERMES_PY_VERSION ($HERMES_PYTHON)"

# Install from the official repository's main branch rather than PyPI, which
# lags it. Both packages come from one commit: mnemosyne-hermes on main requires
# the mnemosyne-memory version that ships beside it. The commit is pinned in the
# archive URL, so a re-run picks up whatever main has moved on to. Resolved
# before the venv is touched, so a failure here leaves an existing install alone.
MNEMOSYNE_REPO='mnemosyne-oss/mnemosyne'
GITHUB_TOKEN_VALUE="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
# Prints the response body, a newline, then the HTTP status ("000" when unreachable).
fetch_commit() {
    local auth=()
    if [[ -n "$1" ]]; then auth=(-H "Authorization: Bearer $1"); fi
    curl -sSL -H 'Accept: application/vnd.github.sha' ${auth[@]+"${auth[@]}"} \
        -w '\n%{http_code}' "https://api.github.com/repos/$MNEMOSYNE_REPO/commits/main" 2>/dev/null || true
}
COMMIT_RESPONSE="$(fetch_commit "$GITHUB_TOKEN_VALUE")"
COMMIT_STATUS="${COMMIT_RESPONSE##*$'\n'}"
if [[ "$COMMIT_STATUS" == 401 && -n "$GITHUB_TOKEN_VALUE" ]]; then
    # An expired token, or one for another host, must not block a public repo.
    echo 'Note: GitHub rejected the token in GITHUB_TOKEN/GH_TOKEN; retrying without it.' >&2
    GITHUB_TOKEN_VALUE=''
    COMMIT_RESPONSE="$(fetch_commit '')"
    COMMIT_STATUS="${COMMIT_RESPONSE##*$'\n'}"
fi
MNEMOSYNE_COMMIT="${COMMIT_RESPONSE%$'\n'*}"
if [[ "$COMMIT_STATUS" != 200 || ! "$MNEMOSYNE_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: could not resolve the latest commit of https://github.com/$MNEMOSYNE_REPO (main)." >&2
    case "$COMMIT_STATUS" in
        403|429)
            if [[ -n "$GITHUB_TOKEN_VALUE" ]]; then
                echo "GitHub answered HTTP $COMMIT_STATUS to the token in GITHUB_TOKEN/GH_TOKEN (rate limit or" >&2
                echo 'missing access). Unset it, or wait, and re-run.' >&2
            else
                echo 'GitHub API rate limit reached (60 requests/hour per IP without a token).' >&2
                echo 'Wait and re-run, or export GITHUB_TOKEN and re-run.' >&2
            fi ;;
        000|'') echo 'api.github.com is unreachable; check the network or proxy and re-run.' >&2 ;;
        200) echo 'api.github.com answered, but not with a commit id; a proxy or captive portal may be' >&2
             echo 'intercepting HTTPS. Check the network and re-run.' >&2 ;;
        *) echo "api.github.com answered HTTP $COMMIT_STATUS; re-run later." >&2 ;;
    esac
    echo 'The existing Mnemosyne environment was left untouched.' >&2
    exit 1
fi
MNEMOSYNE_ARCHIVE="https://github.com/$MNEMOSYNE_REPO/archive/$MNEMOSYNE_COMMIT.zip"

# Hermes vendors its own uv, which is not on PATH: under tools/uv-<version>-<platform>/
# (HERMES_RUNTIME_DIR relocates tools/), or bin/ on older installs. Prefer it, newest
# first, as the Windows installer does. Missing it is what silently downgraded this
# step to the system python.
find_uv() {
    local candidate tools="${HERMES_RUNTIME_DIR:-$HERMES_HOME/tools}"
    candidate="$(for c in "$tools"/uv-*/uv; do
        [[ -x "$c" ]] || continue
        v="${c##*/uv-}"; printf '%s\t%s\n' "${v%%-*}" "$c"
    done | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1 | cut -f2)"
    if [[ -n "$candidate" ]]; then printf '%s\n' "$candidate"; return 0; fi
    if [[ -x "$HERMES_HOME/bin/uv" ]]; then printf '%s\n' "$HERMES_HOME/bin/uv"; return 0; fi
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
    for candidate in "$HOME/.local/bin/uv" "$HOME/.local/share/uv/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]]; then printf '%s\n' "$candidate"; return 0; fi
    done
    return 1
}

installed_version() { "$VENV_PYTHON" -c "import importlib.metadata as m; print(m.version('$1'))" 2>/dev/null || true; }
OLD_VERSION=""
if [[ -x "$VENV_PYTHON" ]]; then OLD_VERSION="$(installed_version mnemosyne-memory)"; fi

# Rebuild the venv when it is broken *or* built against the wrong Python.
if [[ -e "$MNEMOSYNE_VENV" ]]; then
    if [[ ! -x "$VENV_PYTHON" ]] \
        || ! "$VENV_PYTHON" -c 'import pip' >/dev/null 2>&1 \
        || [[ "$(py_minor_version "$VENV_PYTHON")" != "$HERMES_PY_VERSION" ]]; then
        rm -rf "$MNEMOSYNE_VENV"
    fi
fi
VENV_CREATED=0
if [[ ! -x "$VENV_PYTHON" ]]; then
    VENV_CREATED=1
    UV_BIN="$(find_uv || true)"
    if [[ -n "$UV_BIN" ]]; then
        # --seed installs pip into the uv-created venv.
        "$UV_BIN" venv "$MNEMOSYNE_VENV" --python "$HERMES_PY_VERSION" --seed
    elif "$HERMES_PYTHON" -c 'import ensurepip' >/dev/null 2>&1; then
        "$HERMES_PYTHON" -m venv "$MNEMOSYNE_VENV"
    else
        python3 -m venv "$MNEMOSYNE_VENV"
    fi
fi

MNEMOSYNE_PY_VERSION="$(py_minor_version "$VENV_PYTHON")"
if [[ "$MNEMOSYNE_PY_VERSION" != "$HERMES_PY_VERSION" ]]; then
    cat >&2 <<EOF
ERROR: Python version mismatch between Hermes and the Mnemosyne environment.
  Hermes:    $HERMES_PY_VERSION  ($HERMES_PYTHON)
  Mnemosyne: ${MNEMOSYNE_PY_VERSION:-unknown}  ($VENV_PYTHON)
Hermes imports Mnemosyne in-process, so compiled wheels built for
$MNEMOSYNE_PY_VERSION cannot load under $HERMES_PY_VERSION. Memories would be stored
without embeddings and semantic recall would silently never match.
Install a Python $HERMES_PY_VERSION interpreter (or uv) and re-run.
EOF
    exit 1
fi
if (( ALL_EXTRAS )); then
    EXTRAS='[all]'
elif (( NO_EMBEDDINGS )); then
    EXTRAS=''
    echo 'Note: mnemosyne-hermes depends on mnemosyne-memory[embeddings], so the extras are'
    echo '      installed regardless. Dense retrieval is disabled via MNEMOSYNE_NO_EMBEDDINGS=1.'
else
    EXTRAS='[embeddings]'
fi
MEMORY_SPEC="mnemosyne-memory$EXTRAS @ $MNEMOSYNE_ARCHIVE"
HERMES_SPEC="mnemosyne-hermes @ $MNEMOSYNE_ARCHIVE#subdirectory=integrations/hermes"
echo "Installing Mnemosyne from $MNEMOSYNE_REPO main at ${MNEMOSYNE_COMMIT:0:12}"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --upgrade "$MEMORY_SPEC" "$HERMES_SPEC"
# pip skips a direct-URL package whose version is already installed, and main
# keeps one version string across many commits, so --upgrade alone would leave
# a re-run on the old commit. Force just these two; their dependencies were
# resolved from this commit's metadata by the line above. A venv created in
# this run already holds this commit.
if (( ! VENV_CREATED )); then
    "$VENV_PYTHON" -m pip install --force-reinstall --no-deps "$MEMORY_SPEC" "$HERMES_SPEC"
fi
NEW_VERSION="$(installed_version mnemosyne-memory)"
if [[ -n "$OLD_VERSION" && "$OLD_VERSION" != "$NEW_VERSION" ]]; then
    echo "mnemosyne-memory upgraded: $OLD_VERSION -> $NEW_VERSION (commit ${MNEMOSYNE_COMMIT:0:12})"
else
    echo "mnemosyne-memory $NEW_VERSION (commit ${MNEMOSYNE_COMMIT:0:12})"
fi
# --force is required for re-runs: without it this errors out with "already
# exists" as soon as the plugin directory is there, which would both break
# re-running the installer and leave the wrapper pointing at a stale
# site-packages after the venv has been rebuilt.
"$MNEMOSYNE_HERMES" install --mode wrapper --force --python "$VENV_PYTHON"

# `hermes memory status` reports "available" purely from the registration, so it
# stays green even when the embedding stack cannot load in Hermes' interpreter.
# Import it the way the wrapper does, in isolated mode (-I) as Hermes launches
# it, and say so plainly.
if (( ! NO_EMBEDDINGS )); then
    MNEMOSYNE_SITE="$MNEMOSYNE_VENV/lib/python$HERMES_PY_VERSION/site-packages"
    if "$HERMES_PYTHON" -I -c "import sys; sys.path.insert(0, '$MNEMOSYNE_SITE'); import numpy, onnxruntime" >/dev/null 2>&1; then
        echo "Verified: Hermes' interpreter can load the Mnemosyne embedding stack."
    else
        echo 'Warning: Hermes cannot import numpy/onnxruntime from the Mnemosyne environment.' >&2
        echo '         Memories would be stored without embeddings and semantic recall would not match.' >&2
    fi
fi
if (( ! SKIP_HERMES_CONFIGURATION )); then
    hermes config set memory.provider mnemosyne
    # Upstream recommends turning the built-in MEMORY.md / USER.md store off once
    # Mnemosyne is the provider, so the two do not both consume context. Opt-in,
    # because it changes what the agent remembers rather than how it is stored.
    if (( DISABLE_BUILTIN_MEMORY )); then
        hermes config set memory.memory_enabled false
        hermes config set memory.user_profile_enabled false
        echo 'Built-in MEMORY.md / USER.md injection disabled; Mnemosyne is now the only store.'
    fi
    if [[ "$(uname -s)" == Linux ]] && command -v loginctl >/dev/null 2>&1; then
        if loginctl show-user "$(id -u)" -p Linger --value 2>/dev/null | grep -qx 'no'; then
            echo 'Enabling systemd user-service linger for the current user...'
            if command -v sudo >/dev/null 2>&1; then sudo loginctl enable-linger "$(id -un)" || echo 'Could not enable linger automatically.' >&2
            else echo 'Run: sudo loginctl enable-linger '"$(id -un)" >&2; fi
        fi
    fi
    # With no background service registered, `hermes gateway restart` falls back
    # to running the gateway in the foreground and never returns, which hangs
    # the installer before it reaches its verification steps. Registering the
    # service first makes restart a real, bounded service restart.
    hermes gateway install || echo 'Could not install the gateway service. Run: hermes gateway install' >&2
    run_bounded 300 hermes gateway restart || echo 'Gateway restart was not completed. Run: hermes gateway restart' >&2
fi
printf '\nInstallation complete.\nHermes home:    %s\nMnemosyne data: %s\n' "$HERMES_HOME" "$MNEMOSYNE_HOME"
hermes memory status
"$MNEMOSYNE_CLI" stats
