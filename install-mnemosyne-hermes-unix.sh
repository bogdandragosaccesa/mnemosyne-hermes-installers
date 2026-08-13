#!/usr/bin/env bash
set -Eeuo pipefail

NO_EMBEDDINGS=0
SKIP_HERMES_CONFIGURATION=0
usage() { cat <<'EOF'
Usage: install-mnemosyne-hermes-unix.sh [options]
  --no-embeddings              Install without vector-search extras
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
mkdir -p "$HERMES_HOME" "$MNEMOSYNE_HOME"
persist_env() {
    local rc_file="$1"; touch "$rc_file"
    grep -qxF 'export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' "$rc_file" || echo 'export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"' >> "$rc_file"
    grep -qxF 'export MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HOME/.hermes/mnemosyne}"' "$rc_file" || echo 'export MNEMOSYNE_HOME="${MNEMOSYNE_HOME:-$HOME/.hermes/mnemosyne}"' >> "$rc_file"
}
case "$(uname -s)" in
    Darwin) persist_env "$HOME/.zprofile" ;;
    Linux) persist_env "$HOME/.profile" ;;
    *) echo "Unsupported operating system: $(uname -s)" >&2; exit 1 ;;
esac
VENV_PYTHON="$MNEMOSYNE_VENV/bin/python"
MNEMOSYNE_HERMES="$MNEMOSYNE_VENV/bin/mnemosyne-hermes"
MNEMOSYNE_CLI="$MNEMOSYNE_VENV/bin/mnemosyne"
if [[ -e "$MNEMOSYNE_VENV" ]] && { [[ ! -x "$VENV_PYTHON" ]] || ! "$VENV_PYTHON" -c 'import pip' >/dev/null 2>&1; }; then rm -rf "$MNEMOSYNE_VENV"; fi
if [[ ! -x "$VENV_PYTHON" ]]; then
    if command -v uv >/dev/null 2>&1; then
        # --seed installs pip into the uv-created venv.
        uv venv "$MNEMOSYNE_VENV" --python 3.11 --seed
    else
        python3 -m venv "$MNEMOSYNE_VENV"
    fi
fi
if (( NO_EMBEDDINGS )); then PACKAGE='mnemosyne-memory'; else PACKAGE='mnemosyne-memory[embeddings]'; fi
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install --upgrade "$PACKAGE" mnemosyne-hermes
"$MNEMOSYNE_HERMES" install --mode wrapper --python "$VENV_PYTHON"
if (( ! SKIP_HERMES_CONFIGURATION )); then
    hermes config set memory.provider mnemosyne
    if [[ "$(uname -s)" == Linux ]] && command -v loginctl >/dev/null 2>&1; then
        if loginctl show-user "$(id -u)" -p Linger --value 2>/dev/null | grep -qx 'no'; then
            echo 'Enabling systemd user-service linger for the current user...'
            if command -v sudo >/dev/null 2>&1; then sudo loginctl enable-linger "$(id -un)" || echo 'Could not enable linger automatically.' >&2
            else echo 'Run: sudo loginctl enable-linger '"$(id -un)" >&2; fi
        fi
    fi
    hermes gateway restart || echo 'Gateway restart was not completed. Run: hermes gateway restart' >&2
fi
printf '\nInstallation complete.\nHermes home:    %s\nMnemosyne data: %s\n' "$HERMES_HOME" "$MNEMOSYNE_HOME"
hermes memory status
"$MNEMOSYNE_CLI" stats
