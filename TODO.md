# TODO

## macOS verification

The macOS code paths are written but have **never run on real hardware**. Everything
below was reasoned about or audited statically during the v1.0.0 work on Linux and
Windows; none of it is confirmed. Until it is, treat macOS support as untested rather
than working.

### 1. The uninstaller only understands systemd

The one item here that is a known defect rather than an unknown.

`uninstall-mnemosyne-hermes-unix.sh` hardcodes the gateway service as a systemd unit:

```bash
GATEWAY_UNIT="$HOME/.config/systemd/user/hermes-gateway.service"
GATEWAY_UNIT_LINK="$HOME/.config/systemd/user/default.target.wants/hermes-gateway.service"
```

On macOS `hermes gateway install` registers a **launchd agent** instead. The normal path
still works, because the actual removal is done by `hermes gateway uninstall`, which is
cross-platform. But the two safety nets around it are Linux-only:

- the fallback that deletes the unit file directly finds nothing, and
- **the verify step cannot see a stranded agent**, so the script can print
  `Nothing left behind` while a `~/Library/LaunchAgents` entry survives.

To do:

- [ ] Confirm the plist path and label from the Hermes source rather than guessing —
      `hermes_cli` is the authority. Expected shape:
      `~/Library/LaunchAgents/<label>.plist`.
- [ ] Add it alongside `GATEWAY_UNIT`, selected on `uname -s`.
- [ ] Cover it in all three places the systemd unit appears: the removal plan, the
      fallback delete under `--include-hermes`, and the leftovers check in `Verifying`.
- [ ] Consider `launchctl bootout` / `unload` before deleting the plist, mirroring the
      `systemctl --user daemon-reload` call.

### 2. `timeout` does not exist on stock macOS

`run_bounded` in the installer falls through to running the command unbounded when
`timeout` is missing, which is the case on a stock macOS. The primary hang fix
(`hermes gateway install` before `restart`) is cross-platform, so this only costs the
backstop — but the backstop is exactly what protects against this recurring.

- [ ] Fall back to `gtimeout` when present. Homebrew's coreutils installs it under that
      name, so `command -v timeout` never finds it.
- [ ] Decide whether an unbounded restart is acceptable when neither exists, or whether
      to warn.

### 3. Confirm the launcher layout matches

`--include-hermes` removes entries in `~/.local/bin` that point into `HERMES_HOME`, found
by reading symlink targets and scanning shebang scripts for the path.

- [ ] Confirm Hermes installs to `~/.local/bin` on macOS at all, and that the wrapper
      scripts and vendored `node`/`npm`/`npx` symlinks have the same shape there.
- [ ] If the layout differs, `hermes_launchers()` needs the extra location — it matches on
      where entries point, so it should generalise, but the directory list is hardcoded.

### 4. Confirm the Python-version fix works there

The v1.0.0 fix reads Hermes' interpreter version and finds uv at `$HERMES_HOME/bin/uv`.

- [ ] Confirm Hermes vendors uv at the same path on macOS.
- [ ] Confirm `$HERMES_HOME/hermes-agent/venv/bin/python` is the right probe target.
- [ ] Run the post-install health check and confirm `numpy` / `onnxruntime` import under
      Hermes' interpreter on Apple Silicon as well as Intel.

### 5. Run the actual test matrix

The same sequence used on Linux and Windows:

- [ ] Install from bare, confirm no hang and the health check reports `Verified`
- [ ] Store and recall through the provider under Hermes' own interpreter; confirm
      memories get a `memory_embeddings` row and semantic recall scores above zero
- [ ] Retention across a gateway restart
- [ ] `--dry-run`, refusal without `--force`, `--keep-data`, full removal, reinstall,
      `--include-hermes`, and a repeat run on a clean system
- [ ] Confirm nothing is left in `~/Library/LaunchAgents`

### 6. Confirm the portability assumptions

Audited statically, never executed against BSD userland or bash 3.2:

- [ ] bash 3.2 (stock `/bin/bash`) — parallel arrays, `${arr[@]+"${arr[@]}"}` guards, no
      `mapfile`
- [ ] BSD `sed -E`, `mktemp` with a template, bare `readlink`, `pgrep -f`, `cmp -s`,
      `grep -qxF`
- [ ] `persist_env` writing to `~/.zprofile`, and the uninstaller stripping that exact
      line back out
- [ ] `install_system_package` via `brew`

## Elsewhere

- [ ] `--no-embeddings` / `-NoEmbeddings` still does nothing, because `mnemosyne-hermes`
      0.5.0 declares `mnemosyne-memory[embeddings]` as a hard dependency. Either drop the
      flag or make it install `mnemosyne-memory` with `--no-deps` handling.
- [ ] No CI. `pre-commit run --all-files` is manual; a workflow running it (and
      `shellcheck`) on push would stop regressions reaching a release.
