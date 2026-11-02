# dev-team deciders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every dev-team profile a cheap `deciders` CLI (decide / extract / summarize /
tier) over the operator's restricted OpenRouter key, let the plan stage pick each implement
card's model from an operator-written tier table, and count decider spend in the board budget.

**Architecture:** A stdlib Python CLI beside `forge` (`dev-team/deciders/`), same shape:
a thin `deciders.py` entry point over `deciders_core.main(argv, *, home, env, transport, out)`
with an injectable HTTP transport so tests never touch the network. One shared skill
(`dev-team-deciders`) tells agents when to use it. The plan-stage skill calls `deciders tier`;
the budget script reads the shared ledger; both installers install and wire it.

**Tech Stack:** Python 3.10+ stdlib (`argparse`, `json`, `urllib.request`, `unittest`), bash 3.2,
PowerShell 5.1.

**Spec:** `docs/superpowers/specs/2026-10-29-dev-team-deciders-design.md` (until the first build
merges: `~/.hermes/dev-team/pending-specs/2026-10-29-dev-team-deciders-design.md`).

**Branch:** a new branch `dev-team-deciders` cut from `autonomous-dev-team` after its PR is
merged by the human. Same git identity rule as the parent plan.

## Global Constraints

- Stdlib only; Python 3.10+; installer pieces bash 3.2 / PowerShell 5.1.
- Key variable name: `OPENROUTER_DECIDERS_KEY`. Never `OPENROUTER_API_KEY`. Secret-store name:
  `openrouter/deciders`.
- Endpoints: `POST https://openrouter.ai/api/alpha/decisions` (decide, tier);
  `POST https://openrouter.ai/api/v1/chat/completions` (extract, summarize).
- Pinned default models (versioned ids, never `~…-latest`): decide/tier `liquid/d1.1`;
  extract `scaledown/extract`; summarize `scaledown/summarize`. Each overridable by `--model`.
- Exit codes: `0` answer printed; `2` could not answer, with `{"error": "<label>: <detail>"}`
  on stdout. No exit `1`.
- Error labels (classified by HTTP status and exception type only, never by message text):
  `deciders: no-key`, `deciders: auth` (401), `deciders: key-cap` (402), `deciders: forbidden`
  (403), `deciders: model-unavailable` (404), `deciders: http-<code>` (other 4xx),
  `deciders: upstream` (429/5xx after one retry), `deciders: timeout`, `deciders: bad-response`
  (shape validation failed), `deciders: bad-input` (unreadable/invalid input files).
- Every stdout string passes `scrub(text, key)`: forge's `redact()` plus literal replacement of
  the key value with `***`.
- Tier rule constants: `TIER_CONFIDENT = 0.65`; tiers ordered `("fast", "standard", "deep")`.
- Ledger: `$HERMES_HOME/dev-team/deciders-ledger.jsonl` (a symlink to the shared team home in
  installed profiles); one `write()` of one JSON line per answered call, opened with `"a"`.
  Fields: `ts` (ISO-8601 UTC), `board`, `task_id`, `subcommand`, `model`, `cost`. `board` from
  `HERMES_KANBAN_BOARD`, `task_id` from `HERMES_KANBAN_TASK`, `null` when unset.
- Tier table: `$HERMES_HOME/dev-team/model-tiers.json`,
  `{"fast": {"provider": …, "model": …}, "standard": {…}, "deep": {…}}`; any tier may be absent.
- Tests use recorded fixtures; no test calls the network; no key value in any fixture.

## Review Focus

1. **The key leaking through an error path** (HTTP error body, exception text, `--help`, a
   traceback): expect only `***`. Test: `test_key_never_printed_on_any_error` (Task 1) drives
   every error label with a fake key and asserts the key string is absent from stdout.
2. **Missing key on a platform without `secret`**: expect exit `2` `deciders: no-key`, not a
   shell error or a hang. Tests: `test_no_key_exit_2` (Task 1); the skill text's fallback
   (Task 4).
3. **Skill load in a non-interactive kanban worker with the key variable unset** (Hermes may
   prompt for a declared required variable in the interactive CLI): expect the worker to
   proceed. Verified live in Task 6 step 4 on a scratch board.
4. **A decider answer whose probabilities don't sum to 1 or contain an unknown option**:
   expect `bad-response`, never a guessed tier. Test: `test_tier_rejects_unknown_option`
   (Task 2).
5. **Two workers appending to the ledger at once**: expect two intact lines. Test:
   `test_ledger_concurrent_appends_stay_whole` (Task 2) with two processes writing 200 lines
   each.

---

### Task 1: `deciders` core — decide, extract, summarize

**Files:**
- Create: `dev-team/deciders/deciders.py`, `dev-team/deciders/deciders_core.py`
- Create: `dev-team/deciders/tests/test_deciders_core.py`
- Create: `dev-team/deciders/tests/fixtures/` — `decide_ok.json`, `decide_bad_shape.json`,
  `extract_ok.json`, `summarize_ok.json` (shapes copied from the spike's real responses, values
  synthetic)

**Interfaces:**
- Consumes: `forge_core.redact(text: str) -> str`, imported by inserting
  `Path(__file__).resolve().parent.parent / "forge"` on `sys.path` (one owner; no copy).
- Produces:
  - `EXIT_OK = 0`, `EXIT_UNANSWERED = 2`
  - `class DecidersError(Exception)` with `.label: str` (one of the Global Constraints labels)
  - `Transport = Callable[[str, dict, dict, float], dict]` — `(url, headers, body, timeout) ->
    parsed JSON`; raises `DecidersError` for HTTP status / timeout
  - `urllib_transport(url, headers, body, timeout) -> dict` (the real one)
  - `scrub(text: str, key: str | None) -> str`
  - `main(argv: list[str], *, home: Path, env: Mapping[str, str], transport: Transport =
    urllib_transport, out=sys.stdout) -> int`
  - stdout JSON per subcommand:
    - decide: `{"answers": {...as returned...}, "model": str, "cost": float}`
    - extract: `{"entities": [{"type","text","start","end","confidence"}], "model", "cost"}`
    - summarize: `{"summary": str, "evidence": false, "model", "cost"}`

- [ ] **Step 1: Write the failing tests** in `test_deciders_core.py` (unittest, `FakeTransport`
  that records calls and returns a fixture or raises a given `DecidersError`):
  - `test_decide_builds_request` — argv `decide --state s.json --questions q.json`; asserts URL
    is the decisions endpoint, header `Authorization` is `Bearer <fake>`, body has `model ==
    "liquid/d1.1"`, `state`, `questions`; exit `0`; stdout `model`/`cost` copied from fixture.
  - `test_extract_builds_request` — body `model == "scaledown/extract"`,
    `messages == [{"role": "user", "content": <input>}]`,
    `provider == {"options": {"scaledown": {"entities": <entities>}}}`; stdout entities parsed
    from the fixture's `choices[0].message.content` JSON string.
  - `test_summarize_marks_not_evidence` — stdout `evidence is False`.
  - `test_model_flag_overrides_default` — `--model typesafe/jev-1.13` lands in the body.
  - `test_no_key_exit_2` — env without the variable → exit `2`, error starts
    `deciders: no-key`, transport never called.
  - `test_status_labels` — parametrised over 401→auth, 402→key-cap, 403→forbidden,
    404→model-unavailable, 418→http-418, 503 twice→upstream, timeout→timeout.
  - `test_retries_once_on_429_then_succeeds` — transport raises 429 then returns the fixture;
    exit `0`; exactly 2 calls.
  - `test_bad_shape_is_bad_response` — `decide_bad_shape.json` (no `answers`) → `bad-response`.
  - `test_key_never_printed_on_any_error` — fake key `sk-or-v1-FAKEKEYFAKEKEY`; every error
    path above plus a transport exception whose message contains the key; assert the key string
    never appears in stdout.
  - `test_redact_is_forges` — `deciders_core.redact is forge_core.redact`.
- [ ] **Step 2: Run** `cd dev-team/deciders && python3 -m unittest discover -s tests -v`.
  Expected: errors (module not found).
- [ ] **Step 3: Implement** `deciders_core.py` and `deciders.py` (entry point resolves
  `HERMES_HOME` exactly as `forge.py` `_home()` does and passes `os.environ` as `env`).
  Validation: decide response must have dict `answers` and str `model`; chat responses must
  have `choices[0].message.content`; extract content must parse as JSON with an `entities`
  list. Timeout 60 s. Anything else raises `DecidersError("bad-response")`. A catch-all in
  `main` turns any exception into exit `2` with a scrubbed message, as `forge_core.main` does.
- [ ] **Step 4: Run** the same command. Expected: all pass. Also run forge's suite
  (`cd dev-team/forge && python3 -m unittest discover -s tests`) to confirm nothing changed
  there.
- [ ] **Step 5: Commit** `deciders: decide, extract, summarize over OpenRouter`.

### Task 2: ledger and `deciders tier`

**Files:**
- Modify: `dev-team/deciders/deciders_core.py`
- Create: `dev-team/deciders/tests/test_tier_and_ledger.py`,
  `dev-team/deciders/tests/fixtures/tier_confident.json`, `tier_split.json`,
  `tier_unknown_option.json`

**Interfaces:**
- Consumes: Task 1's `main`, `Transport`, `DecidersError`.
- Produces:
  - `TIER_CONFIDENT = 0.65`, `TIERS = ("fast", "standard", "deep")`
  - `choose_tier(probabilities: dict[str, float]) -> str` — the rule from the spec
  - `ledger_path(home: Path) -> Path` → `home / "dev-team" / "deciders-ledger.jsonl"`
  - `append_ledger(home: Path, env: Mapping, subcommand: str, model: str, cost: float) -> None`
  - `tier` subcommand: `deciders tier --title <text> --spec <file|->` → stdout
    `{"tier": str, "probabilities": {...}, "model": str, "cost": float,
      "override": {"provider": str, "model": str} | null, "override_reason": str | null}`
    where `override` comes from `home/dev-team/model-tiers.json` and `override_reason` is
    `"no tier table"` / `"no entry for <tier>"` when null.

- [ ] **Step 1: Write the failing tests:**
  - `test_choose_tier_confident` — `{"fast":0.1,"standard":0.7,"deep":0.2}` → `standard`.
  - `test_choose_tier_split_takes_higher` — `{"fast":0.02,"standard":0.57,"deep":0.41}` →
    `deep` (the spike's real Task 7 answer).
  - `test_choose_tier_split_low_pair` — `{"fast":0.5,"standard":0.45,"deep":0.05}` →
    `standard`.
  - `test_tier_rejects_unknown_option` — fixture with option `huge` → exit `2` `bad-response`.
  - `test_tier_reads_table` — table with `deep` → `override == {"provider": "anthropic",
    "model": "claude-opus-5"}`.
  - `test_tier_without_table` — no file → `override is None`, `override_reason ==
    "no tier table"`; missing entry → `"no entry for deep"`.
  - `test_tier_question_is_fixed` — request body `questions` has exactly one `choice` question
    whose criteria keys are `fast`, `standard`, `deep`.
  - `test_ledger_written_on_success_only` — success → one line with `board`/`task_id` from
    `HERMES_KANBAN_BOARD`/`HERMES_KANBAN_TASK`; exit-2 call → no line.
  - `test_ledger_concurrent_appends_stay_whole` — two `multiprocessing` workers × 200
    `append_ledger` calls; file has 400 lines, each valid JSON.
- [ ] **Step 2: Run** `python3 -m unittest discover -s tests -v`. Expected: the new tests fail.
- [ ] **Step 3: Implement.** The tier question's instructions and criteria are the spike's
  (`"Which model tier does this software task need to be done right the first time?"`;
  fast: mechanical moves, renames, small doc edits; standard: routine implementation from a
  precise spec; deep: security-sensitive code, subtle failure handling, or design judgment the
  spec leaves open). State is `{"title": …, "spec": <first 20000 chars>}`. Every subcommand
  appends to the ledger after a successful answer. Beside `TIER_CONFIDENT`, a comment states
  each mistake's consequence, as the spec requires: too low sends uncertain cards to a cheaper
  model and costs a rework round; too high escalates routine cards and costs money; and the
  spike's evidence for 0.65 is weak (see `PROBES.md`).
- [ ] **Step 4: Run** the suite. Expected: all pass.
- [ ] **Step 5: Commit** `deciders: tier subcommand, tier table, spend ledger`.

### Task 3: budget script counts decider spend

**Files:**
- Modify: `dev-team/scripts/kanban-budget.py` (`collect`, `report`, the `--json` branch of
  `main`)
- Create: `dev-team/scripts/tests/test_kanban_budget_deciders.py`

**Interfaces:**
- Consumes: Task 2's ledger path and fields.
- Produces: `deciders_spend(root: Path, board: str) -> float`; `collect()` result gains
  `"deciders": float`, and `"total"` includes it. Text report prints
  `  Decision models:      $<x>` beside `Attributed to cards`.

- [ ] **Step 1: Write the failing tests** (temporary Hermes root with a minimal board DB and
  ledger, as the existing script's own fixtures do, or built in the test):
  - `test_deciders_spend_filters_board` — lines for boards `a` and `b`; `a` sums only `a`.
  - `test_total_includes_deciders` — `collect()["total"]` = runs + ledger.
  - `test_ledger_drives_exit_code` — budget $1, default 15% reserve, runs $0.80: without the
    ledger line → exit `0` (below the $0.85 usable line); with a $0.10 ledger line → exit `1`
    (into the reserve); with a $0.25 ledger line → exit `2` (exhausted).
  - `test_missing_or_corrupt_ledger` — absent file → `0.0`; a corrupt line is skipped and
    counted in a printed warning, never crashes.
- [ ] **Step 2: Run** `python3 -m unittest discover -s dev-team/scripts/tests -v`. Expected:
  fail.
- [ ] **Step 3: Implement.** Ledger path is `hermes_root() / "dev-team" /
  "deciders-ledger.jsonl"`.
- [ ] **Step 4: Run.** Expected: pass; also run the script against the live `dev-team-build`
  board read-only and check its total is unchanged (no ledger lines for that board).
- [ ] **Step 5: Commit** `kanban-budget: count decision-model spend`.

### Task 4: the `dev-team-deciders` skill, plan-stage tier step, soul references

**Files:**
- Create: `dev-team/skills/dev-team-deciders/SKILL.md`
- Modify: `dev-team/skills/dev-team-stage-plan/SKILL.md` (the "The child card" section)
- Modify: `dev-team/skills/dev-team-stage-pr-ci/SKILL.md` (step 5, CI red)
- Modify: every `dev-team/souls/SOUL-*.md` — one line under each soul's tools/skills section
  naming `dev-team-deciders`
- Create: `dev-team/skills/tests/test_skill_texts.py`

**Interfaces:**
- Consumes: the CLI contract from Tasks 1–2.

- [ ] **Step 1: Write the failing test** `test_skill_texts.py`:
  - `test_deciders_skill_frontmatter` — parses the YAML front matter by hand (stdlib) and
    asserts `name: dev-team-deciders` and a `required_environment_variables` entry named
    `OPENROUTER_DECIDERS_KEY`.
  - `test_deciders_skill_states_the_rules` — the body contains the exact strings
    `never evidence`, `exit 2`, `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders`,
    `0.35` and `0.65`.
  - `test_plan_skill_calls_tier` — plan SKILL.md contains `deciders tier` and
    `model-tiers.json`.
  - `test_every_soul_names_the_skill` — each `SOUL-*.md` contains `dev-team-deciders`.
- [ ] **Step 2: Run** `python3 -m unittest discover -s dev-team/skills/tests -v`. Expected:
  fail.
- [ ] **Step 3: Write the texts.** The skill carries the spec's per-subcommand rules verbatim
  in substance (decide: repeated bounded questions, the agent decides itself between 0.35 and
  0.65; extract: a pointer, open the spans yourself; summarize: never evidence, never on test,
  review or CI output; exit `2`: do the work directly, never block); the invocation rule (if
  `command -v secret` succeeds, wrap in `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders
  --`, otherwise call `deciders` directly and rely on the passed-through variable); and the
  decision-model weak spots (quantities, dates, negation, indirection, self-arguing text). The
  plan-stage addition: before each implement `kanban_create`, run `deciders tier`; if
  `override` is non-null pass its `provider` and `model` to `kanban_create`; put `tier`,
  `probabilities` and `override_reason` in the card body; the soul's routing overrides win
  (retry drops a tier, two failures escalate once, architecture and security are deep); on exit
  `2` pick the tier yourself. The pr-ci addition: on red, `deciders extract` over the CI log to
  find failing tests and file:line, then judge "caused by this change vs flake" yourself.
- [ ] **Step 4: Run** the test. Expected: pass.
- [ ] **Step 5: Commit** `dev-team: deciders skill, plan-stage tier choice, soul references`.

### Task 5: installers

**Files:**
- Modify: `dev-team/install-dev-team-unix.sh`
- Modify: `dev-team/install-dev-team-windows.ps1` (from the parent plan's Task 9)

**Interfaces:**
- Consumes: `dev-team/deciders/deciders.py`, `dev-team/skills/dev-team-deciders/`, the tier
  table format.

- [ ] **Step 1: Unix installer.**
  - New flags `--tier-fast P:M`, `--tier-standard P:M`, `--tier-deep P:M` (provider and model
    split on the first `:`; reject a value without `:` with exit `2`, as the other flags do).
    Defaults: deep from `$FRONTIER_MODEL`, standard from `$MID_MODEL` (split on the first `/`),
    fast absent unless given.
  - Write `$DEV_TEAM_HOME/model-tiers.json` from those (overwrite; `--dry-run` prints it).
  - Install `dev-team-deciders` into every profile's `skills/dev-team/` beside the stage
    skills.
  - Install `~/.local/bin/deciders` as the same kind of wrapper as `forge`.
  - Print, never write, key setup: if `command -v secret` → "store it with
    `secret set openrouter/deciders`"; otherwise "add `OPENROUTER_DECIDERS_KEY` to each
    profile's `.env`". The installer never reads, prompts for or writes the key.
  - Verification block: `deciders --help` exits `0`; `model-tiers.json` parses.
- [ ] **Step 2: Run** `bash -n` and the installer with `--dry-run --prefix t9-` on this
  machine; expected: the new lines appear, nothing changes. Then a real run with
  `--prefix t9- --only qa`; expected: skill present, tier table written, `deciders --help`
  works; then remove the `t9-qa` profile.
- [ ] **Step 3: Windows installer.** The same four additions in PowerShell 5.1 (flags
  `-TierFast`, `-TierStandard`, `-TierDeep`; a `deciders.cmd` wrapper beside `forge.cmd`; the
  `.env` key note — there is no secret store on Windows).
- [ ] **Step 4: Run** PowerShell's parser check (`pwsh -NoProfile -Command
  "[ScriptBlock]::Create((Get-Content -Raw dev-team/install-dev-team-windows.ps1)) | Out-Null"`)
  and `-DryRun` on the Windows machine reachable over WinRM (password via
  `secret exec`, as in the parent plan).
- [ ] **Step 5: Commit** `dev-team installers: deciders CLI, skill and tier table`.

### Task 6: live acceptance and probes

**Files:**
- Create: `dev-team/deciders/PROBES.md`

- [ ] **Step 1: Live calls.** With
  `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders …` run one `decide`,
  `extract`, `summarize` and `tier`; expected exit `0` each. Then: no variable → `no-key`; a
  deliberately wrong key value (`OPENROUTER_DECIDERS_KEY=invalid`) → `auth`;
  `--model perplexity/pplx-decider-v1.2` → record which label the policy refusal gets.
- [ ] **Step 2: Re-run the spike's probes through the CLI** on the parent build's board: the 11
  masked review summaries (expected 11/11 for `liquid/d1.1`), the implement-card tier probe,
  and extract on qa's Task 2 terminal output. Record accuracy, probabilities, latency and cost
  in `PROBES.md`, plus the date and the resolved model strings.
- [ ] **Step 3: Scratch-board plan run.** On a new board `deciders-check`, create one plan card
  for a trivial change on a scratch directory and let the pm run it; check the implement card
  carries the provider/model from `model-tiers.json` and the probabilities in its body.
  Archive the board afterwards.
- [ ] **Step 4: Non-interactive skill load.** In that run, confirm the worker that loaded
  `dev-team-deciders` without `OPENROUTER_DECIDERS_KEY` set in its `.env` did not stall on a
  prompt (it should have used `secret exec`).
- [ ] **Step 5: Key check.** `grep -r "sk-or-" dev-team/ ~/.hermes/dev-team/` and the
  scratch-board comments; expected: no match. Commit `deciders: live probes`.
