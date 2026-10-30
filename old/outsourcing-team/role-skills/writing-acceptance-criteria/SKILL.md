---
name: writing-acceptance-criteria
description: "Use when requirements are vague. Turn wants into numbered testable criteria a stranger can check."
version: 1.0.0
author: engagement-lead
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [requirements, acceptance-criteria, analysis, traceability]
    category: analysis
---

# Turning a want into testable acceptance criteria

Your output is not a description of what the client asked for. It is a numbered list a
stranger could check without asking you a single question. If verifying a criterion requires
your interpretation, it is not yet a criterion.

## The test that decides everything

> Could someone who has never met the client, and never spoken to me, determine
> unambiguously whether this is true?

Apply it to every line. It eliminates almost every bad requirement on its own.

| Not a criterion | Why it fails | Criterion |
| --- | --- | --- |
| "Modernise the platform" | A mood, not an outcome | — decompose until each piece is checkable |
| "The page should be fast" | No threshold, no condition | "`/orders` responds in <500 ms p95 with 10k rows seeded" |
| "Export should work properly" | "Properly" is unspecified | "`GET /orders.csv` returns `text/csv` with header `id,customer,status,total`" |
| "Handle errors gracefully" | Which errors? What behaviour? | "On upstream 5xx, return 503 with `{error}` and log the correlation id; no stack trace in the body" |
| "Users can filter" | Which users, which filters, what result | "A signed-in user selecting status=dispatched sees only dispatched orders; the filter survives pagination" |

## Anatomy of a criterion

Each one names: **the trigger, the condition, the observable result.**

```text
3. GET /orders.csv?status=dispatched returns only rows whose status is dispatched,
   in the same column order as the unfiltered export.
```

Trigger (`GET …?status=dispatched`), condition (rows with that status), observable result
(only those rows, same column order). No adjectives. No "correctly".

## Always specify the negative space

The commonest cause of a dispute at UAT is not a criterion that was wrong — it is behaviour
nobody wrote down. For each feature, state explicitly:

- **What must not change.** "Existing `/orders` HTML behaviour is unchanged." This single
  line prevents more regressions than any test plan.
- **The empty case.** Zero rows, no results, nothing selected.
- **The unauthorised case.** Signed out, wrong role, expired token.
- **The boundary.** Maximum size, longest string, oldest date, concurrent edit.

## Assumptions are load-bearing — label them permanently

Write every assumption you are proceeding on, and flag the ones where being wrong
invalidates the plan. An assumption in your head is a future argument with no evidence; an
assumption on the record is a decision the client had the chance to correct.

```text
ASSUMPTION (load-bearing): orders never exceed 50k rows, so export is synchronous.
  If false: export needs a job queue and the estimate roughly doubles.
ASSUMPTION: "customer" means the billing account, not the shipping recipient.
```

Keep the label on the ones that turned out right too. That is what makes it credible on the
one that did not.

## Traceability

Every criterion maps to something a client asked for, and every client ask maps to at least
one criterion. When neither is true you have found either invented scope or a gap — both are
worth raising before anyone builds.

| # | Criterion | Source | Verified by |
| --- | --- | --- | --- |
| 1 | `GET /orders.csv` returns `text/csv`… | Client email 3 Sep | e2e `orders-csv.spec.js:23` |

## When to route elsewhere

- **The vertical decides the answer** — regulatory vs. merely customary practice in
  manufacturing, finance, healthcare, public sector → raise a `domain-consultant` card.
- **It touches auth, personal data, or payments** → raise a `security-compliance` card.
- **It implies a technology commitment or a one-way door** → raise a `solution-architect` card.
- **Scope or commercial terms would change** → escalate to `engagement-lead`. You may not
  absorb a change; that is the owner's signature.

Raise the card, link it with `kanban_link`, and say what you need back.

## Pitfalls

1. **Criteria written from our capability rather than their need.** Scope is bounded by what
   the client can absorb and operate, not by what we can build.
2. **Compound criteria.** "Returns CSV and emails the user and logs the export" is three
   criteria wearing one number — it cannot be partially passed.
3. **Interpreting an ambiguity instead of flagging it.** If you had to decide, the client
   did not. Record the decision as an assumption and make it visible.
4. **Dropping the "must not change" line.** It is the cheapest regression protection there is.
5. **Requirements with no owner.** If nobody on the client side can accept it, it cannot be
   accepted — say so before work starts.
