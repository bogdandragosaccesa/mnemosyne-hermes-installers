---
name: dev-team-stage-review
description: Run the review stage of a dev-team pipeline card (reviewer): judge the diff against the plan, approve or send back with findings.
---

# dev-team stage: review

You hold the `review` card: it carries the skill `dev-team-stage-review`, which is what the
`dev-team-gate` guard reads. Your parent is the `validate` card whose `head_sha` you are
reviewing — exactly one parent must carry `dev-team-stage-validate`, or the guard refuses before
it looks at your metadata.

Your soul governs how you judge: diff against plan and acceptance criteria, never your own
work, never a fix you wrote yourself. Your refusals are stop conditions, same as every other
specialist's.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `head_sha` | string | a git object name, 7–64 hex characters; must equal, case-insensitively, the `head_sha` the parent `validate` card recorded in its last completed run |
| `verdict` | string | must be exactly `"approve"` or `"changes"` |
| `findings` | list, **required only when `verdict` is `"changes"`** | non-empty |

## The child card

- **`verdict: "approve"`** → create one `pr-ci` child:
  ```
  kanban_create(assignee="devops", parents=[<this card's id>],
                skills=["dev-team-stage-pr-ci"], title="...")
  ```
- **`verdict: "changes"`** (or any other value) → create one new `implement` child carrying
  your `findings`:
  ```
  kanban_create(assignee="backend-db", parents=[<this card's id>],
                skills=["dev-team-stage-implement"], title="...")
  ```

The guard requires exactly one card in `created_cards` with the right stage skill and this card
as a parent. A `"changes"` verdict is a rework round and counts against the round limit (see
`dev-team-stage-implement`); if the guard refuses the child with `review: rounds-exhausted`,
`kanban_block(kind="needs_input")` with that label instead of retrying.

## Ending

- `kanban_complete` with the metadata above (plus `findings` on `"changes"`) and the right
  child created, or
- `kanban_block(kind="needs_input")` with a labelled reason — including refusing to review your
  own work, which is a stop condition, not something you work around.

No other kanban verb finishes this card.

## Attention labels you may use

- `review: rounds-exhausted` — the guard refused a 4th implement round from your `"changes"`.
- `review: self-review` — you authored or materially edited the change under review and cannot
  judge it.
- any label from your soul's own stop conditions, prefixed the same way.
