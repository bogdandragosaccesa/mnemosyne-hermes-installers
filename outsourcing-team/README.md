# IT outsourcing delivery team profiles

Eleven Hermes profiles covering how a software services company actually delivers into
somebody else's business, each with its own SOUL and each wired to Mnemosyne. Optional: the
memory installers in the repository root do not need this, and this needs them to have run
first.

| Script | Platform |
| --- | --- |
| `install-outsourcing-team-unix.sh` | Linux / macOS |
| `install-outsourcing-team-windows.ps1` | Windows (PowerShell 5.1+) |

## How this differs from `sdlc-team`

`sdlc-team` is a product team: it owns the code, the roadmap and the risk. This team does not.
Every soul here is written for the vendor side of a contract, which changes the defaults:

- **The client owns the code, the data and the decision.** Risk acceptance belongs to a named
  human on the client side. A specialist's refusal is escalated to that person, never
  overridden internally — not even by the engagement lead.
- **Scope is contractual.** A "quick clarification" may be a change request, and saying so is
  somebody's explicit job rather than an afterthought.
- **The estate already exists and is only partly visible.** Designs are constrained by
  incumbent platforms and by what the client's own team can operate.
- **The engagement ends.** Handover, runbooks, credential transfer and the client team's
  ability to run the thing without us are deliverables, not paperwork.
- **Industry-neutral, industry-aware.** One profile exists purely to supply the vertical
  knowledge — manufacturing, finance, e-commerce, healthcare, public sector — that the
  engineers do not have.

## The profiles

| Profile | Role |
| --- | --- |
| `engagement-lead` | Client relationship, commercial framing, escalation, scope boundary, exit |
| `delivery-manager` | Decomposes outcomes into cards, sequences work, manages burn and margin |
| `business-analyst` | Elicits and traces requirements into testable acceptance criteria |
| `domain-consultant` | Industry vocabulary, regulatory constraints, real operational edge cases |
| `solution-architect` | Boundaries, contracts and technology commitments inside the client's estate |
| `app-engineer` | Builds features end to end, in the client's codebase and conventions |
| `integration-engineer` | Interfaces to unchangeable systems; data migration with reconciliation |
| `qa-lead` | Acceptance evidence, traceability, client UAT, regression, exit criteria |
| `platform-sre` | Pipelines, environments, telemetry — handed over operable |
| `security-compliance` | Threat models, security findings, control and evidence gaps |
| `presales-writer` | Proposals, SOW scope language, assumptions and exclusions, status reports |

Each profile's personality is `souls/SOUL-<name>.md`, copied to the profile's `SOUL.md`. Edit a
soul and re-run to push the change. Each description is also registered with `hermes profile
describe`, which is what the kanban decomposer routes tasks on — so tasks are matched by role
rather than by profile name alone.

## Prerequisites

Hermes must already be installed with Mnemosyne as the active memory provider:

```bash
../install-mnemosyne-hermes-unix.sh --disable-builtin-memory
```

`--disable-builtin-memory` matters here beyond the usual context argument. `hermes profile
create --clone` copies `config.yaml` as a **file**, so whatever is set on the root profile at
clone time is what every profile is created with. Turn the built-in store off first and all
eleven inherit that; turn it off afterwards and you have changed only the root. Both scripts
refuse to run when `memory.provider` is not `mnemosyne`, and warn when the built-in store is
still on.

## Install

Linux / macOS:

```bash
./install-outsourcing-team-unix.sh
```

| Flag | Effect |
| --- | --- |
| `--model MODEL` | Model to set on each profile (default: `anthropic/claude-sonnet-5`) |
| `--only a,b,c` | Create only these profiles instead of all eleven |
| `--prefix PREFIX` | Prepend `PREFIX` to every profile name, e.g. `--prefix os-` → `os-qa-lead` |
| `--skip-model` | Leave each profile's model at whatever it inherited |
| `--keep-soul` | Do not overwrite an existing `SOUL.md` |
| `--dry-run` | Print the plan and change nothing |
| `-h`, `--help` | Show usage |

Windows:

```powershell
.\install-outsourcing-team-windows.ps1
```

| Flag | Effect |
| --- | --- |
| `-Model MODEL` | As above |
| `-Only a,b,c` | As above |
| `-Prefix PREFIX` | As above |
| `-SkipModel` | As above |
| `-KeepSoul` | As above |
| `-DryRun` | As above |

`--skip-model` and `--model` are mutually exclusive; passing both is an error rather than a
silent precedence rule. An unrecognised name in `--only` is also an error, rather than a
successful run that created nothing.

Re-running is safe: existing profiles are updated in place — SOUL, model and plugin link —
rather than erroring out on the name.

### Running alongside `sdlc-team`

No profile name here collides with the nine in `sdlc-team`, so both teams can be installed at
once. `--prefix` exists for the other case: running two *engagements* of this same team side by
side, each with its own board and history.

```bash
./install-outsourcing-team-unix.sh --prefix acme-
```

Note that profiles sharing one Mnemosyne install also share one memory database — see below —
so use `memory.mnemosyne.profile_isolation` if the engagements must not see each other.

## Why each profile needs its own plugin link

This is the part that is easy to get wrong, and it fails quietly.

A named profile redirects `HERMES_HOME` to its own directory, and Hermes discovers memory
providers under `$HERMES_HOME/plugins/`. The Mnemosyne plugin is installed once, at the root.
So a freshly cloned profile ends up with `memory.provider: mnemosyne` in its config — copied
from the root — and no plugin to satisfy it:

```text
Provider:  mnemosyne
Plugin:    NOT installed ✗
```

Hermes does not fail on this. The profile runs with no memory at all, and the only sign is a
status line nobody thinks to check. Both scripts close the gap by linking each profile's
`plugins/mnemosyne` back to the single real install, then verifying `memory status` reports
`available` for every profile before exiting — non-zero if any does not.

On Unix that link is a symlink. On Windows it is a directory junction, because a symlink there
needs Developer Mode or elevation while a junction needs neither. If even a junction cannot be
created, the Windows script copies the plugin and says so: a copy loads fine, but a later
`mnemosyne-hermes install --force` upgrades only the original, so re-run this script after
upgrading Mnemosyne.

Because all eleven link to one install, they share **one** Mnemosyne database. That is the
intended arrangement for a team that hands work between roles across one engagement: the
analyst's recorded assumption is the same assumption the QA lead tests against. To isolate them
instead, set `memory.mnemosyne.profile_isolation: true`, which gives each profile its own
Mnemosyne bank — see [Configuration](../docs/configuration.md#hermes-provider-keys).

## Uninstall

There is no separate uninstaller. Profiles are removed one at a time, which also deletes that
profile's sessions and history:

```bash
hermes profile delete engagement-lead
```

The root install and the shared memory database are untouched by that; the repository-root
uninstallers handle those, and they already unset `memory.provider` in every profile.
