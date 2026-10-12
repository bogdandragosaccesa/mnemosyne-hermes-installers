# How it works

## Wrapper mode

Hermes discovers memory providers as Python packages under `<HERMES_HOME>/plugins`. The
installer registers Mnemosyne in `wrapper` mode rather than as a symlink: it writes a
small package that prepends the Mnemosyne virtual environment's `site-packages` to
`sys.path` and then imports the real one.

```python
_PYTHON = '<venv>/bin/python'
_SITE   = '<venv>/lib/python3.11/site-packages'

if _SITE not in sys.path:
    sys.path.insert(0, _SITE)

from mnemosyne_hermes import *
```

Wrapper mode is used on Windows because creating a symlink there requires Developer Mode
or elevation, and on Unix for parity.

The consequence that matters: **Hermes' own interpreter imports Mnemosyne in-process.**
There is no subprocess boundary. Everything Mnemosyne does at runtime — storing a
memory, computing an embedding, running a query — happens inside the Hermes process,
using Hermes' Python.

## Why the Python version has to match

Compiled wheels are ABI-locked to one Python minor version. `numpy`, `onnxruntime`,
`tokenizers` and friends ship as `cp311` builds that a 3.14 interpreter cannot load, and
vice versa.

If the Mnemosyne environment is built against a different version than Hermes runs, the
import does not fail cleanly. Pure-Python modules load, so:

- the provider constructs and registers,
- `hermes memory status` reports `Plugin: installed`, `Status: available`,
- storing a memory succeeds,
- keyword/FTS recall returns rows.

But `numpy` and `onnxruntime` raise `ImportError` inside Hermes, so the embedding model
never loads. Every memory Hermes writes is stored **without a vector**, and semantic
recall can never match it. Nothing reports an error; recall simply comes back with
`dense_score: 0.0000` forever, or empty.

Three further subsystems degrade with warnings that only appear on import:

| Missing module | Effect |
| --- | --- |
| `mnemosyne.batch_tool` | Batch tool calls return an error |
| `mnemosyne.hermes_config` | `memory.mnemosyne` config keys fall back to defaults |
| `mnemosyne.integrations` | Persona injection is disabled |

So the installers treat the version match as an invariant: read the version from Hermes'
interpreter, build against that, rebuild an environment built against anything else,
abort if a mismatch remains, and then prove the result by importing `numpy` and
`onnxruntime` through Hermes' interpreter.

Note that `uv` is easy to miss. Hermes vendors its own under `HERMES_HOME/tools/uv-*`
(`HERMES_HOME/bin` on older installs), which is not on `PATH`. Missing it is what silently
downgraded the Unix installer to the system Python, and on Windows it made the installer
fetch a second uv from winget.

Finding Hermes' interpreter is the other half. Current Hermes has no virtual environment
inside its checkout: it runs from a managed Python under `HERMES_HOME/tools`, and its
launcher reports that interpreter through `hermes --print-runtime-command`, a JSON array
whose first element is the interpreter path. The installers ask that first. Launchers
without the flag — older installs, and the official Docker image, which runs Hermes from
`/opt/hermes/.venv` — still answer `hermes --version`, which prints the Python Hermes runs
(`Python: 3.14.7`) and its `Install directory:`. The installers then use the `.venv` or
`venv` there, or the old `HERMES_HOME/hermes-agent/venv`, **only if its version is the one
Hermes reports**, and say so; a leftover venv of another version is never adopted. A
launcher that hangs (60-second bound) or names an interpreter that does not exist is an
error. When nothing answers they stop rather than guess a version, and
`MNEMOSYNE_HERMES_PYTHON` names the interpreter explicitly.

## The gateway

The two platforms differ enough to matter.

**Linux/macOS** — `hermes gateway install` registers a systemd user unit or a launchd
agent. Without that, `hermes gateway restart` falls back to running the gateway in the
**foreground**, where it never returns. An installer that calls `restart` on an
unregistered gateway therefore hangs forever, and `stop`'s output does not say whether a
gateway was running (with a unit installed it reports "Stopped" either way).

So the scripts read gateway state from `HERMES_HOME/gateway_state.json`, which the gateway
process writes itself under every manager: its `gateway_state` plus its PID. A live PID in
state `running`, `degraded` or `draining` — Hermes' own up-and-serving set — counts as
running. The installer registers the service where Hermes allows one (it
refuses inside containers), then **restarts** a running gateway — `restart` detached,
judged by a new gateway PID appearing within 90 seconds, so a busy gateway draining its
turns is reported as in progress rather than failed — or **starts** a stopped one. The
uninstaller remembers whether a gateway was running, stops it, and at the end starts it
again only if it was. Starting uses `hermes gateway start`, which goes through systemd
(user or system scope), launchd or s6; when no manager brings a gateway up within 45
seconds, the scripts run `hermes gateway run` detached, writing to Hermes' own
`logs/gateway.log`. Their own steps are logged to
`HERMES_HOME/logs/mnemosyne-installer-gateway.log` (`logs/gateway-restart.log` belongs to
Hermes' dashboard).

Both names are **profile-scoped**, which is why the uninstaller matches them by glob
rather than by one fixed name:

| Profile | Linux | macOS |
| --- | --- | --- |
| default | `~/.config/systemd/user/hermes-gateway.service` | `~/Library/LaunchAgents/ai.hermes.gateway.plist` |
| `coder` | `…/hermes-gateway-coder.service` | `…/ai.hermes.gateway-coder.plist` |

On macOS the agent lives under the **login account's** home from the passwd database, not
`$HOME` — profile-mode Hermes repoints `HOME`, but launchd agents stay in the real user's
`Library`.

**Windows** — there is no service. `hermes gateway` spawns the process directly and
registers a login item at
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Hermes_Gateway.vbs`. Because
the spawn is detached, `restart` returns immediately and no registration step is needed.

Both have to be reversed while the Hermes binary still exists, so `--include-hermes`
runs `hermes gateway uninstall` before deleting `HERMES_HOME`.

## Storage layout

```text
<HERMES_HOME>/
├── config.yaml                                  memory.provider lives here
├── plugins/mnemosyne/                           the wrapper package
├── profiles/*/plugins/mnemosyne/                per-profile registrations
├── skills/memory/mnemosyne-memory-override/     bundled skill
└── mnemosyne/data/mnemosyne.db                  the memory database

<MNEMOSYNE_VENV>/                                default ~/.mnemosyne-venv
```

Memories land in the `working_memory` table first and are vector-indexed via
`memory_embeddings`. `mnemosyne sleep` consolidates older working memories into the
episodic tier, which is why `mnemosyne stats` can show `Total memories: 0` alongside a
non-zero working count.

## Uninstall ordering

The uninstallers are ordered so each step can still see what it needs:

1. Stop the gateway, then any process holding the virtual environment.
2. Run `mnemosyne-hermes uninstall` and `cleanup`, which also handle layouts from older
   releases the scripts do not know about.
3. Unset `memory.provider` explicitly. `mnemosyne-hermes cleanup` only rewrites
   `config.yaml` when `provider` is the first key under `memory:`, and Hermes writes it
   last.
4. Rewrite profile configs, which `hermes config` does not touch.
5. Remove the gateway service *before* `HERMES_HOME`.
6. Delete trees, then prune the parent directories the installer created, but only while
   they are empty.
7. Strip the persisted environment variables.
8. Verify, and exit non-zero if anything survived.
