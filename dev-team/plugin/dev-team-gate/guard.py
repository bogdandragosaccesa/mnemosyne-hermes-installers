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

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.parse
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
    board: Board, task_id: str, args: dict, ci_status: Callable[[str, str], str],
    pr_status: Callable[[str, str], dict] | None = None,
) -> str | None:
    """Rules, in order: unguarded card passes; metadata valid; head_sha equals
    the parent stage's; exactly one right child; round limit; a green pr-ci is
    tied to its PR (in the project, head == head_sha) and CI is re-verified.

    `ci_status(slug, sha)` and `pr_status(slug, pr_url)` read the forge; without
    `pr_status` a green pr-ci is refused."""
    try:
        return _check(board, task_id, args, ci_status, pr_status)
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


def _check(board, task_id, args, ci_status, pr_status=None):
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

    # Plan: every card it names exists, is an implement card, and is a direct
    # child of this plan card.
    if stage == "plan":
        for cid in md["cards"]:
            cview, cstage = _view_and_stage(board, cid, "plan")
            if cstage != "implement":
                raise _Refuse(
                    f"plan: card {cid} carries {f'dev-team-stage-{cstage}' if cstage else 'no dev-team-stage-* skill'}"
                    f", not dev-team-stage-implement")
            if task_id not in cview.parents:
                raise _Refuse(f"plan: card {cid} is not a child of plan card {task_id}")

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

    # 6. CI claimed green: the guard re-reads it from the forge, tied to the PR.
    # In this order: the PR belongs to the project, its head is head_sha, CI on
    # that SHA is green, and the PR is marked ready.
    if stage == "pr-ci" and md["ci"] == "green":
        _check_green_pr(board, task_id, md, ci_status, pr_status)

    return None


def _canon_url(url: object) -> tuple | None:
    if not isinstance(url, str) or "://" not in url:
        return None
    parts = urllib.parse.urlsplit(url.strip())
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"))


def _check_green_pr(board, task_id, md, ci_status, pr_status):
    sha, claimed = md["head_sha"], md["pr_url"]
    slug = project_slug(board, task_id)
    if pr_status is None:
        raise _Refuse(f"forge: unreachable: no PR check available to verify {claimed}")
    try:
        pr = pr_status(slug, claimed)
    except Exception as exc:
        raise _Refuse(
            f"forge: unreachable: cannot read PR {claimed} of project {slug} ({_describe(exc)})"
        ) from None
    if not isinstance(pr, dict):
        raise _Refuse(f"forge: unreachable: the forge returned no PR facts for {claimed}")

    # (a) the PR belongs to the project: the project's own forge reports this
    # exact URL for the PR number.
    if _canon_url(pr.get("pr_url")) is None or _canon_url(pr.get("pr_url")) != _canon_url(claimed):
        raise _Refuse(
            f"pr: not-in-project: {claimed} is not a pull request of project {slug}; its "
            f"forge reports {pr.get('pr_url')!r} for that number"
        )
    # (a.5) the PR must be open: a closed or merged PR cannot be completed
    # green even if its head, CI and ready flag all still look fine (#7/N7).
    state = pr.get("state")
    if not isinstance(state, str) or state.strip().lower() not in ("open", "opened"):
        raise _Refuse(
            f"pr: not-open: {claimed} has state {pr.get('state')!r}, not open/opened; "
            f"completion with ci=green refused"
        )
    # (b) the PR's head is the SHA review approved and CI is read for.
    head = pr.get("head_sha")
    if not isinstance(head, str) or not head.lower().startswith(sha.lower()):
        raise _Refuse(
            f"pr: head-mismatch: the head of {claimed} is {head!r}, not head_sha {sha}; CI "
            f"on {sha} says nothing about this PR. Block rather than complete."
        )
    # (c) CI on that SHA.
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
    # (d) marked ready.
    if pr.get("ready") is not True:
        raise _Refuse(
            f"pr: not-ready: {claimed} is still a draft; run `forge mark-ready` and complete "
            f"again"
        )


# ---------------------------------------------------------------------------
# The terminal check.
#
# Commands are tokenised with shlex and matched on argv, never by a substring
# search of the raw string, so `git commit -m "gh pr merge"` is one argument and
# passes. Shell operators (; & | ( ) < > ` and newline) split the line into
# simple commands; `sh -c`, `eval`, `alias x=...`, `$(...)` and backticks are
# parsed again. Inside each simple command a rule matches at any argv position,
# so prefixes such as `FOO=1`, `sudo -u x`, `env`, `timeout 60` or `cd x &&` do
# not hide it.
#
# What cannot be read from argv is refused rather than guessed: a script fed to
# a shell or interpreter on stdin, a command name that is a shell expansion,
# `env -S`, shell function definitions, git config that can redirect a push or
# define an alias, an unknown git subcommand (it may be an alias).
#
# This is a best-effort guard in front of irreversible actions, and it only
# sees the `terminal` tool. Branch protection on the default branch is
# REQUIRED (see the README): it is the real boundary.
# ---------------------------------------------------------------------------

MERGE_LABEL = "merge: human-only"

# basename -> [(group words or None, verbs)]. A rule matches when a positional
# argument after the tool is in `verbs` (after a `group` word, when given).
_ALWAYS_BLOCK: dict[str, list[tuple[frozenset[str] | None, frozenset[str]]]] = {
    "gh": [(frozenset({"pr"}), frozenset({"merge"}))],
    "glab": [(frozenset({"mr"}), frozenset({"merge"}))],
    "tea": [(frozenset({"pr", "prs", "pulls"}), frozenset({"merge"}))],
    "kubectl": [
        (None, frozenset({"apply", "rollout", "delete", "patch", "replace", "scale", "edit"})),
        (frozenset({"set"}), frozenset({"image", "env", "resources", "selector",
                                        "serviceaccount", "subject"})),
    ],
    "helm": [(None, frozenset({"upgrade", "install", "rollback", "uninstall", "delete"}))],
    "terraform": [(None, frozenset({"apply", "destroy"}))],
    "tofu": [(None, frozenset({"apply", "destroy"}))],
    "docker": [(None, frozenset({"push"}))],
    "podman": [(None, frozenset({"push"}))],
}
# Image builders whose `build`/`bake` can push with a flag.
_IMAGE_BUILDERS = frozenset({"docker", "podman", "buildah", "nerdctl"})

# `hermes kanban <verb>` that would move a card (or release a needs_input
# block) without the `kanban_complete` hook (design: "The terminal is not a
# side door"). The target card ids are read with Hermes' own `kanban`
# argparse tree when `hermes_cli` can be imported (`_hermes_kanban_parser`);
# that is the only way to match argparse's option abbreviations (`--ti` means
# `--title`) exactly. Without it, `_SIDE_DOOR_VALUE_OPTS`/`_MULTI`/`_FLAG`
# below are an EXACT allowlist: any `--option` not listed there makes the
# whole call unparseable, so an abbreviation is refused rather than
# misread as a bare flag.
_SIDE_DOOR_VERBS = frozenset({
    "complete", "request-changes", "edit", "unblock", "reopen-review",
    "promote", "request-review", "archive", "assign", "reassign", "reclaim",
    "link", "unlink",
})
# Verbs with exactly one target positional (others, like `complete`, take
# every remaining positional as a target -- see `_SIDE_DOOR_TARGET_ATTRS` and
# the fallback parser).
_SIDE_DOOR_ONE_TARGET = frozenset({
    "request-changes", "edit", "request-review", "promote", "assign", "reassign", "reclaim",
})
# Verbs with exactly two positional targets (parent_id, child_id).
_SIDE_DOOR_TWO_TARGETS = frozenset({"link", "unlink"})
# Verbs that release a `needs_input` block the same way `unblock` does.
_SIDE_DOOR_NEEDS_INPUT_VERBS = frozenset({"unblock", "promote"})

# Real-parser path: the argparse attribute(s) holding target card ids, per verb.
_SIDE_DOOR_TARGET_ATTRS: dict[str, tuple[str, ...]] = {
    "complete": ("task_ids",),
    "request-changes": ("task_id",),
    "edit": ("task_id",),
    "unblock": ("task_ids",),
    "reopen-review": ("task_ids",),
    "promote": ("task_id", "ids"),
    "request-review": ("task_id",),
    "archive": ("task_ids", "purge_ids"),
    "assign": ("task_id",),
    "reassign": ("task_id",),
    "reclaim": ("task_id",),
    "link": ("parent_id", "child_id"),
    "unlink": ("parent_id", "child_id"),
}
# Fallback path: exact long options each verb accepts, by how they consume
# argv. `_VALUE`: one following word. `_MULTI`: every following word up to
# the next option (`--ids a b`). `_FLAG`: no value.
_SIDE_DOOR_VALUE_OPTS: dict[str, frozenset[str]] = {
    "complete": frozenset({"--result", "--summary", "--metadata"}),
    "request-changes": frozenset(),
    "edit": frozenset({"--title", "--body", "--priority", "--result", "--summary", "--metadata"}),
    "unblock": frozenset({"--reason"}),
    "reopen-review": frozenset({"--reason"}),
    "promote": frozenset(),
    "request-review": frozenset({"--summary", "--reviewer", "--metadata"}),
    "archive": frozenset(),
    "assign": frozenset(),
    "reassign": frozenset({"--reason"}),
    "reclaim": frozenset({"--reason"}),
    "link": frozenset(),
    "unlink": frozenset(),
}
_SIDE_DOOR_MULTI_OPTS: dict[str, frozenset[str]] = {
    "promote": frozenset({"--ids"}),
    "archive": frozenset({"--rm"}),
}
_SIDE_DOOR_FLAG_OPTS: dict[str, frozenset[str]] = {
    "complete": frozenset({"--force"}),
    "promote": frozenset({"--dry-run", "--json"}),
    "request-review": frozenset({"--force"}),
    "reassign": frozenset({"--reclaim"}),
}
_CARD_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

# Branch names protected even when no project record says otherwise.
_ALWAYS_PROTECTED = frozenset({"main", "master"})

_OPERATOR_CHARS = ";&|()<>`\n"
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish", "csh", "tcsh"})
_POWERSHELLS = frozenset({"pwsh", "powershell"})
_MAX_NESTING = 4
# Shell long options that take a separate value (`bash --rcfile f -c ...`).
_SHELL_LONG_VALUE_OPTS = frozenset({"--rcfile", "--init-file"})
# Arguments that make a shell or interpreter read its script from stdin.
_STDIN_PATHS = ("/dev/stdin", "/dev/fd/", "/proc/self/fd/")
# A push destination containing any of these is a shell expansion or a
# pattern, so the branch it names cannot be read from argv.
_EXPANSION_CHARS = frozenset("$`{}~?[*")

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Commands that run the rest of argv as a command, with their options that
# take a separate value. Used to find the command word of a simple command.
_WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T",
                       "--user", "--group", "--chdir", "--host", "--prompt"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "xargs": frozenset({"-I", "-n", "-L", "-P", "-d", "-E", "-s", "-a", "--max-args",
                        "--max-procs", "--delimiter", "--arg-file", "--replace"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "nohup": frozenset(), "exec": frozenset(), "command": frozenset(), "time": frozenset(),
    "setsid": frozenset(), "builtin": frozenset(), "unbuffer": frozenset(),
}

# git global options that take a separate value (`git -C dir push ...`).
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
# `git push` options that take a separate value.
_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# `git push` options that update branches no refspec names.
_PUSH_ALL_OPTS = frozenset({"--all", "--mirror", "--branches", "--prune"})
# git config whose value can change which branch a push updates, or what a git
# command runs: aliases, remote push refspecs, push.default, branch upstreams,
# included config files, URL rewrites.
_GIT_MAPPING_KEYS = ("alias.", "remote.", "push.", "branch.", "include.", "includeif.", "url.")
_GIT_MAPPING_SECTIONS = frozenset({"alias", "remote", "push", "branch", "include", "includeif", "url"})
_GIT_CONFIG_ENV = re.compile(r"^GIT_CONFIG\w*=", re.IGNORECASE)
# `git config` options that take a separate value.
_GIT_CONFIG_VALUE_OPTS = frozenset({"-f", "--file", "--blob", "--type", "--default", "--comment",
                                    "--value", "--fixed-value-of"})
_GIT_CONFIG_READ_OPTS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l",
                                   "--list", "--get-color", "--get-colorbool"})
_GIT_CONFIG_WRITE_OPTS = frozenset({"--add", "--replace-all", "--unset", "--unset-all",
                                    "--rename-section", "--remove-section"})
# Subcommands that update remote refs without `git push`'s refspec rules.
_GIT_REMOTE_WRITERS = frozenset({"send-pack", "http-push", "remote-ext", "remote-fd"})
# git subcommands (git --list-cmds=builtins,main, git 2.53). Anything else
# after `git` may be an alias, so it is refused: aliases cannot shadow these.
_GIT_BUILTINS = frozenset("""
add am annotate apply archive backfill bisect blame branch bugreport bundle cat-file check-attr
check-ignore check-mailmap check-ref-format checkout checkout--worker checkout-index cherry
cherry-pick citool clean clone column commit commit-graph commit-tree config count-objects
credential credential-cache credential-cache--daemon credential-store daemon describe diagnose
diff diff-files diff-index diff-pairs diff-tree difftool difftool--helper fast-export fast-import
fetch fetch-pack filter-branch fmt-merge-msg for-each-ref for-each-repo format-patch fsck
fsck-objects fsmonitor--daemon gc get-tar-commit-id grep gui hash-object help hook http-backend
http-fetch http-push imap-send index-pack init init-db instaweb interpret-trailers last-modified
log ls-files ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file
merge-index merge-octopus merge-one-file merge-ours merge-recursive merge-recursive-ours
merge-recursive-theirs merge-resolve merge-subtree merge-tree mergetool mktag mktree
multi-pack-index mv name-rev notes pack-objects pack-redundant pack-refs patch-id pickaxe prune
prune-packed pull push quiltimport range-diff read-tree rebase receive-pack reflog refs remote
remote-ext remote-fd remote-ftp remote-ftps remote-http remote-https repack replace replay repo
request-pull rerere reset restore rev-list rev-parse revert rm send-email send-pack shortlog show
show-branch show-index show-ref sparse-checkout stage stash status stripspace submodule
submodule--helper subtree switch symbolic-ref tag unpack-file unpack-objects update-index
update-ref update-server-info upload-archive upload-archive--writer upload-pack var
verify-commit verify-pack verify-tag version web--browse whatchanged worktree write-tree
""".split())
# External git commands known not to update branch refs.
_GIT_EXTERNAL_OK = frozenset({"lfs"})

# Interpreters: (inline-code flags, flags that run a module/file, value options).
_PYTHON = re.compile(r"^(python|pypy)[0-9.]*$")
_INTERPRETERS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    "python": (frozenset({"-c"}), frozenset({"-m"}), frozenset({"-W", "-X", "-Q"})),
    "perl": (frozenset({"-e", "-E"}), frozenset(), frozenset({"-I", "-M", "-m"})),
    "ruby": (frozenset({"-e"}), frozenset(), frozenset({"-I", "-r"})),
    "node": (frozenset({"-e", "--eval", "-p", "--print"}), frozenset(),
             frozenset({"-r", "--require", "--import"})),
    "php": (frozenset({"-r"}), frozenset({"-f"}), frozenset({"-d", "-c"})),
    "lua": (frozenset({"-e"}), frozenset(), frozenset({"-l"})),
}
_INTERPRETER_ALIASES = {"nodejs": "node", "deno": "node", "bun": "node", "luajit": "lua"}
# Inline interpreter code that mentions any of these may push, merge, move a
# card or touch a fence; it cannot be checked, so it is refused.
_SUSPECT_INLINE = re.compile(r"\b(push|merge|send-pack|kanban)\b|HERMES_", re.IGNORECASE)

# Forge CLI `api` calls and raw HTTP clients that write to a merge or ref endpoint.
_API_TOOLS = frozenset({"gh", "glab", "tea"})
_API_VALUE_OPTS = frozenset({"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input",
                             "-H", "--header", "-q", "--jq", "-t", "--template", "--hostname",
                             "-R", "--repo", "--cache", "-p", "--preview", "-d", "--data",
                             "--login", "--remote"})
_API_DATA_OPTS = frozenset({"-f", "-F", "--field", "--raw-field", "--input", "-d", "--data"})
_MERGE_OR_REF_PATH = re.compile(r"(?:^|/)merges?(?=$|[/?#])|git/refs|/contents/", re.IGNORECASE)
_GRAPHQL_WRITE = re.compile(r"merge|updateRefs?|createRef|deleteRef|createCommitOnBranch", re.IGNORECASE)
_CURL_VALUE_OPTS = frozenset({"-X", "--request", "-d", "--data", "--data-raw", "--data-binary",
                              "--data-urlencode", "--json", "-F", "--form", "-T", "--upload-file",
                              "-H", "--header", "-o", "--output", "-u", "--user", "--url",
                              "-A", "--user-agent", "-e", "--referer", "-b", "--cookie",
                              "-c", "--cookie-jar", "-m", "--max-time", "--connect-timeout"})
_CURL_DATA_OPTS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
                             "--json", "-F", "--form", "-T", "--upload-file"})
# curl short option letters that take a value (joined in the cluster, as in
# `-sXPUT`, or the next word, as in `-sd '{}'`); every other letter in a
# cluster is a bare flag (`-s`, `-S`, `-L`, `-k`, `-v`, `-i`, ...).
_CURL_SHORT_VALUE = frozenset("XdFTHoeAucbm")
_CURL_SHORT_TO_LONG = {
    "X": "-X", "d": "-d", "F": "-F", "T": "-T", "H": "-H", "o": "-o", "e": "-e",
    "A": "-A", "u": "-u", "c": "-c", "b": "-b", "m": "-m",
}

# Fences: HERMES_* variables carry the worker's identity and Hermes' own
# guards (HERMES_KANBAN_TASK, HERMES_DELEGATED_CHILD_CONTEXT, ...).
_HERMES_ASSIGN = re.compile(r"^HERMES_[A-Za-z0-9_]*=", re.IGNORECASE)
_HERMES_NAME = re.compile(r"^HERMES_", re.IGNORECASE)
_ENV_DECLARERS = frozenset({"unset", "export", "declare", "typeset", "local", "readonly"})
_RAW_FENCE = re.compile(
    r"\$env:HERMES_\w*\s*=(?!=)"
    r"|\b(?:remove-item|ri|rm|del|erase|clear-item|set-item|new-item)\b[^;|\n]*\benv:\\?HERMES_"
    r"|SetEnvironmentVariable\s*\(\s*['\"]HERMES_",
    re.IGNORECASE)
# Shell function definitions: `f() {` or `function f`. The body runs under a
# name the guard cannot follow.
_RAW_FUNCTION = re.compile(
    r"(?:^|[;&|\n({]\s*)(?:function\s+[\w.:-]+|[\w.:-]+\s*\(\s*\))", re.MULTILINE)
# A command substitution in command position: `$(echo git) push ...`.
_RAW_SUBST_COMMAND = re.compile(
    r"(?:^|[;&|\n(]\s*)(?:[A-Za-z_]\w*=\S*\s+)*(?:\$\(|`)", re.MULTILINE)

# Paths the guard trusts (#4): the forge code it imports, the `forge` wrapper
# agents call, the plugin itself (and its link in each profile), and the
# project records. Matched on any argv word or redirection target.
_PROTECTED_PATHS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\.local/bin/forge(?![\w.-])",
    r"forge\.cmd(?![\w.-])",
    r"dev-team/projects(?![\w.-])",
    r"plugins/dev-team-gate(?![\w.-])",
))
# `dev-team/forge` and `dev-team/plugin` are also the real paths of two
# directories any clone of THIS repo has (the team works on the guard and
# the forge adapters here), so an unanchored substring match over-blocks
# ordinary development (N5): a commit message, `git diff -- dev-team/forge/`,
# `python3 -m unittest discover -s dev-team/forge/tests`, `git add
# dev-team/plugin/...`. Require a path separator right before the match (an
# absolute path, `~/...`, `$HOME/...`, `$HERMES_HOME/...`, or any other
# nested path) so a bare relative word at the start of the argv token is
# never enough, while `cp x ~/.hermes/dev-team/forge/forge_core.py` still is.
_PROTECTED_PATHS_ANCHORED = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"dev-team/plugin(?:/|$)",
    r"dev-team/forge(?![\w.-])",
))
# Commands that only read what they are given. Anything else naming a
# protected path is refused.
_READ_ONLY_COMMANDS = frozenset({
    "cat", "ls", "head", "tail", "less", "more", "stat", "file", "wc", "grep", "egrep",
    "fgrep", "rg", "jq", "diff", "cmp", "sha256sum", "sha1sum", "md5sum", "b2sum", "test",
    "[", "readlink", "realpath", "dirname", "basename", "echo", "printf", "du", "tree",
    "forge", "get-content", "gc", "get-childitem", "select-string", "get-filehash",
    "test-path",
})
_PLUGIN_REMOVERS = frozenset({"disable", "remove", "rm", "uninstall", "unlink", "delete"})


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


def _split(tokens: Iterable[str], redirects: list | None = None) -> list[list[str]]:
    """Simple commands, split on shell operators. A redirection's target
    (`> file`, `2>&1`, `<<< 'text'`, `<<EOF`) is not a command: it goes to
    `redirects` as (operator, target)."""
    out, cur, pending = [], [], None
    for tok in tokens:
        if _is_operator(tok):
            redirect = "<" in tok or ">" in tok
            separates = any(c in tok for c in ";|()\n") or ("&" in tok and not redirect)
            if separates or (redirect and "(" in tok):  # `<(...)` is a new command
                if cur:
                    out.append(cur)
                cur, pending = [], None
            if redirect and "(" not in tok:
                pending = tok
            continue
        if pending is not None:
            if redirects is not None:
                redirects.append((pending, tok))
            pending = None
            continue
        cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _shell_c_script(args: list[str]) -> str | None:
    """The script of `sh/bash [options] -c script`, else None.

    Options may come in any order: long options (`--login`, `--norc`), option
    values (`-o pipefail`, `-O extglob`, `+o posix`) and short clusters
    (`-lc`, `-ec`). As in bash, the script is the first non-option argument
    once a cluster containing `c` has been seen.
    """
    saw_c = False
    j = 0
    while j < len(args):
        a = args[j]
        if a == "--":
            j += 1
            break
        if a.startswith("--"):
            j += 2 if a in _SHELL_LONG_VALUE_OPTS else 1
            continue
        if len(a) > 1 and a[0] in "-+":
            cluster = a[1:]
            if a[0] == "-" and "c" in cluster:
                saw_c = True
            j += 2 if ("o" in cluster or "O" in cluster) else 1
            continue
        break
    if saw_c and j < len(args):
        return args[j]
    return None


def _shell_reads_stdin(args: list[str]) -> bool:
    """True when `sh/bash [options] [script]` (no -c) reads its script from stdin."""
    j = 0
    while j < len(args):
        a = args[j]
        if a == "-":
            return True
        if a == "--":
            return j + 1 >= len(args) or args[j + 1].startswith(_STDIN_PATHS)
        if a.startswith("--"):
            j += 2 if a in _SHELL_LONG_VALUE_OPTS else 1
            continue
        if len(a) > 1 and a[0] in "-+":
            if a[0] == "-" and "s" in a[1:]:
                return True
            j += 2 if ("o" in a[1:] or "O" in a[1:]) else 1
            continue
        return a.startswith(_STDIN_PATHS)  # a script file, unless it is stdin
    return True


def _powershell_script(args: list[str]) -> tuple[str, str | None]:
    """("command", script) | ("stdin", None) | ("encoded", None) | ("file", None)."""
    for j, a in enumerate(args):
        low = a.lower()
        if low in ("-encodedcommand", "-enc", "-ec", "-e"):
            return "encoded", None
        if low in ("-command", "-c"):
            rest = args[j + 1:]
            if not rest or rest[0] == "-":
                return "stdin", None
            return "command", " ".join(rest)
        if low in ("-file", "-f"):
            return "file", None
        if not a.startswith("-"):
            return "command", " ".join(args[j:])
    return "stdin", None


def _interpreter(base: str) -> str | None:
    if _PYTHON.match(base):
        return "python"
    base = _INTERPRETER_ALIASES.get(base, base)
    return base if base in _INTERPRETERS else None


def _interpreter_mode(kind: str, args: list[str]) -> tuple[str, str | None]:
    """("stdin"|"file", None) or ("inline", code) for `python3 [options] ...`."""
    inline, module, values = _INTERPRETERS[kind]
    j = 0
    while j < len(args):
        a = args[j]
        if a == "-" or a.startswith(_STDIN_PATHS):
            return "stdin", None
        if a == "--":
            return ("file", None) if j + 1 < len(args) else ("stdin", None)
        if a.startswith("-") and len(a) > 1:
            name = a.split("=", 1)[0]
            if name in inline:
                code = a.split("=", 1)[1] if "=" in a else (args[j + 1] if j + 1 < len(args) else "")
                return "inline", code
            if not a.startswith("--"):
                # a short cluster such as `-uc` or `-pe`: the first inline flag
                # letter in it takes the rest of the cluster, or the next word
                for k, ch in enumerate(a[1:], 1):
                    if f"-{ch}" in inline:
                        code = a[k + 1:] or (args[j + 1] if j + 1 < len(args) else "")
                        return "inline", code
                    if f"-{ch}" in module:
                        return "file", None
            if name in module:
                return "file", None
            j += 2 if a in values else 1
            continue
        return "file", None
    return "stdin", None


def _command_index(argv: list[str]) -> int | None:
    """Index of the command word of a simple command: past `VAR=value`
    assignments and wrappers such as `sudo -u x`, `env`, `timeout 60`, `xargs`."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if _ASSIGNMENT.match(tok):
            i += 1
            continue
        base = _basename(tok)
        values = _WRAPPER_VALUE_OPTS.get(base)
        if values is None:
            return i
        i += 1
        while i < len(argv):
            a = argv[i]
            if a == "--":
                i += 1
                break
            if a.startswith("-") and len(a) > 1:
                i += 2 if a in values else 1
                continue
            if base == "timeout" and a[:1].isdigit():
                i += 1
            break
    return None


def _simple_commands(command: str, depth: int = 0,
                     redirects: list | None = None) -> list[list[str]]:
    """Every simple command in `command`, nested scripts included.

    Raises ValueError when the command cannot be tokenised.
    """
    if depth > _MAX_NESTING:
        raise ValueError("command nests shells too deeply to check")
    out = []
    for argv in _split(_lex(command), redirects):
        out.append(argv)
        for i, tok in enumerate(argv):
            base = _basename(tok)
            if base in _SHELLS:
                script = _shell_c_script(argv[i + 1:])
                if script is not None:
                    out.extend(_simple_commands(script, depth + 1, redirects))
            elif base in _POWERSHELLS:
                kind, script = _powershell_script(argv[i + 1:])
                if kind == "command" and script:
                    out.extend(_simple_commands(script, depth + 1, redirects))
            elif base == "eval" and i + 1 < len(argv):
                out.extend(_simple_commands(" ".join(argv[i + 1:]), depth + 1, redirects))
            elif base == "alias":
                for definition in argv[i + 1:]:
                    if "=" in definition:
                        out.extend(_simple_commands(definition.split("=", 1)[1], depth + 1, redirects))
            if "$(" in tok or "`" in tok:  # command substitution inside a quoted word
                inner = tok.replace("$(", " ( ").replace("`", " ` ")
                out.extend(_simple_commands(inner, depth + 1, redirects))
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


def _merge_label(what: str) -> str:
    return (
        f"{MERGE_LABEL}: `{what}` merges or deploys. Merge and deploy are "
        f"human-only; stop at a ready PR and leave it to the operator."
    )


def _merge_or_deploy(argv: list[str]) -> str | None:
    for i, tok in enumerate(argv):
        base = _basename(tok)
        if base in _IMAGE_BUILDERS:
            msg = _image_push(base, argv[i + 1:])
            if msg:
                return msg
        rules = _ALWAYS_BLOCK.get(base)
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
                    return _merge_label(" ".join([base] + ([pos[start - 1]] if group else []) + [p]))
    return None


def _image_push(base: str, args: list[str]) -> str | None:
    """`docker [buildx] build|bake --push` and `--output type=registry|push=true`."""
    pos = _positional(args)
    if "build" not in pos and "bake" not in pos:
        return None
    for j, a in enumerate(args):
        value = None
        if a == "--push" or a.startswith("--push="):
            return _merge_label(f"{base} build --push")
        if a in ("-o", "--output"):
            value = args[j + 1] if j + 1 < len(args) else ""
        elif a.startswith("--output="):
            value = a.split("=", 1)[1]
        elif a.startswith("-o") and len(a) > 2:
            value = a[2:]
        if value and re.search(r"type=registry|push=true", value, re.IGNORECASE):
            return _merge_label(f"{base} build --output {value}")
    return None


def _api_write(argv: list[str]) -> str | None:
    """`gh|glab|tea api` and curl/wget writes to a merge or git-ref endpoint."""
    for i, tok in enumerate(argv):
        base = _basename(tok)
        if base in _API_TOOLS and "api" in argv[i + 1:]:
            args = argv[i + 1:]
            args = args[args.index("api") + 1:]
            method, data, endpoint, rest = None, False, None, []
            j = 0
            while j < len(args):
                a = args[j]
                name = a.split("=", 1)[0] if a.startswith("--") else a
                if a.startswith("-") and len(a) > 1:
                    joined = "=" in a if a.startswith("--") else (len(a) > 2 and a[:2] in _API_VALUE_OPTS)
                    short = a[:2] if not a.startswith("--") else name
                    value = (a.split("=", 1)[1] if a.startswith("--") else a[2:]) if joined else (
                        args[j + 1] if j + 1 < len(args) else "")
                    if short in ("-X", "--method"):
                        method = value
                    if short in _API_DATA_OPTS:
                        data = True
                    rest.append(value)
                    j += 1 if joined or short not in _API_VALUE_OPTS else 2
                    continue
                if endpoint is None:
                    endpoint = a
                rest.append(a)
                j += 1
            writes = method.upper() not in ("GET", "HEAD") if method else data
            if not writes or endpoint is None:
                continue
            if endpoint.lower() == "graphql":
                if any(_GRAPHQL_WRITE.search(x) for x in rest) or "--input" in args:
                    return _merge_label(f"{base} api graphql (merge or ref mutation)")
                continue
            if _EXPANSION_CHARS & set(endpoint) or _MERGE_OR_REF_PATH.search(endpoint):
                return _merge_label(f"{base} api {method or 'POST'} {endpoint}")
        elif base in ("curl", "wget"):
            msg = _http_write(base, argv[i + 1:])
            if msg:
                return msg
    return None


def _http_write(base: str, args: list[str]) -> str | None:
    method, data, urls = None, False, []
    j = 0
    while j < len(args):
        a = args[j]
        if base == "wget":
            if a.startswith("--method="):
                method = a.split("=", 1)[1]
            elif a.startswith(("--post-data", "--post-file", "--body-data", "--body-file")):
                data = True
            elif not a.startswith("-"):
                urls.append(a)
            j += 1
            continue
        if a.startswith("--"):
            if "=" in a:
                name, value = a.split("=", 1)
            else:
                name, value = a, (args[j + 1] if j + 1 < len(args) else "")
            if name in ("-X", "--request"):
                method = value
            if name in _CURL_DATA_OPTS:
                data = True
            if name == "--url":
                urls.append(value)
            j += 1 if "=" in a else (2 if name in _CURL_VALUE_OPTS else 1)
            continue
        if a.startswith("-") and len(a) > 1:
            # A short option cluster, walked letter by letter (`-sXPUT`,
            # `-sd '{}'`): the first value-taking letter consumes the rest of
            # the cluster, or the next word when the cluster ends there.
            consumed_next = False
            for k, ch in enumerate(a[1:], 1):
                if ch in _CURL_SHORT_VALUE:
                    value = a[k + 1:]
                    if not value and j + 1 < len(args):
                        value = args[j + 1]
                        consumed_next = True
                    name = _CURL_SHORT_TO_LONG[ch]
                    if name == "-X":
                        method = value
                    if name in _CURL_DATA_OPTS:
                        data = True
                    break
            j += 2 if consumed_next else 1
            continue
        urls.append(a)
        j += 1
    writes = method.upper() not in ("GET", "HEAD") if method else data
    if writes and any(_MERGE_OR_REF_PATH.search(u) for u in urls):
        return _merge_label(f"{base} {method or 'POST'} to a merge or git-ref endpoint")
    return None


def _gh_alias_merge(argv: list[str]) -> str | None:
    """`gh alias set NAME 'pr merge ...'`: the alias body is never run by the
    guard, so a merge hidden behind an alias name is refused at definition
    time (N4)."""
    for i, tok in enumerate(argv):
        if _basename(tok) != "gh":
            continue
        pos = _positional(argv[i + 1:])
        if pos[:2] == ["alias", "set"] and len(pos) > 2:
            body = " ".join(pos[3:])
            if re.search(r"\bpr\s+merge\b", body, re.IGNORECASE):
                return _merge_label(f"gh alias set {pos[2]} '{body}'")
    return None


def _git_calls(argv: list[str]) -> list[tuple[int, list[str], str | None, list[str], bool]]:
    """Every git invocation in argv: (index, `-c` config entries, subcommand,
    arguments after it, dashed). `/usr/lib/git-core/git-push` counts as
    `git push` (dashed=True)."""
    out = []
    for i, tok in enumerate(argv):
        base = _basename(tok)
        if base == "git":
            j, config = i + 1, []
            while j < len(argv) and argv[j].startswith("-"):
                a = argv[j]
                if a in ("-c", "--config-env"):
                    if j + 1 < len(argv):
                        config.append(argv[j + 1])
                    j += 2
                    continue
                if a.startswith("--config-env="):
                    config.append(a.split("=", 1)[1])
                elif a[:2] == "-c" and len(a) > 2:
                    config.append(a[2:])
                j += 2 if a in _GIT_VALUE_OPTS else 1
            sub = argv[j] if j < len(argv) else None
            out.append((i, config, sub, argv[j + 1:], False))
        elif base.startswith("git-") and len(base) > 4:
            out.append((i, [], base[4:], argv[i + 1:], True))
    return out


def _unverifiable_push(why: str) -> str:
    return (
        f"{MERGE_LABEL}: cannot tell which branch this `git push` updates ({why}). "
        f"Pushing to the default branch is human-only; name the destination branch, "
        f"e.g. `git push origin <feature-branch>`."
    )


def _git_refusal(what: str, why: str) -> str:
    return (
        f"{MERGE_LABEL}: `{what}` is refused: {why}. Pushing to the default branch "
        f"is human-only; use a plain `git push origin <feature-branch>`."
    )


def _config_key_redirects(key: str) -> bool:
    key = key.split("=", 1)[0].strip().lower()
    return key.startswith(_GIT_MAPPING_KEYS) or key in _GIT_MAPPING_SECTIONS


def _git_config_refusal(args: list[str]) -> str | None:
    """`git config` that writes a key able to redirect a push or define an alias."""
    pos, reads, writes = [], False, False
    j = 0
    while j < len(args):
        a = args[j]
        if a in ("-e", "--edit"):
            return _git_refusal("git config --edit", "an editor session can rewrite any key")
        if a.startswith("-"):
            name = a.split("=", 1)[0]
            reads = reads or name in _GIT_CONFIG_READ_OPTS
            writes = writes or name in _GIT_CONFIG_WRITE_OPTS
            j += 2 if a in _GIT_CONFIG_VALUE_OPTS else 1
            continue
        pos.append(a)
        j += 1
    if pos and pos[0] in ("get", "list"):
        return None
    if pos and pos[0] == "edit":
        return _git_refusal("git config edit", "an editor session can rewrite any key")
    if pos and pos[0] in ("set", "unset", "rename-section", "remove-section"):
        keys = pos[1:3] if pos[0] == "rename-section" else pos[1:2]
    elif writes:
        keys = pos[:2] if "--rename-section" in args else pos[:1]
    elif reads or len(pos) < 2:
        return None
    else:
        keys = pos[:1]
    for key in keys:
        if _config_key_redirects(key):
            return _git_refusal(f"git config {key}", "this key can redirect what a push "
                                "updates or define a git alias")
    return None


def _branch_candidates(dst: str) -> list[str] | None:
    """Branch names a push destination may update, the way git resolves it:
    `refs/heads/X` is X; an unqualified `heads/X` matches `refs/heads/X` (rule
    `refs/%s`) or a branch literally named `heads/X`; other `refs/...` are not
    branches."""
    if dst.startswith("refs/"):
        rest = dst[len("refs/"):]
        return [rest[len("heads/"):]] if rest.startswith("heads/") else None
    out = [dst]
    if dst.startswith("heads/"):
        out.append(dst[len("heads/"):])
    return out


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
        if _EXPANSION_CHARS & set(dst):
            return _unverifiable_push(f"refspec {spec!r} names the branch by a shell expansion")
        names = _branch_candidates(dst)
        if names is None:
            continue  # tags, notes: not a branch
        for name in names:
            if name in ("HEAD", "@") or "@{" in name or not name:
                return _unverifiable_push(f"refspec {spec!r} does not name the branch")
        dests.extend(names)
    return dests


def _git_rules(argv: list[str], cmd_index: int | None) -> list[str] | str:
    """Push destinations named in argv, or a refusal."""
    for tok in argv:
        if _GIT_CONFIG_ENV.match(tok):
            name = tok.split("=", 1)[0]
            return _git_refusal(name, "GIT_CONFIG_* variables inject config, including "
                                "aliases and push refspecs")
    dests: list[str] = []
    for i, config, sub, args, dashed in _git_calls(argv):
        for entry in config:
            if _config_key_redirects(entry):
                key = entry.split("=", 1)[0]
                return _git_refusal(f"git -c {key}", "this key can redirect what a push "
                                    "updates or define a git alias")
        if sub is None:
            continue
        if any(c in sub for c in "$`") or _EXPANSION_CHARS & set(sub):
            return _git_refusal(f"git {sub}", "the subcommand is a shell expansion")
        if sub in _GIT_REMOTE_WRITERS:
            return _git_refusal(f"git {sub}", "it updates remote refs directly")
        if sub == "subtree" and "push" in _positional(args):
            return _git_refusal("git subtree push", "it pushes to a branch the guard cannot "
                                "check")
        if sub == "config":
            msg = _git_config_refusal(args)
            if msg:
                return msg
        if sub == "branch":
            for a in args:
                if a in ("-u", "--set-upstream", "--set-upstream-to") or a.startswith(
                        "--set-upstream-to="):
                    return _git_refusal("git branch --set-upstream-to", "with push.default="
                                        "upstream a plain push follows the upstream branch")
        if sub == "push":
            if any(_basename(t) in ("xargs", "parallel") for t in argv[:i]):
                return _unverifiable_push("xargs may append refspecs")
            found = _push_destinations(args)
            if isinstance(found, str):
                return found
            dests.extend(found)
        elif (not dashed and i == cmd_index and sub not in _GIT_BUILTINS
              and sub not in _GIT_EXTERNAL_OK):
            return _git_refusal(f"git {sub}", "it is not a git builtin, so it may be an alias "
                                "for push")
    return dests


def _unverifiable(what: str) -> str:
    return (
        f"guard: {what}, so the guard cannot see what it runs; refusing. Run the "
        f"command directly (sh -c '...' is fine), with no stdin script or expansion."
    )


def _env_refusal(args: list[str]) -> str | None:
    """`env` options: -S (split string), -i / - (clear the environment), -u HERMES_*."""
    j = 0
    while j < len(args):
        a = args[j]
        if _ASSIGNMENT.match(a):
            j += 1
            continue
        if not a.startswith("-"):
            return None
        if a == "-" or a == "--ignore-environment":
            return _fence_refusal("env -i clears HERMES_* variables")
        if a.startswith("--split-string"):
            return _unverifiable("`env -S` re-splits a string into a command")
        unset = None
        if a == "--unset" or a == "-u":
            unset = args[j + 1] if j + 1 < len(args) else ""
            j += 1
        elif a.startswith("--unset="):
            unset = a.split("=", 1)[1]
        elif not a.startswith("--"):
            cluster = a[1:]
            for k, ch in enumerate(cluster):
                if ch == "S":
                    return _unverifiable("`env -S` re-splits a string into a command")
                if ch == "i":
                    return _fence_refusal("env -i clears HERMES_* variables")
                if ch in "uC":
                    value = cluster[k + 1:] or (args[j + 1] if j + 1 < len(args) else "")
                    if not cluster[k + 1:]:
                        j += 1
                    if ch == "u":
                        unset = value
                    break
        if unset is not None and _HERMES_NAME.match(unset):
            return _fence_refusal(f"env -u {unset}")
        j += 1
    return None


def _fence_refusal(what: str) -> str:
    return (
        f"guard: `{what}` removes or overrides a Hermes fence (HERMES_* variables carry "
        f"this worker's identity and Hermes' own guards); refusing. A fence that blocks "
        f"you is a reason to kanban_block(kind=\"needs_input\") and ask, never something "
        f"to remove."
    )


def _fence(argv: list[str]) -> str | None:
    for i, tok in enumerate(argv):
        if _HERMES_ASSIGN.match(tok):
            return _fence_refusal(tok)
        base = _basename(tok)
        if base in _ENV_DECLARERS:
            for a in argv[i + 1:]:
                if _HERMES_NAME.match(a):
                    return _fence_refusal(f"{base} {a}")
        elif base == "env":
            msg = _env_refusal(argv[i + 1:])
            if msg:
                return msg
    return None


def _shell_bypass(argv: list[str], cmd_index: int | None) -> str | None:
    if cmd_index is None:
        return None
    word = argv[cmd_index]
    if "$" in word or "`" in word:
        return _unverifiable(f"the command name `{word}` is a shell expansion")
    base = _basename(word)
    rest = argv[cmd_index + 1:]
    if base in _SHELLS:
        if _shell_c_script(rest) is None and _shell_reads_stdin(rest):
            return _unverifiable(f"`{base}` reads its script from stdin or a here-string")
    elif base in _POWERSHELLS:
        kind, _ = _powershell_script(rest)
        if kind in ("stdin", "encoded"):
            return _unverifiable(f"`{base}` runs an {kind} script")
    elif base in ("source", "."):
        if not rest or rest[0].startswith(_STDIN_PATHS):
            return _unverifiable(f"`{base}` reads its script from stdin or a here-string")
    else:
        kind = _interpreter(base)
        if kind:
            mode, code = _interpreter_mode(kind, rest)
            if mode == "stdin":
                return _unverifiable(f"`{base}` reads its script from stdin or a here-string")
            if mode == "inline" and code and _SUSPECT_INLINE.search(code):
                return _unverifiable(f"`{base}` inline code mentions push/merge/kanban/HERMES_")
    return None


def _protected_path(token: str) -> bool:
    """True when `token` names a path under a trusted location (#4).

    `_PROTECTED_PATHS` matches anywhere (as before): those filenames
    (`.local/bin/forge`, `forge.cmd`, `dev-team/projects`,
    `plugins/dev-team-gate`) do not collide with ordinary relative
    development paths. `_PROTECTED_PATHS_ANCHORED` additionally requires a
    path separator right before the match, since `dev-team/forge` and
    `dev-team/plugin` are also real relative paths inside any clone of this
    repo (N5): see the comment above `_PROTECTED_PATHS_ANCHORED`.
    """
    text = token.replace("\\", "/")
    if any(p.search(text) for p in _PROTECTED_PATHS):
        return True
    for pattern in _PROTECTED_PATHS_ANCHORED:
        for m in pattern.finditer(text):
            if m.start() > 0 and text[m.start() - 1] == "/":
                return True
    return False


def _trust_refusal(what: str) -> str:
    return (
        f"guard: `{what}` writes into a path the guard trusts (the forge code and wrapper, "
        f"the dev-team-gate plugin, or the project records); refusing. Only the operator "
        f"changes those. A fence that blocks you is a reason to block and ask."
    )


def _protected_write(argv: list[str], cmd_index: int | None) -> str | None:
    """A command naming a trusted path that is not a read-only command, or one
    that tampers with the forge or the plugin through their own CLIs."""
    for i, tok in enumerate(argv):
        base = _basename(tok)
        rest = argv[i + 1:]
        if base in ("forge", "forge.py") and "detect" in rest and "--force" in rest:
            return _trust_refusal("forge detect --force")
        if base == "hermes":
            pos = _positional(rest)
            if "plugins" in pos:
                verbs = pos[pos.index("plugins") + 1:]
                if verbs and verbs[0] in _PLUGIN_REMOVERS and "dev-team-gate" in verbs:
                    return _trust_refusal(f"hermes plugins {verbs[0]} dev-team-gate")
            if "config" in pos:
                verbs = pos[pos.index("config") + 1:]
                if verbs[:1] == ["edit"] or (
                        verbs[:1] in (["set"], ["unset"]) and len(verbs) > 1
                        and verbs[1].lower().startswith("plugins")):
                    return _trust_refusal("hermes config " + " ".join(verbs[:2]))
    hits = [t for t in argv if _protected_path(t)]
    if not hits:
        return None
    word = argv[cmd_index] if cmd_index is not None else argv[0]
    if _basename(word) in _READ_ONLY_COMMANDS and word not in hits:
        return None
    return _trust_refusal(f"{_basename(word)} {hits[0]}")


def _record_default_branch(slug: str) -> str:
    """`default_branch` of the project record `$HERMES_HOME/dev-team/projects/<slug>.json`,
    resolved the way `forge` resolves it."""
    if not slug or slug in (".", "..") or any(c in slug for c in "/\\\0"):
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


_KANBAN_ARGPARSER: object = None  # lazily built; False once import fails, None if an argparser


def _hermes_kanban_parser():
    """The real `hermes kanban` argparse tree (`hermes_cli.kanban_parser`),
    or None when `hermes_cli` (the agent's own CLI) cannot be imported. Built
    once: a throwaway top-level parser just holds the `kanban` subcommand so
    `parse_known_args` sees the exact same option abbreviations Hermes does."""
    global _KANBAN_ARGPARSER
    if _KANBAN_ARGPARSER is None:
        try:
            from hermes_cli import kanban_parser as kp
        except Exception:
            _KANBAN_ARGPARSER = False
        else:
            top = argparse.ArgumentParser(prog="hermes", add_help=False)
            kp.build_parser(top.add_subparsers(dest="_cmd"))
            _KANBAN_ARGPARSER = top
    return _KANBAN_ARGPARSER or None


def _hermes_entry_points(argv: list[str]) -> list[int]:
    """Index right before `kanban ...` may start: the `hermes` token itself,
    or the `hermes_cli.main` module name right after `python3 -m` (so
    `python3 -m hermes_cli.main kanban ...` is covered too)."""
    out = []
    for i, tok in enumerate(argv):
        base = _basename(tok)
        if base == "hermes":
            out.append(i)
        elif _interpreter(base) == "python":
            for j in range(i + 1, len(argv) - 1):
                if argv[j] == "-m" and argv[j + 1] == "hermes_cli.main":
                    out.append(j + 1)
                    break
    return out


def _side_door_via_real_parser(parser, argv_from_kanban: list[str]):
    """(verb, board, targets, handled) using Hermes' own argparse tree, or
    raises _Refuse when the call cannot be parsed at all (fail closed)."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            ns, _extra = parser.parse_known_args(argv_from_kanban)
    except SystemExit as exc:
        if exc.code in (0, None):  # --help or similar: nothing to check
            return None, None, [], False
        # A required positional missing (no reason, no task id, ...) is not
        # an option-abbreviation ambiguity: fall back to the strict allowlist
        # parse, which still finds the verb and whatever targets ARE given
        # and fails closed on any option it cannot place exactly.
        return _side_door_via_fallback(argv_from_kanban)
    verb = getattr(ns, "kanban_action", None)
    if verb not in _SIDE_DOOR_VERBS:
        return None, None, [], False
    board = getattr(ns, "board", None)
    targets: list[str] = []
    for attr in _SIDE_DOOR_TARGET_ATTRS[verb]:
        value = getattr(ns, attr, None)
        if value is None:
            continue
        targets.extend(str(v) for v in value) if isinstance(value, list) else targets.append(str(value))
    return verb, board, targets, True


def _side_door_via_fallback(argv_from_kanban: list[str]):
    """(verb, board, targets, handled): a strict, exact-allowlist parse used
    only when `hermes_cli` cannot be imported. Any `--option` not on the
    verb's exact allowlist makes the call unparseable (raises _Refuse):
    argparse's abbreviations (`--ti` means `--title`) cannot be replicated
    here, so an unrecognised option is never assumed to be a bare flag."""
    rest = argv_from_kanban[1:]  # past "kanban"
    board = None
    j = 0
    while j < len(rest) and rest[j].startswith("-") and rest[j] not in ("-h", "--help"):
        if rest[j] == "--board":
            board = rest[j + 1] if j + 1 < len(rest) else ""
            j += 2
            continue
        if rest[j].startswith("--board="):
            board = rest[j].split("=", 1)[1]
            j += 1
            continue
        raise _Refuse(
            f"guard: `hermes kanban` option `{rest[j]}` is not on the exact allowlist "
            f"(Hermes' own parser is unavailable to check it); refusing"
        )
    if j >= len(rest):
        return None, None, [], False
    verb = rest[j]
    if verb not in _SIDE_DOOR_VERBS:
        return None, None, [], False
    value_opts = _SIDE_DOOR_VALUE_OPTS.get(verb, frozenset())
    multi_opts = _SIDE_DOOR_MULTI_OPTS.get(verb, frozenset())
    flag_opts = _SIDE_DOOR_FLAG_OPTS.get(verb, frozenset())
    targets: list[str] = []
    k = j + 1
    while k < len(rest):
        a = rest[k]
        if a in ("-h", "--help"):
            return None, None, [], False
        if a == "--":
            targets.extend(rest[k + 1:])
            break
        if a.startswith("--"):
            name = a.split("=", 1)[0]
            if name in value_opts:
                k += 1 if "=" in a else 2
                continue
            if name in multi_opts:
                k += 1
                while k < len(rest) and not rest[k].startswith("-"):
                    targets.append(rest[k])
                    k += 1
                continue
            if name in flag_opts:
                k += 1
                continue
            raise _Refuse(
                f"guard: `hermes kanban {verb}` option `{name}` is not on the exact "
                f"allowlist (Hermes' own parser is unavailable to check an abbreviation "
                f"like this); refusing"
            )
        targets.append(a)
        k += 1
    if verb in _SIDE_DOOR_ONE_TARGET:
        targets = targets[:1]
    elif verb in _SIDE_DOOR_TWO_TARGETS:
        targets = targets[:2]
    return verb, board, targets, True


def _side_doors(argv: list[str]) -> list[tuple[str, str | None, list[str]]]:
    """(verb, board named in argv or None, target card ids) for every
    `hermes [opts] kanban [--board b] <verb> ...` in argv, `python3 -m
    hermes_cli.main kanban ...` included. Checks EVERY positional target, not
    just the first (N1)."""
    out = []
    parser = _hermes_kanban_parser()
    for i in _hermes_entry_points(argv):
        rest = argv[i + 1:]
        if "kanban" not in rest:
            continue
        argv_from_kanban = rest[rest.index("kanban"):]
        if parser is not None:
            verb, board, targets, handled = _side_door_via_real_parser(parser, argv_from_kanban)
        else:
            verb, board, targets, handled = _side_door_via_fallback(argv_from_kanban)
        if handled:
            out.append((verb, board, targets))
    return out


def _check_side_door(verb: str, board: Board, targets: list[str]) -> None:
    """Refuse a side-door verb unless every TARGET card can be read and is
    not a dev-team card (and, for `unblock`/`promote`, is not waiting on a
    human with kind `needs_input`: `promote` is also an unblock)."""
    if not targets:
        raise _Refuse(f"guard: cannot tell which card `hermes kanban {verb}` targets; refusing")
    for target in targets:
        if not _CARD_ID.match(target):
            raise _Refuse(
                f"guard: cannot tell which card `hermes kanban {verb} {target}` targets "
                f"(not a literal card id); refusing")
        try:
            view = board.task(target)
            if view is None:
                raise LookupError("not found on the board")
            stage = stage_from_skills(view.skills)
        except Exception as exc:
            raise _Refuse(
                f"guard: cannot verify card {target} for `hermes kanban {verb}`, refusing "
                f"({_describe(exc)})") from None
        if stage is not None:
            raise _Refuse(
                f"guard: `hermes kanban {verb}` is not allowed on dev-team card {target}: "
                f"it would bypass the stage checks. Finish with the kanban_complete tool, "
                f"or stop with kanban_block.")
        if verb in _SIDE_DOOR_NEEDS_INPUT_VERBS:
            if view.status is None:
                raise _Refuse(
                    f"guard: cannot tell how card {target} is blocked; refusing "
                    f"`hermes kanban {verb}`")
            if view.status == "blocked" and view.block_kind == "needs_input":
                raise _Refuse(
                    f"guard: card {target} is blocked waiting for a human (kind needs_input); "
                    f"only the operator releases it (`hermes kanban {verb}` refused). A fence "
                    f"that blocks you is a reason to block and ask, never something to remove.")


def check_terminal(
    board: Board, task_id: str | None, command: str,
    open_board: Callable[[str | None], Board] | None = None,
) -> str | None:
    """Rules: merge and deploy commands always block; `git push` whose
    destination is the default branch (or cannot be read from argv) always
    blocks, as do git config/alias tricks that could redirect it; commands the
    guard cannot see into (stdin scripts, expansions as the command name,
    `env -S`) block; overriding a HERMES_* variable blocks; `hermes kanban
    complete|request-changes|edit|unblock|reopen-review` blocks when its TARGET
    card is a dev-team card, cannot be read, or (unblock) waits on a human; on
    a guarded card an unparseable command blocks too.

    `open_board(name)` opens a board named in argv (`hermes kanban --board`);
    `board` is used when argv names none, or when no `open_board` is given
    (`pre_tool_call` always passes one)."""
    try:
        return _check_terminal(board, task_id, command, open_board)
    except _Refuse as refusal:
        return str(refusal)
    except Exception as exc:  # fail closed
        return f"guard: cannot check the command, refusing ({_describe(exc)})"


def _check_terminal(board, task_id, command, open_board=None):
    if not isinstance(command, str):
        raise _Refuse("guard: terminal command is not a string, refusing")
    card = _CardState(board, task_id)

    # Raw-text rules: things argv does not show.
    m = _RAW_FENCE.search(command)
    if m:
        return _fence_refusal(m.group(0).strip())
    if _RAW_FUNCTION.search(command):
        return _unverifiable("the command defines a shell function")
    if _RAW_SUBST_COMMAND.search(command):
        return _unverifiable("a command substitution is the command name")

    parse_error = None
    redirects: list = []
    try:
        commands = _simple_commands(command, redirects=redirects)
    except ValueError as exc:
        parse_error = exc
        commands = _lenient_commands(command)

    for op, target in redirects:
        if ">" in op and _protected_path(target):
            return _trust_refusal(f"{op} {target}")

    # Rules that need no board.
    pushes = []
    for argv in commands:
        cmd_index = _command_index(argv)
        for msg in (_fence(argv), _protected_write(argv, cmd_index), _merge_or_deploy(argv),
                    _api_write(argv), _gh_alias_merge(argv)):
            if msg:
                return msg
        found = _git_rules(argv, cmd_index)
        if isinstance(found, str):
            return found
        pushes.extend(found)
        msg = _shell_bypass(argv, cmd_index)
        if msg:
            return msg
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
        for verb, board_name, targets in _side_doors(argv):
            if board_name is None or open_board is None:
                target_board = board
            else:
                target_board = open_board(board_name)
            _check_side_door(verb, target_board, targets)
    return None


# ---------------------------------------------------------------------------
# The forge read and the pre_tool_call hook.
#
# The guard never runs whatever `forge` is on PATH: that file is owned by the
# same user the agents run as. It imports forge_core and the adapters by path
# from the `forge/` directory next to this plugin (dev-team/forge, reached
# through the plugin link's real path), under private module names so nothing
# on sys.path can stand in for them. Terminal writes into that directory, the
# plugin, the `forge` wrapper and the project records are refused (see
# _PROTECTED_PATHS).
# ---------------------------------------------------------------------------

# Under the 30 s `plugins.hook_callback_timeout` that bounds the whole hook;
# one budget covers every forge read of one kanban_complete.
FORGE_TIMEOUT_S = 20

PLUGIN_DIR = Path(__file__).resolve().parent
FORGE_DIR = PLUGIN_DIR.parent.parent / "forge"

_FORGE: tuple | None = None  # (forge_core module, adapter registry), loaded once
_FORGE_LOCK = threading.Lock()


def _import_path(name: str, path: Path, package: bool = False):
    spec = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=[str(path.parent)] if package else None)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _load_forge():
    """(forge_core, {vendor: adapter class}) imported by path from FORGE_DIR."""
    global _FORGE
    with _FORGE_LOCK:
        if _FORGE is None:
            if not (FORGE_DIR / "forge_core.py").is_file():
                raise FileNotFoundError(
                    f"forge not found next to the plugin at {FORGE_DIR} (a copied, not "
                    f"linked, plugin cannot verify CI)")
            core = _import_path("_dev_team_gate_forge_core", FORGE_DIR / "forge_core.py")
            pkg = _import_path("_dev_team_gate_forge_adapters",
                               FORGE_DIR / "adapters" / "__init__.py", package=True)
            _FORGE = (core, dict(pkg.ADAPTERS))
        return _FORGE


def _adapter_registry() -> dict:
    return _load_forge()[1]


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


class ForgeReader:
    """In-process forge reads for one hook call, all under one deadline.

    Each read runs in a daemon thread; past the deadline it raises
    TimeoutError and check_complete reports `forge: unreachable`."""

    def __init__(self, adapters: dict | None = None, home: Path | None = None,
                 budget: float | None = None):
        self._adapters, self._home = adapters, home
        self._budget = FORGE_TIMEOUT_S if budget is None else budget
        self._deadline = time.monotonic() + self._budget

    def _run(self, fn, *args):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"forge did not answer within {self._budget}s")
        box: dict = {}

        def target():
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # handed to the caller below
                box["error"] = exc

        worker = threading.Thread(target=target, name="dev-team-gate-forge", daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            raise TimeoutError(f"forge did not answer within {self._budget}s")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _record(self, core, slug):
        return core.load_record(slug, self._home or _hermes_home())

    def ci_status(self, slug: str, sha: str) -> str:
        """"green" or a negative state ("red", "pending", "none"); raises when
        CI cannot be verified."""
        core = _load_forge()[0]
        adapters = self._adapters if self._adapters is not None else _adapter_registry()
        return self._run(lambda: core.ci_state(self._record(core, slug), adapters, sha))

    def pr_status(self, slug: str, pr_url: str) -> dict:
        """{pr, state, pr_url, head_sha, ready} of the project's PR `pr_url`."""
        core = _load_forge()[0]
        adapters = self._adapters if self._adapters is not None else _adapter_registry()
        return self._run(lambda: core.pr_facts(self._record(core, slug), adapters, pr_url))


def forge_ci_status(slug: str, sha: str) -> str:
    return ForgeReader().ci_status(slug, sha)


def forge_pr_status(slug: str, pr_url: str) -> dict:
    return ForgeReader().pr_status(slug, pr_url)


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
            reader = ForgeReader()
            msg = check_complete(board, task_id, args, reader.ci_status, reader.pr_status)
        else:
            command = args.get("command")
            if command is None:
                return None
            task_id = os.environ.get("HERMES_KANBAN_TASK") or None
            msg = check_terminal(_make_board(None), task_id, command, open_board=_make_board)
    except Exception as exc:  # fail closed, with our label rather than Hermes' generic one
        msg = f"guard: {tool_name} check failed, refusing ({_describe(exc)})"
    return _block(msg) if msg else None
