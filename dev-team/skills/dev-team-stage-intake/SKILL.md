---
name: dev-team-stage-intake
description: Run the intake stage of a dev-team pipeline card (pm): capture definition of done, budget and repo before any work starts.
---

# dev-team stage: intake

You hold the `intake` card because it carries the skill `dev-team-stage-intake`. That skill
name is the only thing the `dev-team-gate` guard reads to know a card is guarded and which
stage it is — never the title or body. Everything below is what the guard (`guard.py`,
`stages.py`) will actually check when you call `kanban_complete`; it is not advisory.

Intake is the entry point of one unit of work: one sentence, a budget and a repository in.
Follow your soul's own **Intake: Turning One Line Into A Plan** section for how to turn the
human's one-liner into a brief, assumptions, scope tiers and stop conditions — that is not
restated here. This skill only covers the pipeline mechanics the guard enforces on top of it.

## Required `metadata` on `kanban_complete`

The guard rejects completion unless every field below is present and well-typed. Extra fields
are allowed.

| Field | Type | Check |
| --- | --- | --- |
| `definition_of_done` | list | must be a list — the guard's check (`stages.py`'s `_list`) accepts `[]`; keep it normally non-empty as your soul's own guidance, not a guard rule |
| `budget` | object `{amount, unit}` | `amount` a finite non-negative number; `unit` a non-empty string |
| `repo` | string | non-empty; this is the forge **project slug** — every later stage's forge calls and the project record `$HERMES_HOME/dev-team/projects/<repo>.json` are keyed on this exact string, so make it slug-safe (e.g. `org-reponame`), not a URL |

Getting `repo` wrong is expensive to unwind later: devops's onboarding (`dev-team-stage-pr-ci`)
and the guard's own CI re-verification both resolve everything through this one string.

## Assignee names carry the team prefix

Profiles are installed with a team prefix (`dev-` by default, set by the installer's
`--prefix`). The installer writes it to `$HERMES_HOME/dev-team-prefix`; read it with
`cat "$HERMES_HOME/dev-team-prefix"` (PowerShell: `Get-Content "$env:HERMES_HOME\dev-team-prefix"`)
and build every assignee as `<prefix><role>`, e.g. `dev-qa`. In the examples below `<prefix>`
stands for that value. Never assign a bare role name such as `qa`: the dispatcher would spawn
a different, unguarded profile of that name. If the file is missing or empty,
`kanban_block(kind="needs_input")` with a reason starting `setup: no-prefix` instead of guessing.

## The child card

On every successful `kanban_complete`, create exactly one `plan` card before completing:

```
kanban_create(
    title="...",
    assignee="<prefix>pm",
    parents=[<this card's id>],
    skills=["dev-team-stage-plan"],
)
```

Pass its id back in `created_cards` on `kanban_complete`. The guard requires exactly one card
in `created_cards` that carries `dev-team-stage-plan` and lists this card as a parent — not
zero, not two.

## Ending

You end this card in exactly one of two ways:

- `kanban_complete` with the metadata above and the plan child created, or
- `kanban_block(kind="needs_input")` with a reason starting with a failure-class label, when
  you cannot construct a testable definition of done, cannot get a budget figure, or hit any
  other stop condition from your soul.

There is no third ending. Finishing through any other kanban verb on this card is blocked by
the guard's terminal check (see `dev-team-stage-pr-ci` for the full list of blocked side-door
verbs — it applies to every guarded card, not only `pr-ci`).

## Attention labels you may use

Label every block with the failure class it belongs to, so pm's ledger (see
`running-the-pipeline`) can count recurring classes:

- `intake: no-testable-dod` — the one-liner plus reasonable assumptions still doesn't resolve
  into a testable definition of done.
- `budget: missing` or `budget: insufficient` — no usable number/unit, or the number cannot
  cover even the Must tier plus your soul's mandatory reserves.
- `scope: ...` — a load-bearing ambiguity about what's in or out.
- any other label from your soul's stop conditions (assumption invalidated, irreversible
  action required, specialist refusal), prefixed the same way.
