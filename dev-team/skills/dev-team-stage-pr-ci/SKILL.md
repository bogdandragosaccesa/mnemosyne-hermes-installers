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

## Forge onboarding (fallback — devops onboarding should already have run)

`running-the-pipeline` sequences a devops onboarding card before the first `implement` card, so
by the time a `pr-ci` card runs, the project record below should already exist. Treat the steps
here as the fallback for when it doesn't:

Check whether a project record already exists at
`$HERMES_HOME/dev-team/projects/<repo-slug>.json` (`<repo-slug>` is the `repo` string the
intake card recorded — see `dev-team-stage-intake`). If it does not:

1. Clone the repository — never the operator's own clone — to
   `$HERMES_HOME/dev-team/repos/<repo-slug>/`.
2. Add `.worktrees/` to that clone's `.git/info/exclude` (local only — never committed, never
   written into the target repository itself).
3. Run `forge detect --repo-dir $HERMES_HOME/dev-team/repos/<repo-slug>/ --slug <repo-slug>`
   (`--repo-dir` is required; always pass `--slug` with intake's `repo` string explicitly —
   without it, `forge detect` defaults the slug to the clone directory's name, not intake's
   `repo`). If it exits `2` with `"ambiguous: pass --pr-adapter"`, decide the PR and CI
   adapters yourself and re-run with `--pr-adapter <name>` and/or `--ci-adapter <name>`.
4. `forge detect` already writes the project record for you (via `save_record`) — do not
   hand-write one; a hand-written record risks a schema the guard/forge can't read. It always
   writes `default_branch: "main"` regardless of this repository's actual default branch, so if
   that differs, amend only that one field afterward — never rewrite the whole record, and
   credentials never go in it.
5. Set the board's `default_workdir` to that team-owned clone
   (`hermes kanban boards set-default-workdir`), so every later card on this board works there
   instead of in the operator's checkout.

This record is what the guard itself reads later (`_record_default_branch`) to know which
branch `git push` must never target on this project, and what `forge ci-status` resolves
against. Get `repo-slug` right the first time — intake already fixed it; don't re-derive it.

## Shipping the branch

Once your parent `review` card has approved `head_sha`:

1. Push the branch: `git push origin <branch>`. (The guard only blocks pushes to the project's
   protected default branch — this is not that.)
2. Open the PR: `forge pr-open --slug <repo-slug> --branch <branch> --title <title>
   --body-file <path>`.
3. Poll `forge ci-status --slug <repo-slug> --sha <head_sha>` while it reports `pending`, up to
   a bounded number of attempts with backoff — `pending` right after opening a PR is normal,
   not an attention item by itself.
4. On `green` (exit `0`): `forge mark-ready --slug <repo-slug> --pr <pr-number>`, then complete
   as below.
5. On `red` (exit `1`): judge per Outcomes below.
6. On `none`, or `pending` that never resolves within your poll bound: block per Outcomes.

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

## Assignee names carry the team prefix

Profiles are installed with a team prefix (`dev-` by default, set by the installer's
`--prefix`). The installer writes it to `$HERMES_HOME/dev-team-prefix`; read it with
`cat "$HERMES_HOME/dev-team-prefix"` (PowerShell: `Get-Content "$env:HERMES_HOME\dev-team-prefix"`)
and build every assignee as `<prefix><role>`, e.g. `dev-qa`. In the examples below `<prefix>`
stands for that value. Never assign a bare role name such as `qa`: the dispatcher would spawn
a different, unguarded profile of that name. If the file is missing or empty,
`kanban_block(kind="needs_input")` with a reason starting `setup: no-prefix` instead of guessing.

## Outcomes (binding — anything else blocks)

- **CI green, and the guard's own `forge ci-status` re-read confirms it.** Complete with
  `ci: "green"`. **No child card.** This stage is terminal: merge is always human.
- **CI red, caused by this change.** Complete with `ci: "red"` and non-empty `findings`, and
  create exactly one new `implement` child carrying those findings:
  ```
  kanban_create(assignee="<prefix>backend-db", parents=[<this card's id>],
                skills=["dev-team-stage-implement"], title="...")
  ```
  The guard requires exactly one card in `created_cards` with `dev-team-stage-implement` and
  this card as a parent. This is a rework round and counts against the round limit — a refusal
  of `review: rounds-exhausted` means block, not retry.
- **A flake or infrastructure failure on paths this change doesn't touch.** Re-run CI once,
  then judge again as above. Still red after the re-run, or anything you can't resolve into
  green/red-caused-by-the-change: **block**, you do not get a third chance to re-run.
- **Anything else** — `none`, a poll timeout waiting on `pending`, a CI state the forge can't
  resolve into green or red, the forge adapter is unreachable, or you cannot verify which the
  red belongs to — `kanban_block(kind="needs_input")` with a reason labelled `ci: other`
  (unresolvable state or timeout) or `forge: unreachable` (the forge itself can't be reached).
  Never guess, and never complete with `ci: "green"` to get past an ambiguous state.

## Irreversible actions

`forge` has no merge or deploy subcommand, by design — you cannot accidentally reach for one.
The guard additionally blocks `git push` to the project's default branch, known merge/deploy
CLIs and API calls, git config or aliases that could redirect a push, and scripts it cannot see
into (a script piped into a shell, a `$var` as the command name, `env -S`), on every `terminal`
call on this card — see "What the guard covers" in the dev-team README for the full list. It
also blocks the side-door kanban verbs (`complete`, `request-changes`, `edit`, `unblock`,
`reopen-review`) run through `hermes kanban` against any dev-team card, whoever runs them. Stop
at a ready PR; the operator merges.

## Ending

- `kanban_complete` with the metadata above, or
- `kanban_block(kind="needs_input")` with one of the labels above.

No other kanban verb finishes this card.
