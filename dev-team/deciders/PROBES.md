# Live acceptance and probes — Task 6

Round 1: 2026-11-02 ~13:15 EET (commit `0ace9e9`). Round 2 (review round 1 fixes, card
`t_9900788a`): 2026-11-02 13:45–13:55 EET. Round 3 (card `t_74e4149b`): ~14:25 EET, with no
live calls. Repo branch `dev-team-deciders`. Every live call used
the operator's OpenRouter key only as
`secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- ...`, with
`HERMES_HOME=$HOME/.hermes` for the process, so `deciders` reads
`$HOME/.hermes/dev-team/model-tiers.json` and appends to the shared ledger
`$HOME/.hermes/dev-team/deciders-ledger.jsonl` (run rule A4). Ledger `board` =
`dev-team-deciders` on every row.

**Resolved model strings returned by the live API** (the response's `model` field):
`liquid/d1.1-20261027`, `perplexity/pplx-decider-v1.2-20261016`, `scaledown/extract`,
`scaledown/summarize` (the last two come back unchanged).

**Spend.**
- Round 2: **$0.00041916**, all through the CLI and all in the ledger: 29 ledger rows,
  ledger lines 7–35 (decide 12, tier 13, extract 3, summarize 1). Calls that failed
  (http-400, auth, no-key, timeout) cost nothing and write no ledger row.
- Round 1: $0.00007128 in the ledger, plus about $0.00037 from direct `curl` calls that
  **bypassed the ledger**, so the board budget never counted them. Round 2 replaced every one
  of those probes with a CLI call; see Step 2.

## Round 2 fixes

1. **`tier` sends a record.** `_cmd_tier` now sends `questions` as `{"tier": {...}}`. The live
   Decisions API rejects an array with `http-400` ("Invalid input: expected record, received
   array", path `questions`). In round 1, every live `tier` call failed this way. The
   unit-test fake transport never checked the shape, so the suite stayed green. The unit test
   (`TierQuestionShapeTests`) now asserts a record keyed `tier`. Live, `tier` exits 0 (Step 1).
2. **Skill doc, `decide --questions`.** `dev-team-deciders/SKILL.md` now documents it as a
   record keyed by question id, `{"q1": {"type": "choice", ...}}`, and says that an array gets
   `http-400`.
3. **Skill doc, `extract --entities`.** It is now documented as the flat record
   `{"<type-name>": "<description>"}`. The old example `{"types": [...]}` was a silent false
   negative in round 1 (exit 0, zero entities). In round 2 it timed out twice (see 2c).
4. **Decisions-only models in the tier table (operator addition).**
   - The problem: the shared table mapped `fast` to `openrouter` / `liquid/d1.1`. That model
     only answers `/api/alpha/decisions`, so an agent worker on a `fast` card could not run at
     all. The installer's `--tier-fast` help text even suggested it.
   - `deciders_core.py` now has a one-line `DECISION_MODELS` tuple.
   - Both installers refuse any `--tier-*` / `-Tier*` model in that tuple, or a dated variant
     of one. They exit 2 before writing anything.
   - `fast` now defaults to `anthropic` / `claude-haiku-4-5`, and the example text was
     corrected.
   - `~/.hermes/dev-team/model-tiers.json` was restored with that `fast` entry. The previous
     file is kept at `~/.hermes/profiles/qa/cache/scratch/t6r2/model-tiers.before.json`.
   - New tests: `test_install_unix.py` (refusal for each tier flag, the haiku default, help
     text) and `test_install_windows.py` (refusal run under `pwsh`, plus a static check).

## Round 3 fixes

Card `t_74e4149b`, 2026-11-02 ~14:25 EET. No live API calls in this round, so no spend and no
ledger rows.

1. **The live team's tier table still had `fast` = `openrouter` / `liquid/d1.1`.**
   - The round 2 fix rewrote only `~/.hermes/dev-team/model-tiers.json`. The current `dev-`
     team was installed from the **devops** profile, so its table is
     `~/.hermes/profiles/devops/dev-team/model-tiers.json` (written 12:40, before the round 2
     fix).
   - Every installed `dev-*` profile links to that file: dev-architect, dev-backend-db,
     dev-devops, dev-frontend, dev-pm, dev-qa, dev-reviewer and dev-uiux. I checked each link
     with `readlink -f`.
   - **Fix:** an operator edit of that one file, changing only `fast` to `anthropic` /
     `claude-haiku-4-5`. `standard` and `deep` are unchanged. I chose this over re-running the
     installer because a re-run would also rewrite every `dev-*` profile, which this card does
     not need. The previous file is kept at
     `~/.hermes/profiles/qa/cache/scratch/t6r3/devops-model-tiers.before.json`.
   - **Check:** `_tier_override` reads each tier through `profiles/dev-pm`, `profiles/devops`
     and `~/.hermes` (read-only, no API call). All three give `fast` → haiku, `standard` →
     sonnet-5 and `deep` → opus-5, each with `override_reason` null. Only two real
     `model-tiers.json` files exist under `~/.hermes` (the two above), and
     `grep -l 'liquid/d1.1\|pplx-decider'` matches neither.
   - **Caveat, for whoever reinstalls:** the installed `dev-pm` skills were copied on Oct 29,
     before this branch. Its `dev-team-stage-plan/SKILL.md` does not mention `deciders` yet,
     so today's live `dev-pm` does not call `deciders tier`. It will once the team is
     reinstalled from this branch. The table had to be fixed anyway, because that reinstall
     links to this same root, and the `deciders` wrapper on PATH runs the repo copy directly.
2. **Read-time check (recommended by the reviewer, done).** `_tier_override` now refuses a
   table entry that names a `DECISION_MODELS` model. It uses the installers' rule: the bare
   model or `provider/model`, either exact or followed by a dated `-…` suffix.
   - On a match, `override` is null and `override_reason` is
     `decisions-only model for <tier>: <provider>/<model>`.
   - Run against a copy of the pre-fix devops table, `fast` gives
     `(None, 'decisions-only model for fast: openrouter/liquid/d1.1')`.
   - New tests in `TierOverrideTableTests`: refusal of `liquid/d1.1`, its dated variant,
     `liquid` + `d1.1`, and `perplexity/pplx-decider-v1.2`; plus a near miss (`liquid/d1.10`)
     that is still used. Before the code change, the 4 refusal subtests failed on
     `assertIsNone(data["override"])`.
   - `dev-team-stage-plan/SKILL.md` gets the same rule for the pm's own table lookup (the path
     where a soul rule changed the tier), so that path cannot skip the check.
   - Suites: deciders 56 OK, forge 88 OK, dev-team-gate 228 OK, skills 4 OK, scripts 5 OK.

## Step 1: live calls (round 2, all through the shipped CLI)

| call | input | result | exit | latency |
|---|---|---|---|---|
| `decide` | `--questions` record `{"q1": {...}}` (the new documented shape) | `choice yes`, P(yes)=0.990, `liquid/d1.1-20261027`, $0.00000228 | 0 | 0.92 s |
| `decide` | `--questions` as an array (the old documented shape) | `{"error": "deciders: http-400"}` | 2 | 0.35 s |
| `extract` | `{"synthetic-secret": "<description>"}` (the new documented shape), synthetic 4-line log | 2 spans, confidence 0.993 / 0.999, with offsets | 0 | 1.61 s |
| `summarize` | one-line spec | summary, `"evidence": false` | 0 | 0.97 s |
| `tier` | rename-a-helper spec | `fast` (P 0.909 / 0.088 / 0.003), override `anthropic` / `claude-haiku-4-5`, `override_reason: null` | 0 | 0.92 s |

- **No key** (variable unset): `{"error": "deciders: no-key"}`, exit 2. No network call is
  made.
- **Wrong key** (`OPENROUTER_DECIDERS_KEY=invalid`, the literal string): `{"error":
  "deciders: auth"}`, exit 2.
- **`--model perplexity/pplx-decider-v1.2`**, retried once through `deciders tier`: exit 0, a
  normal answer (`fast` 0.980), resolved `perplexity/pplx-decider-v1.2-20261016`, $0.00000332.
  Round 1's direct call also got HTTP 200. **Policy refusal: not reproducible.** The
  design's spike note says this model was refused under the account's data-policy setting.
  Either that setting has changed since 2026-10-29, or the spike used a different model
  string. No refusal label could be captured.

## Step 2: spike probes, re-run through the CLI (round 2)

Driver: `t6r2/step2_cli.py` in qa's scratch dir. It runs `deciders decide|tier|extract` once per
item as a subprocess, under one `secret exec`. Inputs are round 1's read-only extracts from board
`dev-team-build`, reused unchanged. That board was never written to.

### 2a. 11 masked review summaries → `deciders decide`

- **Source:** the first-round review cards for Tasks 1–11 on `dev-team-build` (`t_10b2f8ce` …
  `t_ea6ce53e`), read through read-only sqlite (`?mode=ro`).
- **Masking:** a case-insensitive regex, `\b(approved?|approval|changes|change-requested)\b`, is
  replaced with `[MASKED]` in each run summary. It also masks a few ordinary uses of the noun
  "changes", which makes the task harder, not easier.
- **Question:** `{"needs_changes": {"type": "choice", "instructions": "Does this code review
  require changes before it can be approved?", ...}}`, with state `{title, review_summary}`.
- **Ground truth:** each review's own `metadata.verdict`.

**Result: 11/11 correct**, `liquid/d1.1` resolved as `liquid/d1.1-20261027`.

| task | actual | predicted | P(yes = needs changes) |
|---|---|---|---|
| t_10b2f8ce | approve | approve | 0.0015 |
| t_a7e7dd6f | changes | changes | 0.9342 |
| t_5ce76022 | changes | changes | 0.9622 |
| t_a0a97284 | approve | approve | 0.0026 |
| t_e71643c8 | approve | approve | 0.0015 |
| t_e4423fec | changes | changes | 0.7990 |
| t_be5b996c | changes | changes | 0.9797 |
| t_b4b00e9d | approve | approve | 0.0075 |
| t_67b203c5 | changes | changes | 0.9942 |
| t_c0a42077 | approve | approve | 0.0016 |
| t_ea6ce53e | changes | changes | 0.8587 |

- Latency: 0.44–0.62 s per call (CLI process start included).
- Cost: $0.0000507 for all 11.
- The spike claimed 11/11, 0.39 s avg and $0.00005. This run matches on accuracy and cost.

### 2b. Implement-card tier probe → `deciders tier`

Source: the 11 implement cards "Task N: …" on `dev-team-build` (`t_80dbb864` … `t_f7b8e0f1`).
The spike mentions seven implement cards, but no record says which seven, so all 11 were run.
Each call gets `--title` and `--spec` = the card body; the CLI truncates the spec to 20000
characters. The tier column is `choose_tier`'s pick: it takes the leading option at
P ≥ 0.65 and otherwise escalates one tier.

| task | tier | P(fast) | P(standard) | P(deep) | override |
|---|---|---|---|---|---|
| Task 1: Move old teams | fast | 0.674 | 0.321 | 0.005 | anthropic / claude-haiku-4-5 |
| Task 2: forge core | deep | 0.003 | 0.394 | 0.603 | anthropic / claude-opus-5 |
| Task 3: forge adapters | deep | 0.004 | 0.498 | 0.498 | anthropic / claude-opus-5 |
| Task 4: Gate stage rules | standard | 0.003 | 0.813 | 0.184 | anthropic / claude-sonnet-5 |
| Task 5: Gate board view | deep | 0.005 | 0.393 | 0.601 | anthropic / claude-opus-5 |
| **Task 6: terminal guard** | **deep** | 0.003 | 0.072 | **0.925** | anthropic / claude-opus-5 |
| Task 7: Souls and skills | standard | 0.145 | 0.676 | 0.179 | anthropic / claude-sonnet-5 |
| Task 8: Unix installer | deep (escalated: standard 0.622 < 0.65) | 0.012 | 0.622 | 0.366 | anthropic / claude-opus-5 |
| Task 9: Windows installer | deep | 0.011 | 0.319 | 0.670 | anthropic / claude-opus-5 |
| Task 10: CI | standard | 0.009 | 0.736 | 0.255 | anthropic / claude-sonnet-5 |
| Task 11: Docs | standard | 0.145 | 0.836 | 0.019 | anthropic / claude-sonnet-5 |

- All 11 calls exited 0. Latency 0.46–0.80 s. Cost $0.00030 for all 11.
- `override_reason` is `null` on every row. The table is present and has every tier.
- Task 6, the card that needed a fix round on that board, lands at **deep 0.925**. The spike
  put it at deep 0.90–0.99.
- Five of the 11 cards sit at or near a tier boundary: Tasks 1, 2, 3, 5 and 8. So the tier
  choice is a weak signal, as the design says. It is useful as "escalate when unsure", not as
  the only router.

### 2c. Extract on qa's Task 2 terminal output → `deciders extract`

- **Input:** the kanban DB holds no raw terminal transcript for qa's Task 2 review
  (`t_a7e7dd6f`). The input is the repro commands and outputs that its summary and
  `metadata.findings` quote verbatim.
- **Placeholders:** before anything was sent to a third-party API, the two leaked credential
  values in that text were replaced with obviously synthetic placeholders.

| entities spec | exit | result |
|---|---|---|
| flat record `{"leaked-credential": "<description>"}` (documented shape) | 0 | **both credentials found**, confidence 0.99 / 0.99, $0.0000199, 1.26 s |
| nested `{"entities": [{"type": ..., "pattern": ...}]}` | 0 | **0 entities** (silent false negative) |
| array `["credential", ...]` | 2 | `deciders: http-400` |
| old doc example `{"types": ["synthetic-secret"]}` (Step 1 input) | 2 | `deciders: timeout`, 2 of 2 tries (61 s each). In round 1 it exited 0 with 0 entities. |

Every shape except the flat record either fails or returns nothing. The skill doc now gives
only the flat record.

## Step 3: scratch-board plan run

**Round 2 status: passed. Run by the operator, because a dispatched worker is pinned to its
board DB (a Hermes fence, correctly not bypassed).** Evidence file:
`~/.hermes/dev-team/reviews/t6-step3-operator-evidence.md`. The operator ran it at
14:10–14:12 on 2026-11-02 from a non-dispatched session, at branch `dev-team-deciders` `5582af2`:
- **Install:** `bash dev-team/install-dev-team-unix.sh --prefix t9- --only pm`, exit 0. The
  t9-pm tier table: fast = `anthropic/claude-haiku-4-5`, standard = `anthropic/claude-sonnet-5`,
  deep = `anthropic/claude-opus-5`.
- **Board:** `deciders-check` (budget $3). Plan card `t_da3b9383` (assignee `t9-pm`, a dir
  workspace on a scratch git repo) was dispatched and finished in about 1 min.
- **Implement card:** `t_d5d1bda0`, on `deciders-check`. No `t9-*` cards landed on
  `dev-team-deciders`. It is assigned to `t9-backend-db`, with `model_override` =
  `claude-haiku-4-5` and `provider_override` = `anthropic`.
  - The body says: tier `fast` (final); probabilities `{"fast": 0.9347, "standard": 0.0624,
    "deep": 0.0029}`; `override_reason` none; `deciders tier` exit 0; model
    `liquid/d1.1-20261027`; cost $4.36e-06.
  - These are real probabilities, not `unavailable`, so the round 1 finding is closed.
- **Ledger:** one line, board `deciders-check`, subcommand `tier`, `task_id` null. That is a
  known gap (open item 4), not a blocker.
- **Cleanup:** `t_da3b9383` and `t_d5d1bda0` archived, board `deciders-check` archived, profile
  `t9-pm` deleted, scratch target removed. The stray `t_557bb79f` (below) is archived too.
- **No tier table (A6):** not re-run. The round 1 evidence (`t_231a22c3` → `t_8f11a5b4`: no
  override, and the body says why) is accepted by the operator.

The worker attempts below are kept as the record of why the operator had to run it.

Second worker attempt (round 2, run 38, after the operator created board `deciders-check`):
- `hermes kanban --board deciders-check list`, even with `HERMES_KANBAN_DB` and
  `HERMES_KANBAN_BOARD` pointed at `deciders-check` for that one command, returns
  `kanban: delegate_task child contexts cannot mutate Kanban tasks or boards`. The guard now
  rejects read-only commands too, so the operator's "confirm with list" check cannot run here.
- The `kanban_create` tool with `board="deciders-check"` created `t_557bb79f` (assignee `t9-pm`),
  but a read-only sqlite query shows it in `dev-team-deciders`'s database and not in
  `deciders-check`'s. The explicit `board=` argument does not override the pinned
  `HERMES_KANBAN_DB`.
- The card was created **before** `t9-pm` existed, so it could not dispatch. `t9-pm` was then
  deliberately not installed, and no other card was created. `t_557bb79f` never ran
  (`skipped_nonspawnable`) and has since been archived.
- Conclusion: a kanban-dispatched worker cannot place a card on another board through either
  path. The plan card for Step 3 has to be created by a non-dispatched session.

Round 2, first attempt:
- From this kanban-dispatched worker, `hermes kanban boards create deciders-check` returns
  `kanban: delegate_task child contexts cannot mutate Kanban tasks via the CLI`. The worker's
  environment sets the delegated-child marker and pins `HERMES_KANBAN_DB` to the build board's
  database.
- `hermes kanban boards list` shows the same task counts for every board. This is consistent
  with every board resolving to that pinned database.
- That pinning explains round 1's finding: the `kanban_create` tool's `board=` landed the
  cards on `dev-team-deciders`.
- This worker did not bypass the guard, and it created no cards on the live board this time.
- The re-run needs board `deciders-check` to exist first. A human or a non-dispatched session
  has to create it (see the card's block reason).

**Round 1 record** (on the wrong board, kept as evidence of the plan stage's override
behaviour):
- **Profile:** `t9-pm`, installed with `--prefix t9- --only pm`.
- **Table present:** plan card `t_108f55dd` produced implement card `t_ea796baa`.
  - `model_override` / `provider_override` were set to `anthropic` / `claude-sonnet-5`, which
    matches the table's `standard` entry.
  - The body says `probabilities: unavailable`, because `tier` failed with `http-400` then.
  - The body carries a real `override_reason`.
- **Table absent:** plan card `t_231a22c3` produced `t_8f11a5b4`.
  - `model_override` and `provider_override` are both `null`.
  - The body explains that the table is missing and that no model was guessed.
- **Restore:** the table was restored, and `cmp` matched the backup byte for byte.
- **Cleanup:** the operator has since archived all 4 scratch tasks. `t9-pm` was deleted.
  - The run left a 2-tier table (no `fast`) at `~/.hermes/profiles/qa/dev-team/model-tiers.json`.
    That happened because the installer ran with qa's `HERMES_HOME`.
  - The operator has since deleted it (orchestrator decision, below). A copy is at
    `t6r2/qa-profile-model-tiers.before.json`.

## Step 4: non-interactive skill load

Round 1: both `t9-pm` runs finished in 60–72 s, from dispatch to the `completed` event.
- Each `secret exec ... deciders tier` call returned promptly with a JSON error. The run never
  sat in `running` waiting on an `OPENROUTER_DECIDERS_KEY` prompt.
- **Round 2: passed. Run by the operator, together with Step 3** (same reason: a dispatched
  worker is pinned to its board DB, a Hermes fence, correctly not bypassed). The plan worker
  finished in about 1 min with `tier` exit 0. There was no key prompt and no stall.

## Step 5: key check

```
$ grep -rl "<the OpenRouter key prefix>" dev-team/          # repo: no match
$ grep -rl "<the OpenRouter key prefix>" ~/.hermes/dev-team/ # 1 match: pending-specs/…-plan.md
```

- **The one match** outside the repo is the plan document. It contains the plan's own text
  about the test fixture's fake key and this grep instruction, not a key.
- **Card text:** round 1 also checked every board's task bodies, results and comments
  through read-only sqlite. The only hits were orchestrator cards that describe this check.
- **The real key** was never printed, written to a file, put in a card, or passed on a command
  line. It only reached `deciders` subprocess environments through `secret exec`.

## Open items

1. **Step 3/4 re-run: closed.** The operator ran it from a non-dispatched session, because a
   dispatched worker is pinned to its board DB (a Hermes fence, correctly not bypassed). It
   passed, and it is cleaned up (see Step 3).
2. **Tier-table source of truth (decided by the operator).** It is the installer-written
   per-profile file `$HERMES_HOME/dev-team/model-tiers.json`, which is what the skill and
   `deciders tier` read.
   - The installer writes it to `$HERMES_ROOT/dev-team/` (the root of the installing process's
     `hermes config path`), and links every installed profile's `dev-team/` to it.
   - **The current `dev-` install is rooted at the devops profile.** Its table is
     `~/.hermes/profiles/devops/dev-team/model-tiers.json`, and every installed `dev-*`
     profile's `dev-team/model-tiers.json` links there. That is the table the team reads.
     Round 3 corrected its `fast` entry (see Round 3 fixes).
   - `~/.hermes/dev-team/model-tiers.json` is a separate table, for the default `HERMES_HOME`.
     Only builder calls run with `HERMES_HOME=$HOME/.hermes` (this build's probes) read it. It
     is kept, and its content now matches the devops table.
   - So a fix to the team's table must go to the root it was installed from. A reinstall must
     run from that same `HERMES_HOME` (devops today), or the new table lands in a different
     root and the team keeps reading the old one. Run from any other profile, the installer
     writes the table into that profile, which is how the stray qa-profile table appeared. The
     operator has deleted that table.
3. **The `tier` table is checked when it is read: closed in round 3.** A hand-edited entry that
   names a decisions-only model now gets no override, and `override_reason` says why. Other
   wrong entries (an unknown provider, a misspelled chat model) are still passed through as
   they are.
4. **Fixed (T3 followup, `dev-team-followups`).** The ledger's `task_id` was null on round-2
   rows because `HERMES_KANBAN_TASK` is not set in this worker's terminal environment, while
   `board` is. Root-caused by probing a live dispatched task's actual shell env directly
   (`printenv HERMES_KANBAN_TASK` with no `secret exec` involved): `HERMES_KANBAN_BOARD`,
   `HERMES_KANBAN_WORKSPACE`, `HERMES_KANBAN_DB` and others were all present, but
   `HERMES_KANBAN_TASK` was absent — ruling out `secret exec` scrubbing (it `export`s then
   `exec`s, inheriting the full environment) and pointing instead at the harness never handing
   that one variable to the subprocess a dispatched worker's shell commands run in, even though
   the worker's own tool-calling context (and therefore the calling agent) does know its task
   id. Fix: `deciders` now takes an explicit `--task-id` flag on every subcommand, which
   `append_ledger` prefers over `$HERMES_KANBAN_TASK`; the skill instructs always passing it
   with the agent's own known task id. The env var remains a fallback for callers that don't
   pass the flag.
5. **Fixed in round 2 (operator request):** the `dev-team-gate` failure
   `test_prefixed_assignees.test_no_line_about_assignees_names_a_bare_role`, failing since
   `aeb21ca`. `dev-team-deciders/SKILL.md` said "**Plan stage (pm):**". It now says
   `<prefix>pm` and names the `dev-team-prefix` file. The test was not changed. Suites:
   deciders 54 OK, forge 88 OK, dev-team-gate 228 OK, skills 4 OK.
