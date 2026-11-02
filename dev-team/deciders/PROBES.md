# Live acceptance and probes — Task 6

Date: 2026-11-02. Repo branch `dev-team-deciders`. Live calls used the operator's
OpenRouter key via `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- ...`, with
`HERMES_HOME=$HOME/.hermes` set for the process so `deciders` reads
`$HOME/.hermes/dev-team/model-tiers.json` and appends to the shared ledger at
`$HOME/.hermes/dev-team/deciders-ledger.jsonl` (run rule A4).

**Resolved model strings actually returned by the live API** (the `model` field in each
response, not the request string): `liquid/d1.1-20261027`, `scaledown/extract` (unchanged),
`scaledown/summarize` (unchanged), `perplexity/pplx-decider-v1.2-20261016`.

**Total live OpenRouter spend for this task: ~$0.00044** (ledger-recorded calls:
$0.00007128, from 6 successful CLI calls; plus ~$0.00037 from calls made directly against the
Decisions API with a corrected request body — see "Headline finding" below — needed to
reproduce Step 2's spike probes, since the shipped `decide`/`tier` CLI commands cannot
complete a live call at all). Both well under the $0.05 estimate and the $4 stop-and-block
line.

## Headline finding (blocks Step 1 and Step 2 as specified): `decide` and `tier` cannot
## complete a live call using the shipped/documented request shape

OpenRouter's real Decisions API (`POST https://openrouter.ai/api/alpha/decisions`) requires
`questions` to be a JSON **object/record keyed by question id**, e.g.
`{"q1": {"type": "choice", ...}}`. Both the shipped code and the shipped skill doc send an
**array** instead:

- `dev-team/skills/dev-team-deciders/SKILL.md` documents `--questions` as
  `[{"id": "q1", "type": "choice", ...}]` (a JSON array) — this is the array shape, and it is
  the one every profile is told to use.
- `deciders_core.py`'s `_cmd_tier` hardcodes `questions = [ {...} ]` as a Python list
  literal — this one cannot be fixed by the caller at all; `tier`'s request shape is wrong on
  every call, unconditionally.
- `_cmd_decide` passes the caller's `--questions` file through verbatim, so `decide` only
  fails if the caller follows the documented (array) shape — which the skill tells them to do.

Reproduced directly against the real endpoint:

```
$ curl .../api/alpha/decisions -d '{"model":"liquid/d1.1","state":{"title":"probe"},
      "questions":[{"id":"q1","type":"choice","instructions":"Pick yes or no.",
      "criteria":{"yes":"affirmative","no":"negative"}}]}'
HTTP 400
{"error":{"message":"[{\"expected\":\"record\",\"code\":\"invalid_type\",\"path\":[\"questions\"],
  \"message\":\"Invalid input: expected record, received array\"}]","code":400}, ...}
```
Changing only the shape of `questions` from an array to `{"q1": {...}}` (same content,
record-keyed) gets `HTTP 200` with a normal decisions response. The *contents* of each
question object are otherwise unchanged.

**Effect on the shipped CLI:**
- `deciders tier --title ... --spec ...` → **always** `exit 2 {"error": "deciders: http-400"}`
  against the live API. This is Goal 3 of the design (pm's plan-stage tier routing) — as
  shipped, it never succeeds against the real API, only against the unit tests' `FakeTransport`.
- `deciders decide --state ... --questions ...` → fails the same way whenever `--questions`
  follows the skill's own documented array example (which is what I used for Step 1, and what
  any profile copying the skill doc would produce). It only succeeds if the caller independently
  discovers the record shape.
- `deciders extract` and `deciders summarize` are **not affected** — they hit the ordinary
  chat-completions endpoint, not the decisions endpoint, and worked live on the first try.

**Severity:** high. `tier` is unconditionally broken against the real API (not a caller
mistake, a hardcoded wrong shape), and it is exactly the capability Goal 3 depends on. `decide`
is broken for any caller who follows the shipped skill doc, which is the only documented way to
call it. This was not caught by the existing test suite because `FakeTransport` never calls the
real API and so never enforces OpenRouter's actual schema.

**I did not fix this** (Task 6's scope is this file only) — flagging to the orchestrator/human
for a fix-round task against `deciders_core.py` (`_cmd_tier`'s hardcoded list → a dict keyed by
id) and `dev-team-deciders/SKILL.md`'s `--questions` example (array → record). `extract`'s own
doc example (`{"types": ["synthetic-secret"]}`) has the same class of bug — see "extract entity
spec" below — it doesn't error, it silently returns zero entities, which is worse.

**Live confirmation, not just a lab reproduction:** Step 3's real `t9-pm` plan-stage run (below)
hit the identical `deciders: http-400` independently, on both the table-present and
table-absent probes, and correctly fell back to pm judgment per the skill's own
"on failure: exit 2 → fall back" rule rather than blocking or guessing. The pipeline degrades
safely; it's `tier`'s live-decision signal, specifically, that is unavailable.

## Step 1: live calls

All via `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- python3 deciders.py ...`
(the CLI as shipped), `HERMES_HOME=$HOME/.hermes`.

| call | input | result | exit |
|---|---|---|---|
| `decide` | `--questions` as a JSON **array** (the skill's documented shape) | `{"error": "deciders: http-400"}` | 2 |
| `decide` | `--questions` as a JSON **record** (corrected shape, my own construction, not documented anywhere) | `{"answers": {"q1": {...,"choice":"yes","probabilities":{"yes":0.63,"no":0.37}}}, "model": "liquid/d1.1-20261027", "cost": 9.2e-07}` | 0 |
| `extract` | entities spec as a record (`{"type-name": "description"}`) | real spans with offsets/confidence | 0 |
| `summarize` | plain text | real summary, `"evidence": false` | 0 |
| `tier` | shipped hardcoded request | `{"error": "deciders: http-400"}` (always; see above) | 2 |

- **No key** (`OPENROUTER_DECIDERS_KEY` unset): `{"error": "deciders: no-key"}`, exit 2. Correct,
  and this check runs before any network call (no cost).
- **Wrong key** (`OPENROUTER_DECIDERS_KEY=invalid`, the literal string, never the real key):
  `{"error": "deciders: auth"}`, exit 2, on both `decide` and `extract` — confirms OpenRouter
  validates the Authorization header before validating the request body, so this label is
  reliable even though `decide`'s body shape is also wrong in my first probe.
- **`--model perplexity/pplx-decider-v1.2`**: through the shipped `tier` CLI this cannot be
  observed — the call fails at `http-400` (the hardcoded-list bug) before the model is even
  relevant. Repeating the *same* call with the corrected record shape, directly against the
  endpoint: **`HTTP 200`**, a normal decisions response (`model` resolved to
  `perplexity/pplx-decider-v1.2-20261016`, cost $0.00000174). No refusal was observed. This
  contradicts this design doc's own spike note ("`perplexity/pplx-decider-*` was refused: the
  account's OpenRouter data-policy setting excludes its endpoint") — either the account's
  data-policy setting changed since 2026-10-29, or the exact model string tested then differed
  from `pplx-decider-v1.2`. Record this as a label-to-watch-for question rather than a confirmed
  refusal path: **no refusal label was reproducible today**, so the policy-refusal label this
  step asked me to record could not be captured live.

## Step 2: spike probes replayed

The CLI's `decide`/`tier` commands cannot run live as shipped (see Headline finding), so these
probes were run directly against `https://openrouter.ai/api/alpha/decisions` with the corrected
(record-shaped) `questions`, same model, same credentials, same question content the shipped
code sends for `tier` resp. the question content I built for `decide` — the only difference
from "running it through the CLI" is the one bug above. Numbers below are the model's answer to
that question; nothing about the masking, the data, or the scoring changed from what the CLI
would have reported if it worked.

### 2a. 11 masked review summaries (board `dev-team-build`, read-only)

Source: the first-round review tasks for Tasks 1–11 on `dev-team-build`
(`t_10b2f8ce` … `t_ea6ce53e`), read via `select ... from tasks`/`task_runs` on the board's
sqlite file opened `?mode=ro`. Never written to.

**Masking method:** regex `\b(approved?|approval|changes|change-requested)\b` (case
insensitive) replaced with `[MASKED]` in each review's run summary text. This masks the verdict
word itself and its close variants; it does over-mask a few unrelated uses of "changes" as an
ordinary noun inside a couple of summaries (e.g. "Two real defects block [MASKED]"), which if
anything makes the task harder for the model, not easier.

**Question asked:** one `decide` call per review, `{"needs_changes": {"type": "choice",
"instructions": "Does this code review require changes before it can be approved?",
"criteria": {"yes": "...", "no": "..."}}}`, state = `{title, review_summary: masked_text}`.

**Model:** `liquid/d1.1` (resolved `liquid/d1.1-20261027`).

**Result: 11/11 correct** (predicted "changes" iff `yes` probability led; ground truth = the
review's own `metadata.verdict`).

| task | actual | predicted | P(yes=needs changes) |
|---|---|---|---|
| t_10b2f8ce | approve | approve | 0.0005 |
| t_a7e7dd6f | changes | changes | 0.9513 |
| t_5ce76022 | changes | changes | 0.9537 |
| t_a0a97284 | approve | approve | 0.0010 |
| t_e71643c8 | approve | approve | 0.0024 |
| t_e4423fec | changes | changes | 0.7903 |
| t_be5b996c | changes | changes | 0.9537 |
| t_b4b00e9d | approve | approve | 0.0075 |
| t_67b203c5 | changes | changes | 0.9972 |
| t_c0a42077 | approve | approve | 0.0017 |
| t_ea6ce53e | changes | changes | 0.8587 |

Latency: 0.37–0.67 s per call (avg ~0.44 s). Total cost for all 11: **$0.0000507**.

This matches the design doc's spike claim ("`liquid/d1.1` 11/11, 0.39 s avg, $0.00005 for all
11") closely — same accuracy, same order-of-magnitude latency and cost, on the same 11 cards
(this build's review cards, not a re-sample).

### 2b. Implement-card tier probe (11 implement cards, board `dev-team-build`, read-only)

Source: the 11 "Task N: ..." implement cards (`t_80dbb864` … `t_f7b8e0f1`), same board,
read-only. (The design doc's spike mentions "the seven implement cards" — I could not find a
record of which 7 of these 11 that referred to; I ran all 11 that exist on this board, which is
a superset.)

**Question:** `tier`'s exact hardcoded criteria (fast / standard / deep), `state = {title, spec:
card body[:20000]}`. Same model.

| task | choice | P(fast) | P(standard) | P(deep) |
|---|---|---|---|---|
| Task 1: Move old teams | fast | 0.675 | 0.321 | 0.004 |
| Task 2: forge core | deep | 0.003 | 0.472 | 0.525 |
| Task 3: forge adapters | deep | 0.009 | 0.367 | 0.624 |
| Task 4: Gate stage rules | standard | 0.003 | 0.805 | 0.192 |
| Task 5: Gate board view | deep | 0.002 | 0.472 | 0.525 |
| **Task 6: terminal guard** | **deep** | 0.003 | 0.072 | **0.924** |
| Task 7: Souls and skills | standard | 0.157 | 0.694 | 0.149 |
| Task 8: Unix installer | standard | 0.007 | 0.802 | 0.191 |
| Task 9: Windows installer | deep | 0.014 | 0.295 | 0.690 |
| Task 10: CI | standard | 0.022 | 0.706 | 0.272 |
| Task 11: Docs | standard | 0.095 | 0.878 | 0.028 |

Task 6 (the one that actually needed a fix round on this board, per `t_996a4d09`'s history)
lands at **deep 0.924**, consistent with the design doc's own spike note ("both deciders put
Task 6 at deep 0.90–0.99"). Total cost for 11 calls: **$0.00030**. Latency 0.39–0.51 s.
Confirms the design doc's "tier choice is a weak signal" framing is still the right read: four
of the other ten cards also land at or near a tier boundary (e.g. Task 1 at
fast 0.675/standard 0.321, Task 5 and Task 2 both essentially a coin flip between standard and
deep) — useful as "escalate when unsure" input, not as a sole router, exactly as designed.

### 2c. Extract on qa's Task 2 terminal output

**Source note:** the kanban DB holds no raw terminal transcript for the historical qa Task 2
review (`t_a7e7dd6f`) — only its run `summary` and `metadata.findings`, which already quote the
repro commands and their outputs verbatim (the actual credential-leak reproduction the design
doc's spike point 4/5 refers to). I used that quoted text, with the real leaked token values
replaced by obviously-synthetic placeholders (`ghp_FAKE...LEAK`, a base64 string that is not a
real secret) before sending anything to a third-party API, since I have no way to confirm the
original value was itself already a test fixture rather than a real one.

**Entities spec, three shapes tried** (the skill's doc gives exactly one, broken, example):

1. `{"entities": [{"type": "email", "pattern": "..."}]}` (nested dict wrapping a list) →
   `exit 0`, **0 entities found** — no error, silently wrong.
2. `["credential", "secret", "token", "password"]` (plain array) → `exit 2 http-400` — same
   "expected record, received array" schema error as `decide`/`tier`.
3. `{"leaked-credential": "description of what to find"}` (a flat record, type name → prompt) →
   `exit 0`, **found both leaked credentials**, confidence 0.99 each, with character offsets and
   context.

**This confirms a second, separate instance of the same defect class, this time in the
skill doc's own extract example:** `dev-team-deciders/SKILL.md` documents `--entities` as
`{"types": ["synthetic-secret"]}` — I tried this exact documented shape directly: `exit 0`, **0
entities found**. Silent, not an error. A caller who follows the skill's own usage example for
`extract` gets a clean exit 0 and an empty result every time, and has no signal that the entity
spec was the wrong shape rather than "nothing found." This is worse than `decide`/`tier`'s loud
`http-400`, because the failure mode is an unflagged false negative on exactly the use case
(finding leaked credentials, failing tests) the design calls out as its best case.

With the correct (record) shape, `extract`'s own behaviour matches the design doc's spike claim
well: both leaked credentials found at confidence 0.99, cost $0.0000199, under 1 s — no false
positive this time (the spike reported one false positive on a correctly-redacted value; my
synthetic sample didn't include one, so this isn't a contradiction, just a smaller sample).

## Step 3: scratch-board plan run

**Deviation from the plan as written:** I created a `t9-pm` profile via
`bash dev-team/install-dev-team-unix.sh --prefix t9- --only pm` (real run, not dry — verified
`deciders --help` exit 0, `model-tiers.json` parses, profile created at
`~/.hermes/profiles/t9-pm`, no existing profile touched). I then tried to create an isolated
board named `deciders-check` via `hermes kanban boards create deciders-check` — this and every
other board-mutating CLI call (`boards create`, `boards rm`, `archive <id>`) is rejected with
`kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI`, because this
worker is itself a kanban-dispatched child. The `kanban_create` MCP tool's `board=` parameter
also did **not** create a separate board or db file — all four scratch tasks I created with
`board="deciders-check"` landed in the live `dev-team-deciders` board's own `kanban.db`
(confirmed by locating the task rows directly in that file; no `deciders-check` board or db
exists anywhere under `~/.hermes`). I could not find a tool or CLI path available to this
worker that creates a genuinely separate board. **I did not stop here** — the acceptance this
step cares about (does the pm's plan stage read the tier table and attach
tier/provider/model/probabilities/override_reason correctly) does not depend on board isolation,
so I ran the probe on the current board instead and I'm flagging the isolation gap for the
orchestrator/human rather than treating it as a hard blocker.

**Plan card 1 (table present):** created task `t_108f55dd` (title "deciders-check plan probe:
trivial README edit"), assignee `t9-pm`, workspace `dir` at a scratch target
(`~/.hermes/profiles/qa/cache/scratch/t6_plan_target`, **not** this repo). Dispatcher picked it
up and ran it to completion in ~70 s. Result: one implement child card, `t_ea796baa`
(assignee `t9-backend-db`, which does not exist — confirmed `skipped_nonspawnable` in its event
log, never dispatched). Its body carries:
- `tier: standard`
- `provider/model: anthropic / claude-sonnet-5`, also set as the card's real
  `model_override`/`provider_override` columns (verified via `kanban_show`, not just the body
  text) — matches the operator's `model-tiers.json` entry for `standard` exactly.
- `probabilities: unavailable` — because `deciders tier` hit the Headline finding's `http-400`;
  the pm's own run log shows it tried twice (`--spec <file>` and `--spec -`) before falling
  back to its own judgment per the skill's documented failure-handling rule, and said so
  plainly in the card body rather than inventing a number.
- `override_reason`: a real sentence explaining why no escalation rule applied and why
  `standard` (the table's lowest tier) was used directly.

**Plan card 2 (table absent, assumption A6):** moved
`~/.hermes/profiles/qa/dev-team/model-tiers.json` aside (this is qa's own profile directory,
not a shared or another profile's directory — `t9-pm/dev-team` is a symlink to it, since the
installer links a new profile's team state to the installing profile's own) before creating
task `t_231a22c3`. Dispatcher ran it; implement child card `t_8f11a5b4` carries
`model_override: null`, `provider_override: null` (verified via `kanban_show`) and a body that
explicitly states the table path does not exist, names the two sibling files it deliberately
did **not** read as a substitute (`model-tiers.json.aside_t6`, `.bak_t6`), and says plainly that
no model was guessed. This matches the design's acceptance exactly: "with the table absent, the
card carries no override."

**Table restored:** moved the original back over the live path; `cmp` against the byte-for-byte
backup taken before the move reported no difference (`IDENTICAL`). The backup file was then
removed; the restored file is the only copy.

**Cleanup:** `t9-pm` profile deleted (`hermes profile delete t9-pm -y` — confirmed: directory
and `~/.local/bin/t9-pm` wrapper both removed). The four scratch tasks
(`t_108f55dd`, `t_231a22c3`, `t_ea796baa`, `t_8f11a5b4`) remain on the live `dev-team-deciders`
board — I have no available tool or CLI path to archive them from inside this worker (see
above). Two are `done` (harmless, inert); two are `ready` but permanently un-dispatchable
(`t9-backend-db`/`t9-pm`-adjacent assignees that no longer exist) and will sit in `ready`
forever without interfering with any other task. **Recommend**: a human or
non-kanban-dispatched session runs `hermes kanban archive t_108f55dd t_231a22c3 t_ea796baa
t_8f11a5b4` to clean these up.

## Step 4: non-interactive skill load

Both `t9-pm` runs completed in 60–72 s end to end (dispatch to `completed` event) with a clean
exit and a JSON-shaped `deciders: http-400` error from `deciders tier`, not a hang: a stalled
prompt for `OPENROUTER_DECIDERS_KEY` would have shown as the task sitting in `running` past its
heartbeat interval with no completion event. Both runs' own `secret exec
OPENROUTER_DECIDERS_KEY=openrouter/deciders -- ...` calls (quoted in their result metadata)
returned promptly. No stall observed in either run.

## Step 5: key check

```
$ grep -rl "sk-or-" dev-team/                         # repo: no match
$ grep -rl "sk-or-" ~/.hermes/dev-team/                # one match, inspected below
```

The one match, `~/.hermes/dev-team/pending-specs/2026-10-29-dev-team-deciders-plan.md`, is the
plan document itself describing the test fixture's fake key and quoting this very grep
instruction (e.g. "the plan's fake key value is `sk-or-...`", "step 5 greps `dev-team/` for
`sk-or-...` and expects no match") — not a real key. Also checked, via read-only sqlite, every
board's `tasks.body`/`tasks.result`/`task_comments.body` for the literal `sk-or-`: the only
hits (11 on `dev-team-deciders`, one comment on `t_5267172c`) are, again, every orchestrator
task's own description of this exact Step-5 check (including this task's own body) and the
test's "fake key" sentence — no actual key value appears anywhere. **No key exposure found.**
The real `OPENROUTER_DECIDERS_KEY` value was never printed, written to a file, or put on a
command line by this run; it only ever passed through `secret exec` into a subprocess
environment.

## Open items for the orchestrator / a fix-round task

1. **High**: `deciders_core.py`'s `_cmd_tier` sends `questions` as a hardcoded list; OpenRouter's
   Decisions API requires a record keyed by question id. Every live `tier` call fails with
   `http-400`. Fix: build `questions` as `{"tier": {...}}` instead of `[{"id": "tier", ...}]`.
2. **High**: `dev-team-deciders/SKILL.md`'s `decide --questions` example is the same wrong
   (array) shape; any caller following the doc gets the same `http-400`. Fix the example to a
   record keyed by id, and say so explicitly (the live API's error message is clear enough —
   "expected record, received array" — to quote in the doc as a troubleshooting note).
3. **Medium-high**: `dev-team-deciders/SKILL.md`'s `extract --entities` example
   (`{"types": ["synthetic-secret"]}`) silently returns zero entities instead of erroring — the
   worst-shaped failure mode, since it looks like success. Fix the example to a flat record
   (`{"<type-name>": "<description>"}`), which is what actually works.
4. **Low**: the Step 1 "model-refusal" probe for `perplexity/pplx-decider-v1.2` did not
   reproduce the design doc's spike claim that this model is refused by the account's
   OpenRouter data-policy setting — it returned a normal `HTTP 200` today. Worth re-checking
   whether that policy setting changed, or whether a different model string was actually tested
   in the spike.
5. **Process gap, not a code defect**: no tool or CLI path available to a kanban-dispatched
   worker can create or archive an isolated board (`hermes kanban boards create/rm` and
   `hermes kanban archive <id>` are all rejected with "delegate_task child contexts cannot
   mutate Kanban tasks via the CLI"; the `kanban_create` tool's `board=` parameter silently
   falls back to the caller's current board instead of erroring or creating the named one).
   Four scratch probe tasks from Step 3 remain on `dev-team-deciders`
   (`t_108f55dd`, `t_231a22c3`, `t_ea796baa`, `t_8f11a5b4`) for a human to archive.
6. **Operational note, not a code defect**: `install-dev-team-unix.sh` writes
   `model-tiers.json` under the *installing profile's own* `dev-team/` directory
   (`$HERMES_HOME/dev-team/model-tiers.json`, where `$HERMES_HOME` defaults to the caller's own
   profile directory, e.g. `~/.hermes/profiles/devops/dev-team/...`), not under a single shared
   `~/.hermes/dev-team/`. Task 5's own completion metadata records the table as if it were the
   shared path; it is not, by default — this orchestrator's run rule ("set
   `HERMES_HOME=$HOME/.hermes` for the live `deciders` calls") only works because I manually
   copied the installed table from `~/.hermes/profiles/devops/dev-team/model-tiers.json` to
   `~/.hermes/dev-team/model-tiers.json` for this task's probes. Worth confirming whether
   `$HOME/.hermes/dev-team/` (used by the installer's own symlinking for most profiles) or each
   profile's own `dev-team/` is the intended single source of truth, and whether the two can
   actually drift, since each profile's `deciders` process resolves the table relative to its
   *own* `$HERMES_HOME` by default.
