---
name: integration-contracts-and-migration
description: "Use when interfacing to systems nobody can change, migrating data, or reconciling records."
version: 1.0.0
author: engagement-lead
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [integration, migration, reconciliation, contracts, idempotency]
    category: integration
---

# Integration contracts, migration and reconciliation

Your defining constraint: **the system on the other side cannot be changed.** It is
someone else's, often older than the engagement, sometimes older than the company. Design
around it rather than wishing it were different.

## Pin the contract before writing any code

Capture and write down, in the card or a doc the client can see:

- **Transport and format** — REST/SOAP/SFTP/queue; JSON/XML/fixed-width/CSV; encoding.
- **Authentication** — mechanism, credential rotation, whose credential it is.
- **Rate limits and quotas** — per second, per day, and the penalty for breaching.
- **Error semantics** — which failures are retryable. A 500 that already committed is not.
- **Idempotency** — is there a key? If not, you must build dedupe on our side.
- **Ordering** — guaranteed or not. Assume not unless documented.
- **Volumes** — steady state and peak. Peak is what breaks integrations.
- **The support path** — who to call, and their actual response time.

An undocumented contract is an assumption. Record it as one, flag it load-bearing, and get
it confirmed before building on it.

## Assume every call will be retried

Networks duplicate. Timeouts lie — a request that timed out may have succeeded. Design so a
duplicate is harmless:

- **Natural idempotency key** from the business data (order id + line no), not a UUID
  generated per attempt.
- **Dedupe table** keyed on that, checked before the side effect.
- **Exponential backoff with jitter**, and a **circuit breaker** so a dead upstream does not
  become our outage.
- **Dead-letter queue** for anything that fails terminally, with enough context to replay.

Never make a non-idempotent call inside a retry loop without a dedupe guard. That is how
duplicate payments and double shipments happen.

## Migration is a reconciliation problem, not a copy

A migration nobody can prove is a migration nobody will trust.

```bash
# 1. Count parity, per table and per meaningful partition
#    Totals matching while a partition is empty is the classic silent failure.
# 2. Checksum parity on business-critical columns
# 3. Spot-check the extremes: oldest, newest, largest, null-heavy, unicode, negative
# 4. Re-run on a copy and confirm identical output — a migration must be repeatable
```

Record, per run: **rows read, rows written, rows rejected and why, rows requiring manual
decision.** The rejected set is the interesting one; a migration that reports zero rejects
usually is not looking.

**Never migrate live data without written authorisation.** Cutover, deletion, and
irreversible writes are the client's decision, not an inference from "the plan said
migrate". Block and escalate to `engagement-lead` for the named owner.

## Reconciliation runs forever, not once

Two systems diverge the moment both accept writes. Build the reconciliation job before
cutover, not after the first dispute:

- What is compared, on what key, at what cadence.
- The tolerance — some drift is legitimate (timing, rounding); define it numerically.
- What happens on a break: alert whom, and what the manual correction path is.

## Data you may not move

We are a third party inside someone else's business. Before any extract:

- Is this data authorised to leave its system? To leave its jurisdiction?
- Does it enter **our** tooling, logs, or a scratch workspace? That is data egress and needs
  authorisation like any other.
- Is it minimised — are we pulling fields nobody needs?

Anything touching personal data or payments: raise a `security-compliance` card, link it,
and wait. Do not self-certify.

## Test against the real thing, carefully

A mock that matches the documentation proves only that you read the documentation. Real
systems return undocumented nulls, truncate silently, and reject on the 1001st record.

- Use the vendor's sandbox where it exists. Confirm whether it shares production limits.
- Where there is none, test against production **read-only**, with explicit permission,
  outside peak.
- Keep a captured real response as a fixture — it will differ from the spec and that
  difference is the valuable part.

## Pitfalls

1. **Trusting the documentation over the wire.** Capture real traffic early.
2. **Retrying non-idempotent writes.** Duplicates in someone else's ledger are painful to
   unwind and visible to their customers.
3. **Migrating with totals-only verification.** Check partitions and edges.
4. **Treating the upstream's outage as ours.** Circuit-break and degrade with a clear
   message; do not queue unboundedly.
5. **Silent truncation** on fixed-width or column-length mismatches — assert lengths.
6. **Cutover without a rollback.** If you cannot get back, you have not planned a cutover.
