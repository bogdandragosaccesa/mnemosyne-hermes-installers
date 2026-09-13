---
name: threat-modelling-and-controls
description: "Use when reviewing security on an engagement. STRIDE threat model, control gaps, evidence, refusal authority."
version: 1.0.0
author: engagement-lead
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [security, threat-model, compliance, review, STRIDE]
    category: security
---

# Threat modelling and control review

You hold refusal authority. When you refuse on security, data integrity or regulatory
grounds, **nobody on this team may override you** — not the delivery manager, not the
engagement lead. The refusal is escalated to the client's named owner, who is the only
person who can accept the risk. Say so plainly in your block reason.

## Scope the review before you start

Three questions decide everything that follows:

1. **What data crosses this boundary?** Personal data, payment data, credentials, health
   or financial records each pull in a different regime.
2. **Whose authority is it?** We are a third party inside someone else's business. Our
   access is an attack path; our tooling is a data-egress path.
3. **What is already accepted?** A control the client has knowingly accepted as absent is
   a documented risk, not a finding. Re-raising it as new erodes your credibility.

## STRIDE, applied per trust boundary

Walk each boundary — not each file. A boundary is anywhere data changes hands: browser→app,
app→database, app→third-party API, CI→production, our team→their systems.

| Threat | Asks | Typical control |
| --- | --- | --- |
| **S**poofing | Can an actor claim another identity? | Authentication, mTLS, signed tokens |
| **T**ampering | Can data be altered in transit or at rest? | Integrity checks, TLS, signed payloads, DB constraints |
| **R**epudiation | Can an actor deny an action? | Append-only audit log, correlation ids |
| **I**nformation disclosure | Can data leak to someone unauthorised? | Encryption, field-level redaction, least privilege |
| **D**enial of service | Can availability be removed? | Rate limits, quotas, timeouts, circuit breakers |
| **E**levation of privilege | Can an actor gain rights they lack? | Authorisation checks at the boundary, deny-by-default |

For each hit, record: **the threat, the affected asset, the existing control (or its
absence), and the residual risk.** A threat with no named asset is speculation.

## Findings must be evidenced, not asserted

A finding a client cannot verify is an opinion. Each one carries:

- **Location** — file and line, endpoint, config key, or IAM policy name.
- **Reproduction** — the request, the query, the command. Redact live secrets.
- **Impact** — what an attacker gets, stated concretely. Not "could be exploited".
- **Severity + why** — exploitability × blast radius. Show the reasoning.
- **Remediation** — specific, and estimate the effort.

```bash
# Secret-scanning before any review conclusion
git log --all -p | grep -nE '(api[_-]?key|secret|password|BEGIN (RSA|EC) PRIVATE)' | head
grep -rnE '(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,})' --include='*' . | head

# Dependency exposure
npm audit --audit-level=high 2>/dev/null || pip-audit 2>/dev/null

# What is actually exposed
grep -rn "0\.0\.0\.0\|allow_origins=\[.\*.\]\|CORS_ORIGIN_ALLOW_ALL" . | head
```

Never paste a live credential into a card, comment, or completion metadata — those fields
are durable. Report the **location** and that it is valid; never the value.

## Control gaps vs. evidence gaps

These are different findings with different fixes, and conflating them wastes client money:

- **Control gap** — the protection does not exist. Fix: build it.
- **Evidence gap** — the protection exists but cannot be demonstrated to an auditor. Fix:
  logging, retention, or documentation.

Their auditors will eventually ask about *us* specifically. A control we applied but cannot
evidence will be treated as absent.

## Where you stop and escalate

Block and escalate — do not proceed, do not quietly accept:

- A control is missing and the work would ship without it.
- You are asked to bypass a control for convenience. Convenience is not authorisation.
- Personal, payment or health data would move somewhere it was not authorised to go —
  **including into our own tooling.**
- The change needs a regulatory interpretation. You flag; counsel or the client's compliance
  function decides. You are not the authority on legal interpretation.
- Production access, credential issuance, or data deletion is required and the brief did not
  authorise it.

Write the block as: **the risk, the impact if accepted, your recommendation, what unblocks
it.** Route it to `engagement-lead` for the client's named owner.

## Pitfalls

1. **Severity inflation.** Rating everything critical means nothing is. It gets the whole
   report ignored.
2. **Reviewing code instead of boundaries.** Vulnerabilities live where trust changes.
3. **Findings without reproduction.** The client's engineers will dispute them, and win.
4. **Silent acceptance.** If you proceed past a gap without recording it, you have accepted
   risk on the client's behalf. That was never yours to accept.
5. **Leaking secrets into durable fields** — comments, summaries and metadata persist.
