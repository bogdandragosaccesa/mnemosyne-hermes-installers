"""Vendor-neutral project record and CLI contract for the forge command.

Team state (project records) lives under ``$HERMES_HOME/dev-team/`` and is
never written into the target repository. Credentials are never written to
the project record, stdout JSON, or logs.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, Protocol


CI_STATES = ("green", "red", "pending", "none")
EXIT_OK, EXIT_NEGATIVE, EXIT_UNVERIFIABLE = 0, 1, 2


class RecordError(Exception):
    """Raised when a project record cannot be loaded or is malformed."""


@dataclasses.dataclass(frozen=True)
class ProjectRecord:
    slug: str
    repo_url: str
    pr_adapter: str
    ci_adapter: str
    default_branch: str
    ci_hint: dict


def validate_slug(slug) -> str:
    """The project slug names a file under ``projects/``; the guard applies the
    same rule (``guard._record_default_branch``). Anything that could leave
    that directory is refused."""
    if (not isinstance(slug, str) or not slug or slug in (".", "..")
            or "/" in slug or "\\" in slug or "\0" in slug):
        raise RecordError(f"unsafe project slug {slug!r}")
    return slug


def record_path(slug: str, home: Path) -> Path:
    return home / "dev-team" / "projects" / f"{validate_slug(slug)}.json"


def save_record(rec: ProjectRecord, home: Path) -> Path:
    path = record_path(rec.slug, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(rec)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def load_record(slug: str, home: Path) -> ProjectRecord:
    path = record_path(slug, home)
    try:
        raw = path.read_text()
    except FileNotFoundError as exc:
        raise RecordError(f"no project record for slug {slug!r}") from exc
    except OSError as exc:
        raise RecordError(f"cannot read project record for slug {slug!r}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecordError(f"malformed project record for slug {slug!r}: {exc}") from exc

    try:
        return ProjectRecord(**data)
    except TypeError as exc:
        raise RecordError(f"malformed project record for slug {slug!r}: {exc}") from exc


class Adapter(Protocol):
    name: str

    def detect(self, repo_dir: Path) -> bool:
        ...

    def pr_open(self, branch: str, title: str, body: str) -> dict:
        ...

    def pr_status(self, pr: str) -> dict:
        ...

    def ci_status(self, sha: str) -> str:
        ...

    def mark_ready(self, pr: str) -> dict:
        ...


# Filled in by Task 3 with real vendor adapters (github, gitlab, gitea, jenkins).
ADAPTERS: Dict[str, Callable[[ProjectRecord], Adapter]] = {}


_SECRET_SUBSTITUTIONS = (
    (re.compile(r"token=\S+", re.IGNORECASE), "token=***"),
    (re.compile(r"Bearer\s+\S+", re.IGNORECASE), "Bearer ***"),
    (re.compile(r"Authorization:\s*[^\r\n\"']*", re.IGNORECASE), "Authorization: ***"),
)


def redact(text: str) -> str:
    """Strip common credential shapes out of text before it is ever printed."""
    out = text
    for pattern, replacement in _SECRET_SUBSTITUTIONS:
        out = pattern.sub(replacement, out)
    return out


def _emit(out, obj: dict) -> None:
    out.write(json.dumps(obj))
    out.write("\n")


def _emit_error(out, exc: BaseException) -> None:
    _emit(out, {"error": redact(str(exc))})


def _build_adapter(
    rec: ProjectRecord, adapters: Dict[str, Callable[[ProjectRecord], Adapter]], name: str
) -> Adapter:
    factory = adapters.get(name)
    if factory is None:
        raise RecordError(f"unknown adapter: {name}")
    return factory(rec)


GIT_TIMEOUT_S = 10


def _git_output(repo_dir: Path, *args: str) -> str | None:
    """stdout of `git -C repo_dir <args>`, stripped, or None when git fails."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT_S, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _strip_userinfo(url: str) -> str:
    """Drop `user:token@` from an http(s)/ssh URL: credentials never go in the record."""
    if "://" not in url:
        return url  # scp-like `git@host:owner/repo`: the user is not a secret
    parts = urllib.parse.urlsplit(url)
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit(parts._replace(netloc=host))


def _origin_facts(repo_dir: Path) -> tuple[str, str]:
    """(repo_url, default_branch) from the clone's `origin`, or RecordError."""
    url = _git_output(repo_dir, "remote", "get-url", "origin")
    if not url:
        raise RecordError(
            f"cannot read `git remote get-url origin` in {repo_dir}; clone the repository "
            f"there first")
    head = _git_output(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
    prefix = "refs/remotes/origin/"
    if not head or not head.startswith(prefix) or not head[len(prefix):]:
        raise RecordError(
            f"cannot read the default branch from refs/remotes/origin/HEAD in {repo_dir}; "
            f"run `git -C {repo_dir} remote set-head origin --auto` and re-run detect")
    return _strip_userinfo(url), head[len(prefix):]


def _cmd_detect(args, home: Path, adapters, out) -> int:
    try:
        repo_dir = Path(args.repo_dir)
        slug = validate_slug(args.slug if args.slug is not None else repo_dir.name)
        path = record_path(slug, home)
        if path.exists() and not getattr(args, "force", False):
            raise RecordError(
                f"a project record for {slug!r} already exists at {path}; refusing to "
                f"overwrite it (pass --force to replace it)")
        repo_url, default_branch = _origin_facts(repo_dir)

        pr_adapter = getattr(args, "pr_adapter", None)
        ci_adapter = getattr(args, "ci_adapter", None)

        if not pr_adapter or not ci_adapter:
            detected = None
            for name, factory in adapters.items():
                probe = ProjectRecord(
                    slug=slug,
                    repo_url=repo_url,
                    pr_adapter=name,
                    ci_adapter=name,
                    default_branch=default_branch,
                    ci_hint={},
                )
                try:
                    candidate = factory(probe)
                    if candidate.detect(repo_dir):
                        detected = name
                        break
                except Exception:
                    continue
            pr_adapter = pr_adapter or detected
            ci_adapter = ci_adapter or detected

        if not pr_adapter or not ci_adapter:
            # Ambiguous: nothing was auto-detected and the caller didn't
            # pass an explicit adapter. Never silently write a "none"
            # record -- devops must decide and pass --pr-adapter (and/or
            # --ci-adapter) at onboarding.
            _emit(out, {"error": "ambiguous: pass --pr-adapter"})
            return EXIT_UNVERIFIABLE

        rec = ProjectRecord(
            slug=slug,
            repo_url=repo_url,
            pr_adapter=pr_adapter,
            ci_adapter=ci_adapter,
            default_branch=default_branch,
            ci_hint={},
        )
        save_record(rec, home)
    except Exception as exc:  # fail closed: cannot verify
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
    _emit(out, dataclasses.asdict(rec))
    return EXIT_OK


def _cmd_pr_open(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        adapter = _build_adapter(rec, adapters, rec.pr_adapter)
        body = Path(args.body_file).read_text()
        result = adapter.pr_open(args.branch, args.title, body)
    except Exception as exc:  # fail closed: cannot verify
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
    _emit(out, result)
    return EXIT_OK


_PR_URL_NUMBER = re.compile(r"/(?:pull|pulls|merge_requests)/(\d+)/?(?:[?#].*)?$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


def parse_pr_number(ref: str) -> str:
    """The PR number from `7` or a PR/MR URL; RecordError otherwise."""
    ref = (ref or "").strip()
    if ref.isdigit():
        return ref
    m = _PR_URL_NUMBER.search(ref)
    if m and "://" in ref:
        return m.group(1)
    raise RecordError(f"cannot read a pull request number from {ref!r}")


def pr_facts(rec: ProjectRecord, adapters, pr_ref: str) -> dict:
    """{pr, state, pr_url, head_sha, ready} for PR `pr_ref` (number or URL) of
    the project's own repository, read through its PR adapter. Raises on
    anything off the contract: the guard ties a green pr-ci to these facts."""
    number = parse_pr_number(pr_ref)
    adapter = _build_adapter(rec, adapters, rec.pr_adapter)
    data = adapter.pr_status(number)
    if not isinstance(data, dict):
        raise RecordError("pr-status: adapter returned no object")
    url, head, ready = data.get("pr_url"), data.get("head_sha"), data.get("ready")
    if not isinstance(url, str) or "://" not in url:
        raise RecordError(f"pr-status: no PR URL for PR {number}")
    if not isinstance(head, str) or not _SHA.match(head):
        raise RecordError(f"pr-status: no head SHA for PR {number}")
    if not isinstance(ready, bool):
        raise RecordError(f"pr-status: no ready state for PR {number}")
    return {"pr": number, "state": str(data.get("state", "unknown")), "pr_url": url,
            "head_sha": head, "ready": ready}


def ci_state(rec: ProjectRecord, adapters, sha: str) -> str:
    """The CI state of `sha` through the project's CI adapter; raises on an
    unknown state."""
    adapter = _build_adapter(rec, adapters, rec.ci_adapter)
    state = adapter.ci_status(sha)
    if state not in CI_STATES:
        raise RecordError(f"unknown ci state: {state!r}")
    return state


def _cmd_pr_status(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        result = pr_facts(rec, adapters, args.pr)
    except Exception as exc:
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
    _emit(out, result)
    return EXIT_OK


def _cmd_ci_status(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        state = ci_state(rec, adapters, args.sha)
    except Exception as exc:
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE

    _emit(out, {"state": state})
    return EXIT_OK if state == "green" else EXIT_NEGATIVE


def _cmd_mark_ready(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        adapter = _build_adapter(rec, adapters, rec.pr_adapter)
        result = adapter.mark_ready(args.pr)
    except Exception as exc:
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
    _emit(out, result)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge")
    sub = parser.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect")
    p_detect.add_argument("--repo-dir", required=True)
    p_detect.add_argument("--slug")
    p_detect.add_argument("--pr-adapter")
    p_detect.add_argument("--ci-adapter")
    p_detect.add_argument("--force", action="store_true",
                          help="replace an existing project record")

    p_pr_open = sub.add_parser("pr-open")
    p_pr_open.add_argument("--slug", required=True)
    p_pr_open.add_argument("--branch", required=True)
    p_pr_open.add_argument("--title", required=True)
    p_pr_open.add_argument("--body-file", required=True)

    p_pr_status = sub.add_parser("pr-status")
    p_pr_status.add_argument("--slug", required=True)
    p_pr_status.add_argument("--pr", required=True)

    p_ci_status = sub.add_parser("ci-status")
    p_ci_status.add_argument("--slug", required=True)
    p_ci_status.add_argument("--sha", required=True)

    p_mark_ready = sub.add_parser("mark-ready")
    p_mark_ready.add_argument("--slug", required=True)
    p_mark_ready.add_argument("--pr", required=True)

    return parser


_DISPATCH = {
    "detect": _cmd_detect,
    "pr-open": _cmd_pr_open,
    "pr-status": _cmd_pr_status,
    "ci-status": _cmd_ci_status,
    "mark-ready": _cmd_mark_ready,
}


def main(
    argv: list,
    *,
    home: Path,
    adapters: Dict[str, Callable[[ProjectRecord], Adapter]] = ADAPTERS,
    out=sys.stdout,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = _DISPATCH[args.command]
    try:
        return handler(args, home, adapters, out)
    except Exception as exc:  # catch-all: never let an exception escape unredacted
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
