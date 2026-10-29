---
name: dev-team-stage-validate
description: Run the validate stage of a dev-team pipeline card (qa): verify the implement card's head_sha actually works, pass or fail with findings.
---

# dev-team stage: validate

You hold the `validate` card: it carries the skill `dev-team-stage-validate`, which is what the
`dev-team-gate` guard reads. Your parent is the `implement` card whose `head_sha` you are
validating — exactly one parent must carry `dev-team-stage-implement`, or the guard refuses
your completion before it even looks at your own metadata.

Apply your soul's risk model and fault-line checklist to this specific diff. "Does it work" is
the whole question at this stage — the reviewer judges correctness-against-plan afterwards, you
judge whether it functions.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `head_sha` | string | a git object name, 7–64 hex characters; **must equal, case-insensitively, the `head_sha` the parent `implement` card recorded in its last completed run** — a mismatch refuses completion with a `head_sha:` message naming both values |
| `verdict` | string | must be exactly `"pass"` or `"fail"` |
| `evidence` | list | must be a list |
| `findings` | list, **required only when `verdict` is `"fail"`** | non-empty |

## Assignee names carry the team prefix

Profiles are installed with a team prefix (`dev-` by default, set by the installer's
`--prefix`). The installer writes it to `$HERMES_HOME/dev-team-prefix`; read it with
`cat "$HERMES_HOME/dev-team-prefix"` (PowerShell: `Get-Content "$env:HERMES_HOME\dev-team-prefix"`)
and build every assignee as `<prefix><role>`, e.g. `dev-qa`. In the examples below `<prefix>`
stands for that value. Never assign a bare role name such as `qa`: the dispatcher would spawn
a different, unguarded profile of that name. If the file is missing or empty,
`kanban_block(kind="needs_input")` with a reason starting `setup: no-prefix` instead of guessing.

## The child card

- **`verdict: "pass"`** → create one `review` child:
  ```
  kanban_create(assignee="<prefix>reviewer", parents=[<this card's id>],
                skills=["dev-team-stage-review"], title="...")
  ```
- **anything else (`"fail"`, or any value the guard doesn't recognise as the success case)** →
  create one new `implement` child instead, carrying your `findings`:
  ```
  kanban_create(assignee="<prefix>backend-db", parents=[<this card's id>],
                skills=["dev-team-stage-implement"], title="...")
  ```
  (pick whichever profile — `<prefix>backend-db` or `<prefix>frontend` — the findings actually need)

Either way, the guard requires exactly one card in `created_cards` with the right stage skill
and this card as a parent. A `"fail"` verdict is a rework round and counts against the round
limit (see `dev-team-stage-implement`); if the guard refuses with `review: rounds-exhausted`,
stop and `kanban_block(kind="needs_input")` with that label instead of retrying.

## Ending

- `kanban_complete` with the metadata above (plus `findings` on a fail) and the right child
  created, or
- `kanban_block(kind="needs_input")` with a labelled reason, e.g. when the test environment
  itself is unreachable and you cannot produce a verdict at all.

No other kanban verb finishes this card.

## Attention labels you may use

- `qa: environment-unreachable` — you cannot exercise the change to produce any verdict.
- `review: rounds-exhausted` — the guard refused a 4th implement round from your `"fail"`.
- any label from your soul's own stop conditions, prefixed the same way.
