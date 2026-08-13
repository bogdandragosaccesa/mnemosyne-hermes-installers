# Mnemosyne + Hermes installers

Install and uninstall scripts for the [Mnemosyne](https://pypi.org/project/mnemosyne-memory/)
memory provider on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

| Script | Platform |
| --- | --- |
| `install-mnemosyne-hermes-unix.sh` | Linux / macOS |
| `uninstall-mnemosyne-hermes-unix.sh` | Linux / macOS |
| `install-mnemosyne-hermes-windows.ps1` | Windows (PowerShell 5.1+) |
| `uninstall-mnemosyne-hermes-windows.ps1` | Windows (PowerShell 5.1+) |

Every installer bootstraps Hermes itself when it is missing, creates a dedicated virtual
environment for Mnemosyne, registers the memory provider with Hermes, and points
`memory.provider` at it. The Windows scripts are a port of the Unix ones.

## Install

Linux / macOS:

```bash
./install-mnemosyne-hermes-unix.sh
```

| Flag | Effect |
| --- | --- |
| `--no-embeddings` | Request `mnemosyne-memory` without the `[embeddings]` extra. See the caveat below. |
| `--skip-hermes-configuration` | Register the provider but leave `memory.provider` and the gateway alone. |

Windows:

```powershell
.\install-mnemosyne-hermes-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-NoEmbeddings` | Request `mnemosyne-memory` without the `[embeddings]` extra. See the caveat below. |
| `-SkipHermesConfiguration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-NonInteractive` | Never prompt. Implied when no console is attached. |

Re-running an installer is safe: it upgrades the packages in place, re-registers the
provider, and rebuilds the virtual environment if it was built against the wrong Python.

## Uninstall

Linux / macOS:

```bash
./uninstall-mnemosyne-hermes-unix.sh --dry-run   # preview
./uninstall-mnemosyne-hermes-unix.sh             # prompts before removing
```

| Flag | Effect |
| --- | --- |
| `--keep-data` | Preserve the Mnemosyne data directory. |
| `--include-hermes` | Also remove Hermes Agent: the whole `HERMES_HOME` tree, the gateway service, every `~/.local/bin` entry pointing into it, and the `HERMES_HOME` export. |
| `--dry-run` | Print the removal plan and change nothing. |
| `--force` | Skip the confirmation prompt. |
| `--non-interactive` | Never prompt. Implied when stdin is not a terminal; needs `--force` to remove anything. |

Windows:

```powershell
.\uninstall-mnemosyne-hermes-windows.ps1 -DryRun   # preview
.\uninstall-mnemosyne-hermes-windows.ps1           # prompts before removing
```

| Flag | Effect |
| --- | --- |
| `-KeepData` | Preserve the Mnemosyne data directory. |
| `-IncludeHermes` | Also remove Hermes Agent: the whole `HERMES_HOME` tree, its PATH entries, and its environment variables. |
| `-DryRun` | Print the removal plan and change nothing. |
| `-Force` | Skip the confirmation prompt. |
| `-NonInteractive` | Never prompt. Requires `-Force` to remove anything. |

The default is a complete removal, **including the memory database**.

Both scripts stop the gateway and any process still holding the virtual environment,
unset `memory.provider` in the main config and in every profile, remove the provider
registration, the bundled memory skill, the virtual environment and the persisted
environment variables, then verify and exit non-zero if anything survived.

Where they persist those variables differs: the Unix script strips the `export` lines
the installer appended to your shell rc file, while the Windows script clears User
environment variables.

Shared tooling is deliberately left in place. Neither script removes the system
packages the installer may have pulled in (`curl` and `python3-venv` on Unix, winget
packages on Windows), because it cannot prove it introduced them; the Windows script
prints the `winget uninstall` commands instead. `--include-hermes` likewise leaves
systemd lingering enabled and prints the command to disable it, since other user
services may depend on it.

## The gateway background service

`hermes gateway restart` only restarts a service that has been registered with
`hermes gateway install`. With no service registered it silently falls back to running
the gateway in the **foreground**, where it never returns — which would hang the
installer before it reached its verification steps. The Unix installer therefore runs
`hermes gateway install` first, and bounds the restart with `timeout` where that
command exists.

`--include-hermes` reverses this: it runs `hermes gateway uninstall` while the Hermes
binary still exists, because removing `HERMES_HOME` first would strand the systemd unit
pointing at a deleted interpreter.

## Matching Hermes' Python

The provider is registered in `wrapper` mode, which means **Hermes' own interpreter
imports Mnemosyne in-process** from the Mnemosyne virtual environment. Compiled wheels
are ABI-locked to one Python minor version, so if the two disagree, `numpy` and
`onnxruntime` fail to import inside Hermes while everything else keeps working.

That failure is silent and easy to miss. `hermes memory status` still reports
`available ✓`, storing and keyword recall still work — but **every memory Hermes writes
is stored with no embedding**, so semantic recall can never match it. Three further
subsystems degrade with warnings only on import: batch tool calls error out,
`memory.mnemosyne` config keys fall back to defaults, and persona injection is disabled.

The Unix installer therefore:

- reads the version from `HERMES_HOME/hermes-agent/venv/bin/python` and builds the
  Mnemosyne venv against **that** version rather than a hardcoded one;
- looks for `uv` in `HERMES_HOME/bin` as well as on `PATH` — Hermes vendors its own uv
  there and never adds it to `PATH`, and missing it is what silently downgraded this
  step to the system Python;
- rebuilds an existing venv that was built against the wrong version;
- aborts with an explanation if the versions still differ;
- after registering the provider, imports `numpy` and `onnxruntime` through Hermes'
  interpreter and reports whether the embedding stack actually loads.

If you see `Warning: Hermes cannot import numpy/onnxruntime`, memories will be stored
without embeddings — fix the interpreter mismatch before relying on recall.

## Windows differences from the Unix scripts

These are forced by the platform, not preferences:

- Hermes' Windows installer puts `HERMES_HOME` at `%LOCALAPPDATA%\hermes`, not `~/.hermes`.
- Environment variables persist as **User** environment variables instead of lines
  appended to a shell rc file.
- System packages come from winget instead of apt/dnf/pacman/brew.
- The systemd `loginctl enable-linger` step has no Windows equivalent and is omitted.
- Mnemosyne resolves its database from `MNEMOSYNE_DATA_DIR`, falling back to
  `$HERMES_HOME\mnemosyne`; `MNEMOSYNE_HOME` alone does not move it. When a
  non-default `MNEMOSYNE_HOME` is given, the installer also sets `MNEMOSYNE_DATA_DIR`
  so the data lands where it was asked to.
- The provider is registered in `wrapper` mode, because creating a symlink on Windows
  requires Developer Mode or elevation.
- Deleting an open file fails on Windows, so the uninstaller kills file-locking
  processes and retries; on Unix unlinking an open file always succeeds.

## Known upstream quirks

**`-NoEmbeddings` / `--no-embeddings` does not do anything.** As of
`mnemosyne-hermes` 0.5.0 that package declares `mnemosyne-memory[embeddings]` as a
hard dependency, so pip installs fastembed / onnxruntime / sqlite-vec either way.
The flag is kept for parity; the Windows script warns when you pass it.

**Config encoding.** Mnemosyne writes `<data>\config.yaml` using Python's default
text encoding — the ANSI codepage on Windows — but always reads it back as UTF-8, so
the template's em dash makes every command print
`Failed to inspect legacy provider defaults: 'utf-8' codec can't decode byte 0x97`.
The installer re-encodes that file as UTF-8, which preserves the content and silences
the warning.

**Long paths.** `onnxruntime` ships module paths around 120 characters deep. With
`LongPathsEnabled=0` a deeply nested virtual environment makes pip fail partway
through with a misleading `No such file or directory`. The installer warns when the
venv path is long; set `MNEMOSYNE_VENV` to something shorter if you hit it.

**`hermes gateway restart` hangs without a registered service.** See
[The gateway background service](#the-gateway-background-service).

## Requirements

Linux / macOS:

- bash (the scripts are written for bash 3.2, so stock macOS works)
- `sudo` rights, if `curl` or the Python `venv` module still need installing

Windows:

- Windows 10 1803+ / Server 2019+ (for the bundled `curl.exe`)
- PowerShell 5.1 or newer
- winget, if uv or Python still need to be installed

## Development

The repository uses [pre-commit](https://pre-commit.com/) so that malformed docs and
broken shell scripts cannot be committed. After cloning:

```bash
pre-commit install
```

`pre-commit run --all-files` checks the whole tree. The hooks are whitespace and
end-of-file fixers, `check-yaml`, a shebang/executable-bit consistency check,
[codespell](https://github.com/codespell-project/codespell) for typos in prose and help
text, [markdownlint](https://github.com/igorshubovych/markdownlint-cli) for the docs,
and [shellcheck](https://www.shellcheck.net/) for the installers.
