#!/usr/bin/env bash
set -Eeuo pipefail

NO_EMBEDDINGS=0
SKIP_HERMES_CONFIGURATION=0
usage() { cat <<'EOF'
Usage: install-mnemosyne-hermes-unix.sh [options]
  --no-embeddings              Disable dense vector retrieval (sets MNEMOSYNE_NO_EMBEDDINGS=1)
  --skip-hermes-configuration  Do not change Hermes provider configuration
  -h, --help                   Show help
EOF
}
for arg in "$@"; do
    case "$arg" in
        --no-embeddings) NO_EMBEDDINGS=1 ;;
        --skip-hermes-configuration) SKIP_HERMES_CONFIGURATION=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
    esac
done
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
if ! command -v python3 >/dev/null 2>&1 && ! command -v uv >/dev/null 2>&1; then
    echo 'Neither python3 nor uv is available.' >&2; exit 1
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
HERMES_VENV_PYTHON="$HERMES_HOME/hermes-agent/venv/bin/python"

py_minor_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }

# The provider is registered in wrapper mode, so Hermes' own interpreter imports
# the packages out of this venv's site-packages. Compiled wheels (numpy,
# onnxruntime) are ABI-locked to one Python minor version, so a venv built
# against a different one imports far enough to look healthy while silently
# losing embeddings. Match Hermes' interpreter, and refuse to proceed if we
# cannot.
HERMES_PY_VERSION=""
if [[ -x "$HERMES_VENV_PYTHON" ]]; then HERMES_PY_VERSION="$(py_minor_version "$HERMES_VENV_PYTHON")"; fi
if [[ -z "$HERMES_PY_VERSION" ]]; then
    echo "Warning: could not determine Hermes' Python version; falling back to 3.11." >&2
    HERMES_PY_VERSION='3.11'
fi

# Hermes ships its own uv under HERMES_HOME/bin, which is not on PATH. Missing
# it is what silently downgraded this step to the system python.
find_uv() {
    local candidate
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
    for candidate in "$HERMES_HOME/bin/uv" "$HOME/.local/bin/uv" "$HOME/.local/share/uv/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]]; then printf '%s\n' "$candidate"; return 0; fi
    done
    return 1
}

# Rebuild the venv when it is broken *or* built against the wrong Python.
if [[ -e "$MNEMOSYNE_VENV" ]]; then
    if [[ ! -x "$VENV_PYTHON" ]] \
        || ! "$VENV_PYTHON" -c 'import pip' >/dev/null 2>&1 \
        || [[ "$(py_minor_version "$VENV_PYTHON")" != "$HERMES_PY_VERSION" ]]; then
        rm -rf "$MNEMOSYNE_VENV"
    fi
fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    UV_BIN="$(find_uv || true)"
    if [[ -n "$UV_BIN" ]]; then
        # --seed installs pip into the uv-created venv.
        "$UV_BIN" venv "$MNEMOSYNE_VENV" --python "$HERMES_PY_VERSION" --seed
    elif [[ -x "$HERMES_VENV_PYTHON" ]] && "$HERMES_VENV_PYTHON" -c 'import ensurepip' >/dev/null 2>&1; then
        "$HERMES_VENV_PYTHON" -m venv "$MNEMOSYNE_VENV"
    else
        python3 -m venv "$MNEMOSYNE_VENV"
    fi
fi

MNEMOSYNE_PY_VERSION="$(py_minor_version "$VENV_PYTHON")"
if [[ "$MNEMOSYNE_PY_VERSION" != "$HERMES_PY_VERSION" ]]; then
    cat >&2 <<EOF
ERROR: Python version mismatch between Hermes and the Mnemosyne environment.
  Hermes:    $HERMES_PY_VERSION  ($HERMES_VENV_PYTHON)
  Mnemosyne: ${MNEMOSYNE_PY_VERSION:-unknown}  ($VENV_PYTHON)
Hermes imports Mnemosyne in-process, so compiled wheels built for
$MNEMOSYNE_PY_VERSION cannot load under $HERMES_PY_VERSION. Memories would be stored
without embeddings and semantic recall would silently never match.
Install a Python $HERMES_PY_VERSION interpreter (or uv) and re-run.
EOF
    exit 1
fi
if (( NO_EMBEDDINGS )); then
    PACKAGE='mnemosyne-memory'
    echo 'Note: mnemosyne-hermes depends on mnemosyne-memory[embeddings], so the extras are'
    echo '      installed regardless. Dense retrieval is disabled via MNEMOSYNE_NO_EMBEDDINGS=1.'
else
    PACKAGE='mnemosyne-memory[embeddings]'
fi
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --upgrade "$PACKAGE" mnemosyne-hermes
# --force is required for re-runs: without it this errors out with "already
# exists" as soon as the plugin directory is there, which would both break
# re-running the installer and leave the wrapper pointing at a stale
# site-packages after the venv has been rebuilt.
"$MNEMOSYNE_HERMES" install --mode wrapper --force --python "$VENV_PYTHON"

# `hermes memory status` reports "available" purely from the registration, so it
# stays green even when the embedding stack cannot load in Hermes' interpreter.
# Import it the way the wrapper does and say so plainly.
if (( ! NO_EMBEDDINGS )) && [[ -x "$HERMES_VENV_PYTHON" ]]; then
    MNEMOSYNE_SITE="$MNEMOSYNE_VENV/lib/python$HERMES_PY_VERSION/site-packages"
    if "$HERMES_VENV_PYTHON" -c "import sys; sys.path.insert(0, '$MNEMOSYNE_SITE'); import numpy, onnxruntime" >/dev/null 2>&1; then
        echo "Verified: Hermes' interpreter can load the Mnemosyne embedding stack."
    else
        echo 'Warning: Hermes cannot import numpy/onnxruntime from the Mnemosyne environment.' >&2
        echo '         Memories would be stored without embeddings and semantic recall would not match.' >&2
    fi
fi
if (( ! SKIP_HERMES_CONFIGURATION )); then
    hermes config set memory.provider mnemosyne
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
