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
import sys
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


def record_path(slug: str, home: Path) -> Path:
    return home / "dev-team" / "projects" / f"{slug}.json"


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
    (re.compile(r"Authorization:\s*\S+", re.IGNORECASE), "Authorization: ***"),
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


def _cmd_detect(args, home: Path, adapters, out) -> int:
    repo_dir = Path(args.repo_dir)
    slug = args.slug or repo_dir.name

    pr_adapter = "none"
    ci_adapter = "none"
    for name, factory in adapters.items():
        probe = ProjectRecord(
            slug=slug,
            repo_url="",
            pr_adapter=name,
            ci_adapter=name,
            default_branch="main",
            ci_hint={},
        )
        try:
            candidate = factory(probe)
            if candidate.detect(repo_dir):
                pr_adapter = name
                ci_adapter = name
                break
        except Exception:
            continue

    rec = ProjectRecord(
        slug=slug,
        repo_url="",
        pr_adapter=pr_adapter,
        ci_adapter=ci_adapter,
        default_branch="main",
        ci_hint={},
    )
    save_record(rec, home)
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


def _cmd_pr_status(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        adapter = _build_adapter(rec, adapters, rec.pr_adapter)
        result = adapter.pr_status(args.pr)
    except Exception as exc:
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE
    _emit(out, result)
    return EXIT_OK


def _cmd_ci_status(args, home: Path, adapters, out) -> int:
    try:
        rec = load_record(args.slug, home)
        adapter = _build_adapter(rec, adapters, rec.ci_adapter)
        state = adapter.ci_status(args.sha)
    except Exception as exc:
        _emit_error(out, exc)
        return EXIT_UNVERIFIABLE

    if state not in CI_STATES:
        _emit(out, {"error": f"unknown ci state: {state!r}"})
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
    return handler(args, home, adapters, out)
