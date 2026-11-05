# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.3.3] - 2026-11-05

### Added

- **`forge` honours `DEV_TEAM_GH_TOKEN`.** The GitHub adapter's single subprocess choke point,
  `GithubAdapter._gh`, now passes an explicit environment to `gh`: when `DEV_TEAM_GH_TOKEN` is
  set and non-empty and `GH_TOKEN` is unset or empty, `GH_TOKEN` is set from it **for that
  subprocess only**. `os.environ` is never mutated, the value never reaches argv (which the
  `dev-team-gate` guard lexes) and is never printed or logged; an existing `GH_TOKEN` always
  wins, so an operator's own configuration is untouched. Without a token configured, behaviour
  is exactly as before. The module docstring no longer claims the adapter reads no token
  itself. (#15)
- **The `pr-ci` stage skill documents how the forge credential reaches a worker.**
  `export GH_TOKEN="$DEV_TEAM_GH_TOKEN"` followed by `gh auth setup-git`, and nothing else. This
  mechanism was chosen over a `GIT_ASKPASS` helper for three verified reasons: `gh auth
  setup-git` writes only `credential.*` git-config keys, which is not among the key prefixes the
  guard refuses as push-redirecting (`alias.`, `remote.`, `push.`, `branch.`, `include.`,
  `includeif.`, `url.`), whereas `GIT_ASKPASS` needs a helper **script file on disk**; `gh` reads
  `GH_TOKEN` from the environment, so the value never appears in a command line; and `GH_TOKEN`
  is not a `HERMES_*` variable, so exporting it touches no Hermes fence. The skill states the
  token is passed **by reference** only — never echoed, never interpolated into a URL, never
  written to a file — and that a missing or empty `DEV_TEAM_GH_TOKEN` is a
  `kanban_block(kind="needs_input")` labelled `setup: no-forge-token` with **no** fallback to
  `gh auth login`, a stored credential, a keyring, SSH, or `GITHUB_TOKEN`. (#15)
- **The installers wire up the forge credential: `--forge-token-source COMMAND` (Unix) /
  `-ForgeTokenSource COMMAND` (Windows, refused).** The `pr-ci` stage skill documents the
  token as Unix-only; on Windows the operator configures the `dev-devops` profile by hand.
  Neither installer had a flag wiring the credential in, so the credential the whole stage
  depends on had no documented way in. **Unix only** actually configures anything: the flag
  records `COMMAND` as the `<prefix>devops` profile's `secrets.command.command` **and** sets
  `secrets.command.enabled true` — Hermes' `CommandSource` requires both leaf keys (`enabled`
  defaults to `false`, and a bare `secrets.command` string is refused once the key already holds
  a correct mapping) — and **appends** `DEV_TEAM_GH_TOKEN` to that profile's
  `terminal.env_passthrough`, so a name already in that list is kept. The profile name is derived
  from the installed prefix, so a `--prefix t2-` install configures `t2-devops`, never a
  hardcoded `dev-devops`. `hermes config set` merges into the existing config, the write of the
  two leaf keys is idempotent even over an already-correct mapping, and the list merge is one
  shared implementation, `dev-team/scripts/env_passthrough_merge.py`, called by both installers
  (it fails closed on malformed input rather than silently treating it as an empty list).
  Re-running with the same `COMMAND` is a no-op; a different `COMMAND` replaces only
  `secrets.command.command`. A `COMMAND` containing a literal token shape (reusing `forge_core.py`'s
  `github_pat_`/`gh[pours]_` patterns) is refused outright, exit 2, before anything is written —
  so neither `--dry-run` output nor the config can ever hold a token. **`-ForgeTokenSource` on
  the Windows installer is refused outright, exit 2, nothing written:** Hermes' `command` secret
  source is POSIX-only (it shells out to `/bin/sh`), so the flag would otherwise record devops
  profile config Hermes running on Windows can never read — a failure that would only surface
  much later as a `pr-ci` card blocking with "no forge token", disconnected from its install-time
  cause. **Only the command string is ever stored, and this installer itself never executes
  `COMMAND`** — not to a config, not to stdout, and not under `--dry-run`. Once the source is
  enabled, though, Hermes itself runs the helper at the start of every later
  `hermes -p <prefix>devops ...` call the install makes and discards its stderr, so the value
  still never leaks, but it is not true that nothing ever runs `COMMAND`. A canary test asserts a
  sentinel token value appears in no file the installer writes and in neither stdout nor stderr,
  on a real run and under `--dry-run`, and that the command was not run; a separate test runs the
  real `hermes config set` against a real Hermes install and confirms the registry reports
  `command` enabled (skipped cleanly when Hermes is not importable). The flag is documented in
  `dev-team/README.md` and `docs/how-the-dev-team-works.md`; with `--only` excluding `devops` the
  Unix installer says on stderr that there was no profile to configure rather than silently doing
  nothing. (#15)

### Changed

- **`git push` from a card with no dev-team stage skill is now refused.** Such a card has no
  intake ancestor, so the guard cannot resolve a project for it and `_CardState.protected()`
  fell back to `{main, master}` — which let a push through to a project whose recorded default
  branch is something else (`develop`, say). Rather than give `protected()` a second
  project-resolution path fed by an input the worker controls (a card's `workspace_path` is
  editable via `hermes kanban edit`, which is permitted against a non-stage card), the push
  itself is refused: the branch it would create is one no `validate`, `review` or `pr-ci` card
  owns. `_ALWAYS_PROTECTED`, `protected()`, `guarded()`, `project_slug()`,
  `_record_default_branch()` and `stage_from_skills()` are all unchanged. The new check runs
  **after** the existing protected-destination check, so a non-stage card pushing `main` or
  `master` keeps its `merge: human-only` message byte for byte. Local commits, branches and all
  read-only git are unaffected, and **no card at all** (`HERMES_KANBAN_TASK` unset — an
  interactive operator shell) is deliberately unchanged, since there is no board-side identity
  to resolve a project from and refusing it would break every interactive session. (#15)
- **`kanban_create` is now checked: the new card's `assignee` must carry the installed team
  prefix.** A bare role name (`assignee="qa"`) makes the dispatcher spawn the unprefixed `qa`
  profile — another team's worker, or an unguarded root profile — so the created card would run
  with no dev-team gate at all. The prefix is read from `$HERMES_HOME/dev-team-prefix`, what the
  installer writes, and the comparison is **case-insensitive on both sides**
  (`.strip().lower()`, matching Hermes' own `profiles.normalize_profile_name`): Hermes
  lower-cases an assignee before storing it, so under prefix `dev-` an assignee `Dev-qa`
  resolves to the installed `dev-qa` and is accepted, and an operator who installed with
  `--prefix T2-` can create `T2-qa` cards. (This now agrees with branch handling, which
  casefolds, and with the protected-path check, which is case-insensitive.) It **fails closed**:
  a missing, unreadable or empty prefix file refuses the create rather than becoming a bypass.
  The check consults no board and is not gated on the creating card's stage, so a card with no
  skills or with an unknown `dev-team-stage-*` skill cannot bypass it; the plugin still declares
  only the `pre_tool_call` hook. (#15)
- **The assignee prefix rule now covers the terminal too.** `hermes kanban create --assignee`
  was not a side-door verb at all, and `hermes kanban assign` / `reassign` refused only when
  the *target card* carried a stage skill — so a worker could hand an ordinary card to the
  unprefixed, unguarded `qa` profile from a shell and bypass `check_create` entirely. `create`
  is now in `_SIDE_DOOR_VERBS` (with its own exact option allowlist for the fallback parser, and
  no target card, since it makes one), and the one prefix rule (`_check_assignee`) is applied to
  the assignee of `create`, `assign` and `reassign` for **every** card, stage skill or not,
  before any board read. Both parse paths are covered: Hermes' own `kanban` argparse tree when
  `hermes_cli` can be imported, and the strict fallback otherwise. `none` — the CLI's unassign
  sentinel — is let through; a `create` with no assignee at all is refused, exactly as the tool
  path refuses it. The pre-existing stage-card refusal on `assign`/`reassign` is unchanged and
  still applies on top. (#15)
- **`dev-team-prefix` is now a protected path.** `_PROTECTED_PATHS` trusted the forge, the
  plugin and the project records, but not the file the prefix check itself reads — so a worker
  could rewrite `$HERMES_HOME/dev-team-prefix` and then assign itself any profile it liked,
  defeating the rule above. The **unanchored** variant is the right one here: the file lives in
  a profile directory under `$HERMES_HOME`, not inside a repo, so there is no relative
  `dev-team-prefix` path in a clone of this repository for the anchored form (the N5 carve-out
  for `dev-team/forge` and `dev-team/plugin`) to protect. Reads are still allowed; any
  non-read-only command naming it is refused. (#15)
- **`redact()` also removes the literal token values.** The `_SECRET_SUBSTITUTIONS` shapes are a
  heuristic about what a credential looks like; whenever `DEV_TEAM_GH_TOKEN` or `GH_TOKEN` is
  set and non-blank, the operator's actual value is known exactly. Those literals are now
  stripped **on top of** (never instead of) the shape match, by literal string replacement so a
  value containing regex metacharacters is handled, and a blank value is skipped — replacing
  `" "` would rewrite every space in the message. A forge whose tokens do not match a GitHub
  shape, or a vendor that changes its prefixes, can therefore no longer leak a value verbatim
  through `forge`'s JSON error. (#15)
- **The guard redacts `_describe(exc)` before the agent sees it.** Every fail-closed refusal
  embeds an exception string — a `gh` failure, a git error, a forge response — and that text went
  to the worker unfiltered. It now goes through `forge_core.redact` (imported by path, like
  every other forge read, through a new core-only loader so `redact` does not depend on the
  vendor adapter package importing cleanly); if the forge cannot be imported at all the
  description becomes `<redaction unavailable>` rather than unredacted text. Truncation to
  `_DETAIL_MAX` is unchanged. (#15)
- **`running-the-pipeline` states `--default-workdir` as a requirement**, not just an example,
  and says why `pr-ci` depends on it: cards inherit their workspace from the board's
  `default_workdir`, so a board created without it leaves `pr-ci` with no clone to push from —
  it blocks and the PR is never opened, after plan, implement, validate and review have all been
  paid for. `pr-ci` will not fall back to the operator's own checkout, and no later step can set
  the value, so such a board has to be recreated. (#15)

### Fixed

- **`redact()` missed a bare GitHub token.** `_SECRET_SUBSTITUTIONS` covered `token=<value>` and
  `Bearer <value>`, so a token appearing on its own in a `gh` error message — "failed to
  authenticate with ghp_…", which becomes `forge`'s JSON error through `_emit_error` → `redact`
  — was printed in full. Both GitHub token shapes are now redacted: `gh[pours]_<body>` (classic,
  OAuth, user-to-server, server-to-server, refresh) and `github_pat_<body>` (fine-grained),
  case-insensitively and by prefix rather than exact length, so a future length change cannot
  silently un-redact one. (#15)

### Known issues

- **`gh repo sync` can write a remote branch when the clone's `origin` is a fork.** The `pr-ci`
  stage skill tells devops to sync before pushing, and `gh repo sync` with no explicit
  `--source`/`--branch` will update a branch on the *fork's* remote — a remote write the guard
  does not inspect, since it sees `gh repo sync` and not a `git push`. This **predates PR #15**
  (the command and the skill text both do) and is deliberately **out of scope** here: PR #15
  fixes the five findings of its own independent review and nothing else. It is recorded so the
  next change to the `pr-ci` stage does not have to rediscover it. Branch protection on the
  default branch, and a forge token without merge rights, remain the controls that actually
  bound this (see `dev-team/README.md`, "Required controls"). (#15)

## [1.3.2] - 2026-11-04

### Changed

- **Which paths `forge.cmd` / `deciders.cmd` embed literally is now decided by three
  characters, not nine.** `cmd.exe`'s metacharacter set is `& ^ % ! ( ) " < >`, but every
  embedded path is wrapped in double quotes, and inside double quotes `cmd.exe` treats `&`,
  `^`, `(` and `)` as literal text. So a pure-ASCII path containing only those — e.g.
  `C:\Users\bob\src (copy)\...`, `...\Programs\R&D\python.exe`, or
  `C:\Program Files (x86)\Python312\python.exe`, which matches no `%ENVVAR%` prefix at all —
  is embedded literally and unguarded, exactly as 1.3.1 wrote it, instead of being rewritten
  to an `%ENVVAR%` form (which would add variable guards, exit 1 under a trimmed
  environment, and follow `%USERPROFILE%` to whichever user runs the wrapper) or refused.
  Only `%` (starts another expansion even inside quotes), `"` (closes the quoting) and `!`
  (consumed by `!var!` under `cmd /v:on`) are treated as unsafe, but of those three, only
  `!` can be rewritten: a `!` path is rewritten when it sits under one of the prefix
  folders, and **refused with exit 2 otherwise** — so a checkout or Python at e.g.
  `D:\dev!\...`, outside every profile folder, no longer installs, where 1.3.1 accepted it.
  A `%` or `"` **below the matched prefix folder** is refused with exit 2 even under a
  prefix folder: the remainder check in `Resolve-CmdEmbeddablePath` skips
  a rewritten remainder bearing either character, so that part of the path is never
  rewritten. A `%` *in the prefix folder's own path*, however, is substituted away by the
  `%ENVVAR%` embed before the remainder check ever sees it, so the path is still rewritten
  and the install succeeds — only a `%` or `"` that survives into the remainder is refused.
  When the unusable path came from `py -3`, that source is skipped in favour
  of `PATH` rather than failing the install, and the skip now prints a `Write-Warning`
  (in `Resolve-WrapperPython`) naming both the path that was passed
  over and the one used instead, with `PY_PYTHON` / `py.ini` as the way to choose
  differently — 1.3.1 used the `py -3` interpreter in that situation, so the switch must
  not be silent. An unusable `PATH` Python, being the last source, cannot be skipped in
  favour of anything and ends in a refusal with exit 2. (#12, #13)

### Fixed

- **Resolves the v1.3.1 known issues "no ASCII-path Python, no wrappers" and "a non-ASCII
  `py -3` path gives a vague error".** A non-ASCII wrapper Python, `forge.py` or
  `deciders.py` path is now embedded in `forge.cmd` / `deciders.cmd` as an `%ENVVAR%`
  prefix (`%LOCALAPPDATA%`, `%APPDATA%`, `%USERPROFILE%`, `%ProgramW6432%` — or plain
  `%ProgramFiles%` on a 32-bit Windows OS, where `%ProgramW6432%` does not exist — or a
  *persisted* `HERMES_ROOT`, tried last, below every OS profile variable) plus an ASCII
  remainder, instead of refusing outright — so a Windows user with a non-ASCII user name
  whose only Python is Hermes' own under `%LOCALAPPDATA%`, and whose checkout is under
  their profile, can now install. `cmd.exe` expands the variable from its own UTF-16
  environment at run time, so the `.cmd` file on disk stays pure ASCII. Each rewritten
  path gets an `if not defined` guard (an unset prefix variable can never silently resolve
  to the current drive's root) followed by an `if not exist` guard (via `goto`, not a
  parenthesized block, since an expanded value can contain `)`) that fails with a clear
  message instead of silently running a stale, wrong, or planted interpreter; a wrapper
  with at least one rewritten path also gets `setlocal DisableDelayedExpansion`, so a
  literal `!` in an expanded path is never consumed by `cmd /v:on`'s own `!var!` syntax
  before the guard and the run line agree on it. The path comparison that decides whether a
  path sits under one of those folders now canonicalizes both sides to the long (non-8.3)
  path form first, so a process whose own path arrived as an 8.3 short name (e.g. a
  `RUNNER~1` account) against a long-form `%LOCALAPPDATA%` still matches instead of being
  wrongly refused; and the installer never writes a wrapper with an empty embedded
  interpreter path — that is a hard refusal (exit 2), not a silently broken `.cmd` file. A
  non-ASCII path under none of those folders is still refused with exit 2, before anything
  is written, naming the offending path and the folders it was *not* found under. `py -3`
  discovery now asks Python to print `sys.executable` as an ASCII-only hex encoding of its
  UTF-16LE code units (taking the last non-empty output line, so a deprecation notice or
  launcher warning printed before it is never mistaken for the hex itself), decoded in
  PowerShell, so a non-ASCII launcher path is no longer mangled by the console code page
  under Windows PowerShell 5.1 and no longer reported as "No Python 3 found" when the
  launcher did in fact report one. A `py -3` or `PATH` Python that exists but cannot be
  embedded (non-ASCII and under none of those folders, or containing one of the characters
  named under "Changed" below) no longer forces a hard refusal on
  its own: each of those two sources now falls back to the next one when a *later* source
  has an embeddable candidate, and only refuses when nothing embeddable was found anywhere;
  the refusal message also now mentions `PY_PYTHON`/`py.ini` as a way to point the `py`
  launcher at a different Python 3. The interpreter Hermes itself installed under
  `%LOCALAPPDATA%\hermes\tools` is preferred purely by discovery order again —
  representability no longer steers which interpreter is chosen for that one source, only
  whether its path can be embedded — and it is still never skipped in favour of a fallback,
  unlike `py -3`/`PATH`. An all-ASCII install whose path takes the literal embed (see
  "Changed" above) gets a wrapper byte-identical to 1.3.1's with exactly one exception: when
  that literally embedded path contains a `^`, the wrapper gains a single `setlocal
  DisableDelayedExpansion` line, so `cmd /v:on`'s delayed-expansion pass cannot strip that
  caret (it strips carets inside quotes on any line that also carries a `!`, which a
  caller's argument can supply through `%*`) and run a path other than the one embedded. An
  all-ASCII path containing `%`, `"` or `!` is not part of that byte-identical case: as
  "Changed" above states, only a `!` path is either rewritten to the `%ENVVAR%` form
  described here (gaining the `if not defined`/`if not exist` guards and `setlocal`) or
  refused with exit 2, depending on whether it sits under a prefix folder — a `%` or `"`
  below the matched prefix folder is refused with exit 2 even under a prefix folder, while
  one that lies in the prefix folder's own path is substituted away and the path is still
  rewritten. (#12)
- **A Turkish-style `İ` (U+0130) or `K` (U+212A, Kelvin sign) in a path was silently
  corrupted instead of triggering the `%ENVVAR%` rewrite.** PowerShell's default
  case-insensitive `-match` folded both to an ASCII letter, so the printable-ASCII check —
  and the defence-in-depth check before the wrapper is written — both missed them, and
  `-Encoding ascii` turned them into `?`. Both checks now compare ordinal character codes
  with no case folding and no regex engine, so the result no longer depends on the
  machine's culture. (#12)

### Internal

These describe bugs introduced and fixed *within this PR*. No released version ever carried
them, so they are not "Fixed" entries; they are listed because the review trail refers to
them.

- **An 8.3 short name (e.g. `C:\DEV-TE~1\DRGO~1\...`) bypassed the new `%ENVVAR%` rewrite
  instead of triggering it.** A short name is printable ASCII *by construction* even when
  the long form it denotes is not, and the ASCII-ness check that decides the wrapper's fast
  path ran on the path exactly as given, before its long form was ever looked at — so a
  non-ASCII install surfaced through an 8.3 short name had its short name embedded in
  `forge.cmd`/`deciders.cmd` literally, unrewritten. A path containing `~` is now
  canonicalized to its long form first, and the ASCII-ness decision (and, when it isn't
  ASCII, the prefix rewrite) is made on that long form. A path with no `~` still takes the
  fast path with no `GetLongPathName` call at all; every path that misses the fast path, and
  every prefix value, is canonicalized. (#12)
- **The new guard's "not found" message could run a command instead of just printing a
  path.** `cmd.exe` expands every `%VAR%` in a line before parsing it, so the message
  printed the *expanded* path unquoted; an `&` in it (a legal filename character, present in
  e.g. `R&D`) split the line and ran the trailing fragment as a command on the very path
  meant to fail closed. The message now prints the unexpanded `%ENVVAR%` form (every `%`
  doubled) wrapped in double quotes, so it is inert. (#12)
- **`Test-WrapperPythonEmbeddable` kept a second copy of the literal-embed rule** and so
  could call a candidate embeddable that the real check then refused, aborting the install
  (exit 2) instead of falling back to the next interpreter source. The rule now has a single
  implementation, consulted from one place. (#12)

### Known issues

- **A persisted `HERMES_ROOT` is not canonicalized to its long (non-8.3) form before the
  equality check that decides whether it is offered as an `%ENVVAR%` prefix at all.** The
  *prefix match itself* does canonicalize it, like every other candidate. What is not
  canonicalized is the separate comparison of the effective persisted `HERMES_ROOT` value
  against the Hermes root this installer is working on: if one side arrives in 8.3 short
  form and the other in long form, that comparison fails and `HERMES_ROOT` is never added as
  a candidate. This fails *closed* — at worst the installer falls through to the next
  candidate or refuses (exit 2) rather than embedding a wrong path, and `HERMES_ROOT` is
  already the lowest-priority (tier 2) candidate. Tracked for a future round. (#12)

## [1.3.1] - 2026-11-04

### Added

- `dev-team/scripts/env_pin_check.py`, a stdlib-only Python checker for every `.env` pin the
  dev-team installers write. A value is accepted only if transcriptions of **both** readers
  — python-dotenv (Hermes) and Mnemosyne's own `_load_dotenv` — return it byte-exact, and it
  is valid UTF-8 with no lone surrogates. It behaves the same in every locale (`env -i`,
  `LC_ALL=C`, `C.UTF-8`), so ordinary letters such as `ą` are accepted, and it refuses the
  values 1.3.0 still let through: Unicode line separators, NBSP at the ends or before `#`,
  non-UTF-8 bytes and lone surrogates. CI runs its own suite (`dev-team/scripts/tests`).
  (#10)
- docs/how-the-dev-team-works.md: what the dev team installs and how one request flows end
  to end, with two Mermaid diagrams; linked from dev-team/README.md and README.md.

### Changed

- Both dev-team installers run `env_pin_check.py` on the resolved value of every pin and
  fail closed with exit 2. The Windows installer runs it at three points: preflight, before
  the dry-run branch, and on the exact value written. (#10)
- The Windows installer picks the Python embedded in `forge.cmd` / `deciders.cmd` from
  Hermes' own Python under `%LOCALAPPDATA%\hermes\tools`, then `py -3`, then `python` on
  `PATH` (outside `WindowsApps`), preferring the first on a printable-ASCII path, and
  refuses with exit 2, before anything is written, rather than write a wrapper `cmd.exe`
  would misread. (#10)
- Comments that overstated the round-trip guarantee of the unquoted `.env` pins are
  corrected. (#10)
- CHANGELOG.md re-sorted into released versions (1.2.0, 1.3.0, 1.3.1).

### Fixed

- **A trailing newline in a symlink target was lost before the newline check.** On Unix,
  `$(readlink)` stripped it, so the guard never saw it; `readlink` is now captured with a
  sentinel, so the newline is kept and the path refused. This closes the known issue listed
  in 1.3.0. (#10)
- **A working directory containing a newline could be cut short.** On Unix the newline check
  now runs after the `$PWD` join instead of before it. (#10)
- `.github/scripts/check-team-bank.ps1` reads the `.env` as UTF-8 under Windows PowerShell
  5.1. (#10)
- **`forge.cmd` / `deciders.cmd` were corrupted under a non-ASCII path.** They were written
  with `-Encoding ascii`, which turns `C:\Users\<non-ASCII name>` into `?`; that path is now
  refused instead (see Changed). (#10)

### Known issues

- **Windows: no ASCII-path Python, no wrappers.** `cmd.exe` reads a `.cmd` file in the
  console code page, so the installer refuses (exit 2, before anything is written) when the
  path of the wrapper Python, `forge.py` or `deciders.py` is not printable ASCII. On a
  machine whose user name is non-ASCII and whose only Python is Hermes' own under
  `%LOCALAPPDATA%`, the install fails until a Python 3 on an ASCII-only path (for example
  `C:\Python312`, first on `PATH`) is installed and the dev-team checkout is on an ASCII
  path too. (#10) — fixed in Unreleased (#12).
- **Windows PowerShell 5.1: a non-ASCII `py -3` path gives a vague error.** The path that
  `py -3` prints for `sys.executable` is decoded by PowerShell in the console's OEM code
  page, so a non-ASCII path from the launcher comes back garbled, fails `Test-Path` and is
  skipped without a message. If it was the only Python found, the installer exits 1 with
  `No Python 3 found for the forge and deciders wrappers …` instead of naming the
  non-ASCII path. (#10) — fixed in Unreleased (#12).

## [1.3.0] - 2026-11-03

### Added

- `--recommended-config` / `-RecommendedConfig`: opt-in settings for cross-session recall
  over global-scope memories and the strict write classifier (in Mnemosyne's `config.yaml`,
  writing only those keys), and LLM work routed through Hermes (in Hermes' `.env`, the only
  place Mnemosyne reads those switches). Each one is read back and the install fails if one
  does not stick; it warns when a Hermes-side `memory.mnemosyne` key overrides one. It
  cannot be combined with `--skip-hermes-configuration`. With it, consolidation sends
  memory text to Hermes' model provider, which costs tokens. The uninstallers remove the two
  `.env` lines while they still read `true`. See
  [Configuration](docs/configuration.md#recommended-configuration). (#4)
- [`dev-team/`](dev-team/README.md) — an autonomous software delivery team, governed the
  Archon way: Hermes kanban stays exactly as it is, and a small plugin
  (`dev-team-gate`) enforces stage completion mechanically instead of trusting an agent's
  say-so. One sentence, a budget and a repository in; a pull request with green CI, marked
  ready for human merge, out — merge and deploy stay human-only by construction (`forge` has
  no merge or deploy subcommand, and the guard additionally blocks known merge/deploy commands
  and the kanban side-door verbs on every guarded card). (#5)
  - Eight profiles (`pm`, `architect`, `uiux`, `backend-db`, `frontend`, `qa`, `reviewer` —
    new — `devops`), prefixed `dev-` by default (`--prefix`/`-Prefix`, as the outsourcing
    installer's), installed by `install-dev-team-unix.sh` /
    `install-dev-team-windows.ps1` with `--model`/`-Model`, `--only`/`-Only`,
    `--skip-model`/`-SkipModel`, `--keep-soul`/`-KeepSoul`,
    `--separate-memory`/`-SeparateMemory`, `--dry-run`/`-DryRun`. Idempotent; links the gate
    plugin and Mnemosyne into every profile and verifies both before exiting.
  - Six pipeline stages (`intake` → `plan` → `implement` → `validate` → `review` → `pr-ci`),
    each a child card of the last, each required to hand the next a small typed `metadata`
    result the guard validates by type on `kanban_complete`; `pr-ci`'s claimed `ci: "green"`
    is re-verified against the forge itself, not taken on the worker's word. Rework (QA fail,
    review changes, CI red caused by the change) loops back to a new `implement` card up to a
    round limit of 3, enforced from the card's ancestry.
  - `forge`, a vendor-neutral stdlib CLI (`detect`, `pr-open`, `pr-status`, `ci-status`,
    `mark-ready`) with adapters for GitHub, GitLab, Gitea and Jenkins, so no vendor name leaks
    into any soul, stage skill or the guard's logic except the one stage skill
    (`dev-team-stage-pr-ci`) allowed to name it.
  - `dev-team/scripts/kanban-budget.py`, dev-team's own copy of the outsourcing team's budget
    script, run by `pm` at 25/50/75% checkpoints; its exit code (0 fine, 1 closeout reserve, 2
    exhausted) is pm's judgment call, never the guard's.
  - Unit tests for `forge`'s adapters and the `dev-team-gate` guard, plus
    `.github/workflows/dev-team.yml` running them alongside an install/reinstall idempotency
    check on Linux and Windows.
- `dev-team/deciders/`: a stdlib CLI (`decide`, `extract`, `summarize`, `tier`) over
  OpenRouter's Decisions API, for small bounded decisions the team would otherwise pay
  frontier-model prices for. The key is used only through
  `secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders` and never written anywhere;
  every call appends a line to a spend ledger that `kanban-budget.py` counts. At the plan
  stage `deciders tier` proposes fast, standard or deep, mapped to a model through the
  installer-written `model-tiers.json` (`--tier-fast` / `--tier-standard` / `--tier-deep`,
  `-TierFast` / `-TierStandard` / `-TierDeep`; haiku, sonnet and opus by default); without
  a tier table no model override is set and the card says why, and the installers refuse a
  decisions-only model as a tier model. Shipped with the `dev-team-deciders` skill — decide
  and extract are for bounded questions and pointers, `summarize` is never evidence — and
  live probe results in `dev-team/deciders/PROBES.md`. (#6)
- A team-only memory bank. The dev-team profiles share `$HERMES_ROOT/dev-team-mnemosyne`
  (or `--team-memory <dir>` / `-TeamMemory`) instead of the operator's own bank, and each
  profile's `.env` pins `MNEMOSYNE_DATA_DIR` and `MNEMOSYNE_HOME` to it. The operator's bank
  — including one relocated with `MNEMOSYNE_HOME` — is refused as a target, as is anything
  nested in it, reached through links, or relative. Opt-in
  `--adopt-team-memory` / `-AdoptTeamMemory` moves an existing profile's private bank to a
  timestamped backup and links the profile to the team bank; without it, private banks are
  kept. (#8)

### Changed

- Moved `sdlc-team/` and `outsourcing-team/` to `old/sdlc-team/` and `old/outsourcing-team/`
  (`git mv`). Both are superseded by `dev-team/` (added above) and are kept frozen, not
  maintained. Internal relative paths (`../install-...`, `../uninstall-...`, `../docs/...`)
  were updated to `../../...` to account for the extra directory level, and each old README
  gained one line at the top pointing at `../../dev-team/README.md`. (#5)
- The dev-team's shared state lives at `<hermes root>/dev-team` on both platforms. The
  installers used to take `hermes config path`, so with a profile active the team home was
  rooted inside that profile (`profiles/devops/dev-team`). On upgrade, when exactly one old
  home holds data and the root is empty, it is moved and every `dev-*` link is relinked;
  with more than one populated location the installer exits 2, lists them all and touches
  nothing. Every `dev-*` profile is scanned, not only the `--only` selection. (#7)
- `deciders` takes `--task-id`, which wins over `HERMES_KANBAN_TASK`, and the skill always
  passes it: dispatched workers' shells never receive that variable. (#7)
- Onboarding runs unattended. The pm creates a repository's board with
  `--default-workdir <team home>/repos/<slug>` instead of a later
  `boards set-default-workdir`, which Hermes forbids to dispatched workers, and devops blocks
  if the workdir is wrong. Nothing in the skills, souls or README tells an agent to run
  `boards switch`, which changed the current board for every Hermes session on the machine;
  every command names its board, and a text test enforces it. (#8)
- `.env` pins are written as unquoted `KEY=value` on both platforms, because Mnemosyne's
  `.env` reader never unescapes, so any quoted or escaped format corrupts Windows paths. The
  installers instead refuse (exit 2) the shapes known to break a reader — whitespace
  followed by `#`, `${`, a newline or CR, quotes or whitespace at either end — checked on
  the resolved path that is actually pinned. The CI parsers accept both legacy-quoted and
  unquoted pins. (#9)
- The profile-dir guard also refuses a profile dir equal to `$HERMES_ROOT/profiles`. (#9)
- The dev-team README and comments no longer claim protections that do not exist: the pin
  does not protect managed workers, which skip `.env`; `MNEMOSYNE_HOME` in `mcp_server.py`
  only locates a `.env`; stale precedence text and an untested 8.3/`subst` claim are
  removed. (#9)

### Fixed

- [Configuration](docs/configuration.md#sleep-consolidation) listed
  `MNEMOSYNE_AUTO_SLEEP_ENABLED` as `false` and `MNEMOSYNE_WM_TTL_HOURS` as `24`; upstream's
  defaults are `true` and `168`. The Hermes provider key `auto_sleep` was likewise listed as
  `false`; it defaults to `true`. (#4)
- **The deciders test fake could swallow a wrong request shape**, so a malformed call still
  passed any test expecting exit 2. `FakeTransport` now records shape violations and the
  tests assert there are none. (#7)
- **Windows accepted an empty tier model half.** `-TierFast` / `-TierStandard` / `-TierDeep`
  now refuse an empty provider or model with exit 2 and the same message as Unix. (#7)
- **Spend-ledger lines never carried the card id** (`task_id` was always null); fixed by
  `--task-id` (see Changed). (#7)
- **Windows vetted old team homes against too few profile directories**; it now checks
  every directory under `profiles\`, matching Unix, and the conflict path has a pwsh-run
  behavioural test instead of source-text checks. (#8)
- **Windows `.env` pins were written in the ANSI code page** by Windows PowerShell 5.1, so a
  path such as `C:\Users\Drăgoș\...` was pinned garbled and the CLI then created a new empty
  database there. They are written as UTF-8 without a BOM: Hermes reads `utf-8-sig`, but
  Mnemosyne's own reader reads plain `utf-8`, so a BOM would break it. (#9)

### Known issues

- The `.env` pin check is a list of known-bad shapes, not a round-trip guarantee: Unicode
  line separators (U+2028/2029/0085), `\v \f \x1c-\x1e`, NBSP before `#` or at the ends, and
  non-UTF-8 bytes are still accepted but read differently. (#9; fixed in 1.3.1)
- On Unix a symlink target that ends in a newline is truncated by `$(readlink)` before the
  newline guard sees it. Triggering it needs a crafted directory name plus a pre-existing
  decoy directory, and it cannot reach the operator's bank. (#9; fixed in 1.3.1)
- `forge.cmd` / `deciders.cmd` are written ASCII-only, so they break under a non-ASCII
  Python path. (#9; fixed in 1.3.1)

## [1.2.0] - 2026-10-12

### Added

- [`outsourcing-team/`](old/outsourcing-team/README.md) — a second optional profile set, for the
  vendor side of a contract rather than a product team: eleven role profiles
  (engagement-lead, delivery-manager, business-analyst, domain-consultant,
  solution-architect, app-engineer, integration-engineer, qa-lead, platform-sre,
  security-compliance, presales-writer), each with its own SOUL, each pointed at Mnemosyne,
  with `install-outsourcing-team-unix.sh` and `install-outsourcing-team-windows.ps1`.
  Where `sdlc-team` owns its code and its risk, these souls do not: risk acceptance belongs
  to a named client-side owner and a specialist's refusal is escalated rather than overridden
  internally, scope is contractual so "clarification versus change request" is an explicit
  job, the estate pre-exists and is only partly visible, and the engagement ends — so
  handover and the client team's ability to operate the result are deliverables. One profile
  supplies the industry knowledge (manufacturing, finance, e-commerce, healthcare, public
  sector) the engineers do not have. (#2)
- Kanban orchestration for `outsourcing-team/`: the installer enables the kanban toolset on
  every profile (it ships disabled, and dispatcher-spawned workers inherit their profile's
  *CLI* toolsets — without it a worker silently cannot create or link cards for a sibling),
  installs a `running-an-engagement` skill and `kanban-budget.py` on the `engagement-lead`
  entry point, and takes `--no-kanban` to skip all three. `outsourcing-team/docs/` carries the
  communication diagram; the README renders it inline as Mermaid. (#2)
- `outsourcing-team/scripts/kanban-budget.py`, because kanban has no native cost tracking:
  `tasks` has no spend column and `tasks.session_id` is NULL for dispatcher-spawned workers.
  Spend is recovered by correlating each run's profile and time window against that profile's
  own `state.db` sessions; anything it cannot tie to a run is reported as unattributed rather
  than dropped. Exit codes gate a run — 0 fine, 1 into the closeout reserve, 2 exhausted. (#2)
- `--prefix`/`-Prefix` on the outsourcing-team scripts, prepending a prefix to every profile
  name so two engagements of the same team can run side by side. `--only`/`-Only` still takes
  bare role names, unaffected by the prefix, and now fails fast on an unrecognised name
  instead of completing successfully having created nothing. (#2)
- [`sdlc-team/`](old/sdlc-team/README.md) — an optional companion to the memory installers:
  nine role profiles (architect, backend-db, devops, frontend, pm, qa, researcher, uiux,
  writer), each with its own SOUL, each pointed at Mnemosyne, with
  `install-sdlc-team-unix.sh` and `install-sdlc-team-windows.ps1`. Flags:
  `--model`/`-Model`, `--only`/`-Only`, `--skip-model`/`-SkipModel`,
  `--keep-soul`/`-KeepSoul`, `--dry-run`/`-DryRun`. Re-running updates SOUL, model and
  plugin link in place. `--skip-model` with `--model` fails fast, matching the existing
  contradictory-flag handling. (#1)
- The scripts close a failure that is silent by construction. A named profile redirects
  `HERMES_HOME` to its own directory and memory providers are discovered under
  `$HERMES_HOME/plugins/`, so a profile cloned from a Mnemosyne-configured root inherits
  `memory.provider: mnemosyne` with no plugin to satisfy it — `memory status` reports
  `Provider: mnemosyne` next to `Plugin: NOT installed`, and the profile runs with no
  memory at all rather than erroring. Each profile's `plugins/mnemosyne` is linked back
  to the single real install (symlink on Unix; a directory junction on Windows, which
  needs neither Developer Mode nor elevation, falling back to a copy with a warning), and
  every profile's `memory status` is verified before exit, non-zero if any is not
  `available`. (#1)
- Both scripts refuse to run when the root profile's `memory.provider` is not
  `mnemosyne`, and warn when the built-in `MEMORY.md` / `USER.md` store is still on.
  `hermes profile create --clone` copies `config.yaml` as a file, so the root's memory
  settings at clone time are what every profile is created with — ordering that is
  invisible until it is wrong. (#1)

### Changed

- Mnemosyne is installed from the head of `main` in
  [`mnemosyne-oss/mnemosyne`](https://github.com/mnemosyne-oss/mnemosyne) instead of from
  PyPI. The installers resolve the commit through the GitHub API and install
  `mnemosyne-memory` and `mnemosyne-hermes` from that commit's source archive, so both
  packages always come from the same commit (`mnemosyne-hermes` on `main` requires the
  matching `mnemosyne-memory`, which PyPI may not have yet). pip skips a direct-URL
  package whose version is already installed, so a `--force-reinstall --no-deps` of the
  two packages follows the normal install; without it a re-run would stay on the old
  commit, because `main` keeps one version string across many commits.
  **Upgrade note:** an environment installed from PyPI moves from the stable 3.x to the
  pre-release on `main`, and the memory database is upgraded in place with no downgrade
  path; run `mnemosyne backup` first. The installers print the change
  (`mnemosyne-memory upgraded: 3.15.1 -> 4.0.0b5`). See
  [Upgrading from a PyPI install](docs/installation.md#upgrading-from-a-pypi-install). (#3)
- When Hermes' interpreter cannot be found, the installers now stop instead of assuming
  Python 3.11, which would have built an environment Hermes cannot load embeddings from.
  `MNEMOSYNE_HERMES_PYTHON` names the interpreter explicitly (not `HERMES_PYTHON`, which
  Hermes' own tooling sets to unrelated interpreters). (#3)
- The upstream commit is resolved before the virtual environment is touched, so a GitHub
  API failure leaves an existing install working. Rate limiting is reported as such, and
  `GITHUB_TOKEN` / `GH_TOKEN` are sent when set. (#3)
- The embedding check runs Hermes' interpreter with `-I`, as Hermes launches it, so it no
  longer passes on the strength of `PYTHONPATH` or user site-packages. (#3)
- Hermes' bundled `uv` is preferred over one on `PATH` on both platforms, the newest
  version wins when several are present, and `HERMES_RUNTIME_DIR` is honoured. (#3)
- The Windows `hermes --print-runtime-command` call is bounded to 60 seconds, like the
  Unix one, and only the JSON line of its output is parsed. (#3)
- The Visual C++ runtime step installs the package matching the interpreter's
  architecture (x64, arm64 or x86), says it may ask for elevation, and quotes Python's
  actual import error when the stack still does not load. (#3)
- A virtual environment created in the same run skips the redundant `--force-reinstall`. (#3)
- The documented `Set-ExecutionPolicy -Scope Process` lines now pass `-Force`, which skips
  Windows PowerShell 5.1's confirmation prompt. (#3)
- A `GITHUB_TOKEN` / `GH_TOKEN` that GitHub answers with 401, 403 or 429 no longer blocks
  the install: the installers retry the commit lookup without it. An HTTP 200 without a
  commit id (a proxy or captive portal) is named as such. (#3)
- Launchers without `--print-runtime-command` — older installs and the official Docker
  image (`nousresearch/hermes-agent`, which runs Hermes from `/opt/hermes/.venv`) — are
  handled through `hermes --version`, which reports the Python Hermes runs and its install
  directory: a `.venv`/`venv` there, or the old `hermes-agent/venv`, is used only if its
  version matches what Hermes reports, and that is announced. Before, the Docker image could
  not be installed into at all, and a leftover venv could be adopted unchecked. A timeout or
  an unusable answer is an error, each failure says what happened, an unrunnable
  `MNEMOSYNE_HERMES_PYTHON` is reported as that, and both platforms accept the launcher's
  JSON line whatever its exit code. (#3)
- Windows: the Visual C++ runtime is chosen from `sysconfig.get_platform()` (the
  interpreter's own build) rather than `platform.machine()` (the CPU); the timeout path's
  `taskkill` can no longer abort the installer under PowerShell 5.1, and the process is
  reaped before its temp files are deleted. (#3)

### Fixed

- **The Unix uninstaller hung forever without a registered gateway service, and the first
  two fixes for it mishandled gateways.** `hermes gateway restart` runs the gateway in the
  foreground when no service manager owns it. Detecting unit files missed system-scope,
  `XDG_CONFIG_HOME` and s6 gateways; reading `stop`'s message started stopped services
  (it says "Stopped" whenever a unit exists) and left s6 gateways down (`restart` there
  only signals). Both Unix scripts now read the gateway's own `gateway_state.json` and
  verify its PID the way Hermes does — alive (`kill -0`, `/proc`; no `ps` needed), same
  start time on Linux, arguments `gateway run`/`restart` — so a gateway killed outside
  Hermes, whose dead PID may have been reused, is not mistaken for a running one. Every
  state but `stopped`/`startup_failed` counts as up. The uninstaller brings a gateway back
  only if one was up: with `hermes gateway start` (systemd, launchd, s6), falling back to a
  detached `hermes gateway run` except under s6, or with a restart if the old gateway would
  not stop; the old PID never counts as "came back". The installer restarts an up gateway
  and judges success by a new gateway PID, so a busy gateway draining its turns is reported
  as in progress. The stop is bounded at 120 seconds, above Hermes' own 90-second stop wait.
  The scripts log to `logs/mnemosyne-installer-gateway.log` (appended, rotated at 1 MB, and
  skipped rather than fatal when unwritable); an earlier revision truncated
  `logs/gateway-restart.log`, which belongs to Hermes' dashboard. Found by end-to-end runs
  in an `ubuntu:24.04` arm64 container, a systemd VM and Hermes' s6 image, and by review. (#3)
- **The Unix installer could not bootstrap Hermes on a minimal system.** Hermes' own
  installer refuses to run without `git`, which container and cloud server images do not
  ship; the installer now installs it first when Hermes is missing. (#3)
- **On current Hermes the virtual environment was built against the wrong Python.** The
  installers read Hermes' version from `HERMES_HOME/hermes-agent/venv`, which current
  Hermes no longer creates — it runs from a managed interpreter under `HERMES_HOME/tools`.
  They warned `could not determine Hermes' Python version`, fell back to 3.11 and built
  the environment for that while Hermes ran 3.14, so `numpy` / `onnxruntime` could not
  load in-process and every memory was stored without an embedding. The installers now
  ask the launcher (`hermes --print-runtime-command`) for its interpreter and keep the
  old venv path as a fallback for older installs. The Windows bootstrap also looks for a
  freshly installed `hermes` in `HERMES_HOME\bin`, where current Hermes publishes it. (#3)
- **The Windows installer always aborted under Windows PowerShell 5.1.** It read Python
  versions with `python -c 'print("%d.%d" % ...)'`, and Windows PowerShell 5.1 strips
  embedded double quotes from native arguments, so Python got a syntax error and every
  version read came back empty — Hermes' as well as the venv's — ending in
  `Python version mismatch: Hermes runs 3.11 but the Mnemosyne environment is .`. The probe
  now uses single quotes only. PowerShell 7.3+ passes the quotes through, which hid this.
  Found by an end-to-end run on a fresh Windows 11 25H2 VM. (#3)
- **Hermes' vendored `uv` was missed.** Current Hermes keeps it under
  `HERMES_HOME/tools/uv-<version>-<platform>/`, not `HERMES_HOME/bin`, so the Windows
  installer fetched a second `uv` from winget. Both installers now look there first. (#3)
- **The documented Windows commands failed on a fresh machine.** A Windows client's default
  execution policy refuses every `.ps1`; the docs now start each block with
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, and show a ZIP download for
  machines without `git`. (#3)
- **On a fresh Windows, embeddings never loaded.** A fresh Windows 11 has no Visual C++
  runtime, so `onnxruntime` failed with `DLL load failed` inside Hermes and every memory was
  stored without a vector; the installer only warned. It had worked by accident while the
  installer was fetching `uv` from winget, whose package depends on the runtime. The
  installer now installs `Microsoft.VCRedist.2015+.x64` through winget when the import fails
  that way, then checks again. (#3)

## [1.1.0] - 2026-08-14

### Added

- `--disable-builtin-memory` / `-DisableBuiltinMemory`. Hermes' built-in `MEMORY.md` /
  `USER.md` store stays active when an external provider is registered, and
  [upstream recommends](https://docs.mnemosyne.site/api/hermes-plugin) turning it off so
  the two do not both consume context every turn. The flag sets `memory_enabled` and
  `user_profile_enabled` to `false`. It is opt-in because it changes what the agent
  remembers rather than where memories are kept. The uninstallers restore either key
  found set to `false`, so removing the provider cannot leave Hermes with no memory at
  all.
- `--all` / `-All`, installing `mnemosyne-memory[all]` — the profile that enables local
  sleep consolidation. Measured on `mnemosyne-memory` 3.15.1 it takes the virtual
  environment from 36 packages / 224 MB to 65 / 345 MB in about 390 s, most of it
  `llama-cpp-python` compiling from source. Two corrections to the upstream description:
  it does not pull in `sentence-transformers` or `torch`, and the delta is roughly 120 MB
  rather than ~1.5 GB. The GGUF model is fetched on the first consolidation, not at
  install. All of it — including the new compiled `llama_cpp` and `ctransformers` —
  imports cleanly under Hermes' own interpreter.
- Contradictory flag combinations now fail fast instead of applying a silent precedence
  rule: `--all` with `--no-embeddings`, and `--disable-builtin-memory` with
  `--skip-hermes-configuration`.
- `--no-embeddings` / `-NoEmbeddings` now does something. It cannot skip the download —
  `mnemosyne-hermes` depends on `mnemosyne-memory[embeddings]` outright — but it sets
  Mnemosyne's own `MNEMOSYNE_NO_EMBEDDINGS=1`, which disables dense retrieval at runtime.
  Verified by A/B: with the flag a stored memory gets no `memory_embeddings` row and
  recall returns `dense_score: 0.0`; without it, one row and `0.913`. The uninstallers
  clear the variable.
- [`docs/configuration.md`](docs/configuration.md) — the Hermes `memory.mnemosyne`
  provider keys, the `MNEMOSYNE_*` variables that matter operationally, which of them
  these installers set, and where each config file actually lives.

### Fixed

- **Profile gateways were never removed.** Hermes scopes its gateway service per profile
  (`hermes-gateway-coder.service`, `ai.hermes.gateway-coder.plist`), but the Unix
  uninstaller matched one fixed name. Service artifacts are now globbed, and each is
  released from `systemctl --user` / `launchctl` before its file is deleted. This
  affected Linux as well as macOS.
- **`--include-hermes` missed launchers outside `~/.local/bin`.** Hermes links its
  command into `/usr/local/bin` for a root FHS install and `$PREFIX/bin` on Termux, and
  puts the vendored `node`/`npm`/`npx` symlinks in the same place. All three directories
  are now scanned; entries are still only removed when they point into `HERMES_HOME`.
- **macOS gateway handling.** The Unix uninstaller recognised the systemd unit only, so
  on macOS it could report `Nothing left behind` while a launchd agent survived. It now
  handles `~/Library/LaunchAgents/ai.hermes.gateway*.plist`, resolved from the passwd
  home rather than `$HOME` — profile-mode Hermes repoints `HOME`, but the agent does not
  move. Paths were taken from `hermes_cli/gateway.py`, not guessed.
- **`run_bounded` lost its bound on macOS.** It only looked for `timeout`; Homebrew's
  coreutils installs it as `gtimeout`, which is now used as a fallback.
- **The Windows config-encoding workaround never took effect.** It re-encoded
  `config.yaml` as UTF-8 *before* `hermes memory status` and `mnemosyne stats` ran — and
  on a fresh install those are what create the file, in the ANSI codepage. The fix was a
  no-op and every later Mnemosyne command still printed `'utf-8' codec can't decode byte
  0x97`. It now runs after them as well.
- **`--no-embeddings` was sticky.** Setting the variable but never clearing it meant a
  single flagged install disabled dense retrieval permanently — a later install without
  the flag left it in place. Omitting the flag now clears it.
- **Blob storage survived a full uninstall.** `MNEMOSYNE_BLOB_DIR` can point outside the
  data directory; it is now removed with it, and cleared as a variable on Windows.

### Note

bash 3.2 compatibility is now **verified rather than assumed**: bash 3.2.57, the version
stock macOS ships, was compiled and both scripts were run under it — parse, flag
handling, a full `--include-hermes --dry-run` against a live install, both installer
paths, and the whole destructive matrix. No incompatibility surfaced.

That removes the language-version risk but does not make macOS tested. The remaining
macOS changes are written against the Hermes source and still **unverified on real
hardware** — see [TODO](TODO.md#still-requires-a-mac).

## [1.0.0] - 2026-08-13

First tagged release. Install and uninstall scripts for the Mnemosyne memory provider on
Hermes Agent, covering Linux, macOS and Windows.

### Added

- `install-mnemosyne-hermes-unix.sh` — installer for Linux and macOS.
- `uninstall-mnemosyne-hermes-unix.sh` — uninstaller for Linux and macOS, with
  `--keep-data`, `--include-hermes`, `--dry-run`, `--force` and `--non-interactive`.
  Written for bash 3.2 so it runs on stock macOS.
- `install-mnemosyne-hermes-windows.ps1` — installer for Windows (PowerShell 5.1+).
- `uninstall-mnemosyne-hermes-windows.ps1` — uninstaller for Windows, with `-KeepData`,
  `-IncludeHermes`, `-DryRun`, `-Force` and `-NonInteractive`.
- A plan → confirm → verify flow in both uninstallers. They print exactly what will be
  removed, ask before touching anything, and exit non-zero if something survives.
- Interpreter matching in both installers: the Mnemosyne virtual environment is built
  against the Python version Hermes itself runs, an existing environment built against
  the wrong version is rebuilt, and a remaining mismatch aborts the install.
- A post-install health check that imports `numpy` and `onnxruntime` through Hermes' own
  interpreter and reports whether the embedding stack actually loads.
- `pre-commit` configuration: whitespace and end-of-file fixers, `check-yaml`, a
  shebang/executable-bit consistency check, `codespell`, `markdownlint` and `shellcheck`.
- Documentation under [`docs/`](docs/).

### Fixed

- **The Unix installer hung forever.** `hermes gateway restart` only restarts a
  registered service; with none registered it falls back to running the gateway in the
  foreground and never returns, so the installer never reached its verification steps.
  It now registers the service first and bounds the restart with `timeout` where that
  command exists.
- **Memories were stored without embeddings.** Hermes imports Mnemosyne in-process, so
  compiled wheels must match Hermes' Python. The Unix installer fell back to the system
  Python (3.14) against Hermes' 3.11 because `command -v uv` missed the uv that Hermes
  vendors at `HERMES_HOME/bin`. `numpy` and `onnxruntime` then failed to import inside
  Hermes while everything else kept working: the provider still registered,
  `hermes memory status` still reported `available`, and keyword recall still returned
  rows — but nothing Hermes wrote ever got a vector, so semantic recall could not match
  it. Semantic scores went from `0.0000` to `0.90`–`0.93` once the versions matched.
- **Re-running the Unix installer failed.** `mnemosyne-hermes install` aborts with
  "already exists" unless given `--force`, so any re-run exited 1 and, after a rebuild,
  left the wrapper pointing at a stale `site-packages`.
- **`--include-hermes` left broken launchers behind.** Hermes installs both wrapper
  scripts (`hermes`, `hermes-acp`, `hermes-agent`) and vendored-node symlinks (`node`,
  `npm`, `npx`) in `~/.local/bin`. They are now matched by where they point rather than
  by name, so all of them are removed while an unrelated `node` is left alone.
- **`-IncludeHermes` left a Windows login item behind.** Hermes has no Windows service;
  it registers `Hermes_Gateway.vbs` in the Startup folder, which survived removal and
  kept trying to launch a deleted Hermes at every sign-in.
- **The Unix installer was not executable.** It was committed with mode `100644`, so a
  fresh clone on Linux could not run it.

### Known issues

- The macOS code paths are written but untested on real hardware. In particular the
  uninstaller's gateway handling recognises the systemd unit only; on macOS it relies on
  `hermes gateway uninstall` and would not report a stranded launchd agent as a leftover.
- `--no-embeddings` / `-NoEmbeddings` has no effect, because `mnemosyne-hermes` 0.5.0
  declares `mnemosyne-memory[embeddings]` as a hard dependency.

[Unreleased]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.3.3...HEAD
[1.3.3]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/releases/tag/v1.1.0
[1.0.0]: https://github.com/bogdandragosaccesa/mnemosyne-hermes-installers/releases/tag/v1.0.0
