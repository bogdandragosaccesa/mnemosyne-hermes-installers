# Installation

## Linux / macOS

```bash
git clone https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers.git
cd mnemosyne-hermes-installers
./install-mnemosyne-hermes-unix.sh
```

| Flag | Effect |
| --- | --- |
| `--no-embeddings` | Request `mnemosyne-memory` without the `[embeddings]` extra. Currently has no effect — see [Troubleshooting](troubleshooting.md#no-embeddings-does-nothing). |
| `--skip-hermes-configuration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-h`, `--help` | Show usage. |

## Windows

```powershell
git clone https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers.git
cd mnemosyne-hermes-installers
.\install-mnemosyne-hermes-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-NoEmbeddings` | As above. The script warns that the flag does nothing. |
| `-SkipHermesConfiguration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-NonInteractive` | Never prompt. Implied when no console is attached. |

## What the installer does

1. Installs Hermes Agent via its official installer if `hermes` is missing.
2. Resolves a Python interpreter matching the one Hermes runs — see
   [How it works](how-it-works.md#why-the-python-version-has-to-match).
3. Creates a dedicated virtual environment (default `~/.mnemosyne-venv`).
4. Installs `mnemosyne-memory[embeddings]` and `mnemosyne-hermes` into it.
5. Registers the provider with Hermes in `wrapper` mode.
6. Imports `numpy` and `onnxruntime` through Hermes' interpreter and reports whether the
   embedding stack loads.
7. Sets `memory.provider` to `mnemosyne` and restarts the gateway.
8. Prints `hermes memory status` and `mnemosyne stats`.

Re-running is safe. Packages are upgraded in place, the provider is re-registered, and a
virtual environment built against the wrong Python is rebuilt.

## Paths and overrides

| Variable | Default (Linux/macOS) | Default (Windows) |
| --- | --- | --- |
| `HERMES_HOME` | `~/.hermes` | `%LOCALAPPDATA%\hermes` |
| `MNEMOSYNE_HOME` | `$HERMES_HOME/mnemosyne` | `%HERMES_HOME%\mnemosyne` |
| `MNEMOSYNE_VENV` | `~/.mnemosyne-venv` | `%USERPROFILE%\.mnemosyne-venv` |

Export any of these before running to override them. The Unix installer appends
`HERMES_HOME` and `MNEMOSYNE_HOME` exports to your shell rc file (`~/.zprofile` on macOS,
`~/.profile` on Linux); the Windows installer persists them as User environment
variables.

Mnemosyne resolves its database from `MNEMOSYNE_DATA_DIR` first, then `MNEMOSYNE_HOME`,
then the default under `HERMES_HOME`. Setting `MNEMOSYNE_HOME` alone does not move the
database on Windows, so the installer also sets `MNEMOSYNE_DATA_DIR` when a non-default
`MNEMOSYNE_HOME` is given.

## Verifying the install

```bash
hermes memory status                 # Provider: mnemosyne, Plugin: installed
~/.mnemosyne-venv/bin/mnemosyne stats
```

`hermes memory status` reports the *registration*, not whether memory actually works.
For a real check see
[Confirming memories get embeddings](troubleshooting.md#confirming-memories-get-embeddings).

## Requirements

Linux / macOS:

- bash (the scripts target bash 3.2, so stock macOS works)
- `sudo` rights, if `curl` or the Python `venv` module still need installing

Windows:

- Windows 10 1803+ / Server 2019+ (for the bundled `curl.exe`)
- PowerShell 5.1 or newer
- winget, if uv or Python still need installing
- A current Microsoft Visual C++ x64 redistributable — see
  [Troubleshooting](troubleshooting.md#onnxruntime-dll-load-failed-on-windows)
