---
name: dev-team-stage-implement
description: Run the implement stage of a dev-team pipeline card (backend-db or frontend): make the change, prove it with tests, hand off to validate.
---

# dev-team stage: implement

You hold an `implement` card: it carries the skill `dev-team-stage-implement`, which is what
the `dev-team-gate` guard reads. You are either `backend-db` or `frontend`, per whichever role
the plan card assigned. Build the change on the branch the plan (or the prior implement card in
a chain) established, inside the Hermes worktree workspace for this card — never a second
`git worktree add` for the same branch (see Workspaces below).

This is also the card you land on for **rework**: validate, review or pr-ci routes back to a
new `implement` card, carrying `findings`, whenever its own verdict wasn't a clean pass. Read
your parent card's last completed `metadata` for those `findings` before you start — they are
the reason this card exists.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `branch` | string | non-empty |
| `head_sha` | string | a git object name, 7–64 hex characters |
| `changed_files` | list of strings | non-empty; every entry a non-empty string |
| `tests_run` | list of `{command, exit_code}` | non-empty; every `command` a non-empty string; every `exit_code` an integer that must be `0` — **a single non-zero exit_code refuses the whole completion** |

There is no verdict field here: an implement card either produces this metadata and a validate
child, or it is blocked. Don't call `kanban_complete` with a failing test in `tests_run` hoping
the guard will route it as rework — it won't; fix it or block.

## Assignee names carry the team prefix

Profiles are installed with a team prefix (`dev-` by default, set by the installer's
`--prefix`). The installer writes it to `$HERMES_HOME/dev-team-prefix`; read it with
`cat "$HERMES_HOME/dev-team-prefix"` (PowerShell: `Get-Content "$env:HERMES_HOME\dev-team-prefix"`)
and build every assignee as `<prefix><role>`, e.g. `dev-qa`. In the examples below `<prefix>`
stands for that value. Never assign a bare role name such as `qa`: the dispatcher would spawn
a different, unguarded profile of that name. If the file is missing or empty,
`kanban_block(kind="needs_input")` with a reason starting `setup: no-prefix` instead of guessing.

## The child card

Before completing, create exactly one `validate` card as this card's direct child:

```
kanban_create(
    title="...",
    assignee="<prefix>qa",
    parents=[<this implement card's id>],
    skills=["dev-team-stage-validate"],
)
```

The guard requires exactly one card in `created_cards` carrying `dev-team-stage-validate` with
this card as a parent.

## The round limit

The guard counts `implement`-stage cards in this card's ancestry (itself included) every time a
new `implement` child is required by a rework route. A 4th round is refused outright with
`review: rounds-exhausted`; when you see that refusal, do not retry — `kanban_block(kind=
"needs_input")` with a reason starting `review: rounds-exhausted`.

## Ending

- `kanban_complete` with the metadata above, all tests at exit code 0, and the validate child
  created, or
- `kanban_block(kind="needs_input")` with a labelled reason.

No other kanban verb finishes this card.

## Attention labels you may use

- `implement: ambiguous-spec` — the plan or findings don't resolve into buildable work.
- `implement: blocked-dependency` — something outside this branch must change first.
- `review: rounds-exhausted` — the guard refused a 4th implement round.
- any label from your soul's own stop conditions, prefixed the same way.

## Workspaces

Hermes creates this card's worktree at `<repo>/.worktrees/<task-id>`. When this card continues
the same branch as its own `implement`-card parent — a rework round, where validate, review or
pr-ci routed back to a new `implement` card on that branch — reuse the existing worktree path
for that branch instead of adding a second `git worktree add` for it. (A plan-built multi-role
chain on one branch is not a supported path today — see `dev-team-stage-plan`.)
