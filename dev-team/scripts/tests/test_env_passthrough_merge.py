"""dev-team/scripts/env_passthrough_merge.py: the shared list-merge both
installers use for `terminal.env_passthrough` when `--forge-token-source` /
`-ForgeTokenSource` is given.

The contract the installers rely on (finding 1 of the PR #15 review):

  * the current value arrives as JSON on stdin (what `hermes config get
    <key> --json` prints), never on argv -- argv carries only variable NAMES;
  * stdout is ONE line of compact JSON: the existing list with each requested
    name APPENDED only when absent, order otherwise preserved;
  * an absent/empty/`null` current value is an empty list, so a profile with
    no `terminal.env_passthrough` yet gets exactly the requested names;
  * re-running with a value that already contains the name prints the same
    list back, which is what makes the installer idempotent;
  * exit 0 on success, 2 on usage or malformed input, with nothing on stdout.

Standard library only: the CI `scripts` job runs this suite under
`python3 -I` in a venv built `--without-pip`.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "env_passthrough_merge.py"
VAR = "DEV_TEAM_GH_TOKEN"


class MergeTests(unittest.TestCase):
    def merge(self, stdin, *names):
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *names], input=stdin,
            capture_output=True, text=True, timeout=60)

    def ok(self, stdin, *names):
        proc = self.merge(stdin, *names or (VAR,))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.count("\n"), 1, "exactly one line of output")
        return json.loads(proc.stdout)

    def test_the_script_exists(self):
        self.assertTrue(SCRIPT.is_file(), f"missing {SCRIPT}")

    def test_empty_input_yields_just_the_name(self):
        for stdin in ("", "   ", "\n", "null", "[]"):
            with self.subTest(stdin=stdin):
                self.assertEqual(self.ok(stdin), [VAR])

    def test_name_is_appended_to_an_existing_list(self):
        self.assertEqual(self.ok('["FOO", "BAR"]'), ["FOO", "BAR", VAR])

    def test_already_present_is_a_no_op(self):
        """What makes a re-run with the same flag value idempotent."""
        self.assertEqual(self.ok(f'["FOO", "{VAR}", "BAR"]'), ["FOO", VAR, "BAR"])

    def test_existing_order_is_preserved(self):
        self.assertEqual(self.ok('["Z", "A", "M"]'), ["Z", "A", "M", VAR])

    def test_unrelated_names_are_never_dropped(self):
        merged = self.ok('["GH_TOKEN", "MNEMOSYNE_DATA_DIR"]')
        self.assertIn("GH_TOKEN", merged)
        self.assertIn("MNEMOSYNE_DATA_DIR", merged)

    def test_several_names_may_be_requested(self):
        self.assertEqual(self.ok('["FOO"]', "A", "B"), ["FOO", "A", "B"])

    def test_duplicate_requests_are_added_once(self):
        self.assertEqual(self.ok("[]", VAR, VAR), [VAR])

    def test_output_is_compact_json_one_line(self):
        proc = self.merge('["FOO"]', VAR)
        self.assertEqual(proc.stdout, '["FOO", "%s"]\n' % VAR)

    def test_no_names_is_a_usage_error(self):
        proc = self.merge("[]")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_a_bad_variable_name_is_refused(self):
        for name in ("", "lower", "1BAD", "HAS-DASH", "HAS SPACE", "A=B"):
            with self.subTest(name=name):
                proc = self.merge("[]", name)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(proc.stdout, "")

    def test_malformed_json_is_refused_not_guessed(self):
        """Fail closed: silently treating garbage as [] would drop whatever
        the profile already passes through."""
        for stdin in ("[", "{", "not json", '["unclosed"'):
            with self.subTest(stdin=stdin):
                proc = self.merge(stdin, VAR)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(proc.stdout, "")

    def test_a_non_list_json_value_is_refused(self):
        for stdin in ('"FOO"', "7", "{}", '{"a": 1}', "true"):
            with self.subTest(stdin=stdin):
                proc = self.merge(stdin, VAR)
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(proc.stdout, "")

    def test_a_list_with_a_non_string_entry_is_refused(self):
        proc = self.merge('["FOO", 7]', VAR)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(proc.stdout, "")

    def test_nothing_is_read_from_argv_but_names(self):
        """The current value never travels on argv: a JSON list there is just
        a bad variable name and is refused."""
        proc = self.merge("[]", '["FOO"]')
        self.assertEqual(proc.returncode, 2, proc.stdout)


if __name__ == "__main__":
    unittest.main()
