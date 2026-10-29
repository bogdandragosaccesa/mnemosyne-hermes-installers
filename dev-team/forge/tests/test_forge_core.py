"""Tests for forge_core: project record and vendor-neutral CLI contract."""
import io
import json
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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
        return {"state": "open"}

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
            self.assertEqual(mode & 0o022, 0, "group/other write bits must be unset")


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


if __name__ == "__main__":
    unittest.main()
