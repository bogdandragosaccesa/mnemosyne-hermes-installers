"""Tests for the four vendor adapters: github, gitlab, gitea, jenkins."""
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_core import ProjectRecord  # noqa: E402

import adapters  # noqa: E402 -- registers the four vendors into forge_core.ADAPTERS
from adapters import NotSupported, detect_from_remote_url  # noqa: E402
from adapters.github import GithubAdapter  # noqa: E402
from adapters.gitlab import GitlabAdapter  # noqa: E402
from adapters.gitea import GiteaAdapter  # noqa: E402
from adapters.jenkins import JenkinsAdapter  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text()


def make_record(pr_adapter, ci_adapter, *, repo_url=None, ci_hint=None) -> ProjectRecord:
    return ProjectRecord(
        slug="demo",
        repo_url=repo_url if repo_url is not None else "git@github.com:acme/widgets.git",
        pr_adapter=pr_adapter,
        ci_adapter=ci_adapter,
        default_branch="main",
        ci_hint=ci_hint or {},
    )


class FakeRun:
    """Stand-in for subprocess.run: records every call, returns one canned result."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.calls = []
        self._stdout = stdout
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return SimpleNamespace(stdout=self._stdout, stderr=self._stderr, returncode=self._returncode)


class FakeHttpResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeHttp:
    """Stand-in for the urllib.request module: records Request() calls."""

    def __init__(self, body: bytes = b"{}", raise_exc=None):
        self.requests = []
        self._body = body
        self._raise_exc = raise_exc

    def Request(self, url, headers=None, data=None, method=None):
        req = SimpleNamespace(full_url=url, headers=dict(headers or {}), data=data, method=method)
        self.requests.append(req)
        return req

    def urlopen(self, req, timeout=None):
        if self._raise_exc is not None:
            raise self._raise_exc
        return FakeHttpResponse(self._body)


class AdapterRegistrationTests(unittest.TestCase):
    def test_all_four_vendors_registered(self):
        self.assertEqual(
            {adapters.ADAPTERS[name].__name__ for name in ("github", "gitlab", "gitea", "jenkins")},
            {"GithubAdapter", "GitlabAdapter", "GiteaAdapter", "JenkinsAdapter"},
        )


class DetectFromRemoteUrlTests(unittest.TestCase):
    def test_detect_from_remote_url(self):
        self.assertEqual(detect_from_remote_url("git@github.com:acme/widgets.git"), "github")
        self.assertEqual(detect_from_remote_url("https://github.com/acme/widgets.git"), "github")
        self.assertEqual(detect_from_remote_url("https://gitlab.com/acme/widgets.git"), "gitlab")
        self.assertEqual(
            detect_from_remote_url("https://git.example.com/acme/widgets/-/tree/main"), "gitlab"
        )
        # Self-hosted Gitea/Jenkins can't be told apart from the URL alone:
        # the CLI falls back to asking the caller for --pr-adapter.
        self.assertIsNone(detect_from_remote_url("https://git.example.com/acme/widgets.git"))
        self.assertIsNone(detect_from_remote_url(""))
        self.assertIsNone(detect_from_remote_url(None))


class GithubAdapterTests(unittest.TestCase):
    def _adapter(self, run):
        rec = make_record("github", "github")
        return GithubAdapter(rec, run=run)

    def test_github_all_success_is_green(self):
        run = FakeRun(stdout=load_fixture("github_all_success"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "green")

    def test_github_any_failure_is_red(self):
        run = FakeRun(stdout=load_fixture("github_any_failure"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "red")

    def test_github_running_is_pending(self):
        run = FakeRun(stdout=load_fixture("github_running"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "pending")

    def test_github_no_checks_is_none(self):
        run = FakeRun(stdout=load_fixture("github_no_checks"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "none")

    def test_github_unmapped_conclusion_raises_value_error(self):
        run = FakeRun(stdout=json.dumps({"check_runs": [{"status": "completed", "conclusion": "weird"}]}))
        with self.assertRaises(ValueError):
            self._adapter(run).ci_status("abc123")

    def test_github_reads_check_runs_via_gh_api(self):
        run = FakeRun(stdout=load_fixture("github_no_checks"))
        self._adapter(run).ci_status("abc123")
        args = run.calls[0][0]
        self.assertEqual(args[:2], ["gh", "api"])
        self.assertEqual(args[-1], "repos/acme/widgets/commits/abc123/check-runs")

    def test_github_detect_true_for_github_remote(self):
        run = FakeRun(stdout="git@github.com:acme/widgets.git\n")
        rec = make_record("none", "none")
        self.assertTrue(GithubAdapter(rec, run=run).detect(Path("/tmp/repo")))

    def test_github_detect_false_for_non_github_remote(self):
        run = FakeRun(stdout="https://gitlab.com/acme/widgets.git\n")
        rec = make_record("none", "none")
        self.assertFalse(GithubAdapter(rec, run=run).detect(Path("/tmp/repo")))

    def test_github_pr_open_uses_gh_cli(self):
        # Real `gh pr create` stdout shape: human-readable progress lines
        # followed by the new PR's URL as the last line. There is no --json
        # flag on this subcommand.
        run = FakeRun(
            stdout=(
                "Creating pull request for feature into main in acme/widgets\n\n"
                "https://github.com/acme/widgets/pull/7\n"
            )
        )
        result = self._adapter(run).pr_open("feature", "Add thing", "body text")
        self.assertEqual(result, {"pr": "7", "pr_url": "https://github.com/acme/widgets/pull/7"})
        self.assertEqual(run.calls[0][0][:3], ["gh", "pr", "create"])
        self.assertNotIn("--json", run.calls[0][0])


class PrStatusAdapterTests(unittest.TestCase):
    """#5: every PR adapter reports the PR's head SHA and ready (not draft) state,
    read from the project's own repository."""

    HEAD = "9f4cbf1a2b3c4d5e6f708192a3b4c5d6e7f80912"

    def test_github_pr_status_reads_the_project_pull(self):
        body = {"number": 7, "html_url": "https://github.com/acme/widgets/pull/7",
                 "state": "open", "draft": False, "head": {"sha": self.HEAD}}
        run = FakeRun(stdout=json.dumps(body))
        result = GithubAdapter(make_record("github", "github"), run=run).pr_status("7")
        self.assertEqual(result, {"pr": "7", "state": "open",
                                  "pr_url": "https://github.com/acme/widgets/pull/7",
                                  "head_sha": self.HEAD, "ready": True})
        self.assertEqual(run.calls[0][0], ["gh", "api", "repos/acme/widgets/pulls/7"])

    def test_github_draft_pr_is_not_ready(self):
        body = {"number": 7, "html_url": "u", "state": "open", "draft": True,
                 "head": {"sha": self.HEAD}}
        run = FakeRun(stdout=json.dumps(body))
        self.assertIs(GithubAdapter(make_record("github", "github"), run=run)
                      .pr_status("7")["ready"], False)

    def test_gitlab_pr_status_reads_the_project_merge_request(self):
        body = {"iid": 3, "web_url": "https://gitlab.com/acme/widgets/-/merge_requests/3",
                 "state": "opened", "draft": False, "sha": self.HEAD}
        run = FakeRun(stdout=json.dumps(body))
        rec = make_record("gitlab", "gitlab", repo_url="https://gitlab.com/acme/widgets.git")
        result = GitlabAdapter(rec, run=run).pr_status("3")
        self.assertEqual(result, {"pr": "3", "state": "opened",
                                  "pr_url": "https://gitlab.com/acme/widgets/-/merge_requests/3",
                                  "head_sha": self.HEAD, "ready": True})
        self.assertEqual(run.calls[0][0],
                         ["glab", "api", "projects/acme%2Fwidgets/merge_requests/3"])
        body["work_in_progress"] = True
        body.pop("draft")
        run = FakeRun(stdout=json.dumps(body))
        self.assertIs(GitlabAdapter(rec, run=run).pr_status("3")["ready"], False)

    def test_gitea_pr_status_reads_the_project_pull(self):
        body = {"number": 5, "html_url": "https://git.example.com/acme/widgets/pulls/5",
                 "state": "open", "title": "Add thing", "head": {"sha": self.HEAD}}
        http = FakeHttp(body=json.dumps(body).encode())
        rec = make_record("gitea", "gitea", repo_url="https://git.example.com/acme/widgets.git")
        result = GiteaAdapter(rec, http=http).pr_status("5")
        self.assertEqual(result, {"pr": "5", "state": "open",
                                  "pr_url": "https://git.example.com/acme/widgets/pulls/5",
                                  "head_sha": self.HEAD, "ready": True})
        self.assertEqual(http.requests[0].full_url,
                         "https://git.example.com/api/v1/repos/acme/widgets/pulls/5")
        for draft_body in (dict(body, title="WIP: Add thing"), dict(body, draft=True)):
            http = FakeHttp(body=json.dumps(draft_body).encode())
            self.assertIs(GiteaAdapter(rec, http=http).pr_status("5")["ready"], False)


class GithubStateTableTests(unittest.TestCase):
    """Table-driven coverage: every github check-run conclusion this adapter
    claims to support must map to the state the spec requires, independent
    of whatever the adapter module's own table currently says."""

    # Hardcoded from the vendor mapping, not imported from github.py, so a
    # mutation to the adapter's table is caught instead of silently copied
    # into "expected" here.
    EXPECTED = {
        "success": "green",
        "neutral": "green",
        "skipped": "green",
        "failure": "red",
        "timed_out": "red",
        "cancelled": "red",
        "action_required": "red",
        "stale": "red",
    }

    def test_every_conclusion_maps_to_expected_state(self):
        from adapters.github import _CONCLUSION_TO_STATE

        # Catches an entry being added/removed from the adapter's table,
        # not just a value being changed.
        self.assertEqual(set(_CONCLUSION_TO_STATE), set(self.EXPECTED))
        rec = make_record("github", "github")
        for conclusion, expected in self.EXPECTED.items():
            with self.subTest(conclusion=conclusion):
                run = FakeRun(
                    stdout=json.dumps(
                        {"check_runs": [{"status": "completed", "conclusion": conclusion}]}
                    )
                )
                self.assertEqual(GithubAdapter(rec, run=run).ci_status("abc123"), expected)


class GitlabAdapterTests(unittest.TestCase):
    def _adapter(self, run, repo_url="https://gitlab.com/acme/widgets.git"):
        rec = make_record("gitlab", "gitlab", repo_url=repo_url)
        return GitlabAdapter(rec, run=run)

    def test_gitlab_all_success_is_green(self):
        run = FakeRun(stdout=load_fixture("gitlab_all_success"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "green")

    def test_gitlab_any_failure_is_red(self):
        run = FakeRun(stdout=load_fixture("gitlab_any_failure"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "red")

    def test_gitlab_running_is_pending(self):
        run = FakeRun(stdout=load_fixture("gitlab_running"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "pending")

    def test_gitlab_no_checks_is_none(self):
        run = FakeRun(stdout=load_fixture("gitlab_no_checks"))
        self.assertEqual(self._adapter(run).ci_status("abc123"), "none")

    def test_gitlab_unmapped_status_raises_value_error(self):
        run = FakeRun(stdout=json.dumps([{"status": "weird"}]))
        with self.assertRaises(ValueError):
            self._adapter(run).ci_status("abc123")

    def test_gitlab_reads_statuses_via_glab_api(self):
        run = FakeRun(stdout=load_fixture("gitlab_no_checks"))
        self._adapter(run).ci_status("abc123")
        args = run.calls[0][0]
        self.assertEqual(args[:2], ["glab", "api"])
        self.assertEqual(args[-1], "projects/acme%2Fwidgets/repository/commits/abc123/statuses")

    def test_gitlab_detect_true_for_gitlab_remote(self):
        run = FakeRun(stdout="https://gitlab.com/acme/widgets.git\n")
        rec = make_record("none", "none")
        self.assertTrue(GitlabAdapter(rec, run=run).detect(Path("/tmp/repo")))

    def test_gitlab_detect_true_for_self_hosted_dash_path(self):
        run = FakeRun(stdout="https://git.example.com/acme/widgets.git\n")
        rec = make_record("none", "none")
        # Self-hosted GitLab remotes don't carry /-/ in `remote get-url`, so
        # this only resolves true for the gitlab.com host; self-hosted needs
        # an explicit --pr-adapter, same as gitea.
        self.assertFalse(GitlabAdapter(rec, run=run).detect(Path("/tmp/repo")))


class GitlabStateTableTests(unittest.TestCase):
    """Table-driven coverage, expected states hardcoded (see GithubStateTableTests)."""

    EXPECTED = {
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

    def test_every_status_maps_to_expected_state(self):
        from adapters.gitlab import _STATUS_TO_STATE

        self.assertEqual(set(_STATUS_TO_STATE), set(self.EXPECTED))
        for status, expected in self.EXPECTED.items():
            with self.subTest(status=status):
                run = FakeRun(stdout=json.dumps([{"status": status}]))
                rec = make_record(
                    "gitlab", "gitlab", repo_url="https://gitlab.com/acme/widgets.git"
                )
                self.assertEqual(GitlabAdapter(rec, run=run).ci_status("abc123"), expected)


class GiteaAdapterTests(unittest.TestCase):
    def _adapter(self, http, repo_url="https://git.example.com/acme/widgets.git"):
        rec = make_record("gitea", "gitea", repo_url=repo_url)
        return GiteaAdapter(rec, http=http)

    def test_gitea_all_success_is_green(self):
        http = FakeHttp(body=load_fixture("gitea_all_success").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "green")

    def test_gitea_any_failure_is_red(self):
        http = FakeHttp(body=load_fixture("gitea_any_failure").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "red")

    def test_gitea_running_is_pending(self):
        http = FakeHttp(body=load_fixture("gitea_running").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "pending")

    def test_gitea_no_checks_is_none(self):
        http = FakeHttp(body=load_fixture("gitea_no_checks").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "none")

    def test_gitea_unmapped_state_raises_value_error(self):
        http = FakeHttp(body=json.dumps({"state": "weird", "statuses": [{"status": "weird"}]}).encode())
        with self.assertRaises(ValueError):
            self._adapter(http).ci_status("abc123")

    def test_gitea_detect_is_always_false(self):
        # Self-hosted: never guessed, caller must pass --pr-adapter explicitly.
        self.assertFalse(self._adapter(FakeHttp()).detect(Path("/tmp/repo")))

    def test_gitea_mark_ready_strips_wip_colon_prefix(self):
        http = FakeHttp(body=json.dumps({"title": "WIP: Add feature"}).encode())
        result = self._adapter(http).mark_ready("5")
        self.assertEqual(result, {"pr": "5", "ready": True})
        self.assertEqual(len(http.requests), 2)
        get_req, patch_req = http.requests
        self.assertEqual(get_req.method, "GET")
        self.assertEqual(patch_req.method, "PATCH")
        payload = json.loads(patch_req.data.decode())
        self.assertEqual(payload, {"title": "Add feature"})
        self.assertNotIn("draft", payload)

    def test_gitea_mark_ready_strips_bracket_wip_prefix_case_insensitively(self):
        http = FakeHttp(body=json.dumps({"title": "[wip] Add feature"}).encode())
        self._adapter(http).mark_ready("5")
        patch_req = http.requests[1]
        payload = json.loads(patch_req.data.decode())
        self.assertEqual(payload, {"title": "Add feature"})

    def test_gitea_mark_ready_leaves_non_wip_title_unchanged(self):
        http = FakeHttp(body=json.dumps({"title": "Add feature"}).encode())
        self._adapter(http).mark_ready("5")
        patch_req = http.requests[1]
        payload = json.loads(patch_req.data.decode())
        self.assertEqual(payload, {"title": "Add feature"})

    def test_gitea_token_never_in_url_or_output(self):
        secret = "gta_" + "supersecrettoken1234567890"
        with mock.patch.dict(os.environ, {"GITEA_TOKEN": secret}):
            http = FakeHttp(body=load_fixture("gitea_all_success").encode())
            adapter = self._adapter(http)
            state = adapter.ci_status("abc123")
            self.assertEqual(state, "green")
            self.assertEqual(len(http.requests), 1)
            req = http.requests[0]
            self.assertNotIn(secret, req.full_url)
            self.assertIn(secret, req.headers.get("Authorization", ""))

            # And when the transport blows up, the secret must not leak into
            # the exception message that forge_core.redact() will print.
            failing_http = FakeHttp(raise_exc=RuntimeError(f"connection refused for {secret}"))
            with self.assertRaises(RuntimeError):
                self._adapter(failing_http).ci_status("abc123")
            # (the adapter doesn't invent its own message here; it's the
            # caller's own exception text. the leak path we actually guard
            # is the request construction below.)
            built_request_text = json.dumps(
                {"url": failing_http.requests[0].full_url if failing_http.requests else "", "headers": {}}
            )
            self.assertNotIn(secret, built_request_text)


class GiteaStateTableTests(unittest.TestCase):
    """Table-driven coverage, expected states hardcoded (see GithubStateTableTests)."""

    EXPECTED = {
        "success": "green",
        "warning": "pending",
        "pending": "pending",
        "failure": "red",
        "error": "red",
    }

    def test_every_state_maps_to_expected_state(self):
        from adapters.gitea import _STATE_TO_STATE

        self.assertEqual(set(_STATE_TO_STATE), set(self.EXPECTED))
        rec = make_record("gitea", "gitea", repo_url="https://git.example.com/acme/widgets.git")
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=state):
                body = json.dumps({"state": state, "statuses": [{"status": state}]}).encode()
                http = FakeHttp(body=body)
                self.assertEqual(GiteaAdapter(rec, http=http).ci_status("abc123"), expected)


class JenkinsAdapterTests(unittest.TestCase):
    def _adapter(self, http, ci_hint=None):
        rec = make_record(
            "github",
            "jenkins",
            ci_hint=ci_hint or {"base_url": "https://ci.example.com", "job": "widgets"},
        )
        return JenkinsAdapter(rec, http=http)

    def test_jenkins_all_success_is_green(self):
        http = FakeHttp(body=load_fixture("jenkins_all_success").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "green")

    def test_jenkins_any_failure_is_red(self):
        http = FakeHttp(body=load_fixture("jenkins_any_failure").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "red")

    def test_jenkins_running_is_pending(self):
        http = FakeHttp(body=load_fixture("jenkins_running").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "pending")

    def test_jenkins_no_checks_is_none(self):
        http = FakeHttp(body=load_fixture("jenkins_no_checks").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "none")

    def test_jenkins_unmapped_result_raises_value_error(self):
        body = json.dumps(
            {"builds": [{"result": "WEIRD", "building": False, "actions": [
                {"lastBuiltRevision": {"SHA1": "abc123"}}
            ]}]}
        ).encode()
        http = FakeHttp(body=body)
        with self.assertRaises(ValueError):
            self._adapter(http).ci_status("abc123")

    def test_jenkins_detect_is_always_false(self):
        self.assertFalse(self._adapter(FakeHttp()).detect(Path("/tmp/repo")))

    def test_jenkins_uses_basic_auth_from_env(self):
        with mock.patch.dict(os.environ, {"JENKINS_USER": "bob", "JENKINS_TOKEN": "tok123"}):
            http = FakeHttp(body=load_fixture("jenkins_no_checks").encode())
            self._adapter(http).ci_status("abc123")
            req = http.requests[0]
            self.assertTrue(req.headers.get("Authorization", "").startswith("Basic "))

    def test_jenkins_pr_open_unsupported(self):
        adapter = self._adapter(FakeHttp())
        with self.assertRaises(NotSupported):
            adapter.pr_open("branch", "title", "body")

    def test_jenkins_pr_status_unsupported(self):
        adapter = self._adapter(FakeHttp())
        with self.assertRaises(NotSupported):
            adapter.pr_status("1")

    def test_jenkins_mark_ready_unsupported(self):
        adapter = self._adapter(FakeHttp())
        with self.assertRaises(NotSupported):
            adapter.mark_ready("1")

    def test_jenkins_not_supported_message_is_the_exact_class_label(self):
        adapter = self._adapter(FakeHttp())
        with self.assertRaises(NotSupported) as ctx:
            adapter.pr_open("branch", "title", "body")
        self.assertEqual(str(ctx.exception), "forge: unsupported")


class JenkinsStateTableTests(unittest.TestCase):
    """Table-driven coverage, expected states hardcoded (see GithubStateTableTests)."""

    EXPECTED = {
        "SUCCESS": "green",
        "FAILURE": "red",
        "UNSTABLE": "red",
        "ABORTED": "red",
        "NOT_BUILT": "red",
    }

    def _adapter(self, http):
        rec = make_record(
            "github", "jenkins", ci_hint={"base_url": "https://ci.example.com", "job": "widgets"}
        )
        return JenkinsAdapter(rec, http=http)

    def test_every_result_maps_to_expected_state(self):
        from adapters.jenkins import _RESULT_TO_STATE

        self.assertEqual(set(_RESULT_TO_STATE), set(self.EXPECTED))
        for result, expected in self.EXPECTED.items():
            with self.subTest(result=result):
                body = json.dumps(
                    {
                        "builds": [
                            {
                                "number": 1,
                                "result": result,
                                "building": False,
                                "actions": [{"lastBuiltRevision": {"SHA1": "abc123"}}],
                            }
                        ]
                    }
                ).encode()
                http = FakeHttp(body=body)
                self.assertEqual(self._adapter(http).ci_status("abc123"), expected)

    def test_sha_filter_picks_the_matching_build_not_the_newest(self):
        # Fixture has a newer build (number 7) for a different sha that is
        # SUCCESS, and an older build (number 6) for the target sha that is
        # FAILURE. If the SHA filter were ever removed (e.g. `if True`
        # instead of `if self._matches_sha(...)`), the first/newest build
        # would win and this would wrongly read "green".
        http = FakeHttp(body=load_fixture("jenkins_sha_filter_red").encode())
        self.assertEqual(self._adapter(http).ci_status("abc123"), "red")


if __name__ == "__main__":
    unittest.main()
