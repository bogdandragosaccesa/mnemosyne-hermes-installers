# Uninstallation

Both uninstallers follow the same shape: print a removal plan, ask for confirmation,
remove, then verify and exit non-zero if anything survived.

**The default is a complete removal, including the memory database.** Pass `--keep-data`
/ `-KeepData` to preserve it, and preview with `--dry-run` / `-DryRun` first.

## Linux / macOS

```bash
./uninstall-mnemosyne-hermes-unix.sh --dry-run   # preview, changes nothing
./uninstall-mnemosyne-hermes-unix.sh             # prompts before removing
```

| Flag | Effect |
| --- | --- |
| `--keep-data` | Preserve the Mnemosyne data directory (database, `config.yaml`, blobs). |
| `--include-hermes` | Also remove Hermes Agent itself. |
| `--dry-run` | Print the plan and change nothing. |
| `--force` | Skip the confirmation prompt. |
| `--non-interactive` | Never prompt. Implied when stdin is not a terminal; needs `--force` to remove anything. |

## Windows

```powershell
.\uninstall-mnemosyne-hermes-windows.ps1 -DryRun   # preview, changes nothing
.\uninstall-mnemosyne-hermes-windows.ps1           # prompts before removing
```

| Flag | Effect |
| --- | --- |
| `-KeepData` | Preserve the Mnemosyne data directory. |
| `-IncludeHermes` | Also remove Hermes Agent itself. |
| `-DryRun` | Print the plan and change nothing. |
| `-Force` | Skip the confirmation prompt. |
| `-NonInteractive` | Never prompt. Requires `-Force` to remove anything. |

## What gets removed by default

- The provider registration: `<HERMES_HOME>/plugins/mnemosyne`, the legacy
  `plugins/hermes-mnemosyne`, and `profiles/*/plugins/mnemosyne`
- The bundled skill `<HERMES_HOME>/skills/memory/mnemosyne-memory-override`
- `memory.provider` in the main config and in every profile config
- The virtual environment
- The data directory, unless `--keep-data`
- The persisted environment variables — the `export` lines the Unix installer appended to
  your rc file, or the Windows User environment variables
- Any `mnemosyne` packages bootstrapped into Hermes' own virtual environment

Both scripts stop the gateway first, and any process still holding the virtual
environment.

## What `--include-hermes` adds

- The whole `HERMES_HOME` tree
- The gateway background service. On Linux that is the systemd user unit
  `hermes-gateway.service`; on Windows it is the `Hermes_Gateway.vbs` login item in the
  Startup folder. Both are removed **while the Hermes binary still exists**, because
  removing `HERMES_HOME` first would strand them.
- On Unix, every `~/.local/bin` entry that points into `HERMES_HOME` — the `hermes`,
  `hermes-acp` and `hermes-agent` wrapper scripts and the vendored `node`, `npm` and
  `npx` symlinks. They are matched by where they point, never by name, so a separately
  installed `node` is left alone.
- On Windows, Hermes' `PATH` entries and the `HERMES_HOME` / `HERMES_GIT_BASH_PATH`
  variables.

## What is deliberately left alone

Shared tooling that the installer cannot prove it introduced:

- System packages on Unix (`curl`, `python3-venv`)
- winget packages on Windows — the script prints the `winget uninstall` commands instead
- systemd user lingering, which other user services may depend on. `--include-hermes`
  prints `sudo loginctl disable-linger <user>` rather than running it.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Everything removed, or a dry run completed |
| `1` | Something survived, or confirmation was refused in non-interactive mode |
| `2` | Unknown option |

Running an uninstaller on an already-clean system is safe and exits `0`.
