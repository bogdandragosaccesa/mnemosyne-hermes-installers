"""dev-team guard: the `kanban_complete` check, the `terminal` check, and the
`pre_tool_call` hook that dispatches to them.

`check_complete` and `check_terminal` return None to let the call through, or a
refusal message the worker sees as the tool error. They fail closed: any
exception, missing card or unverifiable state becomes a refusal naming what
could not be verified. Only a card with no dev-team stage skill is let through
without the `kanban_complete` checks.

Refusal messages start with a class label (`metadata:`, `head_sha:`, `plan:`,
`created_cards:`, `child:`, `review: rounds-exhausted`, `forge: unreachable`,
`ci:`, `merge: human-only`, `guard:`), so a worker that must block can reuse the
label.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

try:  # loaded as the plugin package
    from .board import Board, KanbanBoard, implement_rounds, project_slug
    from .stages import expected_child, stage_from_skills, validate_metadata
except ImportError:  # loaded flat, as the tests do
    from board import Board, KanbanBoard, implement_rounds, project_slug
    from stages import expected_child, stage_from_skills, validate_metadata

ROUND_LIMIT = 3

# stage -> the stage of the parent card whose head_sha it must repeat.
_PARENT_STAGE = {"validate": "implement", "review": "validate", "pr-ci": "review"}

_DETAIL_MAX = 300


class _Refuse(Exception):
    pass


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 3] + "..."


def check_complete(
    board: Board, task_id: str, args: dict, ci_status: Callable[[str, str], str]
) -> str | None:
    """Rules, in order: unguarded card passes; metadata valid; head_sha equals
    the parent stage's; exactly one right child; round limit; CI re-verified."""
    try:
        return _check(board, task_id, args, ci_status)
    except _Refuse as refusal:
        return str(refusal)
    except Exception as exc:  # fail closed
        return f"guard: cannot verify card {task_id}, refusing ({_describe(exc)})"


def _view_and_stage(board: Board, id: str, label: str):
    view = board.task(id)
    if view is None:
        raise _Refuse(f"{label}: card {id} not found on the board")
    try:
        return view, stage_from_skills(view.skills)
    except ValueError as exc:
        raise _Refuse(f"{label}: card {id}: {exc}") from None


def _stage_of(board: Board, id: str, label: str) -> str | None:
    return _view_and_stage(board, id, label)[1]


def _check(board, task_id, args, ci_status):
    # 1. Unguarded card.
    view = board.task(task_id)
    if view is None:
        raise _Refuse(f"guard: card {task_id} not found on the board")
    stage = stage_from_skills(view.skills)
    if stage is None:
        return None

    if not isinstance(args, dict):
        raise _Refuse("metadata: tool arguments must be an object")
    md = args.get("metadata")

    # 2. Stage table.
    errors = validate_metadata(stage, md)
    if errors:
        raise _Refuse(f"metadata: stage {stage}: " + "; ".join(errors))
    assert isinstance(md, dict)  # validate_metadata rejects anything else

    # 3. head_sha carried over from the parent stage card's last completed run.
    want = _PARENT_STAGE.get(stage)
    if want:
        parents = [p for p in view.parents if _stage_of(board, p, "head_sha") == want]
        if len(parents) != 1:
            raise _Refuse(
                f"head_sha: card {task_id} must have exactly one {want} parent card, "
                f"found {len(parents)}"
            )
        pmd = board.last_metadata(parents[0])
        theirs = pmd.get("head_sha") if isinstance(pmd, dict) else None
        ours = md["head_sha"]
        if not isinstance(theirs, str) or theirs.lower() != ours.lower():
            raise _Refuse(
                f"head_sha: {ours} does not match the head_sha {theirs!r} of {want} "
                f"card {parents[0]}'s last completed run"
            )

    # Plan: every card it names exists and carries a stage skill.
    if stage == "plan":
        for cid in md["cards"]:
            if _stage_of(board, cid, "plan") is None:
                raise _Refuse(f"plan: card {cid} carries no dev-team-stage-* skill")

    # 4. Exactly one child of the expected stage, parented on this card.
    child = expected_child(stage, md)
    if child is not None:
        created = args.get("created_cards")
        if created is None:
            created = []
        if not isinstance(created, list) or not all(isinstance(c, str) for c in created):
            raise _Refuse("created_cards: must be a list of card ids")
        matches = []
        for cid in dict.fromkeys(created):
            cview, cstage = _view_and_stage(board, cid, "created_cards")
            if cstage == child and task_id in cview.parents:
                matches.append(cid)
        if len(matches) != 1:
            raise _Refuse(
                f"child: expected exactly one card in created_cards with skill "
                f"dev-team-stage-{child} and {task_id} as a parent, found "
                f"{len(matches)}{' ' + str(matches) if matches else ''}"
            )

        # 5. Round limit.
        if child == "implement":
            rounds = implement_rounds(board, matches[0])
            if rounds > ROUND_LIMIT:
                raise _Refuse(
                    f"review: rounds-exhausted: card {matches[0]} would be implement "
                    f"round {rounds}, the limit is {ROUND_LIMIT}. Do not complete; "
                    f'kanban_block(kind="needs_input") with a reason starting '
                    f"`review: rounds-exhausted`."
                )

    # 6. CI claimed green: the guard re-reads it from the forge.
    if stage == "pr-ci" and md["ci"] == "green":
        sha = md["head_sha"]
        slug = project_slug(board, task_id)
        try:
            state = ci_status(slug, sha)
        except Exception as exc:
            raise _Refuse(
                f"forge: unreachable: cannot verify CI for {sha} ({_describe(exc)})"
            ) from None
        if state != "green":
            raise _Refuse(
                f"ci: forge reports {state!r} for {sha}, not 'green'; "
                f"completion with ci=green refused"
            )

    return None


# ---------------------------------------------------------------------------
# The terminal check.
#
# Commands are tokenised with shlex and matched on argv, never by a substring
# search of the raw string, so `git commit -m "gh pr merge"` is one argument and
# passes. Shell operators (; & | ( ) < > ` and newline) split the line into
# simple commands; `sh -c`, `eval`, `$(...)` and backticks are parsed again.
# Inside each simple command a rule matches at any argv position, so prefixes
# such as `FOO=1`, `sudo -u x`, `env`, `timeout 60` or `cd x &&` do not hide it.
# Matching errs toward blocking: this is a best-effort guard in front of an
# irreversible action (token scope is the real boundary, see the README).
# ---------------------------------------------------------------------------

MERGE_LABEL = "merge: human-only"

# basename -> [(group words or None, verbs)]. A rule matches when a positional
# argument after the tool is in `verbs` (after a `group` word, when given).
_ALWAYS_BLOCK: dict[str, list[tuple[frozenset[str] | None, frozenset[str]]]] = {
    "gh": [(frozenset({"pr"}), frozenset({"merge"}))],
    "glab": [(frozenset({"mr"}), frozenset({"merge"}))],
    "tea": [(frozenset({"pr", "prs", "pulls"}), frozenset({"merge"}))],
    "kubectl": [(None, frozenset({"apply", "rollout"}))],
    "helm": [(None, frozenset({"upgrade", "install"}))],
    "terraform": [(None, frozenset({"apply"}))],
    "docker": [(None, frozenset({"push"}))],
}

# `hermes kanban <verb>` that would move a guarded card without the
# `kanban_complete` hook (design: "The terminal is not a side door").
_SIDE_DOOR_VERBS = frozenset({"complete", "request-changes", "edit", "unblock", "reopen-review"})

# Branch names protected even when no project record says otherwise.
_ALWAYS_PROTECTED = frozenset({"main", "master"})

_OPERATOR_CHARS = ";&|()<>`\n"
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash"})
_MAX_NESTING = 4

# git global options that take a separate value (`git -C dir push ...`).
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
# `git push` options that take a separate value.
_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# `git push` options that update branches no refspec names.
_PUSH_ALL_OPTS = frozenset({"--all", "--mirror", "--branches", "--prune"})


def _basename(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _lex(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=_OPERATOR_CHARS)
    lex.whitespace = " \t\r"  # newline separates commands; it is an operator here
    lex.whitespace_split = True
    lex.commenters = ""  # keep `#...` as words: never let a comment swallow a newline
    return list(lex)


def _is_operator(token: str) -> bool:
    return bool(token) and all(c in _OPERATOR_CHARS for c in token)


def _split(tokens: Iterable[str]) -> list[list[str]]:
    out, cur = [], []
    for tok in tokens:
        if _is_operator(tok):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _simple_commands(command: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in `command`, nested scripts included.

    Raises ValueError when the command cannot be tokenised.
    """
    if depth > _MAX_NESTING:
        raise ValueError("command nests shells too deeply to check")
    out = []
    for argv in _split(_lex(command)):
        out.append(argv)
        for i, tok in enumerate(argv):
            base = _basename(tok)
            if base in _SHELLS:
                for j in range(i + 1, len(argv)):
                    flag = argv[j]
                    if not flag.startswith("-") or flag.startswith("--"):
                        break
                    if "c" in flag[1:]:
                        if j + 1 < len(argv):
                            out.extend(_simple_commands(argv[j + 1], depth + 1))
                        break
            elif base == "eval" and i + 1 < len(argv):
                out.extend(_simple_commands(" ".join(argv[i + 1:]), depth + 1))
            if "$(" in tok or "`" in tok:  # command substitution inside a quoted word
                inner = tok.replace("$(", " ( ").replace("`", " ` ")
                out.extend(_simple_commands(inner, depth + 1))
    return out


def _lenient_commands(command: str) -> list[list[str]]:
    """Fallback for a command shlex rejects: split on operators, whitespace and
    quote characters. Quoted text falls apart into words, which errs toward
    blocking; it is used only for the always-block rules."""
    out = []
    for part in re.split(r"[;&|()<>`\n]", command):
        argv = [t for t in re.split(r"[\s'\"\\]+", part) if t]
        if argv:
            out.append(argv)
    return out


def _positional(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]


def _merge_or_deploy(argv: list[str]) -> str | None:
    for i, tok in enumerate(argv):
        rules = _ALWAYS_BLOCK.get(_basename(tok))
        if not rules:
            continue
        pos = _positional(argv[i + 1:])
        for group, verbs in rules:
            start = 0
            if group is not None:
                hits = [k for k, p in enumerate(pos) if p in group]
                if not hits:
                    continue
                start = hits[0] + 1
            for p in pos[start:]:
                if p in verbs:
                    what = " ".join([_basename(tok)] + ([pos[start - 1]] if group else []) + [p])
                    return (
                        f"{MERGE_LABEL}: `{what}` merges or deploys. Merge and deploy are "
                        f"human-only; stop at a ready PR and leave it to the operator."
                    )
    return None


def _git_push_args(argv: list[str]) -> list[str] | None:
    """The arguments after `push` when argv contains a `git ... push`, else None."""
    for i, tok in enumerate(argv):
        if _basename(tok) != "git":
            continue
        j = i + 1
        while j < len(argv) and argv[j].startswith("-"):
            j += 2 if argv[j] in _GIT_VALUE_OPTS else 1
        if j < len(argv) and argv[j] == "push":
            return argv[j + 1:]
    return None


def _unverifiable_push(why: str) -> str:
    return (
        f"{MERGE_LABEL}: cannot tell which branch this `git push` updates ({why}). "
        f"Pushing to the default branch is human-only; name the destination branch, "
        f"e.g. `git push origin <feature-branch>`."
    )


def _push_destinations(args: list[str]) -> list[str] | str:
    """Destination branch names of a `git push`, or a refusal when they cannot
    be known from argv alone (no refspec, HEAD, matching, --all ...)."""
    positional, flags, repo_from_opt = [], set(), False
    j = 0
    while j < len(args):
        a = args[j]
        if a == "--":
            positional.extend(args[j + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            name = a.split("=", 1)[0]
            flags.add(name)
            if name == "--repo":
                repo_from_opt = True
            j += 2 if a in _PUSH_VALUE_OPTS else 1
            continue
        positional.append(a)
        j += 1
    if flags & _PUSH_ALL_OPTS:
        return _unverifiable_push(f"{sorted(flags & _PUSH_ALL_OPTS)[0]} pushes unnamed branches")
    refspecs = positional if repo_from_opt else positional[1:]
    if not refspecs:
        if "--tags" in flags:
            return []
        return _unverifiable_push("no refspec, so git picks the branch")
    dests = []
    for spec in refspecs:
        s = spec.lstrip("+")
        dst = s.split(":", 1)[1] if ":" in s else s
        if not dst:
            return _unverifiable_push(f"refspec {spec!r} has no destination")
        if dst.startswith("refs/heads/"):
            dst = dst[len("refs/heads/"):]
        elif dst.startswith("refs/"):
            continue  # tags, notes: not a branch
        if dst in ("HEAD", "@") or "*" in dst or "@{" in dst or not dst:
            return _unverifiable_push(f"refspec {spec!r} does not name the branch")
        dests.append(dst)
    return dests


def _side_door_verb(argv: list[str]) -> str | None:
    for i, tok in enumerate(argv):
        if _basename(tok) != "hermes":
            continue
        pos = _positional(argv[i + 1:])
        if "kanban" in pos:
            for p in pos[pos.index("kanban") + 1:]:
                if p in _SIDE_DOOR_VERBS:
                    return p
    return None


def _record_default_branch(slug: str) -> str:
    """`default_branch` of the project record `$HERMES_HOME/dev-team/projects/<slug>.json`,
    resolved the way `forge` resolves it."""
    if not slug or slug in (".", "..") or "/" in slug or "\\" in slug:
        raise ValueError(f"unsafe project slug {slug!r}")
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home) / "dev-team" / "projects" / f"{slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    branch = data.get("default_branch") if isinstance(data, dict) else None
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError(f"project record {path} has no default_branch")
    return branch.strip()


class _CardState:
    """Board facts the terminal check needs, read lazily and at most once, so a
    command that matches nothing never touches the board."""

    def __init__(self, board: Board, task_id: str | None):
        self.board, self.task_id = board, task_id
        self._guarded: bool | None = None
        self._protected: frozenset[str] | None = None

    def guarded(self) -> bool:
        if self._guarded is None:
            if self.task_id is None:
                self._guarded = False
            else:
                try:
                    view = self.board.task(self.task_id)
                    if view is None:
                        raise LookupError("not found on the board")
                    self._guarded = stage_from_skills(view.skills) is not None
                except Exception as exc:
                    raise _Refuse(
                        f"guard: cannot tell whether card {self.task_id} is a dev-team card, "
                        f"refusing ({_describe(exc)})"
                    ) from None
        return self._guarded

    def protected(self) -> frozenset[str]:
        if self._protected is None:
            names = set(_ALWAYS_PROTECTED)
            if self.guarded():
                slug = None
                try:
                    slug = project_slug(self.board, self.task_id)
                    names.add(_record_default_branch(slug))
                except Exception as exc:
                    raise _Refuse(
                        f"guard: cannot verify the default branch for card {self.task_id} "
                        f"(project {slug!r}), refusing the push ({_describe(exc)})"
                    ) from None
            self._protected = frozenset(n.casefold() for n in names)
        return self._protected


def check_terminal(board: Board, task_id: str | None, command: str) -> str | None:
    """Rules: merge and deploy commands always block; `git push` whose
    destination is the default branch (or cannot be read from argv) always
    blocks; on a guarded card, `hermes kanban complete|request-changes|edit|
    unblock|reopen-review` and an unparseable command block too."""
    try:
        return _check_terminal(board, task_id, command)
    except _Refuse as refusal:
        return str(refusal)
    except Exception as exc:  # fail closed
        return f"guard: cannot check the command, refusing ({_describe(exc)})"


def _check_terminal(board, task_id, command):
    if not isinstance(command, str):
        raise _Refuse("guard: terminal command is not a string, refusing")
    card = _CardState(board, task_id)
    parse_error = None
    try:
        commands = _simple_commands(command)
    except ValueError as exc:
        parse_error = exc
        commands = _lenient_commands(command)

    # Always-block rules first: they need no board.
    for argv in commands:
        msg = _merge_or_deploy(argv)
        if msg:
            return msg
    pushes = []
    for argv in commands:
        args = _git_push_args(argv)
        if args is not None:
            dests = _push_destinations(args)
            if isinstance(dests, str):
                return dests
            pushes.extend(dests)
    if pushes:
        protected = card.protected()
        for dst in pushes:
            if dst.casefold() in protected:
                return (
                    f"{MERGE_LABEL}: `git push` to {dst!r}, the default branch, is "
                    f"human-only. Push the feature branch and let the PR carry the change."
                )

    if parse_error is not None and card.guarded():
        raise _Refuse(
            f"guard: cannot parse the command on dev-team card {task_id} "
            f"({parse_error}); refusing. Quote it so it tokenises."
        )
    for argv in commands:
        verb = _side_door_verb(argv)
        if verb and card.guarded():
            raise _Refuse(
                f"guard: `hermes kanban {verb}` is not allowed on dev-team card {task_id}: "
                f"it would bypass the stage checks. Finish with the kanban_complete tool, "
                f"or stop with kanban_block."
            )
    return None


# ---------------------------------------------------------------------------
# The forge CI read and the pre_tool_call hook.
# ---------------------------------------------------------------------------

# Under the 30 s `plugins.hook_callback_timeout` that bounds the whole hook.
FORGE_TIMEOUT_S = 20

_NEGATIVE_CI = frozenset({"red", "pending", "none"})


def forge_ci_status(slug: str, sha: str) -> str:
    """`forge ci-status --slug <slug> --sha <sha>`: "green", or a negative state.

    Raises when forge is missing, times out, cannot verify (exit 2) or answers
    anything off the contract; check_complete turns that into `forge: unreachable`.
    """
    exe = shutil.which("forge")
    if not exe:
        raise FileNotFoundError("forge is not on PATH")
    proc = subprocess.run(
        [exe, "ci-status", "--slug", slug, "--sha", sha],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=FORGE_TIMEOUT_S,
    )
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"forge ci-status exit {proc.returncode}: output is not JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"forge ci-status exit {proc.returncode}: output is not an object")
    state = data.get("state")
    if proc.returncode == 0 and state == "green":
        return state
    if proc.returncode == 1 and state in _NEGATIVE_CI:
        return state
    if proc.returncode == 2:
        detail = str(data.get("error", ""))[:200]  # forge redacts before printing
        raise RuntimeError(f"forge ci-status cannot verify: {detail}")
    raise RuntimeError(f"forge ci-status exit {proc.returncode} with state {state!r}")


def _make_board(board: str | None) -> Board:
    return KanbanBoard(board)


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


def pre_tool_call(tool_name, args, **kw):
    """Hermes `pre_tool_call` hook. Only `kanban_complete` and `terminal` are
    checked; every other tool (`kanban_block` included) passes untouched."""
    if tool_name not in ("kanban_complete", "terminal"):
        return None
    try:
        args = args if isinstance(args, dict) else {}
        if tool_name == "kanban_complete":
            # Resolve the id the way kanban_complete does: args first, then the env.
            task_id = args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK")
            if not isinstance(task_id, str) or not task_id:
                return _block(
                    "guard: kanban_complete names no task_id and HERMES_KANBAN_TASK is "
                    "unset; cannot verify the card, refusing"
                )
            board = _make_board(args.get("board") or None)
            msg = check_complete(board, task_id, args, forge_ci_status)
        else:
            command = args.get("command")
            if command is None:
                return None
            task_id = os.environ.get("HERMES_KANBAN_TASK") or None
            msg = check_terminal(_make_board(None), task_id, command)
    except Exception as exc:  # fail closed, with our label rather than Hermes' generic one
        msg = f"guard: {tool_name} check failed, refusing ({_describe(exc)})"
    return _block(msg) if msg else None
