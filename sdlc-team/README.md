# SDLC team profiles

Nine Hermes profiles covering a software delivery lifecycle, each with its own SOUL and
each wired to Mnemosyne. Optional: the memory installers in the repository root do not
need this, and this needs them to have run first.

| Script | Platform |
| --- | --- |
| `install-sdlc-team-unix.sh` | Linux / macOS |
| `install-sdlc-team-windows.ps1` | Windows (PowerShell 5.1+) |

## The profiles

| Profile | Role |
| --- | --- |
| `architect` | Boundaries, contracts, data ownership, technology commitments |
| `backend-db` | Services, schemas, queries, migrations, invariants |
| `devops` | Pipelines, clusters, infrastructure, secrets, telemetry |
| `frontend` | Accessible, resilient interfaces across devices and conditions |
| `pm` | Decomposes goals into cards, sequences work, manages budget and risk |
| `qa` | Finds whether the thing actually works; makes risk visible |
| `researcher` | Gathers facts, synthesizes insights, produces structured briefs |
| `uiux` | Decides what should exist and what shape it takes |
| `writer` | Landing pages, emails, social posts, blog posts, scripts |

Each profile's personality is `souls/SOUL-<name>.md`, copied to the profile's `SOUL.md`.
Edit a soul and re-run to push the change. Each description is also registered with
`hermes profile describe`, which is what the kanban decomposer routes tasks on — so tasks
are matched by role rather than by profile name alone.

## Prerequisites

Hermes must already be installed with Mnemosyne as the active memory provider:

```bash
../install-mnemosyne-hermes-unix.sh --disable-builtin-memory
```

`--disable-builtin-memory` matters here beyond the usual context argument. `hermes profile
create --clone` copies `config.yaml` as a **file**, so whatever is set on the root profile
at clone time is what every profile is created with. Turn the built-in store off first and
all nine inherit that; turn it off afterwards and you have changed only the root. Both
scripts refuse to run when `memory.provider` is not `mnemosyne`, and warn when the built-in
store is still on.

## Install

Linux / macOS:

```bash
./install-sdlc-team-unix.sh
```

| Flag | Effect |
| --- | --- |
| `--model MODEL` | Model to set on each profile (default: `anthropic/claude-sonnet-5`) |
| `--only a,b,c` | Create only these profiles instead of all nine |
| `--skip-model` | Leave each profile's model at whatever it inherited |
| `--keep-soul` | Do not overwrite an existing `SOUL.md` |
| `--dry-run` | Print the plan and change nothing |
| `-h`, `--help` | Show usage |

Windows:

```powershell
.\install-sdlc-team-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-Model MODEL` | As above |
| `-Only a,b,c` | As above |
| `-SkipModel` | As above |
| `-KeepSoul` | As above |
| `-DryRun` | As above |

`--skip-model` and `--model` are mutually exclusive; passing both is an error rather than a
silent precedence rule.

Re-running is safe: existing profiles are updated in place — SOUL, model and plugin link —
rather than erroring out on the name.

## Why each profile needs its own plugin link

This is the part that is easy to get wrong, and it fails quietly.

A named profile redirects `HERMES_HOME` to its own directory, and Hermes discovers memory
providers under `$HERMES_HOME/plugins/`. The Mnemosyne plugin is installed once, at the
root. So a freshly cloned profile ends up with `memory.provider: mnemosyne` in its config —
copied from the root — and no plugin to satisfy it:

```text
Provider:  mnemosyne
Plugin:    NOT installed ✗
```

Hermes does not fail on this. The profile runs with no memory at all, and the only sign is
a status line nobody thinks to check. Both scripts close the gap by linking each profile's
`plugins/mnemosyne` back to the single real install, then verifying `memory status` reports
`available` for every profile before exiting — non-zero if any does not.

On Unix that link is a symlink. On Windows it is a directory junction, because a symlink
there needs Developer Mode or elevation while a junction needs neither. If even a junction
cannot be created, the Windows script copies the plugin and says so: a copy loads fine, but
a later `mnemosyne-hermes install --force` upgrades only the original, so re-run this script
after upgrading Mnemosyne.

Because all nine link to one install, they share **one** Mnemosyne database. That is the
intended arrangement for a team that hands work between roles. To isolate them instead, set
`memory.mnemosyne.profile_isolation: true`, which gives each profile its own Mnemosyne bank
— see [Configuration](../docs/configuration.md#hermes-provider-keys).

## Uninstall

There is no separate uninstaller. Profiles are removed one at a time, which also deletes
that profile's sessions and history:

```bash
hermes profile delete architect
```

The root install and the shared memory database are untouched by that; the repository-root
uninstallers handle those, and they already unset `memory.provider` in every profile.
