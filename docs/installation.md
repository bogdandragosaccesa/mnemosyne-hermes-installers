# Installation

## Linux / macOS

```bash
git clone https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers.git
cd mnemosyne-hermes-installers
./install-mnemosyne-hermes-unix.sh
```

| Flag | Effect |
| --- | --- |
| `--no-embeddings` | Disable dense vector retrieval via `MNEMOSYNE_NO_EMBEDDINGS=1`. Does not skip the download — see [Configuration](configuration.md#embeddings). |
| `--all` | Install `mnemosyne-memory[all]` — adds the local consolidation LLM. See [below](#local-sleep-consolidation). |
| `--disable-builtin-memory` | Turn off Hermes' built-in `MEMORY.md` / `USER.md` store — see [below](#the-built-in-memory-store). |
| `--skip-hermes-configuration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `--recommended-config` | Write the recommended settings to Mnemosyne's `config.yaml` and Hermes' `.env` — see [Configuration](configuration.md#recommended-configuration). Opt-in: it makes memories visible across sessions. |
| `-h`, `--help` | Show usage. |

## Windows

```powershell
git clone https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers.git
cd mnemosyne-hermes-installers
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\install-mnemosyne-hermes-windows.ps1
```

A fresh Windows client blocks every `.ps1` with "running scripts is disabled on this
system". `Set-ExecutionPolicy -Scope Process` lifts that for the current PowerShell window
only and changes no machine or user setting; `-Force` skips its confirmation prompt.

Without `git`, which a fresh Windows does not have, download the repository instead:

```powershell
Invoke-WebRequest -UseBasicParsing -OutFile mhi.zip `
    https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/archive/refs/heads/main.zip
Expand-Archive mhi.zip -DestinationPath .
cd mnemosyne-hermes-installers-main
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\install-mnemosyne-hermes-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-NoEmbeddings` | As above; sets the `MNEMOSYNE_NO_EMBEDDINGS` User environment variable. |
| `-All` | As above. |
| `-DisableBuiltinMemory` | As above. |
| `-SkipHermesConfiguration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-RecommendedConfig` | As above. |
| `-NonInteractive` | Never prompt. Implied when no console is attached. |

`--all` and `--no-embeddings` are mutually exclusive, and so is
`--skip-hermes-configuration` with either `--disable-builtin-memory` or
`--recommended-config` (both write Hermes configuration); passing a conflicting pair is an
error rather than a silent precedence rule.

## Local sleep consolidation

`--all` / `-All` installs `mnemosyne-memory[all]`, which is what enables **local sleep
consolidation** — the LLM pass that compresses working memory into episodic memory
without calling out to an API.

What it actually does, measured on `mnemosyne-memory` 3.15.1 rather than taken from the
upstream table:

| | Default (`[embeddings]`) | `--all` |
| --- | --- | --- |
| Packages | 36 | 65 |
| Virtual environment | 224 MB | 345 MB |
| Install time (4-core VM) | ~70 s | ~390 s |

It adds `llama-cpp-python`, `ctransformers`, and the MCP/sync extras. Two corrections to
the upstream description: it does **not** pull in `sentence-transformers` or `torch`, and
the delta is about 120 MB rather than the ~1.5 GB quoted. The bulk of the time is
`llama-cpp-python` compiling from source — pip supplies `cmake` and `ninja` in its build
isolation, so no system toolchain beyond a C++ compiler is needed.

The GGUF model itself (`MNEMOSYNE_LLM_REPO`, default `openbmb/MiniCPM5-1B-GGUF`) is
**not** downloaded at install time. It arrives on the first sleep consolidation, which is
where the remaining disk use shows up.

You do not need `--all` to point consolidation at a remote LLM — set
`MNEMOSYNE_LLM_BASE_URL` instead, and see
[Configuration](configuration.md#sleep-consolidation).

## The built-in memory store

Hermes ships its own `MEMORY.md` / `USER.md` store, and it stays active when an external
provider is registered. Upstream
[recommends turning it off](https://docs.mnemosyne.site/api/hermes-plugin) once Mnemosyne
is the provider, so the two do not both consume context on every turn:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: mnemosyne
```

`--disable-builtin-memory` / `-DisableBuiltinMemory` writes exactly that. It is **opt-in**
because it changes what the agent remembers, not merely where memories are kept, and any
existing `MEMORY.md` content stops being injected. The files are left on disk.

The uninstallers reverse it: removing the provider while the built-in store is off would
leave Hermes with no memory at all, so either key found set to `false` is restored to
`true`.

**Do not** use `hermes tools disable memory` for this. The `memory` toolset key gates
both the built-in tool *and* the provider's tools, so it would take Mnemosyne's entire
tool surface down with it.

## What the installer does

1. Installs Hermes Agent via its official installer if `hermes` is missing.
2. Resolves a Python interpreter matching the one Hermes runs — see
   [How it works](how-it-works.md#why-the-python-version-has-to-match).
3. Creates a dedicated virtual environment (default `~/.mnemosyne-venv`).
4. Installs `mnemosyne-memory[embeddings]` and `mnemosyne-hermes` into it, both from the
   current head of `main` in the official repository — see
   [below](#where-mnemosyne-comes-from).
5. Registers the provider with Hermes in `wrapper` mode.
6. Imports `numpy` and `onnxruntime` through Hermes' interpreter and reports whether the
   embedding stack loads.
7. Sets `memory.provider` to `mnemosyne` and restarts the gateway.
8. Prints `hermes memory status` and `mnemosyne stats`.

Re-running is safe. Mnemosyne is reinstalled from whatever `main` is at that moment, the
provider is re-registered, and a virtual environment built against the wrong Python is
rebuilt.

## Where Mnemosyne comes from

The installers do not use PyPI. They ask the GitHub API for the commit at the head of
`main` in [`mnemosyne-oss/mnemosyne`](https://github.com/mnemosyne-oss/mnemosyne) and
install both packages from that commit's source archive:

```text
mnemosyne-memory[embeddings] @ https://github.com/mnemosyne-oss/mnemosyne/archive/<sha>.zip
mnemosyne-hermes @ https://github.com/mnemosyne-oss/mnemosyne/archive/<sha>.zip#subdirectory=integrations/hermes
```

- **Why not PyPI.** PyPI lags `main`, and `mnemosyne-hermes` on `main` requires the
  `mnemosyne-memory` version released alongside it, which may not be on PyPI yet.
  Installing both from one commit keeps them consistent.
- **What you get.** Whatever `main` was when the installer ran — usually a pre-release
  version such as `4.0.0b5`. The installer prints the commit it used.
- **No `git` needed.** pip downloads the archive over HTTPS.
- **Re-runs.** `main` keeps one version string across many commits, and pip will not
  reinstall a version it already has, so for an existing environment the installers follow
  the normal install with a `--force-reinstall --no-deps` of just these two packages. A
  re-run always ends on the current commit.
- **Network.** `api.github.com` and `github.com` must be reachable. The commit is resolved
  before the virtual environment is touched, so if that fails the existing install is left
  as it was. Unauthenticated, the GitHub API allows 60 requests an hour per IP; the
  installers send `GITHUB_TOKEN` (or `GH_TOKEN`) when it is set, and retry without it if
  GitHub answers 401, 403 or 429, so a stale, rate-limited or foreign token cannot block
  the install.

## Upgrading from a PyPI install

An environment installed from PyPI by an older copy of these scripts holds the stable
`mnemosyne-memory` 3.x. Re-running moves it to the pre-release on `main` and prints the
change, for example `mnemosyne-memory upgraded: 3.15.1 -> 4.0.0b5`. The new version opens
the existing memory database and upgrades it in place: an end-to-end run confirmed a memory
written by 3.15.1 is still recalled under 4.0.0b5. The installers have no downgrade path,
so back the database up first:

```bash
~/.mnemosyne-venv/bin/mnemosyne backup
```

```powershell
& "$env:USERPROFILE\.mnemosyne-venv\Scripts\mnemosyne.exe" backup
```

Note that the stable pairing PyPI resolves today, `mnemosyne-memory` 3.15.1 with
`mnemosyne-hermes` 0.7.2, fails inside Hermes with `cannot import name 'make_write_policy'`,
so an existing PyPI install of that pairing has no working provider to lose.

## Paths and overrides

| Variable | Default (Linux/macOS) | Default (Windows) |
| --- | --- | --- |
| `HERMES_HOME` | `~/.hermes` | `%LOCALAPPDATA%\hermes` |
| `MNEMOSYNE_HOME` | `$HERMES_HOME/mnemosyne` | `%HERMES_HOME%\mnemosyne` |
| `MNEMOSYNE_VENV` | `~/.mnemosyne-venv` | `%USERPROFILE%\.mnemosyne-venv` |
| `MNEMOSYNE_HERMES_PYTHON` | detected from Hermes | detected from Hermes |
| `GITHUB_TOKEN` / `GH_TOKEN` | unset | unset |

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
- `sudo` rights, if `curl`, `git` (needed by Hermes' own installer when Hermes is missing)
  or the Python `venv` module still need installing

Windows:

- Windows 10 1803+ / Server 2019+ (for the bundled `curl.exe`)
- PowerShell 5.1 or newer
- winget, if uv or Python still need installing
- A current Microsoft Visual C++ x64 redistributable — see
  [Troubleshooting](troubleshooting.md#onnxruntime-dll-load-failed-on-windows)
