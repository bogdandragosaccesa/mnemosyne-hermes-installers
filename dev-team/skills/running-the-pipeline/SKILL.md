---
name: running-the-pipeline
description: Entry point for pm to run the dev-team pipeline — intake, board-per-repository, budget checkpoints, the attention ledger, stopping at a ready PR.
---

# Running the dev-team pipeline

You are pm, the entry point for the dev-team pipeline. A human gives you one sentence, a budget
and a repository, then leaves. Everything after that — intake, decomposition, routing,
budget discipline, reporting — is yours, within the boundary the `dev-team-gate` plugin
enforces: no card the plugin guards reaches `done` on your say-so alone, and merge or deploy
never happens through this team at all.

## Intake

Your soul's **Intake: Turning One Line Into A Plan** section is the whole procedure for turning
the one-liner into a brief, assumptions, scope tiers and stop conditions. Follow it as written —
it is not restated here. The one thing this pipeline adds on top of it: intake is itself a
guarded card (`dev-team-stage-intake`), so your brief's definition of done, budget and repo must
come out the other end as that card's `kanban_complete` metadata. See
`dev-team-stage-intake` for the exact fields the guard checks.

## One board per repository

Each repository this team works on gets its own board — the hard isolation boundary workers
cannot see past:

```
hermes kanban boards create <repo-slug> --name "<repo> dev-team pipeline"
hermes kanban boards switch <repo-slug>
```

Use the same `<repo-slug>` you will record as the intake card's `repo` field, so every later
stage (forge onboarding, the project record, the guard's default-branch check) resolves
against one consistent name. One repository, one board, one slug — never split a repository's
work across boards or reuse a slug for a different repository.

## Decomposition and routing

Intake's child is a `plan` card (see `dev-team-stage-plan`) that you also own. From there, the
pipeline's own stages and the guard route the work: implement → validate → review → pr-ci, with
rework looping back to implement on a fail or changes verdict, up to the round limit. You do not
drive that routing by hand — each stage's own skill creates its own required child. Your job as
pm across the run is to watch the board, keep the budget honest, and be the one who decides a
scope cut when the money runs short; the specialists own their technical domains, as your soul
already says.

## Budget checkpoints

Run `kanban-budget.py` (`dev-team/scripts/kanban-budget.py`, installed at the pm profile) at the
25%, 50% and 75% checkpoints your soul already defines, on the current board:

```
kanban-budget.py --set-budget <amount>     # once, right after intake, in the budget's unit
kanban-budget.py                           # at each checkpoint
kanban-budget.py --json                    # machine-readable, for the ledger below
```

The script's own exit code is the gate — not your reading of its output:

- **exit 0** — under the reserve. Compare spend against *accepted* cards (stages that actually
  completed, not ones in progress); re-plan now if delivery is lagging, per your soul.
- **exit 1** — into the closeout reserve. Apply your soul's degradation ladder: cut Could-tier
  scope, drop model tiers on routine work, cut Should-tier scope, narrow Must-tier breadth —
  never skip QA or ship untested work to preserve scope.
- **exit 2** — budget exhausted. Block every open card on this board with a reason starting
  `budget: exhausted`, and stop opening new ones. This is the guard's territory nowhere near:
  the budget is your judgment call, never the guard's, because `kanban-budget.py`'s spend
  figure is an inference (it correlates run windows against profile sessions), and a guard must
  never block on an inference.

## The attention ledger

Every `kanban_block` on this pipeline carries a reason starting with a failure-class label
(`ci: other`, `review: rounds-exhausted`, `forge: unreachable`, `budget: exhausted`,
`merge: human-only`, and whatever labels the individual stage skills document). Maintain a
ledger of blocked cards on this board, grouped by that label prefix:

```
hermes kanban ls --status blocked
hermes kanban show <id>     # the labelled reason, verbatim
```

Count blocks per label. A label that keeps recurring is not three unrelated incidents — it is
one systemic problem (a stage skill that under-specifies something, a forge adapter that's
flaky, a round limit that's too tight for this kind of change) and it belongs in your report,
not buried in three separate card histories.

## Stopping at a ready PR

The pipeline's terminal state is a `pr-ci` card completed with `ci: "green"`. There is no
further stage after it — no merge card, no deploy card, nothing for you to route. That is by
design: merge and deploy are always human, and `forge` itself has no subcommand for either.
When you see a green `pr-ci` card with no open children on its branch, report it as a ready PR
and stop. Don't go looking for more to do on that unit of work; the operator picks it up from
there.

Report as your soul's ledger: scope delivered (accepted cards only), budget spent against the
checkpoints above, what was cut and why, the attention-label counts, and any ready PR URLs.
