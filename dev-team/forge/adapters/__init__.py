"""Vendor adapter registry and shared detection helpers for the forge command.

Each adapter class in this package satisfies forge_core.Adapter by duck
typing and is registered into forge_core.ADAPTERS (the same dict object
forge.py hands to forge_core.main), keyed by vendor name.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Optional, Tuple


class NotSupported(Exception):
    """Raised by an adapter for an operation its vendor cannot perform.

    forge_core's CLI dispatch catches this the same way it catches any
    other exception raised inside a command handler: it prints the
    redacted message as ``{"error": ...}`` and exits 2 (cannot verify).
    Jenkins' pr_open/pr_status/mark_ready always raise this with the
    message "forge: unsupported" -- jenkins is CI-only and a project
    record pairs it with another adapter for pull requests.
    """


def detect_from_remote_url(url: Optional[str]) -> Optional[str]:
    """Guess a PR-forge vendor from a git remote URL.

    Returns "github" for github.com remotes, "gitlab" for gitlab.com
    remotes or any URL carrying GitLab's "/-/" path segment (self-hosted
    GitLab instances use this in project URLs). Returns None when the
    vendor can't be told apart from the URL alone -- which is always true
    for self-hosted Gitea and for Jenkins, since both can live at any
    hostname. In that case the caller (devops, at onboarding) must pass
    --pr-adapter explicitly; forge itself never guesses.
    """
    if not url:
        return None
    if "github.com" in url:
        return "github"
    if "gitlab.com" in url or "/-/" in url:
        return "gitlab"
    return None


def decode_pages(text: str) -> list:
    """The JSON documents in `text`: `gh|glab api --paginate` prints one per
    page, back to back. Raises ValueError on anything that isn't JSON."""
    decoder = json.JSONDecoder()
    docs, i, text = [], 0, text or ""
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return docs
        doc, i = decoder.raw_decode(text, i)
        docs.append(doc)


def combine_states(states) -> str:
    """One forge state from per-check states (green, red, pending, neutral).

    red beats pending beats green; checks that did not run (neutral/skipped)
    count for nothing, so a run where nothing ran, or no checks at all, is
    "none" -- never green."""
    states = set(states)
    if "red" in states:
        return "red"
    if "pending" in states:
        return "pending"
    if "green" in states:
        return "green"
    return "none"


def parse_owner_repo(url: Optional[str]) -> Tuple[str, str]:
    """Best-effort ``(owner, repo)`` extraction from a git remote URL.

    Handles the scp-like ssh form (``git@host:owner/repo.git``) and https
    forms, including GitLab's ``/-/...`` suffixes. Raises ValueError when
    the URL doesn't carry two path segments -- callers should raise this
    lazily (at call time, not from an adapter's constructor) so that
    `forge detect`, which builds every registered adapter against a probe
    record with an empty repo_url, never fails just because the URL isn't
    known yet.
    """
    if not url:
        raise ValueError("no repo_url to derive owner/repo from")
    text = url.strip()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    if "@" in text and "://" not in text and ":" in text:
        # scp-like ssh form: git@host:owner/repo
        _, _, path = text.partition(":")
    else:
        path = urllib.parse.urlparse(text).path
    path = path.strip("/")
    path = path.split("/-/", 1)[0]  # strip GitLab's /-/tree/... etc.
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot derive owner/repo from repo_url {url!r}")
    return parts[-2], parts[-1]


from .github import GithubAdapter  # noqa: E402
from .gitlab import GitlabAdapter  # noqa: E402
from .gitea import GiteaAdapter  # noqa: E402
from .jenkins import JenkinsAdapter  # noqa: E402

# The registry, keyed by vendor name. This package imports nothing from
# forge_core, so the dev-team-gate guard can load it by path next to its own
# copy of forge_core; forge.py copies it into forge_core.ADAPTERS.
ADAPTERS = {
    "github": GithubAdapter,
    "gitlab": GitlabAdapter,
    "gitea": GiteaAdapter,
    "jenkins": JenkinsAdapter,
}

__all__ = [
    "NotSupported",
    "detect_from_remote_url",
    "parse_owner_repo",
    "ADAPTERS",
    "GithubAdapter",
    "GitlabAdapter",
    "GiteaAdapter",
    "JenkinsAdapter",
]
