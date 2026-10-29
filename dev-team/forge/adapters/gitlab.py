"""GitLab adapter: the ``glab`` CLI for pull requests, ``glab api`` for CI.

Credentials are glab's own login (``glab auth login``); this adapter never
reads or writes a token itself.
"""
from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request as urllib_request
from pathlib import Path

from . import detect_from_remote_url, parse_owner_repo

# Explicit vendor-state table: GitLab commit "status" -> forge state. Any
# status not listed here is unmapped and ci_status() raises ValueError,
# which forge_core turns into exit 2.
_STATUS_TO_STATE = {
    "success": "green",
    "skipped": "green",
    "manual": "green",
    "failed": "red",
    "canceled": "red",
    "pending": "pending",
    "running": "pending",
    "created": "pending",
    "waiting_for_resource": "pending",
    "preparing": "pending",
    "scheduled": "pending",
}


class GitlabAdapter:
    name = "gitlab"

    def __init__(self, rec, *, run=subprocess.run, http=urllib_request):
        self._rec = rec
        self._run = run
        self._http = http

    def _project(self) -> str:
        hint = self._rec.ci_hint or {}
        if hint.get("project"):
            project = hint["project"]
        else:
            owner, repo = parse_owner_repo(self._rec.repo_url)
            project = f"{owner}/{repo}"
        return urllib.parse.quote(project, safe="")

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

    def _glab(self, args, timeout=30):
        result = self._run(args, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or f"{args[0]} failed").strip())
        return result.stdout

    def pr_open(self, branch: str, title: str, body: str) -> dict:
        stdout = self._glab(
            [
                "glab",
                "mr",
                "create",
                "--source-branch",
                branch,
                "--title",
                title,
                "--description",
                body,
                "--yes",
            ]
        )
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        url = lines[-1] if lines else ""
        pr_id = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        return {"pr": pr_id, "pr_url": url}

    def pr_status(self, pr: str) -> dict:
        stdout = self._glab(["glab", "api", f"projects/{self._project()}/merge_requests/{pr}"])
        data = json.loads(stdout)
        return {"state": str(data.get("state", "unknown")), "pr_url": data.get("web_url", "")}

    def mark_ready(self, pr: str) -> dict:
        self._glab(["glab", "mr", "update", str(pr), "--ready"])
        return {"pr": str(pr), "ready": True}

    def ci_status(self, sha: str) -> str:
        stdout = self._glab(
            ["glab", "api", f"projects/{self._project()}/repository/commits/{sha}/statuses"]
        )
        statuses = json.loads(stdout)
        if not statuses:
            return "none"
        states = set()
        for item in statuses:
            status = item.get("status")
            try:
                states.add(_STATUS_TO_STATE[status])
            except KeyError:
                raise ValueError(f"gitlab: unmapped commit status {status!r}") from None
        if "red" in states:
            return "red"
        if "pending" in states:
            return "pending"
        return "green"
