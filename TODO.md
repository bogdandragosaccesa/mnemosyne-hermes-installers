# TODO

## macOS verification

The macOS code paths are written and, since the v1.0.0 follow-up work, corrected against
the Hermes source rather than guessed at. They have still **never run on a Mac**. Treat
macOS as unverified until the matrix below has actually been executed.

### Resolved against the Hermes source

These were unknowns; they are now settled by reading `hermes_cli/gateway.py` and
`scripts/install.sh` in an installed Hermes, and the scripts have been changed to match.

- **launchd plist path.** `get_launchd_plist_path()` returns
  `~/Library/LaunchAgents/ai.hermes.gateway.plist`, or `ai.hermes.gateway-<profile>.plist`
  under a profile. It resolves the home from the **passwd database**, not `$HOME`,
  because profile-mode Hermes repoints `HOME` while the agent stays in the login user's
  `Library`. The uninstaller now mirrors both details.
- **Service names are profile-scoped on both platforms.** `_SERVICE_BASE` is
  `hermes-gateway`, so a profile install is `hermes-gateway-coder.service` /
  `ai.hermes.gateway-coder.plist`. The uninstaller now globs instead of matching one
  fixed name — this was also a live gap on **Linux**, where profile gateways were being
  missed.
- **uv location.** `install.sh` states that Hermes "owns its own uv at
  `$HERMES_HOME/bin/uv`. Always install there — no PATH probing", on every Unix
  platform. The v1.0.0 interpreter fix therefore holds on macOS.
- **Command link directory.** `get_command_link_dir()` is `$PREFIX/bin` on Termux,
  `/usr/local/bin` for a root FHS install, and `~/.local/bin` otherwise — and the
  vendored `node`/`npm`/`npx` symlinks go to the same place. `hermes_launchers()` now
  scans all three, so `--include-hermes` no longer misses a root install. This too was a
  Linux bug, not just a macOS one.
- **`timeout` on macOS.** `run_bounded` now falls back to `gtimeout`, which is what
  Homebrew's coreutils installs it as; looking only for `timeout` silently dropped the
  bound on the platform most likely to need it.

### Still requires a Mac

- [ ] Run the full matrix: install from bare, confirm no hang and that the health check
      reports `Verified`; store and recall through the provider under Hermes' own
      interpreter; confirm a `memory_embeddings` row and a non-zero dense score;
      retention across a gateway restart; then `--dry-run`, refusal without `--force`,
      `--keep-data`, full removal, reinstall, `--include-hermes`, and a repeat run on a
      clean system.
- [ ] Confirm nothing survives in `~/Library/LaunchAgents`, and that `launchctl bootout
      gui/<uid>/<label>` is the right release call — the fallback is `launchctl unload`,
      and neither has been executed.
- [ ] Confirm the Python-version probe and health check behave on Apple Silicon as well
      as Intel, where the `onnxruntime` wheel differs.
- [ ] Confirm `persist_env` writes to `~/.zprofile` and the uninstaller strips that exact
      line back out.
- [ ] Confirm `install_system_package` via `brew`.

### Blocked, worth retrying

- [ ] **bash 3.2 verification.** The scripts are written for bash 3.2 (stock macOS
      `/bin/bash`): parallel arrays instead of associative ones, `${arr[@]+"${arr[@]}"}`
      guards, no `mapfile`. To test this without a Mac, bash 3.2.57 was being compiled on
      the Linux VM to run both scripts under it. The build needed `bison` and
      `CFLAGS_FOR_BUILD="-g -std=gnu89"` (GCC 14 rejects the K&R-era build tools); it was
      still building when the VM went offline. Worth finishing — it converts the single
      largest macOS assumption into a fact without owning a Mac.
- [ ] BSD userland behaviour for `sed -E`, `mktemp` with a template, bare `readlink`,
      `pgrep -f`, `cmp -s` and `grep -qxF`. Audited statically, never executed against
      BSD tools.

## Elsewhere

- [ ] No CI. `pre-commit run --all-files` is manual; a workflow running it on push would
      stop regressions reaching a release.
- [ ] The `memory.mnemosyne` block in Hermes' `config.yaml` is left behind on uninstall.
      That is deliberate — it is user tuning, not something the installer wrote — but a
      `--purge-config` flag would be reasonable.
- [ ] Consider surfacing more of Mnemosyne's configuration as installer flags
      (`auto_sleep`, `vector_type`, a remote sleep LLM). Everything is settable by hand
      today; see [docs/configuration.md](docs/configuration.md).
