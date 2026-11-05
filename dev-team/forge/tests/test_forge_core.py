"""Tests for forge_core: project record and vendor-neutral CLI contract."""
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge_core import (  # noqa: E402
    CI_STATES,
    EXIT_NEGATIVE,
    EXIT_OK,
    EXIT_UNVERIFIABLE,
    ProjectRecord,
    load_record,
    main,
    record_path,
    redact,
    save_record,
)


SAMPLE = ProjectRecord(
    slug="demo",
    repo_url="https://example.test/demo.git",
    pr_adapter="fake",
    ci_adapter="fake",
    default_branch="main",
    ci_hint={"workflow": "ci.yml"},
)


def make_repo(parent, url="https://github.com/acme/demo.git", head="main", name="repo"):
    """A real git repo with an `origin` remote and, unless head is None, an
    `origin/HEAD` symbolic ref pointing at `head`."""
    repo = Path(parent) / name
    repo.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    if url is not None:
        git("remote", "add", "origin", url)
    if head is not None:
        git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{head}")
    return repo


def run_main(argv, home, adapters=None):
    out = io.StringIO()
    code = main(argv, home=home, adapters={} if adapters is None else adapters, out=out)
    return code, out.getvalue()


class FakeAdapter:
    """Minimal stand-in for a real vendor adapter, satisfying the Adapter protocol."""

    name = "fake"

    def __init__(self, record=None, ci_state="green", raise_message=None):
        self.record = record
        self.ci_state = ci_state
        self.raise_message = raise_message

    def detect(self, repo_dir):
        return True

    def pr_open(self, branch, title, body):
        return {"pr_url": "https://example.test/pr/1"}

    def pr_status(self, pr):
        self.pr_seen = pr
        return {"pr": str(pr), "state": "open", "pr_url": f"https://example.test/acme/demo/pull/{pr}",
                "head_sha": "9f4cbf1a2b3c4d5e6f708192a3b4c5d6e7f80912", "ready": True}

    def ci_status(self, sha):
        if self.raise_message:
            raise RuntimeError(self.raise_message)
        return self.ci_state

    def mark_ready(self, pr):
        return {"pr": pr, "ready": True}


class RecordRoundtripTests(unittest.TestCase):
    def test_record_roundtrip(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = save_record(SAMPLE, home)
            self.assertEqual(path, record_path(SAMPLE.slug, home))

            loaded = load_record(SAMPLE.slug, home)
            self.assertEqual(loaded, SAMPLE)

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, "the record must be owner read/write only")


class CiStatusTests(unittest.TestCase):
    def _setup(self, tmp, ci_state=None, raise_message=None, with_record=True):
        home = Path(tmp)
        if with_record:
            save_record(SAMPLE, home)
        adapters = {
            "fake": lambda rec: FakeAdapter(rec, ci_state=ci_state, raise_message=raise_message)
        }
        return home, adapters

    def _run_ci_status(self, home, adapters, slug="demo", sha="abc123"):
        out = io.StringIO()
        code = main(
            ["ci-status", "--slug", slug, "--sha", sha],
            home=home,
            adapters=adapters,
            out=out,
        )
        return code, out.getvalue()

    def test_ci_status_green_exits_0(self):
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(tmp, ci_state="green")
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_OK)
            self.assertEqual(json.loads(output), {"state": "green"})

    def test_ci_status_red_exits_1(self):
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(tmp, ci_state="red")
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_NEGATIVE)
            self.assertEqual(json.loads(output), {"state": "red"})
            self.assertIn("red", CI_STATES)

    def test_ci_status_unknown_state_exits_2(self):
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(tmp, ci_state="weird")
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("error", output)

    def test_adapter_exception_exits_2_without_secret(self):
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(tmp, raise_message="auth failed token=abc")
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertNotIn("abc", output)

    def test_missing_record_exits_2(self):
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(tmp, with_record=False)
            code, output = self._run_ci_status(home, adapters, slug="ghost")
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("error", output)

    def test_unknown_adapter_name_exits_2(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            save_record(SAMPLE, home)
            adapters = {}  # "fake" intentionally not registered
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("error", output)

    def test_adapter_exception_with_authorization_token_header_exits_2_without_secret(self):
        secret = "tok" + "EN1234567890abcdef"
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(
                tmp, raise_message="Authorization: token " + secret
            )
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertNotIn(secret, output)

    def test_adapter_exception_with_authorization_basic_header_exits_2_without_secret(self):
        secret = "dXNl" + "cjpzZWNyZXRwYXNz"
        with TemporaryDirectory() as tmp:
            home, adapters = self._setup(
                tmp, raise_message="Authorization: Basic " + secret
            )
            code, output = self._run_ci_status(home, adapters)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertNotIn(secret, output)


class RedactTests(unittest.TestCase):
    def test_redact_strips_full_authorization_token_header(self):
        secret = "tok" + "EN1234567890abcdef"
        redacted = redact("Authorization: token " + secret)
        self.assertNotIn(secret, redacted)

    def test_redact_strips_full_authorization_basic_header(self):
        secret = "dXNl" + "cjpzZWNyZXRwYXNz"
        redacted = redact("Authorization: Basic " + secret)
        self.assertNotIn(secret, redacted)


class DetectFailClosedTests(unittest.TestCase):
    def test_detect_fails_closed_when_record_cannot_be_saved(self):
        with TemporaryDirectory() as tmp:
            repo_dir = make_repo(tmp)
            home = Path(tmp) / "home"
            home.mkdir()
            home.chmod(0o500)  # read+execute only: writes under it must fail
            out = io.StringIO()
            try:
                code = main(
                    [
                        "detect",
                        "--repo-dir",
                        str(repo_dir),
                        "--pr-adapter",
                        "fake",
                        "--ci-adapter",
                        "fake",
                    ],
                    home=home,
                    adapters={},
                    out=out,
                )
            finally:
                home.chmod(0o700)
            output = out.getvalue()
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            parsed = json.loads(output)
            self.assertIn("error", parsed)

    def test_main_catches_adapter_exceptions_that_escape_the_handler(self):
        # A dict an adapter returns that json.dumps cannot serialize must not
        # escape main() as an unhandled exception; it must fail closed.
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            save_record(SAMPLE, home)
            adapters = {"fake": lambda rec: FakeAdapter(rec, ci_state="green")}

            class UnserializableAdapter(FakeAdapter):
                def pr_status(self, pr):
                    return {"value": object()}  # type: ignore[return-value]

            adapters = {"fake": lambda rec: UnserializableAdapter(rec)}
            out = io.StringIO()
            code = main(
                ["pr-status", "--slug", "demo", "--pr", "1"],
                home=home,
                adapters=adapters,
                out=out,
            )
            output = out.getvalue()
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            parsed = json.loads(output)
            self.assertIn("error", parsed)


class DetectAmbiguousTests(unittest.TestCase):
    """forge_core._cmd_detect must never silently write a "none" record."""

    def test_detect_with_nothing_auto_detected_and_no_flag_is_ambiguous(self):
        with TemporaryDirectory() as tmp:
            repo_dir = make_repo(tmp, url="https://git.example.org/acme/demo.git")
            home = Path(tmp) / "home"
            out = io.StringIO()
            code = main(
                ["detect", "--repo-dir", str(repo_dir)],
                home=home,
                adapters={},  # nothing can ever match -> no auto-detection
                out=out,
            )
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertEqual(json.loads(out.getvalue()), {"error": "ambiguous: pass --pr-adapter"})
            # And no project record was written for an ambiguous result.
            self.assertFalse(record_path(repo_dir.name, home).exists())

    def test_detect_with_explicit_pr_adapter_flag_skips_ambiguity(self):
        with TemporaryDirectory() as tmp:
            repo_dir = make_repo(tmp, url="https://git.example.org/acme/demo.git")
            home = Path(tmp) / "home"
            out = io.StringIO()
            code = main(
                [
                    "detect",
                    "--repo-dir",
                    str(repo_dir),
                    "--pr-adapter",
                    "gitea",
                    "--ci-adapter",
                    "gitea",
                ],
                home=home,
                adapters={},  # still nothing auto-detects; flags must win
                out=out,
            )
            self.assertEqual(code, EXIT_OK)
            parsed = json.loads(out.getvalue())
            self.assertEqual(parsed["pr_adapter"], "gitea")
            self.assertEqual(parsed["ci_adapter"], "gitea")

    def test_detect_auto_detection_still_wins_when_no_flag_given(self):
        with TemporaryDirectory() as tmp:
            repo_dir = make_repo(tmp)
            home = Path(tmp) / "home"
            out = io.StringIO()
            adapters = {"fake": lambda rec: FakeAdapter(rec)}
            code = main(
                ["detect", "--repo-dir", str(repo_dir)],
                home=home,
                adapters=adapters,
                out=out,
            )
            self.assertEqual(code, EXIT_OK)
            parsed = json.loads(out.getvalue())
            self.assertEqual(parsed["pr_adapter"], "fake")
            self.assertEqual(parsed["ci_adapter"], "fake")


class DetectOnboardingTests(unittest.TestCase):
    """#6: the record `detect` writes must be usable by ci-status as written."""

    def detect(self, home, repo_dir, *extra):
        return run_main(["detect", "--repo-dir", str(repo_dir), "--slug", "demo",
                         "--pr-adapter", "github", "--ci-adapter", "github", *extra], home)

    def test_detect_fills_repo_url_from_origin(self):
        with TemporaryDirectory() as tmp:
            repo = make_repo(tmp, url="git@github.com:acme/demo.git")
            code, out = self.detect(Path(tmp) / "home", repo)
            self.assertEqual(code, EXIT_OK, out)
            self.assertEqual(json.loads(out)["repo_url"], "git@github.com:acme/demo.git")
            self.assertEqual(load_record("demo", Path(tmp) / "home").repo_url,
                             "git@github.com:acme/demo.git")

    def test_detect_strips_credentials_from_origin_url(self):
        with TemporaryDirectory() as tmp:
            secret = "ghp_" + "S3cr3tT0k3n"
            repo = make_repo(tmp, url=f"https://bot:{secret}@github.com/acme/demo.git")
            code, out = self.detect(Path(tmp) / "home", repo)
            self.assertEqual(code, EXIT_OK, out)
            self.assertNotIn(secret, out)
            rec = load_record("demo", Path(tmp) / "home")
            self.assertEqual(rec.repo_url, "https://github.com/acme/demo.git")
            self.assertNotIn(secret, record_path("demo", Path(tmp) / "home").read_text())

    def test_detect_fills_default_branch_from_origin_head(self):
        with TemporaryDirectory() as tmp:
            repo = make_repo(tmp, head="develop")
            code, out = self.detect(Path(tmp) / "home", repo)
            self.assertEqual(code, EXIT_OK, out)
            self.assertEqual(json.loads(out)["default_branch"], "develop")
            self.assertEqual(load_record("demo", Path(tmp) / "home").default_branch, "develop")

    def test_detect_without_origin_fails_closed(self):
        with TemporaryDirectory() as tmp:
            repo = make_repo(tmp, url=None, head=None)
            code, out = self.detect(Path(tmp) / "home", repo)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("origin", json.loads(out)["error"])
            self.assertFalse(record_path("demo", Path(tmp) / "home").exists())

    def test_detect_without_origin_head_fails_closed(self):
        with TemporaryDirectory() as tmp:
            repo = make_repo(tmp, head=None)
            code, out = self.detect(Path(tmp) / "home", repo)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("set-head", json.loads(out)["error"])
            self.assertFalse(record_path("demo", Path(tmp) / "home").exists())

    def test_detect_refuses_to_overwrite_existing_record(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = make_repo(tmp, head="develop")
            self.assertEqual(self.detect(home, repo)[0], EXIT_OK)
            before = record_path("demo", home).read_text()
            other = make_repo(tmp, url="https://github.com/evil/other.git", name="other")
            code, out = self.detect(home, other)
            self.assertEqual(code, EXIT_UNVERIFIABLE)
            self.assertIn("--force", json.loads(out)["error"])
            self.assertEqual(record_path("demo", home).read_text(), before)

    def test_detect_force_overwrites_existing_record(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertEqual(self.detect(home, make_repo(tmp))[0], EXIT_OK)
            other = make_repo(tmp, url="https://github.com/acme/moved.git", head="trunk",
                              name="other")
            code, out = self.detect(home, other, "--force")
            self.assertEqual(code, EXIT_OK, out)
            rec = load_record("demo", home)
            self.assertEqual((rec.repo_url, rec.default_branch),
                             ("https://github.com/acme/moved.git", "trunk"))

    def test_detect_record_is_usable_by_the_github_adapter(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from adapters.github import GithubAdapter
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.assertEqual(self.detect(home, make_repo(tmp))[0], EXIT_OK)
            self.assertEqual(GithubAdapter(load_record("demo", home))._owner_repo(),
                             ("acme", "demo"))


class SlugValidationTests(unittest.TestCase):
    """Minor: --slug is validated the way the guard validates it."""

    UNSAFE = ("../../x", "a/b", "..", ".", "a\\b", "", "/abs")

    def test_detect_rejects_unsafe_slug(self):
        for slug in self.UNSAFE:
            with self.subTest(slug=slug), TemporaryDirectory() as tmp:
                home = Path(tmp) / "h1" / "h2" / "home"
                code, out = run_main(["detect", "--repo-dir", str(make_repo(tmp)), "--slug", slug,
                                      "--pr-adapter", "github", "--ci-adapter", "github"], home)
                self.assertEqual(code, EXIT_UNVERIFIABLE)
                self.assertIn("unsafe project slug", json.loads(out)["error"])
                written = [p for p in Path(tmp).rglob("*.json")]
                self.assertEqual(written, [])

    def test_every_command_rejects_unsafe_slug(self):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            for argv in (["ci-status", "--slug", "../x", "--sha", "abc1234"],
                         ["pr-status", "--slug", "a/b", "--pr", "1"],
                         ["mark-ready", "--slug", "..", "--pr", "1"]):
                with self.subTest(argv=argv):
                    code, out = run_main(argv, home, {"fake": lambda rec: FakeAdapter(rec)})
                    self.assertEqual(code, EXIT_UNVERIFIABLE)
                    self.assertIn("unsafe project slug", json.loads(out)["error"])

    def test_record_path_rejects_unsafe_slug(self):
        from forge_core import RecordError
        for slug in self.UNSAFE:
            with self.subTest(slug=slug):
                with self.assertRaises(RecordError):
                    record_path(slug, Path("/tmp/h"))


class PrStatusTests(unittest.TestCase):
    """#5: pr-status reports the PR's head SHA and ready state."""

    def run_pr_status(self, pr, adapter):
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            save_record(SAMPLE, home)
            return run_main(["pr-status", "--slug", "demo", "--pr", pr], home,
                            {"fake": lambda rec: adapter})

    def test_pr_status_reports_head_sha_and_ready(self):
        code, out = self.run_pr_status("7", FakeAdapter())
        self.assertEqual(code, EXIT_OK, out)
        data = json.loads(out)
        self.assertEqual(data["head_sha"], "9f4cbf1a2b3c4d5e6f708192a3b4c5d6e7f80912")
        self.assertIs(data["ready"], True)
        self.assertEqual(data["pr_url"], "https://example.test/acme/demo/pull/7")
        self.assertEqual(data["pr"], "7")

    def test_pr_status_accepts_a_pr_url(self):
        for url, number in (("https://github.com/acme/demo/pull/7", "7"),
                            ("https://gitlab.com/g/sub/demo/-/merge_requests/12/", "12"),
                            ("https://git.example.org/acme/demo/pulls/3#issuecomment-1", "3")):
            with self.subTest(url=url):
                adapter = FakeAdapter()
                code, out = self.run_pr_status(url, adapter)
                self.assertEqual(code, EXIT_OK, out)
                self.assertEqual(adapter.pr_seen, number)

    def test_pr_status_unparseable_ref_fails_closed(self):
        for ref in ("abc", "https://github.com/acme/demo", "7; rm -rf /", ""):
            with self.subTest(ref=ref):
                code, out = self.run_pr_status(ref, FakeAdapter())
                self.assertEqual(code, EXIT_UNVERIFIABLE)
                self.assertIn("error", json.loads(out))

    def test_pr_status_missing_facts_fails_closed(self):
        class Partial(FakeAdapter):
            def pr_status(self, pr):
                return {"state": "open", "pr_url": "https://example.test/pull/1"}

        class BadReady(FakeAdapter):
            def pr_status(self, pr):
                return dict(FakeAdapter.pr_status(self, pr), ready="yes")

        class BadSha(FakeAdapter):
            def pr_status(self, pr):
                return dict(FakeAdapter.pr_status(self, pr), head_sha="not-a-sha")

        for adapter in (Partial(), BadReady(), BadSha()):
            with self.subTest(adapter=type(adapter).__name__):
                code, out = self.run_pr_status("1", adapter)
                self.assertEqual(code, EXIT_UNVERIFIABLE)
                self.assertIn("error", json.loads(out))


class GithubTokenRedactTests(unittest.TestCase):
    """M1: `redact()` covers bare GitHub token shapes, not only `token=<value>`."""

    def test_redact_strips_bare_classic_token_shapes(self):
        for prefix in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            with self.subTest(prefix=prefix):
                secret = prefix + "C" * 36
                redacted = redact(f"gh: failed to authenticate with {secret}; retry")
                self.assertNotIn(secret, redacted)
                self.assertIn("retry", redacted)

    def test_redact_strips_bare_fine_grained_pat(self):
        secret = "github_pat_" + "D" * 22 + "_" + "E" * 59
        redacted = redact("remote: Invalid credentials " + secret)
        self.assertNotIn(secret, redacted)

    def test_redact_is_case_insensitive_on_the_prefix(self):
        secret = "GHP_" + "F" * 36
        self.assertNotIn(secret, redact("error " + secret))

    def test_redact_leaves_ordinary_text_alone(self):
        text = "pull request #15 is not mergeable: ghost branch missing"
        self.assertEqual(redact(text), text)


class EnvTokenRedactTests(unittest.TestCase):
    """Finding 5 of the PR #15 review: the token shapes are a heuristic, but
    the operator's ACTUAL token value is known exactly whenever
    `DEV_TEAM_GH_TOKEN` or `GH_TOKEN` is set. Redact those literals too, on
    top of (never instead of) the shape match -- a forge that mints
    non-GitHub-shaped credentials, or a vendor that changes its prefixes,
    otherwise leaks the value verbatim into a `forge` error.
    """

    LITERAL = "zzz-opaque-credential-value-9876"

    def _with(self, **env):
        return mock.patch.dict(os.environ, env)

    def test_dev_team_token_value_is_redacted(self):
        with self._with(DEV_TEAM_GH_TOKEN=self.LITERAL):
            out = redact(f"remote: rejected using {self.LITERAL} as the password")
        self.assertNotIn(self.LITERAL, out)
        self.assertIn("remote: rejected using", out)

    def test_gh_token_value_is_redacted(self):
        with self._with(GH_TOKEN=self.LITERAL):
            self.assertNotIn(self.LITERAL, redact("auth error " + self.LITERAL))

    def test_both_variables_are_covered_at_once(self):
        other = "another-opaque-value-5432"
        with self._with(DEV_TEAM_GH_TOKEN=self.LITERAL, GH_TOKEN=other):
            out = redact(f"{self.LITERAL} and {other}")
        self.assertNotIn(self.LITERAL, out)
        self.assertNotIn(other, out)

    def test_every_occurrence_is_replaced(self):
        with self._with(DEV_TEAM_GH_TOKEN=self.LITERAL):
            out = redact(f"{self.LITERAL} / {self.LITERAL}")
        self.assertNotIn(self.LITERAL, out)

    def test_the_shape_match_still_applies_on_top(self):
        """Additive, not a replacement: a bare `ghp_` token is still redacted
        when the env vars are unset or hold something else."""
        shaped = "ghp_" + "A" * 36
        with self._with(DEV_TEAM_GH_TOKEN=self.LITERAL):
            out = redact(f"{shaped} and {self.LITERAL}")
        self.assertNotIn(shaped, out)
        self.assertNotIn(self.LITERAL, out)

    def test_an_unset_or_empty_variable_changes_nothing(self):
        text = "the sky is blue and empty"
        for env in ({}, {"DEV_TEAM_GH_TOKEN": ""}, {"GH_TOKEN": ""},
                    {"DEV_TEAM_GH_TOKEN": "   "}):
            with self.subTest(env=env):
                patcher = mock.patch.dict(os.environ, env, clear=True)
                with patcher:
                    self.assertEqual(redact(text), text)

    def test_a_whitespace_only_value_is_not_treated_as_a_secret(self):
        """Otherwise every space in the message would be redacted."""
        with mock.patch.dict(os.environ, {"DEV_TEAM_GH_TOKEN": " "}, clear=True):
            self.assertEqual(redact("a b c"), "a b c")

    def test_a_regex_special_value_is_matched_literally(self):
        literal = "a.*b+c?(d)[e]"
        with self._with(DEV_TEAM_GH_TOKEN=literal):
            out = redact(f"failed with {literal} here")
        self.assertNotIn(literal, out)
        self.assertIn("here", out)

    def test_an_error_emitted_by_the_cli_is_redacted(self):
        """The whole point: `_emit_error` -> `redact` is the only path a
        `forge` error takes to stdout."""
        out = io.StringIO()
        with self._with(DEV_TEAM_GH_TOKEN=self.LITERAL):
            rc = main(["pr-status", "--slug", self.LITERAL, "--pr", "1"],
                      home=Path("/nonexistent-forge-home"), adapters={}, out=out)
        self.assertEqual(rc, EXIT_UNVERIFIABLE)
        # The slug is echoed in `no project record for slug ...`; with the
        # value in the environment that echo is a credential leak.
        self.assertIn("no project record", out.getvalue())
        self.assertNotIn(self.LITERAL, out.getvalue())


if __name__ == "__main__":
    unittest.main()
