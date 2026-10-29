---
name: dev-team-stage-pr-ci
description: Run the pr-ci stage of a dev-team pipeline card (devops): onboard the forge project record, open the PR, judge CI red vs green.
---

# dev-team stage: pr-ci

You hold the `pr-ci` card: it carries the skill `dev-team-stage-pr-ci`, which is what the
`dev-team-gate` guard reads. Your parent is the `review` card whose `head_sha` you are shipping
— exactly one parent must carry `dev-team-stage-review`, or the guard refuses before it looks at
your metadata. This is the one stage skill allowed to name a vendor-neutral tool by its actual
name: `forge`. Every other soul and skill in `dev-team` stays vendor-neutral.

## Forge onboarding (first pr-ci card for a repo)

Before opening a PR, check whether a project record already exists at
`$HERMES_HOME/dev-team/projects/<repo-slug>.json` (`<repo-slug>` is the `repo` string the
intake card recorded — see `dev-team-stage-intake`). If it does not:

1. Run `forge detect` in the repository to identify the PR adapter, the CI adapter and the
   default branch. If the result is ambiguous, decide and record how you decided.
2. Make the team's own clone of the repository, never the operator's clone, at
   `$HERMES_HOME/dev-team/repos/<repo-slug>/`.
3. Add `.worktrees/` to that clone's `.git/info/exclude` (local only — never committed, never
   written into the target repository itself).
4. Write the project record (`$HERMES_HOME/dev-team/projects/<repo-slug>.json`) with the PR
   adapter, the CI adapter, the default branch and how CI is found. Credentials never go in
   this file or in any log — they come from the vendor CLI's own login or the operator's
   secret store.
5. Set the board's `default_workdir` to that team-owned clone, so every later card on this
   board works there instead of in the operator's checkout.

This record is what the guard itself reads later (`_record_default_branch`) to know which
branch `git push` must never target on this project, and what `forge ci-status` resolves
against. Get `repo-slug` right the first time — intake already fixed it; don't re-derive it.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `pr_url` | string | an `http(s)://` URL |
| `head_sha` | string | a git object name, 7–64 hex characters; must equal, case-insensitively, the `head_sha` the parent `review` card recorded in its last completed run |
| `ci` | string | must be exactly `"green"` or `"red"` |
| `findings` | list, **required only when `ci` is `"red"`** | non-empty |

**`ci: "green"` is re-verified, not taken on your word.** The guard itself runs
`forge ci-status --slug <repo-slug> --sha <head_sha>` before letting the completion through. If
the forge disagrees, or cannot be reached at all, your completion is refused — claiming green
when it isn't does not get through by retrying the same claim.

## Outcomes (binding — anything else blocks)

- **CI green, and the guard's own `forge ci-status` re-read confirms it.** Complete with
  `ci: "green"`. **No child card.** This stage is terminal: merge is always human.
- **CI red, caused by this change.** Complete with `ci: "red"` and non-empty `findings`, and
  create exactly one new `implement` child carrying those findings:
  ```
  kanban_create(assignee="backend-db", parents=[<this card's id>],
                skills=["dev-team-stage-implement"], title="...")
  ```
  The guard requires exactly one card in `created_cards` with `dev-team-stage-implement` and
  this card as a parent. This is a rework round and counts against the round limit — a refusal
  of `review: rounds-exhausted` means block, not retry.
- **A flake or infrastructure failure on paths this change doesn't touch.** Re-run CI once,
  then judge again as above. Still red after the re-run, or anything you can't resolve into
  green/red-caused-by-the-change: **block**, you do not get a third chance to re-run.
- **Anything else** — CI state the forge can't resolve into green or red, the forge adapter is
  unreachable, or you cannot verify which the red belongs to — `kanban_block(kind="needs_input")`
  with a reason labelled `ci: other` or `forge: unreachable`. Never guess, and never complete
  with `ci: "green"` to get past an ambiguous state.

## Irreversible actions

`forge` has no merge or deploy subcommand, by design — you cannot accidentally reach for one.
The guard additionally blocks `git push` to the project's default branch and known merge/deploy
CLIs (`gh pr merge`, `glab mr merge`, `tea pr merge`, `kubectl apply/rollout`, `helm upgrade/
install`, `terraform apply`, `docker push`, and more) on every `terminal` call on this card, and
on the side-door kanban verbs (`complete`, `request-changes`, `edit`, `unblock`,
`reopen-review`) run through `hermes kanban` directly instead of the tool. Stop at a ready PR;
the operator merges.

## Ending

- `kanban_complete` with the metadata above, or
- `kanban_block(kind="needs_input")` with one of the labels above.

No other kanban verb finishes this card.
