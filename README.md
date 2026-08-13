# Mnemosyne + Hermes installers

Install and uninstall scripts for the [Mnemosyne](https://pypi.org/project/mnemosyne-memory/)
memory provider on [Hermes Agent](https://github.com/NousResearch/hermes-agent).

| Script | Platform |
| --- | --- |
| `install-mnemosyne-hermes-unix.sh` | Linux / macOS |
| `install-mnemosyne-hermes-windows.ps1` | Windows (PowerShell 5.1+) |
| `uninstall-mnemosyne-hermes-windows.ps1` | Windows (PowerShell 5.1+) |

The Windows scripts are a port of the Unix installer. They bootstrap Hermes itself
via the official `install.ps1` when it is missing, create a dedicated virtual
environment for Mnemosyne, register the memory provider with Hermes, and point
`memory.provider` at it.

## Install

```powershell
.\install-mnemosyne-hermes-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-NoEmbeddings` | Request `mnemosyne-memory` without the `[embeddings]` extra. See the caveat below. |
| `-SkipHermesConfiguration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-NonInteractive` | Never prompt. Implied when no console is attached. |

Re-running the installer is safe: it upgrades the packages in place and re-registers
the provider.

## Uninstall

```powershell
.\uninstall-mnemosyne-hermes-windows.ps1 -DryRun   # preview
.\uninstall-mnemosyne-hermes-windows.ps1           # prompts before removing
```

The default is a complete removal, **including the memory database**.

| Flag | Effect |
| --- | --- |
| `-KeepData` | Preserve the Mnemosyne data directory. |
| `-IncludeHermes` | Also remove Hermes Agent: the whole `HERMES_HOME` tree, its PATH entries, and its environment variables. |
| `-DryRun` | Print the removal plan and change nothing. |
| `-Force` | Skip the confirmation prompt. |

It stops the gateway and any file-locking processes first, unsets `memory.provider`
in the main config and in every profile, removes the provider registration, the
bundled memory skill, the virtual environment and the user environment variables,
then verifies and exits non-zero if anything survived.

Tooling that came from winget (`uv`, and with `-IncludeHermes` also `ripgrep` and
`ffmpeg`) is deliberately left in place — the script prints the `winget uninstall`
commands instead of removing shared tooling it cannot prove it introduced.

## Windows differences from the Unix script

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

## Requirements

- Windows 10 1803+ / Server 2019+ (for the bundled `curl.exe`)
- PowerShell 5.1 or newer
- winget, if uv or Python still need to be installed
