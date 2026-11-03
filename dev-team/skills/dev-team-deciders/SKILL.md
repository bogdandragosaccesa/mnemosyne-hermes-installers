---
name: dev-team-deciders
description: Use when a bounded yes/no, one-of-N or graded question, a locate-the-span task over large text, or a non-evidentiary summary would otherwise cost a long read or several agent turns.
required_environment_variables:
  - name: OPENROUTER_DECIDERS_KEY
    prompt: OpenRouter key restricted to decision/extraction/summary models (never your Hermes provider key)
    help: "Operator-provided, capped, restricted to decision models and the two ScaleDown text models. Set it once with `secret set openrouter/deciders` (or, with no `secret` CLI, as a plain user environment variable). The installer never reads, prompts for, or writes this value."
    required_for: "every `deciders decide|extract|summarize|tier` call"
    optional: true
---

# dev-team-deciders

`deciders` is a small stdlib CLI, installed beside `forge`, that spends a few hundredths of a
cent and under a second on calls that would otherwise cost a long agent read or several turns.
It talks to OpenRouter's Decisions API and two ScaleDown text models. It is not a chat agent:
none of its three subcommands hold a conversation, remember anything between calls, or see your
tools.

**Deciders steer, they never approve.** They may decide alone where a wrong answer is cheap and
reversible: which model tier, a routine triage, which of two known causes a failure matches.
They never decide a gate outcome, a rework decision, PR readiness, a security finding, or a
budget overrun — those stay with you and, ultimately, the human. Do not call deciders
reflexively: if you are not currently paying for a long read or several turns of your own
reasoning on a bounded, repeating question, this tool is not for that turn.

## Invocation

```
if command -v secret >/dev/null 2>&1; then
    secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders <subcommand> ... --task-id "<your kanban task id>"
else
    deciders <subcommand> ... --task-id "<your kanban task id>"   # relies on OPENROUTER_DECIDERS_KEY already in the environment
fi
```

Where `secret` is on `PATH`, always wrap the call in
`secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders ...` rather than reading
the key yourself. Elsewhere, call `deciders` directly and let the passed-through environment
variable supply the key. Never print, log, or write the key value — `deciders` already scrubs
its own stdout, but you must not route the value through anything that would re-expose it (a
`terminal` echo, a card body, a commit).

**Always pass `--task-id` with your own kanban task id, literally.** `deciders` ledgers every
call's spend with the task that paid for it, reading `$HERMES_KANBAN_TASK` only as a fallback.
Do not rely on that fallback: a dispatched worker's shell/subprocess environment (what
`secret exec` or a plain `terminal` command actually runs with) is not guaranteed to carry
`HERMES_KANBAN_TASK` even when sibling vars like `HERMES_KANBAN_BOARD` and
`HERMES_KANBAN_WORKSPACE` are present there — confirmed by probing a real dispatched task's
shell. You, the calling agent, already know your task id (it is in your dispatch context,
`kanban_show()`, or the board/task text you were given) whether or not the shell inherits it, so
pass it explicitly as `--task-id`. Never pass the key the same way — `--task-id` takes the task
id string only, never a secret value.

## The three subcommands

- **`deciders decide --state <file|-> --questions <file|->`** — for a bounded yes/no, one-of-N,
  or graded question you would otherwise spend a long read or several turns answering, and that
  repeats (triaging many similar items, matching a failure to one of several known causes). Not
  for one-off reasoning you are already doing in this turn — that call costs you nothing extra
  and deciders would only add a round trip. `--questions` is a JSON object (a record) keyed by
  question id, one entry per question, e.g. `{"q1": {"type": "choice", "instructions": "Is this
  a flake?", "criteria": {"yes": "...", "no": "..."}}}` — answers come back under the same ids.
  A JSON array gets `deciders: http-400` (the API says "expected record, received array"). `decide` returns the model's raw `choice` and
  `probabilities` for each question **unmodified — no threshold is applied in code**; only
  `deciders tier` applies one (via `choose_tier`). You apply the band yourself: for a yes/no
  question, read the `yes` probability — `>= 0.65` means yes, `<= 0.35` means no, anything in
  between you decide yourself without trusting the model's `choice`. For a one-of-N question,
  trust the leading option's `choice` only when its own probability is `>= 0.65`; below that,
  decide yourself — a leading option sitting at, say, 0.30 is a *weaker* signal than the band
  itself, not a usable pick.
- **`deciders extract --entities <file|-> [--input <file|->]`** — to find *where* to look inside
  large text (a CI log, a test run, a long diff): it returns spans with offsets and confidence,
  not conclusions. `--entities` is a flat JSON object mapping each entity type name to a
  description of what to find, `{"<type-name>": "<description>"}`, e.g.
  `{"synthetic-secret": "an API key, token or password written into the text"}` — passed
  through as `provider.options.scaledown.entities`. Any other shape (a nested `{"entities":
  [...]}`, the old `{"types": [...]}`, or an array) is now refused as `deciders: bad-input`
  before any call is made. An extracted span is a pointer, never a finding — you
  must open the reported span yourself and judge it before you act on it or cite it anywhere.
- **`deciders summarize [--input <file|->]`** — only to compress context you will not cite:
  background material, old discussion, anything you are reading for orientation rather than
  evidence. The output always carries `"evidence": false`, and that is a statement of fact, not
  a formality: never evidence for a verdict, a finding, or "tests pass". Never run it on test
  output, review output, or CI output, and never let its summary stand in for reading output
  that a gate or a finding rests on. The spike that justified this rule caught it writing a
  confident, false summary of a review whose tool output showed a credential leak.

## On failure: exit 2

Every subcommand that could not answer — no key, a bad key, an HTTP error, the key's spend cap,
a malformed response, a timeout — returns exit 2 with a labelled `{"error": ...}` on stdout,
never a silent guess. There is no exit `1`: a decision is data, not a pass/fail. On exit 2, fall
back to doing the work directly yourself with your own judgment. Never block a card, and never
delay it, because a decider call was unavailable — the pipeline keeps moving on your own
reasoning.

## Where deciders is known to be weak

OpenRouter's own documentation and this team's spike agree these are the cases decision models
get wrong most often — keep them in code or in your own judgment, never hand them to a decider
call outright:

- **quantities** — "how many", counts, thresholds expressed as numbers;
- **dates** — relative or absolute time comparisons;
- **negation** — "does NOT", "is not", "without";
- **indirection** — the real answer is implied rather than stated;
- **self-arguing text** — input that is itself trying to persuade the classifier of its own
  answer (a summary written to look approved, a log edited to look green).

## Where else this gets used (guidance, not a separate contract)

- **Plan stage (`<prefix>pm`, where `<prefix>` is read from your profile's `dev-team-prefix`
  file):** before each implement-card `kanban_create`, run `deciders tier` — see
  `dev-team-stage-plan`'s "The child card" section for the exact rule. The tier table's single
  source of truth is the installer-written `$HERMES_HOME/dev-team/model-tiers.json` of the
  process that runs `deciders` (per profile). `~/.hermes/dev-team/model-tiers.json` is the same
  file for the default `HERMES_HOME`, used only by calls run with `HERMES_HOME=$HOME/.hermes`.
- **pr-ci stage (devops):** on a red CI run, `deciders extract` over the log to find failing
  tests and file:line before judging cause vs. flake yourself — see `dev-team-stage-pr-ci`
  step 5.
- **qa and the reviewer:** `extract` over large test or mutation output to locate failures,
  never to conclude them.
- **pm:** `decide` for triage across many small, similar items (e.g. which open findings look
  like duplicates) — never for which finding is real.

## Never

- Never let a `summarize` or `extract` result stand in as evidence for a verdict, a finding, a
  "tests pass" claim, or anything else a gate reads.
- Never call deciders reflexively on a question you are only going to reason about once.
- Never retry past `deciders`'s own built-in retry, and never loop calling it hoping for a
  different answer.
- Never print, write, or pass the key value anywhere but through `secret exec` or the
  pre-existing environment variable.
