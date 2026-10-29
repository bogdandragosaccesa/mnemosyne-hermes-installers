"""GitHub adapter: the ``gh`` CLI for pull requests, ``gh api`` for CI.

Credentials are gh's own login (``gh auth login``); this adapter never
reads or writes a token itself.
"""
from __future__ import annotations

import json
import subprocess
import urllib.request as urllib_request
from pathlib import Path

from . import detect_from_remote_url, parse_owner_repo

# Explicit vendor-state table: GitHub check-run "conclusion" -> forge state.
# Any conclusion not listed here is unmapped and ci_status() raises
# ValueError, which forge_core turns into exit 2.
_CONCLUSION_TO_STATE = {
    "success": "green",
    "neutral": "green",
    "skipped": "green",
    "failure": "red",
    "timed_out": "red",
    "cancelled": "red",
    "action_required": "red",
    "stale": "red",
}


class GithubAdapter:
    name = "github"

    def __init__(self, rec, *, run=subprocess.run, http=urllib_request):
        self._rec = rec
        self._run = run
        self._http = http

    def _owner_repo(self):
        hint = self._rec.ci_hint or {}
        if hint.get("owner") and hint.get("repo"):
            return hint["owner"], hint["repo"]
        return parse_owner_repo(self._rec.repo_url)

    def _remote_url(self, repo_dir) -> str:
        try:
            result = self._run(
                ["git", "-C", str(Path(repo_dir)), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except OSError:
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    def detect(self, repo_dir) -> bool:
        return detect_from_remote_url(self._remote_url(repo_dir)) == self.name

    def _gh(self, args, timeout=30):
        result = self._run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or f"{args[0]} failed").strip())
        return result.stdout

    def pr_open(self, branch: str, title: str, body: str) -> dict:
        # `gh pr create` has no --json flag (unlike `gh pr view`/`gh api`); it
        # prints the new PR's URL on stdout as the last non-empty line, with
        # some human-readable progress text ahead of it. The PR number is the
        # URL's final path segment.
        stdout = self._gh(
            [
                "gh",
                "pr",
                "create",
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        url = lines[-1] if lines else ""
        pr_id = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        return {"pr": pr_id, "pr_url": url}

    def pr_status(self, pr: str) -> dict:
        stdout = self._gh(["gh", "pr", "view", str(pr), "--json", "state,url"])
        data = json.loads(stdout)
        return {"state": str(data["state"]).lower(), "pr_url": data["url"]}

    def mark_ready(self, pr: str) -> dict:
        self._gh(["gh", "pr", "ready", str(pr)])
        return {"pr": str(pr), "ready": True}

    def ci_status(self, sha: str) -> str:
        owner, repo = self._owner_repo()
        stdout = self._gh(["gh", "api", f"repos/{owner}/{repo}/commits/{sha}/check-runs"])
        data = json.loads(stdout)
        check_runs = data.get("check_runs") or []
        if not check_runs:
            return "none"
        if any(cr.get("status") != "completed" for cr in check_runs):
            return "pending"
        states = set()
        for cr in check_runs:
            conclusion = cr.get("conclusion")
            try:
                states.add(_CONCLUSION_TO_STATE[conclusion])
            except KeyError:
                raise ValueError(f"github: unmapped check-run conclusion {conclusion!r}") from None
        return "red" if "red" in states else "green"
