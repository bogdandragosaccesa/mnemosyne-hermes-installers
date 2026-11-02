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
    secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders <subcommand> ...
else
    deciders <subcommand> ...   # relies on OPENROUTER_DECIDERS_KEY already in the environment
fi
```

Where `secret` is on `PATH`, always wrap the call in
`secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders -- deciders ...` rather than reading
the key yourself. Elsewhere, call `deciders` directly and let the passed-through environment
variable supply the key. Never print, log, or write the key value — `deciders` already scrubs
its own stdout, but you must not route the value through anything that would re-expose it (a
`terminal` echo, a card body, a commit).

## The three subcommands

- **`deciders decide --state <file|-> --questions <file|->`** — for a bounded yes/no, one-of-N,
  or graded question you would otherwise spend a long read or several turns answering, and that
  repeats (triaging many similar items, matching a failure to one of several known causes). Not
  for one-off reasoning you are already doing in this turn — that call costs you nothing extra
  and deciders would only add a round trip. Code applies the real thresholds; between
  probability `0.35` and `0.65` on the leading answer, the signal is too weak to trust and you
  decide yourself instead of taking the model's top pick.
- **`deciders extract --entities <file|-> [--input <file|->]`** — to find *where* to look inside
  large text (a CI log, a test run, a long diff): it returns spans with offsets and confidence,
  not conclusions. An extracted span is a pointer, never a finding — you must open the reported
  span yourself and judge it before you act on it or cite it anywhere.
- **`deciders summarize [--input <file|->]`** — only to compress context you will not cite:
  background material, old discussion, anything you are reading for orientation rather than
  evidence. The output always carries `"evidence": false`, and that is a statement of fact, not
  a formality: never evidence for a verdict, a finding, or "tests pass". Never run it on test
  output, review output, or CI output, and never let its summary stand in for reading output
  that a gate or a finding rests on. The spike that justified this rule caught it twice writing
  a confident, false summary of a run that had actually failed.

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

- **Plan stage (pm):** before each implement-card `kanban_create`, run `deciders tier` — see
  `dev-team-stage-plan`'s "The child card" section for the exact rule.
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
