"""Jenkins adapter: CI only, plain REST via urllib.

Credentials come from ``JENKINS_USER`` / ``JENKINS_TOKEN`` (HTTP Basic,
Jenkins' own API-token scheme). Jenkins has no concept of a pull request,
so every pr_* method raises NotSupported("forge: unsupported"); a project
record pairs jenkins as the ci_adapter with another vendor as pr_adapter.
detect() always returns False -- jenkins is never auto-selected, for
either role, and must be configured explicitly via ci_hint.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse
import urllib.request as urllib_request
from typing import Tuple

from . import NotSupported

# Explicit vendor-state table: Jenkins build "result" -> forge state. Any
# result not listed here is unmapped and ci_status() raises ValueError,
# which forge_core turns into exit 2. `result` is null while a build is
# running or queued; that case is handled before this table is consulted.
_RESULT_TO_STATE = {
    "SUCCESS": "green",
    "FAILURE": "red",
    "UNSTABLE": "red",
    "ABORTED": "red",
    "NOT_BUILT": "red",
}

_TREE = "builds[number,result,building,actions[parameters[name,value],lastBuiltRevision[SHA1]]]"


class JenkinsAdapter:
    name = "jenkins"

    def __init__(self, rec, *, run=None, http=urllib_request):
        self._rec = rec
        self._run = run
        self._http = http

    def detect(self, repo_dir) -> bool:
        return False

    def _job(self) -> Tuple[str, str]:
        hint = self._rec.ci_hint or {}
        base, job = hint.get("base_url"), hint.get("job")
        if not base or not job:
            raise ValueError("jenkins ci_hint must set 'base_url' and 'job'")
        return base.rstrip("/"), job

    def _headers(self) -> dict:
        user = os.environ.get("JENKINS_USER")
        token = os.environ.get("JENKINS_TOKEN")
        if not user or not token:
            return {}
        credentials = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {credentials}"}

    @staticmethod
    def _matches_sha(build: dict, sha: str) -> bool:
        for action in build.get("actions") or []:
            revision = (action.get("lastBuiltRevision") or {}).get("SHA1")
            if revision == sha:
                return True
            for param in action.get("parameters") or []:
                if str(param.get("value")) == sha:
                    return True
        return False

    @staticmethod
    def _state_of(build: dict) -> str:
        if build.get("building"):
            return "pending"
        result = build.get("result")
        if result is None:
            return "pending"
        try:
            return _RESULT_TO_STATE[result]
        except KeyError:
            raise ValueError(f"jenkins: unmapped build result {result!r}") from None

    def ci_status(self, sha: str) -> str:
        base, job = self._job()
        url = (
            f"{base}/job/{urllib.parse.quote(job)}/api/json"
            f"?tree={urllib.parse.quote(_TREE)}"
        )
        req = self._http.Request(url, headers=self._headers())
        with self._http.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        for build in data.get("builds") or []:
            if self._matches_sha(build, sha):
                return self._state_of(build)
        return "none"

    def pr_open(self, branch: str, title: str, body: str) -> dict:
        raise NotSupported("forge: unsupported")

    def pr_status(self, pr: str) -> dict:
        raise NotSupported("forge: unsupported")

    def mark_ready(self, pr: str) -> dict:
        raise NotSupported("forge: unsupported")
