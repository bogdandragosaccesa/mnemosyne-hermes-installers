---
name: architecture-decisions-and-one-way-doors
description: "Use when setting boundaries, contracts, or technology commitments. ADRs, reversibility, escalation."
version: 1.0.0
author: engagement-lead
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [architecture, ADR, decisions, boundaries, tradeoffs]
    category: architecture
---

# Architecture decisions and one-way doors

Your job is boundaries, contracts and commitments — and knowing which of them can be undone.
The decision that matters is rarely the technology. It is whether we can change our mind
later, and what it costs if we cannot.

## Classify reversibility first

Before analysing any option, classify the decision. It sets how much rigour it deserves and
**who** gets to make it.

| Class | Test | Who decides |
| --- | --- | --- |
| **Two-way door** | We could reverse this in days, cheaply | You. Decide, record, move on |
| **Costly to reverse** | Reversible, but weeks of work or visible disruption | You, with the decision recorded and the cost stated |
| **One-way door** | Data model in production, public API, vendor lock-in, anything the client's customers see | **Not yours.** Escalate to `engagement-lead` for the client's named owner |

Deliberating a two-way door wastes the engagement's money. Deciding a one-way door yourself
spends authority you were never given. Both are failures; the second is worse.

Common one-way doors, easily mistaken for technical detail: a persisted schema once real
data exists; a public URL or API shape once anything consumes it; an identifier scheme;
choice of a managed service that has no export; anything encoded into the client's
customers' expectations.

## Write the ADR — short, and with the rejected options in it

```markdown
# ADR-007: Synchronous CSV export rather than a job queue

Status: accepted | Date: 2026-09-13 | Reversibility: costly (≈1 week to add a queue)

## Context
Orders export is requested from the orders screen. Current volume is ~3k rows;
the client expects <50k within two years (ASSUMPTION, load-bearing — from BA note 3 Sep).

## Decision
Generate CSV synchronously in the request, streaming the response.

## Options rejected
- Job queue + email link — correct at 500k rows; adds a broker, a worker and a
  storage bucket the client's team would have to operate. Rejected as premature.
- Client-side generation — cannot honour server-side filters. Rejected as incorrect.

## Consequences
+ No new infrastructure; their team operates nothing new.
− Above ~50k rows the request will time out. Trigger to revisit: sustained exports
  over 20k rows, or p95 export latency over 5s.
```

Two things make an ADR worth writing: **the options you rejected and why**, and **the
trigger that should make someone revisit it**. Without the rejected options, the next person
re-litigates the decision from scratch. Without the trigger, a good decision silently
becomes a bad one.

## Scope is bounded by what the client can operate

A technically superior architecture their team cannot run, staff or fund next year is a
failure with good test coverage. For each component you introduce, ask: who operates this
after we leave, and have they done it before? If our departure breaks it, we have not
delivered it.

Prefer: fewer moving parts, technology already in their stack, boring over novel, and
anything their existing team already knows.

## Defer what can be deferred

The best time to make a decision is the last responsible moment — when you know the most,
while it still costs nothing to choose. Explicitly record deferrals so they are not mistaken
for oversights:

```text
DEFERRED: caching strategy. Not needed below 100 req/s. Revisit if p95 exceeds 800ms.
```

## Where you stop

Escalate to `engagement-lead` — do not decide:

- Any one-way door by the table above.
- A technology commitment with licensing, cost or vendor lock-in implications.
- Anything that changes the shape of the contract or the scope.
- A non-functional requirement the client has not agreed to (availability target, RPO/RTO,
  retention period) — these are commercial commitments wearing technical clothing.
- Anything where the honest answer is "infeasible as scoped". Say it early. Your being right
  early is cheap; your being right at UAT is not.

Raise the escalation as: **the decision, the options, the recommendation, what it costs to
get it wrong, and the date by which it must be settled.**

## Route to specialists rather than deciding for them

- Auth, personal data, payments, or a new trust boundary → `security-compliance` card.
- Pipelines, environments, telemetry, cutover mechanics → `platform-sre` card.
- An interface to a system nobody can change → `integration-engineer` card.
- Requirements too vague to design against → back to `business-analyst`, with the specific
  ambiguity named.

## Pitfalls

1. **Designing for volumes nobody has.** Record the assumption and the revisit trigger.
2. **Architecture that only we can run.** Handover is a design constraint, not a phase.
3. **Undocumented decisions.** Six weeks later nobody remembers why, so it gets reversed by
   accident.
4. **Deciding a one-way door because the owner was slow to reply.** Wait, or escalate louder.
5. **Novel technology on someone else's production system.** Their risk, our curiosity —
   a bad trade.
