# dev-team: an autonomous software delivery team on Hermes kanban, governed the Archon way

Status: design, awaiting approval
Branch: `autonomous-dev-team`

## Intent

The repository already ships two teams of Hermes profiles: `sdlc-team` and `outsourcing-team`.
Both coordinate through Hermes' built-in kanban. In both, every rule ("nothing is done without QA
acceptance", "stop after three failures", "never merge without authorization") is enforced only by
an agent's judgment, written into a SOUL or a skill.

`dev-team` keeps Hermes kanban exactly as it is and adds what Archon has that the kanban doesn't
have. The engine enforces the governance, and the agents judge the work. A software change moves
through fixed stages. Each stage hands the next a small typed result instead of prose. A stage
cannot be marked done until its result is present and valid, and for CI, until a fresh read from
the forge shows the result is true.

Why now: the operator runs Hermes with these profiles today, wants an unattended development team,
and has seen Archon's model work.

## Goals

- One sentence, a budget and a repository in. A pull request with green CI, marked ready for human
  merge, out. No human in between unless a stop condition fires.
- Stage completion is checked by code, not by the worker's say-so.
- Works with any forge and CI: GitHub, GitLab, Gitea, Jenkins, and room for others, without the
  team's prompts or the guard naming a vendor.
- Unknown failures pause as visible attention items. They are never improvised around.
- Installs like the other teams: a Unix and a Windows script, idempotent, `--dry-run`, Mnemosyne
  wired and verified per profile.

## Non-goals

- Replacing or forking Hermes kanban, its dispatcher, or its worktree workspaces.
- Merging, deploying or releasing. A human always does these. The operator chose this so the team
  has no authorization path to get wrong.
- A general workflow engine or YAML language. The pipeline is one fixed shape.
- Non-engineering roles. Research and copywriting are out of scope.

## Anti-goals (a failure even if it technically works)

- A card reaches `done` because the agent said so while the check it needed never ran.
- A guard that blocks honest work so often that operators turn it off.
- The plugin becomes an application with its own store, lifecycle or scheduler (Archon pack
  constitution, section 9).
- Vendor names leaking into souls, stage skills or the guard's logic.
- Silent fallback. If the guard cannot verify something, the card does not complete.

## Constraints

- Hermes plugin contract: `pre_tool_call` hooks may block a tool call. A timed-out or raising
  `pre_tool_call` callback fails closed. Plugins load only when listed in each profile's
  `plugins.enabled`.
- Kanban facts the design relies on:
  - Workers finish with `kanban_complete(task_id, summary, metadata: object, ...)` or
    `kanban_block(task_id, reason, kind)`.
  - The current card id is in `HERMES_KANBAN_TASK`.
  - `hermes_cli.kanban_db.get_task()` returns a `Task` whose `skills` list holds the skills the card
    was created with (`--skill`).
  - Worktree workspaces with `--branch` exist.
  - The `review` / `request-changes` flow exists.
- Kanban's own `--completion-contract` is GitHub-only. It may be layered on for GitHub projects but
  cannot be the general mechanism.
- Profiles redirect `HERMES_HOME`. Every plugin and the Mnemosyne store must be linked into each
  profile, as the existing installers already do for Mnemosyne.
- Installer conventions from the existing teams hold:
  - bash 3.2 compatible (stock macOS)
  - PowerShell 5.1 parity
  - idempotent re-runs
  - `--only`, `--model`, `--skip-model`, `--keep-soul`, `--dry-run`
  - refuse when `memory.provider` is not `mnemosyne`
- Nothing is written into the target repository except the work itself (branches, commits, PRs).

## Approaches

1. Skills and souls only, as the outsourcing team does. Cheapest, but "done only after checks"
   stays the agent's word, which is the problem this team exists to fix. Rejected.
2. Kanban plus a small Hermes plugin that guards completion, with a vendor-neutral forge command.
   Enforcement lives at the one boundary every worker must cross: `kanban_complete`. **Chosen by
   the operator.**
3. A separate orchestrator driving the board through the kanban CLI as a state machine. Closest to
   Archon's engine, but it duplicates the dispatcher and grows into its own application. Rejected.

## Decided

### Repository layout

- `old/sdlc-team/` and `old/outsourcing-team/`, moved with `git mv`. Each old README gains one line
  at the top: superseded by `dev-team/`, kept frozen.
- Paths that break with the move are fixed in the same change:
  - installer hints `../install-...` become `../../install-...`
  - README links `../docs/...` become `../../docs/...`
  - root README, CHANGELOG and docs references are updated
- New top-level `dev-team/`:
  - `README.md`
  - `install-dev-team-unix.sh` and `install-dev-team-windows.ps1`
  - `souls/`: copies of the seven engineering souls from `sdlc-team`, plus a new `SOUL-reviewer.md`
  - `skills/`: one skill per stage, plus `running-the-pipeline` for the entry point
  - `plugin/dev-team-gate/`: the Hermes plugin, with its tests
  - `forge/`: the vendor-neutral `forge` command and its adapters
  - `scripts/kanban-budget.py`: copied from the outsourcing team. `dev-team` owns this copy.

### Profiles

There are eight. Profile names are prefixed with `dev-` by default so they can never collide with
the old teams (`dev-pm`, `dev-qa`, ...). The installer has `--prefix` to change the prefix, as the
outsourcing installer does.

| Profile | Soul | Stages it works |
| --- | --- | --- |
| pm (entry point) | sdlc pm, unchanged | intake, plan; owns budget, scope cuts and reporting |
| architect | sdlc architect | plan (one-way-door decisions only, as a child card) |
| uiux | sdlc uiux | plan (when the change has an interface) |
| backend-db | sdlc backend-db | implement |
| frontend | sdlc frontend | implement |
| qa | sdlc qa | validate |
| reviewer | **new** | review |
| devops | sdlc devops | forge onboarding, pr-ci |

- The pm soul's "I own the sequence" is kept. It sequences *which work* goes first. The pipeline
  fixes *which stages* each piece of work passes through.
- The new reviewer soul reads the diff against the plan and acceptance criteria and judges
  correctness, scope creep, risk and maintainability. It cannot approve its own work, write the fix,
  or run the tests on the implementer's behalf. Its refusals are stop conditions, as for the other
  specialists.

### Stages

A stage is identified by the stage skill the card is created with:
`--skill dev-team-stage-<stage>`. The guard reads the stage from the card's `skills`, never from
its title or body. A card with no stage skill is not guarded.

| Stage | Assignee | Required `metadata` on `kanban_complete` |
| --- | --- | --- |
| `intake` | pm | `definition_of_done` (list), `budget` ({amount, unit}), `repo` |
| `plan` | pm | `cards` (ids of the implement cards it created, each verified to exist with a stage skill) |
| `implement` | backend-db / frontend | `branch`, `head_sha`, `changed_files` (non-empty), `tests_run` (list of {command, exit_code}, at least one, all exit_code 0) |
| `validate` | qa | `head_sha` (must equal implement's), `verdict` ("pass" or "fail"), `evidence` (list); on "fail" also `findings` |
| `review` | reviewer | `head_sha` (must equal validate's), `verdict` ("approve" or "changes"); on "changes" also `findings` |
| `pr-ci` | devops | `pr_url`, `head_sha` (must equal review's), `ci` ("green"), re-verified by the guard through `forge ci-status` |

- **The guard's rule.** Required fields must be present and well-typed. Each `head_sha` must equal
  the parent stage's `head_sha`, read from the parent card's last completed run. For `pr-ci`, the
  guard itself runs `forge ci-status --sha <head_sha>` and refuses unless it returns `green`.
  Failure to verify (forge unreachable, adapter missing) refuses too, with the reason. It fails
  closed.
- **Refusal message.** Returned to the worker as the tool error, naming the missing or wrong field.
  The worker fixes and retries, or blocks.
- **Card topology (call made after the builder check).**
  - One unit of work is one branch and one PR.
  - Every stage is its own card. Each stage creates the next stage's card as its child
    (`kanban_create` with `parents=[self]` and `--skill dev-team-stage-<next>`) before completing.
  - On `kanban_complete` the guard verifies that the right child exists, with a stage skill, through
    `created_cards`.
  - When a unit needs both backend-db and frontend, plan creates the implement cards in sequence on
    the same branch, linked parent to child. "The implement `head_sha`" is the last one's.
  - Hermes' own `request-changes` lane is not used. It only works on a single card, so QA on its
    own card could not send work back (`kanban_db.py:3428-3443`).
- **Rework loop.**
  - QA or the reviewer completes with `verdict: "fail"` or `"changes"` plus `findings`. Their
    required child is then a new `implement` card carrying the findings, instead of the next stage.
  - devops does the same on CI red caused by the change.
  - The rework chain re-runs validate and review on the new `head_sha`.
  - Round limit 3, enforced by the guard: it counts `implement` cards in the card's ancestry. A
    fourth is refused, and the worker must `kanban_block(kind="needs_input")` with
    `review: rounds-exhausted`.
- **CI red.** devops judges once, as Archon's single CI agent does:
  - Red caused by the change: back to implement through a new linked card.
  - Red from a flake or infrastructure on paths the change doesn't touch: one re-run, then judge
    again.
  - Still red, or anything else: block.
- **Attention items.** Every block's `reason` starts with a failure-class label, such as
  `ci: other`, `review: rounds-exhausted`, `forge: unreachable` or `merge: human-only`. pm's
  ledger counts blocks per label, so recurring classes are visible.

### Irreversible actions

- Merge and deploy are always human. `forge` has no merge or deploy subcommand.
- The guard blocks `terminal` tool calls that run a known merge or deploy command:
  - `gh pr merge`, `glab mr merge`, `tea pr merge`
  - `git push` to the default branch
  - a short list of deploy CLIs

  This is a guard in front of an irreversible action. It is best effort, so the README also tells
  the operator to give the team a forge token without merge rights. Token scope is the real
  boundary.
- **The terminal is not a side door.** On a guarded card the same `terminal` guard blocks
  `hermes kanban complete`, `request-changes`, `edit` and `unblock`. A worker can otherwise finish
  its own card through the CLI and never touch the `kanban_complete` hook (`kanban.py:814-823`).

### Workspaces (call made after the builder check)

- Hermes creates worktrees at `<repo>/.worktrees/<task-id>` (`kanban_db_workspace.py:707-743`).
  So the team never works in the operator's own clone.
- devops onboarding makes a team-owned clone at `~/.hermes/dev-team/repos/<repo-slug>/`, adds
  `.worktrees/` to that clone's `.git/info/exclude` (local, never committed), and sets it as the
  board's `default_workdir`.
- One board per repository.
- Cards on the same branch are sequential by construction: each stage's card is the previous
  card's child. Each reuses the existing worktree path for that branch
  (`worktree:<path>`) instead of a second `git worktree add`.

### Budget (call made after the builder check)

- The budget is pm's judgment, not the guard's, because the only spend measurement
  (`kanban-budget.py`) is an inference and a guard must not block on an inference.
- pm runs `kanban-budget.py` at the 25 / 50 / 75 % checkpoints the pm soul already defines. Exit 1
  means it is in the closeout reserve and applies the soul's degradation ladder. Exit 2 means
  exhausted, and it blocks every open card with `budget: exhausted`.
- The unit is USD, the only unit the script measures.

### The forge command

- A single stdlib-only Python CLI, `forge`, with one subcommand set for every vendor. Every
  subcommand prints JSON and uses exit codes: 0 ok, 1 negative result, 2 cannot verify.
  - `detect`
  - `pr-open --branch --title --body-file`
  - `pr-status --pr`
  - `ci-status --sha`
  - `mark-ready --pr`
- Adapters: `github` (gh CLI), `gitlab` (glab CLI), `gitea` (REST API, since `tea` lacks a stable
  JSON CI status), `jenkins` (CI only; pairs with any of the others for PRs).
- `ci-status` maps each vendor's states to `green` | `red` | `pending` | `none`.
- **Per-project record.** devops runs `forge detect` at onboarding. If the result is ambiguous it
  decides, then writes `~/.hermes/dev-team/projects/<repo-slug>.json` with the PR adapter, the CI
  adapter, the default branch and how CI is found. It lives in Hermes' home, never in the target
  repository, and every stage reads it.
- Credentials come from each vendor's own CLI login or from the operator's `secret` store via
  environment variables. They are never written to the record or logged.

### Installer

Mirrors the outsourcing installer:

- profiles with prefix, souls, model per role
- Mnemosyne plugin and data links
- kanban toolset on every profile
- `running-the-pipeline` skill and `kanban-budget.py` on pm

It adds:

- linking `dev-team-gate` into each profile's `plugins/` and adding it to `plugins.enabled`
- installing the stage skills
- putting `forge` on a PATH the gateway sees: `~/.local/bin`, the lesson from the graft plugin work

It verifies Mnemosyne `available` and the gate plugin loaded (`hermes plugins doctor`) for every
profile, and exits non-zero otherwise.

## Left to the builder

- Exact JSON shapes beyond the required fields; extra fields are allowed.
- The deploy-CLI list in the terminal guard.
- How `running-the-pipeline` words intake. It may reuse the pm soul's intake section rather than
  restate it.
- Test layout. The plugin and `forge` must have unit tests with fake adapters, plus an
  `--only`/`--dry-run` check of both installers.
- Live verification: one end-to-end run on a scratch repository on one forge, chosen by the operator
  at plan time.
