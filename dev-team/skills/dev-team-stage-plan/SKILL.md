---
name: dev-team-stage-plan
description: Run the plan stage of a dev-team pipeline card (pm): decompose into implement cards the guard can verify exist.
---

# dev-team stage: plan

You hold the `plan` card: it carries the skill `dev-team-stage-plan`, which is what the
`dev-team-gate` guard reads to know this card is guarded and at which stage. The guard checks
the facts below on `kanban_complete`; everything else (how you decompose the work, which
decisions you delegate) is yours to judge as pm, per your soul's "Specialists own their
domains. I own the sequence."

Delegate one-way-door architecture decisions to a child card assigned to `<prefix>architect`,
and interface decisions to a child card assigned to `<prefix>uiux` (see the prefix note below), when the change needs them (per the design's Profiles table).
Those are ordinary child cards for your own judgment calls — they do not themselves carry a
`dev-team-stage-*` skill, and the guard does not look for them.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `cards` | list of strings | non-empty; **the guard additionally verifies each id**: every card named must exist on the board, carry `dev-team-stage-implement`, and have this plan card as a parent, or completion is refused with a `plan: card <id> …` message |

`cards` lists the `implement` card(s) you create as this plan card's direct child(ren). List
each one so the check above can verify them.

## Assignee names carry the team prefix

Profiles are installed with a team prefix (`dev-` by default, set by the installer's
`--prefix`). The installer writes it to `$HERMES_HOME/dev-team-prefix`; read it with
`cat "$HERMES_HOME/dev-team-prefix"` (PowerShell: `Get-Content "$env:HERMES_HOME\dev-team-prefix"`)
and build every assignee as `<prefix><role>`, e.g. `dev-qa`. In the examples below `<prefix>`
stands for that value. Never assign a bare role name such as `qa`: the dispatcher would spawn
a different, unguarded profile of that name. If the file is missing or empty,
`kanban_block(kind="needs_input")` with a reason starting `setup: no-prefix` instead of guessing.

## The child card

Before each `kanban_create` for an implement card, run `deciders tier` (see `dev-team-deciders`)
over the card's title and spec: `deciders tier --title "..." --spec <file|->`. Apply its
printed `tier` and, when `override` is non-null, pass that override's `provider` and `model`
straight through to `kanban_create(model=..., provider=...)` — `override` already comes from
the operator-written `model-tiers.json` table, mapping each tier to an exact pair, so you are
never choosing a model `model-tiers.json` doesn't list. When `override` is null (no tier table,
or no entry for this tier), create the card without a model/provider override and say why in
the card body. Put `tier`, `probabilities` and `override_reason` in the card body either way, so
the reviewer can see the reasoning. **Your own soul's routing overrides still win over the
decider's tier**: a retry drops a tier, two failures escalate once, and architecture or security
work is always Frontier (deep) regardless of what `deciders tier` answered. On exit `2` (no key,
bad key, cap, malformed response), pick the tier yourself, as before deciders existed, and note
that in the card body instead of a probability — never block the plan card over an unavailable
decider call.

Then create the `implement` card as this card's direct child:

```
kanban_create(
    title="...",
    assignee="<prefix>backend-db",   # or "<prefix>frontend", whichever role this unit needs
    parents=[<this plan card's id>],
    skills=["dev-team-stage-implement"],
    model=<override["model"] if override else omitted>,
    provider=<override["provider"] if override else omitted>,
)
```

The guard requires exactly one card in `created_cards` carrying `dev-team-stage-implement` with
this plan card as a parent.

**What the guard actually verifies, per implement card.** `guard.py` requires every single
`implement` card — not just a chain's last one — to complete with exactly one `validate` child,
and it matches that `validate` card's `head_sha` to its own one `implement` parent
(`_PARENT_STAGE`). `review` and `pr-ci` likewise each verify against their own direct parent's
`head_sha`, not some later or earlier card's. There is no guard notion of a "final" card in a
chain that review/pr-ci verify instead of the others.

So if you parent impl2 on impl1 to put two roles (backend-db then frontend) on the same branch,
impl1 still spawns its own complete validate→review→pr-ci chain on impl1's `head_sha` alone —
which can produce a "ready PR" covering only impl1's half of the change — and impl2 then spawns
a second, independent validate→review→pr-ci chain on impl2's `head_sha`, meaning a second
`pr-open` attempt on the same branch. That breaks "one branch, one PR." The round limit also
counts every `implement` card in the ancestry including itself, so impl2 starts at round 2 of
3, not a fresh round 1.

**There is today no guard-supported way to land one plan-produced change spanning backend-db
and frontend as a single review/pr-ci pass.** Do not build a same-branch implement chain hoping
later stages will treat it as one unit — they won't. When a unit genuinely needs both roles,
either split it into two separately-planned units (each its own full plan→…→pr-ci pass and its
own PR), or raise the gap as `scope: multi-role-chain` to the orchestrator for a design/guard
decision (e.g. teaching the guard to verify a chain's last head_sha) before building around it.
List in `cards` only the implement card(s) you actually create as this plan card's direct
child(ren).

## Ending

- `kanban_complete` with `cards` naming the implement card you created as this card's direct
  child (existing, carrying a stage skill), or
- `kanban_block(kind="needs_input")` with a labelled reason when decomposition itself hits a
  stop condition.

No other kanban verb finishes this card — the guard's terminal check blocks the side-door verbs
on any guarded card (see `dev-team-stage-pr-ci` for the full list).

## Attention labels you may use

- `plan: card-missing-skill` — you discover, before completing, that a card you meant to
  reference doesn't carry a stage skill (fix it rather than block, when you still can).
- `architecture: one-way-door` — the architect's child card refused or flagged an irreversible
  decision not authorized in the brief.
- `scope: ...` — decomposition reveals the brief doesn't cover something load-bearing.
- any label from your soul's stop conditions, prefixed the same way.
