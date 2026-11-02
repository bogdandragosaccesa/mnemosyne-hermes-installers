# dev-team deciders: cheap decision, extraction and summary calls — design

Status: draft, for operator approval. Follow-up to `2026-10-29-dev-team-design.md`; built
after the first dev-team build (Tasks 1–11) lands. Moves into
`docs/superpowers/specs/` and is committed once that build is merged, so the running team can't
sweep an untracked file into one of its commits.

## Intent

The operator has an OpenRouter key restricted to decision models and two ScaleDown text models.
He wants the dev-team to use them "for fast decisions so we make some tokens and time spending
economy": small bounded choices and bulk reading move off Opus/Sonnet onto calls that cost
fractions of a cent and return in under a second, so the expensive models spend their tokens
on the work itself.

The operator's ruling on authority (agreed in conversation): **deciders steer, they never
approve.** They may decide alone where a wrong answer is cheap and reversible (which model tier,
which skills, which profile, retry vs escalate). Gate pass/fail, rework, PR readiness, security
findings and budget overruns stay with Opus and the human. He also asked that **every profile
have skills to decide, summarize and extract** through these models.

## What the spike measured (2026-10-29, real data from the first build)

All runs used the operator's key; total spend $0.0037. Scripts were throwaway
(`~/.hermes/cache/scratch/decider-spike/`).

1. **Review verdicts are already structured.** All 11 finished review cards carried
   `metadata.verdict`, and it matched the real outcome (a fix round followed iff `changes`)
   11/11. The product's guard already routes on it in code (`stages.py` `_ROUTES`). No model
   belongs in verdict routing.
2. **Decision models judge free text well.** "Does this review require changes?" on the review
   summaries with every verdict word masked: `liquid/d1.1` 11/11, 0.39 s avg, $0.00005 for
   all 11, probabilities 0.00 vs 0.88–1.00; `typesafe/jev-1.13` 11/11, 0.33 s; `upstage/
   solar-decide-flash` 10/11, 1.19 s. `perplexity/pplx-decider-*` was refused: the account's
   OpenRouter data-policy setting excludes its endpoint.
3. **Tier choice is a weak signal.** On the seven implement cards, both deciders put Task 6 at
   deep 0.90–0.99 (it needed a fix round), but Tasks 5 and 7 ran against the "needed a fix
   round" proxy. Usable only as "escalate when unsure", never as the sole router.
4. **`scaledown/summarize` is unsafe as evidence.** On qa's real Task 2 review it twice wrote a
   confident, false summary ("Task 2 review approved… no secrets in output"; "credentials are
   never written to… stdout") when the tool output showed a credential leak. It restates the
   spec, not what happened.
5. **`scaledown/extract` is a good pointer.** From ~8K tokens of terminal output it returned the
   real leaked credentials with character offsets (confidence 0.61–0.96) plus one false
   positive on a correctly redacted value (0.99). 8 s, $0.0004.

## Goals

1. A `deciders` command-line tool with three subcommands, `decide`, `extract`, `summarize`,
   shipped and installed like `forge`.
2. Skills that let any profile use it, saying when it pays and when it must not be used.
3. The pm's plan stage uses a decision to choose each implement card's model tier, up or down
   from the profile's default, within an operator-written tier table, and passes it through
   `kanban_create(model=…, provider=…)`; uncertainty resolves to the larger tier.
4. Decider spend is visible in the pm's budget alongside Hermes session spend.
5. Both installers (Unix and Windows) install and wire it.

## Non-goals

- Deciders making or overriding a gate outcome, a rework decision, PR readiness, a security
  finding, or a budget overrun. These stay where the first design puts them.
- Choosing tools inside a running agent turn. Changing a live session's tool list breaks
  Hermes' prompt cache and costs more than it saves; tool choice happens only at card level
  (which profile and skills a card gets).
- Running agents on these models. They are not chat agents (decision models have no chat
  endpoint; the ScaleDown models have no tools).
- Changing verdict routing in the guard. It is already code over structured metadata.
- Perplexity's decider. Usable only if the operator changes his OpenRouter data policy; the
  tool takes any model id, so nothing here needs to change for it.

## Anti-goals (would make this a failure even if it works)

- An agent citing a summary as evidence for a verdict, a finding, or "tests pass".
- Agents calling deciders reflexively, so the extra tool turns cost more than they save.
- The key appearing in a transcript, a log, the board, or the repository.
- A key outage (cap reached, alpha API down) stalling the pipeline.

## Approaches considered

- **A. Command-line tool plus skills — chosen by the operator.** A stdlib Python CLI beside
  `forge`, run from the terminal; per-role skills explain use. Matches the pattern the team
  already built and tested, ships through the existing installers on both platforms, and costs
  nothing on turns that don't use it. Answers arrive as JSON on stdout rather than native tool
  results.
- **B. Hermes plugin with three typed tools.** Cleaner calls, but three more tool schemas ride
  in every request of every profile that loads them, and it is a second plugin to keep working
  on Windows and Unix. Not chosen.
- **C. Deciders only inside pipeline code, no agent skills.** Dropped: contradicts the
  operator's explicit request that profiles have the skills.

## Design

### The `deciders` tool

`dev-team/deciders/deciders.py` (+ core module and tests), stdlib only, Python 3.10+, installed
next to `forge` and on the same PATH arrangement.

- `deciders decide --state <file|-> --questions <file|->` → the Decisions API
  (`POST https://openrouter.ai/api/alpha/decisions`). Prints the API's `answers` plus
  `model` (resolved, versioned) and `cost`. Supports `noul`, `choice`, `score`.
- `deciders extract --entities <file|-> [--input <file|->]` → `scaledown/extract` via chat
  completions with `provider.options.scaledown.entities`. Prints entities with `type`, `text`,
  `start`, `end`, `confidence`, plus `cost`.
- `deciders summarize [--input <file|->]` → `scaledown/summarize`. Prints the summary with a
  fixed `"evidence": false` field and `cost`, so the output itself says it is not evidence.
- `deciders tier --title <text> --spec <file|->` → one fixed `choice` question (fast /
  standard / deep), then applies the tier rule in code (named constants, below) and prints
  `{tier, probabilities, model}`. *(Builder-check call: the threshold lives in code, not in the
  pm's reading of probabilities, so it can be tested.)*
- Model ids are pinned, versioned ids (never `~…-latest` aliases) in one config value per
  subcommand, overridable by flag. Default decide model: `liquid/d1.1` (best observed
  separation, cheapest); the builder may change it only with a recorded probe (below).
- Exit codes follow `forge`: `0` answer printed, `2` could not answer (no key, HTTP error, cap
  reached, malformed response, timeout) with a labelled `{"error": …}` on stdout. No exit `1`:
  a decision is data, not a pass/fail. Never retries silently beyond one retry on 429/5xx.
- Key: read from `OPENROUTER_DECIDERS_KEY` only. *(Builder-check call.)* The name is
  deliberately not `OPENROUTER_API_KEY`: Hermes' terminal scrub
  (`tools/environments/local_env_policy.py` `_is_provider_env_blocklisted`) drops known
  provider key names from agent terminals, and a Hermes provider key must stay distinct from
  this restricted one anyway. Delivery, in order: where the `secret` CLI is on PATH, the skill
  wraps calls in `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders …`;
  elsewhere (macOS, Windows, Linux without `secret`) the key is a user environment variable that
  the `dev-team-deciders` skill registers for terminal passthrough through Hermes' skill
  environment-variable mechanism. With neither present, `deciders` exits `2` with label
  `deciders: no-key`, so a missing wrapper or variable lands on the same fallback path. The key is never printed; error text is
  passed through `forge`'s `redact()` (import or copy with a conformance test — builder's call,
  but the two must not drift).
- Ledger: every call that returns appends `{ts, board, task_id, subcommand, model, cost}` to
  one shared file, `deciders-ledger.jsonl` in the shared team home (`DEV_TEAM_HOME`, the Hermes
  root's `dev-team/`, as the installer already defines it), not the per-profile
  `$HERMES_HOME`, so every profile's spend lands in one place. Each record is one `write()` of
  one line on a file opened for append. *(Builder-check call.)* Board and task id come from the kanban worker
  environment when present.

### Budget

`dev-team/scripts/kanban-budget.py` adds the ledger's cost for the board to its spend total and
shows it as its own line, so the pm sees it in every checkpoint. It counts toward the total that
drives the script's exit `1` (reserve) and exit `2` (over budget): it is real money from the
operator, and at observed prices it cannot move the gate in practice. A test proves the ledger
is read before the exit code is computed. *(Builder-check call.)* The OpenRouter key's own cap
($50 at time of writing) is a separate hard stop; `deciders` reports a cap refusal as exit `2`
with label `deciders: key-cap`.

### Skills

One skill, `dev-team-deciders`, loaded by every dev-team profile (souls reference it). It
states, per subcommand:

- **decide** — for bounded yes/no, one-of-N, or graded questions an agent would otherwise spend
  a long read or several turns on, and that repeat (triaging many items, matching a failure to
  one of several known causes). Not for one-off reasoning the agent is already doing. Code
  applies thresholds; between 0.35 and 0.65 the agent decides itself.
- **extract** — to find *where* to look in large output (CI logs, test runs, long diffs). The
  agent must then open the reported spans itself; an extracted span is a pointer, not a
  finding.
- **summarize** — only to compress context the agent will not cite: background material, old
  discussion. Never for test output, review output, CI output, or anything a verdict rests on.
- On exit `2`, fall back to doing the work directly. Never block a card because a decider was
  unavailable.

The decision-model weaknesses OpenRouter documents (quantities, dates, negation, indirection,
text arguing for its own classification) are listed in the skill as cases to keep in code or in
the agent's own judgment.

### Plan-stage tier proposal

`dev-team-stage-plan` gains a step: before each `kanban_create` for an implement card, the pm
runs `deciders tier` over the card's title and spec. The rule `deciders tier` applies:
- top probability ≥ 0.65 → that tier;
- otherwise → the higher of the two leading tiers.
0.65 is a starting value, not a calibrated one: the spike found the tier signal weak. Acceptance
includes a tier probe over the first build's implement cards, recorded in the PR, and the
constant is revisited after the first ten real cards. *(Builder-check call.)*
**Tier to model: an operator-written table, and the decider may go up or down (operator's
choice, option 1 of 3).** The installer writes `model-tiers.json` in the shared team home,
mapping each tier to an exact `{provider, model}` the operator names at install (flags for
fast, standard and deep; the soul's routing table names these tiers Small, Mid and Frontier).
The pm passes the mapped pair through `kanban_create(model=…, provider=…)`, so a card may run
on a cheaper or a stronger model than its profile's default, but never on a model the operator
did not list. If the table is missing, or has no entry for the chosen tier, the card is created
without an override (the profile's own model runs) and the card body says why. The soul's
override rules still win over the decider: a retry drops a tier, two failures escalate once,
and architecture and security work is Frontier regardless of the answer. The other options
considered were "the decider may only raise the profile's model" (safe, but saves almost
nothing) and "the tier is advice only, no pinning" (simplest, loses the economy).
The chosen tier and the decider's probabilities go in
the card body so the reviewer can see why. On exit `2` the pm picks the tier itself as today.

### Where else agents should reach for it (guidance in the skill, not enforced)

- devops in `pr-ci`: `extract` over a red CI log to find failing tests and file:line before
  judging "caused by this change vs flake". The judgment itself stays with devops.
- qa and the reviewer: `extract` over large test or mutation output to locate failures.
- pm: `decide` for triage of many small items (e.g. which open findings are duplicates).

## Constraints

- Stdlib only; bash 3.2 and PowerShell 5.1 for the installer pieces, as in the first design.
- The Decisions API is alpha. Request and response shapes are validated; an unknown shape is
  exit `2`, never a guess.
- Thresholds are named constants with the consequence of each error written beside them, and
  every one traces to a recorded probe on real dev-team inputs (the spike's numbers are the
  starting set).
- Tests use recorded fixtures; no test calls the network.

## Left to the builder

- Module layout inside `dev-team/deciders/`, and whether `redact()` is imported or copied with
  a conformance test.
- Exact ledger field names and how `kanban-budget.py` reads them.
- The probe set: re-run the spike's three probes through the shipped CLI and record the numbers
  in the PR description.
- Wording of the skill, within the rules above.

## Acceptance

- `deciders decide|extract|summarize` work against the live API with the operator's key, and
  return exit `2` with a labelled error with no key, a bad key, and a model the key's policy
  refuses.
- Unit tests cover request building, response validation, exit codes, redaction and the ledger,
  offline.
- `kanban-budget.py` shows decider spend separately and in the total.
- A plan card run on a scratch board proposes a tier through `deciders tier`, creates the
  implement card with the `{provider, model}` from `model-tiers.json`, and records the
  probabilities in the card body; with the table absent, the card carries no override.
- Both installers install `deciders` and the skill; the Windows installer documents the key
  variable.
- No key value appears in any file, log, board comment or transcript produced by the tests.
