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

## The child card

Before completing, create exactly one `validate` card as this card's direct child:

```
kanban_create(
    title="...",
    assignee="qa",
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

Hermes creates this card's worktree at `<repo>/.worktrees/<task-id>`. When this card is a link
in a plan-built chain on the same branch (backend-db then frontend, or a rework round), reuse
the existing worktree path for that branch instead of adding a second `git worktree add` for it.
