"""Gitea adapter: plain REST via urllib (``tea`` has no stable JSON CI status).

Credentials come from the ``GITEA_TOKEN`` environment variable and are
sent only as an ``Authorization: token <token>`` request header -- never
in a URL, a log line, or an exception message this adapter constructs.
Gitea is always self-hosted, so detect() never guesses it; callers select
it explicitly via --pr-adapter at onboarding.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request as urllib_request
from typing import Optional, Tuple

from . import parse_owner_repo

# Explicit vendor-state table: Gitea combined-status "state" -> forge
# state. Any state not listed here is unmapped and ci_status() raises
# ValueError, which forge_core turns into exit 2.
_STATE_TO_STATE = {
    "success": "green",
    "warning": "pending",
    "pending": "pending",
    "failure": "red",
    "error": "red",
}


class GiteaAdapter:
    name = "gitea"

    def __init__(self, rec, *, run=None, http=urllib_request):
        self._rec = rec
        self._run = run
        self._http = http

    def detect(self, repo_dir) -> bool:
        # Self-hosted: any hostname could be a Gitea instance. forge never
        # guesses this adapter; the caller passes --pr-adapter explicitly.
        return False

    def _target(self) -> Tuple[str, str, str]:
        hint = self._rec.ci_hint or {}
        base, owner, repo = hint.get("base_url"), hint.get("owner"), hint.get("repo")
        if base and owner and repo:
            return base.rstrip("/"), owner, repo
        owner, repo = parse_owner_repo(self._rec.repo_url)
        parsed = urllib.parse.urlparse(self._rec.repo_url)
        return f"{parsed.scheme}://{parsed.netloc}", owner, repo

    def _headers(self) -> dict:
        token = os.environ.get("GITEA_TOKEN")
        if not token:
            return {}
        return {"Authorization": f"token {token}"}

    def _request(self, url: str, *, method: str = "GET", payload: Optional[dict] = None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = dict(self._headers())
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = self._http.Request(url, headers=headers, data=data, method=method)
        with self._http.urlopen(req, timeout=10) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    def pr_open(self, branch: str, title: str, body: str) -> dict:
        base, owner, repo = self._target()
        data = self._request(
            f"{base}/api/v1/repos/{owner}/{repo}/pulls",
            method="POST",
            payload={"head": branch, "base": self._rec.default_branch, "title": title, "body": body},
        )
        return {"pr": str(data.get("number", "")), "pr_url": data.get("html_url", "")}

    def pr_status(self, pr: str) -> dict:
        base, owner, repo = self._target()
        data = self._request(f"{base}/api/v1/repos/{owner}/{repo}/pulls/{pr}")
        return {"state": str(data.get("state", "unknown")), "pr_url": data.get("html_url", "")}

    def mark_ready(self, pr: str) -> dict:
        base, owner, repo = self._target()
        self._request(
            f"{base}/api/v1/repos/{owner}/{repo}/pulls/{pr}",
            method="PATCH",
            payload={"draft": False},
        )
        return {"pr": str(pr), "ready": True}

    def ci_status(self, sha: str) -> str:
        base, owner, repo = self._target()
        data = self._request(f"{base}/api/v1/repos/{owner}/{repo}/commits/{sha}/status")
        statuses = data.get("statuses") or []
        if not statuses:
            return "none"
        state = data.get("state")
        try:
            return _STATE_TO_STATE[state]
        except KeyError:
            raise ValueError(f"gitea: unmapped commit state {state!r}") from None
