---
name: writing-delivery-cards
description: "Use when decomposing work into kanban cards for specialists. Card anatomy, workspace rules, handoff chains."
version: 1.0.0
author: engagement-lead
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [kanban, decomposition, delegation, planning]
    category: delivery
    requires_toolsets: [kanban]
---

# Writing a card a specialist can actually execute

You decompose and sequence. You do not implement. Every technical task belongs to a
specialist, and the quality of your card decides whether their run succeeds or burns
budget failing.

## The single most expensive mistake

**A code card with a `scratch` workspace cannot succeed.** A scratch workspace is an empty
directory. An engineer assigned a feature there will read the body, find no repo, and
correctly block — because it cannot invent the codebase's framework, conventions, auth
middleware or test style.

This is not hypothetical. The same CSV-export task was run twice on the same model:

| Workspace | Outcome |
| --- | --- |
| `scratch` | blocked, `consecutive_failures=0`, "no codebase is attached to this task" |
| `dir:/tmp/os-demo` | shipped, self-reviewed against 4 criteria, 9 passing Playwright tests |

Model held constant. The workspace was the entire difference. Escalating the model would
have bought nothing and cost several times more.

| Workspace kind | Use for |
| --- | --- |
| `dir:/abs/path` | Work in an existing checkout. The default for code |
| `worktree:` | Isolated git worktree — when several cards touch one repo concurrently |
| `scratch` | Research and writing ONLY. Never for code |

## Card anatomy — all six, every time

```bash
hermes kanban create "Add CSV export to the orders screen" \
  --assignee app-engineer \
  --workspace dir:/path/to/repo \
  --body "$(cat <<'EOF'
The client wants CSV export on the orders screen, honouring the status filter.

Acceptance criteria:
1. GET /orders.csv returns Content-Type text/csv and header row: id,customer,status,total
2. GET /orders.csv?status=dispatched returns only dispatched orders
3. The orders page has a visible Export CSV link carrying the current filter
4. Existing /orders HTML behaviour is unchanged

Codebase: this workspace is the repo. Follow the file's existing style.

BUDGET: this card is worth about $1.50.

When implementation is done, do NOT mark this done yourself. Raise a card assigned to
qa-lead for Playwright e2e tests covering the criteria above, link it as a child with
kanban_link, then call kanban_request_review.
EOF
)"
```

1. **Outcome** — what the client actually wants, in one or two sentences.
2. **Acceptance criteria** — numbered, testable by a stranger. A criterion nobody can check
   is a criterion that gets argued about at UAT.
3. **Workspace** — see above. Non-negotiable for code.
4. **Budget figure** — prose, so the worker can size its own effort.
5. **What NOT to touch** — systems, data, environments outside this card.
6. **What to do on completion** — the handoff chain, spelled out.

## Spell out the handoff or it will not happen

Specialists hold `kanban_create`, `kanban_link`, `kanban_comment`, `kanban_request_review`
and `kanban_block`. They will raise work for a sibling — but only if told to. A worker told
merely "write tests too" will either do it badly itself or skip it.

Proven chains worth writing into the body verbatim:

- `app-engineer` finishes a feature → raises a `qa-lead` card for Playwright e2e tests,
  links it as a child, requests review. **This works end to end.**
- `business-analyst` hits a regulatory question → raises a `domain-consultant` card.
- `solution-architect` hits a one-way door → escalates to `engagement-lead` for the client owner.
- `app-engineer` needs a pipeline or environment → raises a `platform-sre` card.
- Anything touching auth, personal data or payments → a `security-compliance` review card.

Use `--parent` or `kanban_link` so the dependency is real: a parent cannot go to `done`
while children are open. That is what stops a feature being called finished before its
tests exist.

## Decisions belong to you, not to the workers

Workers cannot see sibling context. If two cards would each independently pick a naming
scheme, a schema, a file format or an API shape, **you decide it and write the decision
into both card bodies.** Two workers each answering the same open question is how a
decomposition produces two incompatible halves.

## Model tier per card

Leave `--model` off to inherit the profile default — correct for most implementation work.
If you set it, it must be a **real model id**. `--model mid` fails every run with
`HTTP 404: model: mid`, and because the worker dies before it can call `kanban_complete` or
`kanban_block`, the dispatcher records a protocol violation and retries until the card is
blocked. Nothing in the log says "bad model name" until you read the worker output.

**`claude-fable-*` models are excluded outright** — enormous cost, no exception.

Never upgrade a tier to compensate for a vague card. Rewrite the card.

## Before a card leaves your hands

- [ ] Does it name the workspace, and is it `dir:`/`worktree:` if any code is involved?
- [ ] Can a stranger check every acceptance criterion?
- [ ] Does it say what to do on completion, naming the sibling profile?
- [ ] Is every shared decision written into the body rather than left open?
- [ ] Is the assignee a **real installed profile**? The dispatcher silently drops a card
      with an unknown assignee — it sits in `ready` forever. Verify with
      `hermes kanban assignees`.

## Pitfalls

1. **Unknown assignee = silent death.** No error, no event, the card just never runs.
2. **Acceptance criteria written as adjectives** ("fast", "clean", "user-friendly") come
   back as questions or confidently wrong work.
3. **Parent marked done before children exist** — link first, then let the gate work.
4. **Assigning follow-up work to yourself.** Raise it for the right specialist.
5. **Cards that name no budget** invite unbounded effort on a trivial task.
