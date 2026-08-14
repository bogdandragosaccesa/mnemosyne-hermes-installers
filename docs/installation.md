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
| `-h`, `--help` | Show usage. |

## Windows

```powershell
git clone https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers.git
cd mnemosyne-hermes-installers
.\install-mnemosyne-hermes-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-NoEmbeddings` | As above; sets the `MNEMOSYNE_NO_EMBEDDINGS` User environment variable. |
| `-All` | As above. |
| `-DisableBuiltinMemory` | As above. |
| `-SkipHermesConfiguration` | Register the provider but leave `memory.provider` and the gateway alone. |
| `-NonInteractive` | Never prompt. Implied when no console is attached. |

`--all` and `--no-embeddings` are mutually exclusive, as are
`--disable-builtin-memory` and `--skip-hermes-configuration`; passing both of either pair
is an error rather than a silent precedence rule.

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
