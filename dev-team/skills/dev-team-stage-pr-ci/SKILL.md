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

Resolve the canonical team-home path first — `$HERMES_HOME` is per profile, but every profile's
`dev-team` directory is a symlink into the same shared tree, so `pwd -P` from inside it gives
the one real path every profile agrees on:

```
TEAM_HOME="$(cd "$HERMES_HOME/dev-team" && pwd -P)"
```

Check whether a project record already exists at `$TEAM_HOME/projects/<repo-slug>.json`
(`<repo-slug>` is the `repo` string the intake card recorded — see `dev-team-stage-intake`). If
it does not:

1. Clone the repository — never the operator's own clone — to
   `$TEAM_HOME/repos/<repo-slug>/`.
2. Add `.worktrees/` to that clone's `.git/info/exclude` (local only — never committed, never
   written into the target repository itself).
3. Run `forge detect --repo-dir $TEAM_HOME/repos/<repo-slug>/ --slug <repo-slug>`
   (`--repo-dir` is required; always pass `--slug` with intake's `repo` string explicitly —
   without it, `forge detect` defaults the slug to the clone directory's name, not intake's
   `repo`). If it exits `2` with `"ambiguous: pass --pr-adapter"`, decide the PR and CI
   adapters yourself and re-run with `--pr-adapter <name>` and/or `--ci-adapter <name>`.
4. `forge detect` writes the whole project record for you — do not hand-write or edit one. It
   reads `repo_url` from the clone's `git remote get-url origin` (with any credentials in the
   URL stripped) and `default_branch` from `git symbolic-ref refs/remotes/origin/HEAD`. If it
   exits `2` saying it cannot read the default branch, run
   `git -C $TEAM_HOME/repos/<repo-slug>/ remote set-head origin --auto` and re-run
   it. It refuses to overwrite an existing record; if the record is genuinely wrong (the
   repository moved, or its default branch changed), block with `forge: record-stale` and let
   the operator decide whether to re-run with `--force`. A slug containing `/`, `\`, or equal
   to `.` or `..` is refused. Credentials never go in the record.
5. Check the board's `default_workdir` (`hermes kanban boards list --json`, find the entry
   whose `slug` is `<repo-slug>`, read its `default_workdir` field) against `$TEAM_HOME/repos/<repo-slug>/`.
   The pm sets `default_workdir` at board-creation time (`running-the-pipeline`'s
   `--default-workdir`, already written in this same canonical form) — this step never sets or
   changes it. Compare **resolved real paths, not raw strings**: `realpath` both sides
   (`realpath "<default_workdir>"` vs. `realpath "$TEAM_HOME/repos/<repo-slug>"`) and ignore a
   trailing slash — the stored value and your own clone path are produced by two different
   profiles' shells and are not guaranteed to be byte-identical even when they name the same
   directory. If the resolved paths differ, or `default_workdir` is missing, block with a clear
   reason (e.g. `onboarding: default_workdir missing/mismatched — expected
   $TEAM_HOME/repos/<repo-slug>/, got <default_workdir>`) and let the operator fix the board;
   never mutate it yourself, and never fall back to the operator's own checkout.

This record is what the guard itself reads later (`_record_default_branch`) to know which
branch `git push` must never target on this project, and what `forge ci-status` resolves
against. Get `repo-slug` right the first time — intake already fixed it; don't re-derive it.

## The forge token

`forge` and `git push` both need a credential for the forge. The operator supplies it as
**`DEV_TEAM_GH_TOKEN`** in your environment — set up by the **Unix** installer's
`--forge-token-source` flag, which records only a *command* that prints the token (never the
value) and passes the variable through to this profile. On Windows, `-ForgeTokenSource` is
**refused**, because Hermes' command secret source is POSIX-only; on that platform the operator
must configure the `dev-devops` profile by hand. You never see, choose, or store the token.

**Before you push anything, check it is there:**

```
test -n "$DEV_TEAM_GH_TOKEN" || echo MISSING
```

If it is unset or empty, `kanban_block(kind="needs_input")` with a reason starting
**`setup: no-forge-token`**. There is **no fallback**: never run `gh auth login` (in any form,
interactive or `--with-token`), never reach for a stored credential, a keyring, an SSH key, or
`GITHUB_TOKEN`, and never ask the operator to paste the token into the chat or into a file.
A missing credential is an operator setup task, not something to work around.

When it is present, hand it to `git` through `gh`'s credential helper, once, at the start of
your run:

```
export GH_TOKEN="$DEV_TEAM_GH_TOKEN"
gh auth setup-git
```

That is the whole mechanism. Why this one and not a `GIT_ASKPASS` helper:

- `gh auth setup-git` only writes `credential.*` git-config keys. The guard refuses `git config`
  writes to keys that could redirect a push (`alias.`, `remote.`, `push.`, `branch.`,
  `include.`, `includeif.`, `url.`); `credential.` is not among them, so the command runs. A
  `GIT_ASKPASS` approach needs a helper **script file on disk** — a file that can be committed,
  read, or left behind.
- `gh` reads `GH_TOKEN` from the environment, so the value never appears in a command line (the
  guard lexes argv) and never lands on disk.
- `GH_TOKEN` is not a `HERMES_*` variable, so exporting it does not touch a Hermes fence.
- `forge`'s GitHub adapter reads `DEV_TEAM_GH_TOKEN` itself, for the `gh` subprocess only, so
  `forge pr-open`, `forge ci-status` and `forge mark-ready` need nothing further from you.

**Invariants — these are not style preferences:**

- `export GH_TOKEN="$DEV_TEAM_GH_TOKEN"` passes the token **by reference**. Never type the
  literal value, never `echo` it, never interpolate it into a URL
  (`https://$TOKEN@github.com/...` is forbidden — it lands in `.git/config`, in the reflog and
  in error messages), never write it to a file, and never put it in a card, comment, commit
  message or completion metadata.
- Never `gh auth login --with-token` from an `echo` or a pipe: that puts the value in a command
  line and in the shell's history.
- If a command's output might contain it, do not paste that output anywhere. `forge` redacts
  known token shapes from its own errors, but your shell does not.

## Shipping the branch

Once your parent `review` card has approved `head_sha`:

1. Set up credentials as above (`test -n "$DEV_TEAM_GH_TOKEN"`, then
   `export GH_TOKEN="$DEV_TEAM_GH_TOKEN"` and `gh auth setup-git`), then push the branch:
   `git push origin <branch>`. (The guard only blocks pushes to the project's protected default
   branch — this is not that. It also refuses a push from a card with no dev-team stage skill;
   yours carries `dev-team-stage-pr-ci`, so that is not you either.)
2. Open the PR: `forge pr-open --slug <repo-slug> --branch <branch> --title <title>
   --body-file <path>`.
3. Poll `forge ci-status --slug <repo-slug> --sha <head_sha>` while it reports `pending`, up to
   a bounded number of attempts with backoff — `pending` right after opening a PR is normal,
   not an attention item by itself.
4. On `green` (exit `0`): `forge mark-ready --slug <repo-slug> --pr <pr-number>`, then complete
   as below.
5. On `red` (exit `1`): run `deciders extract` (see `dev-team-deciders`) over the CI log to find
   failing tests and file:line before you look at anything else — it is a pointer into the log,
   not a verdict. Then judge "caused by this change vs. a flake" yourself, using those spans as
   your starting point, and act per Outcomes below.
6. On `none`, or `pending` that never resolves within your poll bound: block per Outcomes.

## Required `metadata` on `kanban_complete`

| Field | Type | Check |
| --- | --- | --- |
| `pr_url` | string | an `http(s)://` URL |
| `head_sha` | string | a git object name, 7–64 hex characters; must equal, case-insensitively, the `head_sha` the parent `review` card recorded in its last completed run |
| `ci` | string | must be exactly `"green"` or `"red"` |
| `findings` | list, **required only when `ci` is `"red"`** | non-empty |

**`ci: "green"` is re-verified, not taken on your word.** Before letting the completion
through, the guard reads the forge itself, in this order: `pr_url` must be a pull request of
this project (the project's own forge reports that exact URL for its number — refusal
`pr: not-in-project`); the PR's head must be your `head_sha` (`pr: head-mismatch`: if anything
was pushed after review, block rather than complete); CI on `head_sha` must be green (`ci:`);
and the PR must be marked ready (`pr: not-ready`: run `forge mark-ready` first). If the forge
disagrees, or cannot be reached at all (`forge: unreachable`), your completion is refused —
claiming green when it isn't does not get through by retrying the same claim. The guard runs
the forge code shipped next to the plugin, never a `forge` found on PATH, and refuses terminal
writes into that code, the `forge` wrapper, the plugin and the project records.

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
