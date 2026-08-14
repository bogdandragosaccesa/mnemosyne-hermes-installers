# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `--no-embeddings` / `-NoEmbeddings` now does something. It cannot skip the download —
  `mnemosyne-hermes` depends on `mnemosyne-memory[embeddings]` outright — but it sets
  Mnemosyne's own `MNEMOSYNE_NO_EMBEDDINGS=1`, which disables dense retrieval at runtime.
  Verified by A/B: with the flag a stored memory gets no `memory_embeddings` row and
  recall returns `dense_score: 0.0`; without it, one row and `0.913`. The uninstallers
  clear the variable.
- [`docs/configuration.md`](docs/configuration.md) — the Hermes `memory.mnemosyne`
  provider keys, the `MNEMOSYNE_*` variables that matter operationally, which of them
  these installers set, and where each config file actually lives.

### Fixed

- **Profile gateways were never removed.** Hermes scopes its gateway service per profile
  (`hermes-gateway-coder.service`, `ai.hermes.gateway-coder.plist`), but the Unix
  uninstaller matched one fixed name. Service artifacts are now globbed, and each is
  released from `systemctl --user` / `launchctl` before its file is deleted. This
  affected Linux as well as macOS.
- **`--include-hermes` missed launchers outside `~/.local/bin`.** Hermes links its
  command into `/usr/local/bin` for a root FHS install and `$PREFIX/bin` on Termux, and
  puts the vendored `node`/`npm`/`npx` symlinks in the same place. All three directories
  are now scanned; entries are still only removed when they point into `HERMES_HOME`.
- **macOS gateway handling.** The Unix uninstaller recognised the systemd unit only, so
  on macOS it could report `Nothing left behind` while a launchd agent survived. It now
  handles `~/Library/LaunchAgents/ai.hermes.gateway*.plist`, resolved from the passwd
  home rather than `$HOME` — profile-mode Hermes repoints `HOME`, but the agent does not
  move. Paths were taken from `hermes_cli/gateway.py`, not guessed.
- **`run_bounded` lost its bound on macOS.** It only looked for `timeout`; Homebrew's
  coreutils installs it as `gtimeout`, which is now used as a fallback.
- **The Windows config-encoding workaround never took effect.** It re-encoded
  `config.yaml` as UTF-8 *before* `hermes memory status` and `mnemosyne stats` ran — and
  on a fresh install those are what create the file, in the ANSI codepage. The fix was a
  no-op and every later Mnemosyne command still printed `'utf-8' codec can't decode byte
  0x97`. It now runs after them as well.
- **Blob storage survived a full uninstall.** `MNEMOSYNE_BLOB_DIR` can point outside the
  data directory; it is now removed with it, and cleared as a variable on Windows.

### Note

These macOS changes are written against the Hermes source but remain **unverified on
real hardware** — see [TODO](TODO.md#still-requires-a-mac).

## [1.0.0] - 2026-08-13

First tagged release. Install and uninstall scripts for the Mnemosyne memory provider on
Hermes Agent, covering Linux, macOS and Windows.

### Added

- `install-mnemosyne-hermes-unix.sh` — installer for Linux and macOS.
- `uninstall-mnemosyne-hermes-unix.sh` — uninstaller for Linux and macOS, with
  `--keep-data`, `--include-hermes`, `--dry-run`, `--force` and `--non-interactive`.
  Written for bash 3.2 so it runs on stock macOS.
- `install-mnemosyne-hermes-windows.ps1` — installer for Windows (PowerShell 5.1+).
- `uninstall-mnemosyne-hermes-windows.ps1` — uninstaller for Windows, with `-KeepData`,
  `-IncludeHermes`, `-DryRun`, `-Force` and `-NonInteractive`.
- A plan → confirm → verify flow in both uninstallers. They print exactly what will be
  removed, ask before touching anything, and exit non-zero if something survives.
- Interpreter matching in both installers: the Mnemosyne virtual environment is built
  against the Python version Hermes itself runs, an existing environment built against
  the wrong version is rebuilt, and a remaining mismatch aborts the install.
- A post-install health check that imports `numpy` and `onnxruntime` through Hermes' own
  interpreter and reports whether the embedding stack actually loads.
- `pre-commit` configuration: whitespace and end-of-file fixers, `check-yaml`, a
  shebang/executable-bit consistency check, `codespell`, `markdownlint` and `shellcheck`.
- Documentation under [`docs/`](docs/).

### Fixed

- **The Unix installer hung forever.** `hermes gateway restart` only restarts a
  registered service; with none registered it falls back to running the gateway in the
  foreground and never returns, so the installer never reached its verification steps.
  It now registers the service first and bounds the restart with `timeout` where that
  command exists.
- **Memories were stored without embeddings.** Hermes imports Mnemosyne in-process, so
  compiled wheels must match Hermes' Python. The Unix installer fell back to the system
  Python (3.14) against Hermes' 3.11 because `command -v uv` missed the uv that Hermes
  vendors at `HERMES_HOME/bin`. `numpy` and `onnxruntime` then failed to import inside
  Hermes while everything else kept working: the provider still registered,
  `hermes memory status` still reported `available`, and keyword recall still returned
  rows — but nothing Hermes wrote ever got a vector, so semantic recall could not match
  it. Semantic scores went from `0.0000` to `0.90`–`0.93` once the versions matched.
- **Re-running the Unix installer failed.** `mnemosyne-hermes install` aborts with
  "already exists" unless given `--force`, so any re-run exited 1 and, after a rebuild,
  left the wrapper pointing at a stale `site-packages`.
- **`--include-hermes` left broken launchers behind.** Hermes installs both wrapper
  scripts (`hermes`, `hermes-acp`, `hermes-agent`) and vendored-node symlinks (`node`,
  `npm`, `npx`) in `~/.local/bin`. They are now matched by where they point rather than
  by name, so all of them are removed while an unrelated `node` is left alone.
- **`-IncludeHermes` left a Windows login item behind.** Hermes has no Windows service;
  it registers `Hermes_Gateway.vbs` in the Startup folder, which survived removal and
  kept trying to launch a deleted Hermes at every sign-in.
- **The Unix installer was not executable.** It was committed with mode `100644`, so a
  fresh clone on Linux could not run it.

### Known issues

- The macOS code paths are written but untested on real hardware. In particular the
  uninstaller's gateway handling recognises the systemd unit only; on macOS it relies on
  `hermes gateway uninstall` and would not report a stranded launchd agent as a leftover.
- `--no-embeddings` / `-NoEmbeddings` has no effect, because `mnemosyne-hermes` 0.5.0
  declares `mnemosyne-memory[embeddings]` as a hard dependency.

[1.0.0]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/releases/tag/v1.0.0
