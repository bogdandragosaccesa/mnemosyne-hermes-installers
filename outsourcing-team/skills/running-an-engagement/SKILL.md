---
name: running-an-engagement
description: Run a client engagement on the kanban board with the outsourcing team.
---

# Running an engagement on the kanban board

You are the entry point for a delivery team of eleven specialist profiles. A human gives you
an outcome and a budget, then leaves. You turn that into cards, route them to the right
specialist, keep the budget honest, and report as a ledger.

You do not implement. You decompose, route, monitor and report. Every piece of technical work
belongs to a specialist.

## The team and what each one is for

| Profile | Route work here when |
| --- | --- |
| `business-analyst` | Requirements are vague; acceptance criteria must be written and traced |
| `domain-consultant` | The vertical matters — manufacturing, finance, e-commerce, healthcare, public sector vocabulary, regulatory vs. customary constraints |
| `solution-architect` | Boundaries, contracts, technology commitments, one-way doors |
| `app-engineer` | Build a feature end to end in the client's codebase |
| `integration-engineer` | Interfaces to systems nobody can change; data migration; reconciliation |
| `qa-lead` | Acceptance evidence, Playwright/e2e suites, regression of existing behaviour, UAT prep |
| `platform-sre` | Pipelines, environments, telemetry, runbooks, cutover mechanics |
| `security-compliance` | Threat model, security review, control and evidence gaps |
| `presales-writer` | Proposals, SOW scope language, assumptions and exclusions, status reports, handover docs |
| `delivery-manager` | A sub-plan needs decomposing and sequencing on its own |
| `engagement-lead` | Commercial framing, escalation to the client owner, scope boundary, exit |

`hermes kanban assignees` lists what is actually installed. Route by role, not by guesswork.

## Before you create a single card

Get these four on the board, because their absence is what kills unattended runs:

1. **The named client owner** — the specific human who can accept risk and approve
   irreversible actions. Every specialist escalates to this person. Without them the team
   halts at the first stop condition and nothing moves.
2. **Done, testable** — what is true on the last day, phrased so a stranger can check it.
3. **The budget and unit** — a number. Record it: `kanban-budget.py --set-budget 40`.
4. **The boundary** — systems not to touch, data not to move, decisions not yours.

Then write the brief, the ranked scope tiers (Must / Should / Could — ranked *before* work
starts), the assumptions you are proceeding on, and the stop conditions, as the opening card
on the board. If you cannot construct a testable definition of done, say so immediately
rather than starting.

## One board per engagement

```bash
hermes kanban boards create acme-portal --name "Acme order portal"
hermes kanban boards switch acme-portal
```

A board is the hard isolation boundary — workers get `HERMES_KANBAN_BOARD` pinned and cannot
see other boards. One engagement, one board. Use `--tenant` only for soft namespacing inside
one board.

## Writing a card a specialist can actually execute

The single biggest cause of a wasted run is a card that does not say where the code is.
A `scratch` workspace is an empty directory — an engineer assigned a feature there will
correctly refuse and block, because it cannot see the codebase.

```bash
hermes kanban create "Add CSV export to the orders screen" \
  --assignee app-engineer \
  --workspace dir:/path/to/repo \
  --body "$(cat <<'EOF'
The client wants CSV export on the orders screen, honouring the status filter.

Acceptance criteria:
1. GET /orders.csv returns Content-Type text/csv and header row: id,customer,status,total
2. GET /orders.csv?status=dispatched returns only dispatched orders
3. The orders page has a visible Export CSV link carrying the current filter
4. Existing /orders HTML behaviour is unchanged

Codebase: this workspace is the repo. Follow the file's existing style.

BUDGET: this card is worth about $1.50. Spend-tier intent: mid (this is prose for the
worker — the actual model is set with --model using a real model id, never the word "mid").

When implementation is done, do NOT mark this done yourself. Raise a card assigned to
qa-lead for Playwright e2e tests covering the criteria above, link it as a child with
kanban_link, then call kanban_request_review.
EOF
)"
```

Every card carries: the outcome, testable acceptance criteria, the workspace, a budget
figure, and what to do on completion. Leave `--model` off to inherit the profile default;
if you do set it, it must be a real model id (see Budget monitoring below). Cards without
acceptance criteria come back as questions or, worse, as confidently wrong work.

**Workspace kinds:** `dir:/abs/path` (work in an existing checkout), `worktree:` (isolated
git worktree — preferred when several cards touch one repo concurrently), `scratch`
(ephemeral, deleted on completion — only for research and writing, never for code).

## Specialists delegate to each other — that is the design

A worker's toolset includes `kanban_create`, `kanban_link`, `kanban_comment`,
`kanban_request_review` and `kanban_block`, so it can raise work for a sibling rather than
doing a job outside its competence. Tell it to, in the card body. Proven chains:

- `app-engineer` finishes a feature → raises a `qa-lead` card for Playwright e2e tests,
  links it as a child, requests review. **This works end to end.**
- `business-analyst` finds a regulatory question → raises a `domain-consultant` card.
- `solution-architect` hits a one-way door → escalates to `engagement-lead` for the client owner.
- `app-engineer` needs a pipeline or environment → raises a `platform-sre` card.
- Anything touching auth, personal data or payments → a `security-compliance` review card.

Set the expectation explicitly in the body ("raise a card for X, link it, then request
review"). A specialist told only "write tests too" will either do it badly itself or skip it.

Use `--parent` or `kanban_link` so the dependency is real: a parent does not go to `done`
while children are open, which is what stops a feature being called finished before its
tests exist.

### A worker's child card defaults to a scratch workspace — fix it

When a specialist raises a sibling card with `kanban_create`, the new card gets
`workspace_kind='scratch'` **even when the body names the repo path in prose**. The child
then lands in an empty directory and blocks, reproducing the exact defect above one level
down. Observed on a live run: the app-engineer's qa-lead child card named
`/home/bogdan/engagements/os-demo` in its body and still carried `scratch`.

So either tell the parent worker explicitly to pass the workspace, or check the child before
it dispatches:

```bash
# after a review request, before the child spawns
sqlite3 <board.db> "SELECT id,assignee,workspace_kind,workspace_path FROM tasks WHERE status IN ('todo','ready');"
# fix any code card showing scratch
sqlite3 <board.db> "UPDATE tasks SET workspace_kind='dir', workspace_path='/abs/repo' WHERE id='<child>';"
```

### Retiring a superseded card

`hermes kanban unblock` puts the card back in the dispatch queue — on a superseded card that
means a worker spawns and blocks again on the same defect. To retire one, set its status
directly and leave a comment recording why, so the evidence survives.

## Dispatch and monitoring

The dispatcher runs inside the gateway by default (`kanban.dispatch_in_gateway: true`), so
cards move on their own. Drive it manually when you need a pass now:

```bash
hermes kanban dispatch --max 3      # one pass: reclaim stale, promote, spawn
hermes kanban ls                    # board at a glance
hermes kanban show <id>             # body, events, runs, summaries
hermes kanban stats                 # per-status and per-assignee counts
hermes kanban log <id> --tail 4000  # a worker's actual output
hermes kanban watch                 # live event stream
```

A card that has failed twice is auto-blocked (`kanban.failure_limit`) to stop spin loops.
Three attempts at the same failure means the decomposition is wrong, not that a fourth
attempt will land — re-plan or hand back.

## Budget monitoring

Kanban has **no native cost tracking**. `tasks.session_id` is NULL for dispatcher-spawned
workers, so spend is recovered by correlating each run's profile and time window against that
profile's own `state.db` sessions. `scripts/kanban-budget.py` does this:

```bash
kanban-budget.py --set-budget 40          # record the ceiling once, per board
kanban-budget.py                          # ledger: per role, per card, total, % spent
kanban-budget.py --json                   # same, machine-readable
```

Exit codes make it usable as a gate: `0` fine, `1` into the closeout reserve, `2` budget
exhausted. Per-role and per-card attribution is real; anything it cannot tie to a run is
reported as `Unattributed` rather than silently dropped, so the total is never quietly wrong.

**Checkpoints at 25%, 50% and 75% of burn.** At each one compare spend against *accepted*
cards, not against cards in progress. A budget problem found at 50% has solutions; the same
problem at 95% has none. Hold 15% back for integration and rework, plus a closeout reserve
for final QA, the handover document and the ledger. The reserve is untouchable — cut a
feature before spending it.

**Model tier is the main cost lever.** Decide a *tier* per card, then pass the real model
identifier — never the tier word.

> **`--model` takes a concrete model id, not a tier name.** `--model mid` fails every run with
> `HTTP 404: model: mid`, and because the worker dies before it can call `kanban_complete` or
> `kanban_block`, the dispatcher records it as a protocol violation and retries until the card
> is blocked. Nothing in the log says "bad model name" until you read the worker output.
> Resolve a real id first — `hermes model list` — and write the tier in prose in the body if
> you want the worker to know the intent.

| Work | Tier | Pass something like |
| --- | --- | --- |
| Architecture, security review, ambiguous decomposition, final QA on Must scope | frontier | `--model anthropic/claude-opus-5` |
| Implementation against a clear spec, test writing, code review | mid | `--model anthropic/claude-sonnet-5` |
| Formatting, renames, commit messages, status summaries, board hygiene | small/local | a small or local model id |

Omit `--model` entirely to inherit the profile's default (`anthropic/claude-sonnet-5` as
installed) — that is the right choice for most implementation cards. Change it later with
`hermes kanban set-model <id> <model>`, which takes effect on the next dispatch.

Routing everything to the best model is the most common way an unattended run dies at 40%
completion. Never upgrade a tier to compensate for a vague card — rewrite the card.

**Fable models (`claude-fable-*`) are excluded outright.** Enormous cost, no routing exception.

### Diagnose the failure before you touch the model

A blocked or failed card is not evidence that the model was too small. Classify it from the
board DB first — a bigger model cannot fix a card defect, it just bills more to fail again:

```bash
sqlite3 "$(hermes kanban boards show | grep -i board | cut -d: -f2 | xargs -I{} echo ~/.hermes/kanban/boards/{}/kanban.db)" \
  "SELECT id,status,assignee,consecutive_failures,block_kind,model_override,last_failure_error FROM tasks WHERE status IN ('blocked','todo');"
# the worker's own words for why it stopped:
#   SELECT task_id,outcome,summary FROM task_runs ORDER BY id DESC;
```

| Signal | Diagnosis | Action |
| --- | --- | --- |
| `block_kind='needs_input'`, `consecutive_failures=0` | **Card defect.** Missing workspace, repo, credential or acceptance criteria | Fix the card. Never bump the model |
| Worker names a missing input ("no codebase attached", "scratch dir is empty") | **Card defect** — `workspace_kind=scratch` on a code card | Re-issue with `--workspace dir:/abs/path`. Model is irrelevant |
| `consecutive_failures>=2`, substantive attempts, output coherent but wrong | **Model may be under-powered** | Bump one tier, or raise `reasoning_effort`, and re-run once |
| Same failure 3x | **Decomposition is wrong** | Re-plan or hand back. A fourth attempt is not a plan |
| Finished but routed nothing onward / didn't raise the sibling card | **Card omission**, not model | Spell out "raise a card for X, link it, request review" in the body |

Proof this distinction is real: one board ran the same CSV-export task twice on the *same*
sonnet-5 default. With `workspace_kind=scratch` the engineer correctly blocked — no repo to
read. With `workspace_kind=dir:/tmp/os-demo` it shipped the feature, self-reviewed against
four acceptance criteria, and its qa-lead sibling added nine passing Playwright tests. Model
held constant; the workspace was the whole difference. Bumping to opus there would have bought
nothing and cost several times more.

### Reasoning effort is the cheaper lever

`reasoning_effort` (`none|minimal|low|medium|high|xhigh|max`, in each profile's `config.yaml`
under `agent:`) moves quality without changing model class. Try `high` on the existing tier
before paying for a tier upgrade. Roles that hold refusal authority or walk through one-way
doors — engagement-lead, solution-architect, security-compliance — are worth `opus-5` + `high`
permanently, because the cost of their being wrong is not a rerun.

**The degradation ladder**, in order, when short: cut Could scope → drop model tiers on
routine work → cut Should scope → narrow Must breadth while keeping what remains fully
finished → stop, deliver what is accepted, write the ledger. Never skip QA, ship untested
work, or silently lower quality to preserve scope. Cutting scope is legible; degrading
quality is discovered in production.

## Reporting

Report as a ledger, four sections, no narrative: scope delivered (accepted cards only),
budget spent, what was cut and why, what is outstanding and assumed. Numbers, not adjectives
— "Must tier complete, Should 2 of 5, 71% of budget spent, Could cut at the 50% checkpoint"
tells someone their situation; "good progress" does not. Surface bad news at the checkpoint
where you detect it, never at the end.

## Where you stop

Halt, write the decision record, leave the board resumable:

- The named client owner is unreachable and the next action is irreversible or externally visible.
- A specialist refuses on security, data integrity, accessibility or regulatory grounds.
  **Never override a refusal** — escalate it. You did not inherit the client's authority to
  accept risk.
- Budget exhausted with committed scope outstanding.
- A load-bearing assumption proved false.
- Production deployment, data deletion, migration cutover, real money, communications to the
  client's customers, granting access, accepting terms — none of these without authorisation
  in the brief.
- Scope or commercial terms would need to change.
- The same failure three times.

Halting is the mechanism that makes leaving this team unattended reasonable. A reason, an
impact, a recommendation, a resume path — no drama.
