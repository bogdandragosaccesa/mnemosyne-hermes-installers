"""Finding 1 of the PR #15 independent review: the installers' forge-token flag.

`dev-team/skills/dev-team-stage-pr-ci/SKILL.md` already promises operators a
flag that records only a *command* which prints the token --
`--forge-token-source` on Unix, `-ForgeTokenSource` on Windows -- and neither
installer had one. These tests pin the flag's name, where it writes, and the
secrecy contract.

Style follows the existing `test_install_unix*.py` / `test_install_windows*.py`
pattern: static/text-level assertions against the installer scripts, plus the
Unix fake-`hermes` harness for behaviour. Real Windows execution is covered by
the `install-windows` / `install-windows-ps51` CI jobs, so nothing here needs a
live Windows host.

The canary test (`ForgeTokenSecrecyTests`) is the load-bearing one: a sentinel
token value must appear in no file the installer writes and in neither stdout
nor stderr, `--dry-run` included, and the token-source command must never be
executed.
"""
import os
import re
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_install_unix import INSTALLER, InstallerSetupMixin  # noqa: E402

DEV_TEAM = Path(__file__).resolve().parents[3]
PS1 = DEV_TEAM / "install-dev-team-windows.ps1"
UNIX_FLAG = "--forge-token-source"
WIN_FLAG = "ForgeTokenSource"
TOKEN_VAR = "DEV_TEAM_GH_TOKEN"
MERGE_SCRIPT = "env_passthrough_merge.py"


class UnixFlagTextTests(unittest.TestCase):
    """Static: the Unix installer accepts exactly `--forge-token-source`."""

    @classmethod
    def setUpClass(cls):
        cls.text = INSTALLER.read_text(encoding="utf-8")

    def test_the_exact_flag_name_the_pr_ci_skill_promises(self):
        self.assertIn(UNIX_FLAG, self.text)

    def test_the_flag_is_parsed_and_needs_a_value(self):
        self.assertTrue(
            re.search(r'--forge-token-source\)\s*FORGE_TOKEN_SOURCE="\$\{2:-\}"', self.text),
            "--forge-token-source is not parsed as a value option")
        self.assertIn('[[ -n $FORGE_TOKEN_SOURCE ]] ||', self.text)

    def test_the_flag_is_documented_in_usage(self):
        usage = self.text[self.text.index("usage()"):]
        self.assertIn(UNIX_FLAG, usage[:4000])

    def test_the_devops_profile_name_is_derived_from_the_prefix(self):
        """Never a hardcoded `dev-devops`: a `--prefix t2-` install must write
        `t2-devops`."""
        self.assertNotIn("dev-devops", self.text)
        self.assertIn('FORGE_TOKEN_ROLE="devops"', self.text)
        self.assertIn('$role == "$FORGE_TOKEN_ROLE"', self.text)

    def test_only_the_two_contracted_keys_are_written(self):
        self.assertIn("secrets.command", self.text)
        self.assertIn("terminal.env_passthrough", self.text)

    def test_the_passthrough_list_is_merged_not_replaced(self):
        self.assertIn(MERGE_SCRIPT, self.text)
        self.assertIn("config get terminal.env_passthrough", self.text)


class WindowsFlagTextTests(unittest.TestCase):
    """Static: the Windows installer accepts exactly `-ForgeTokenSource`."""

    @classmethod
    def setUpClass(cls):
        cls.text = PS1.read_text(encoding="utf-8")

    def test_the_exact_parameter_name_the_pr_ci_skill_promises(self):
        self.assertTrue(re.search(r'\[string\]\s*\$ForgeTokenSource', self.text),
                        "-ForgeTokenSource is not a declared parameter")

    def test_the_parameter_is_documented(self):
        self.assertIn(".PARAMETER ForgeTokenSource", self.text,
                      "no .PARAMETER ForgeTokenSource")

    def test_the_devops_profile_name_is_derived_from_the_prefix(self):
        self.assertNotIn("dev-devops", self.text)
        self.assertIn("$ForgeTokenRole   = 'devops'", self.text,
                      "no $ForgeTokenRole")
        self.assertIn("$role -eq $ForgeTokenRole", self.text,
                      "the devops profile is not selected by role")

    def test_only_the_two_contracted_keys_are_written(self):
        self.assertIn("secrets.command", self.text, "no secrets.command")
        self.assertIn("terminal.env_passthrough", self.text, "no env_passthrough")

    def test_the_passthrough_list_is_merged_not_replaced(self):
        self.assertIn(MERGE_SCRIPT, self.text, f"no {MERGE_SCRIPT}")

    def test_both_installers_name_the_same_variable(self):
        self.assertIn(TOKEN_VAR, self.text, f"ps1 does not name {TOKEN_VAR}")
        self.assertIn(TOKEN_VAR, INSTALLER.read_text(encoding="utf-8"),
                      f"sh does not name {TOKEN_VAR}")


class SkillPromiseTests(unittest.TestCase):
    """The flag names are a promise the pr-ci skill already made; they must
    not drift apart."""

    def test_the_pr_ci_skill_still_names_both_flags(self):
        text = (DEV_TEAM / "skills" / "dev-team-stage-pr-ci" / "SKILL.md").read_text(
            encoding="utf-8")
        self.assertIn(UNIX_FLAG, text)
        self.assertIn("-" + WIN_FLAG, text)

    def test_the_docs_document_the_flag(self):
        for path in (DEV_TEAM / "README.md",
                     DEV_TEAM.parent / "docs" / "how-the-dev-team-works.md"):
            with self.subTest(doc=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(UNIX_FLAG in text,
                                f"{path.name} does not document {UNIX_FLAG}")
                self.assertTrue(WIN_FLAG in text,
                                f"{path.name} does not document -{WIN_FLAG}")


class ForgeTokenConfigTests(InstallerSetupMixin, unittest.TestCase):
    """Behaviour, through the fake `hermes`: which profile is configured and
    with which keys."""

    COMMAND = "pass show forge/github-token"

    def calls(self):
        return (self.root / "calls.log").read_text(encoding="utf-8")

    def test_without_the_flag_nothing_is_configured(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("secrets.command", self.calls())
        self.assertNotIn("terminal.env_passthrough", self.calls())

    def test_the_command_string_is_written_to_the_devops_profile(self):
        proc = self.install(UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"dev-devops config set secrets.command {self.COMMAND}", self.calls())

    def test_the_token_variable_is_added_to_the_passthrough_list(self):
        proc = self.install(UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f'dev-devops config set terminal.env_passthrough ["{TOKEN_VAR}"]',
                      self.calls())

    def test_no_other_profile_is_configured(self):
        proc = self.install(UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for line in self.calls().splitlines():
            if "secrets.command" in line or "terminal.env_passthrough" in line:
                with self.subTest(line=line):
                    self.assertTrue(line.startswith("dev-devops "), line)

    def test_the_profile_is_derived_from_the_installed_prefix(self):
        proc = self.install("--prefix", "t2-", UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"t2-devops config set secrets.command {self.COMMAND}", self.calls())
        self.assertNotIn("dev-devops", self.calls())

    def test_an_existing_passthrough_list_is_appended_to(self):
        proc = self.install(UNIX_FLAG, self.COMMAND,
                            FAKE_ENV_PASSTHROUGH='["FOO", "BAR"]')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            f'dev-devops config set terminal.env_passthrough ["FOO", "BAR", "{TOKEN_VAR}"]',
            self.calls())

    def test_a_list_that_already_has_the_variable_is_unchanged(self):
        """Re-running with the same flag value is a no-op."""
        proc = self.install(UNIX_FLAG, self.COMMAND,
                            FAKE_ENV_PASSTHROUGH=f'["FOO", "{TOKEN_VAR}"]')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(
            f'dev-devops config set terminal.env_passthrough ["FOO", "{TOKEN_VAR}"]',
            self.calls())

    def test_an_empty_flag_value_is_refused(self):
        proc = self.install(UNIX_FLAG, "")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)

    def test_dry_run_configures_nothing(self):
        proc = self.install("--dry-run", UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("secrets.command", proc.stdout)
        if (self.root / "calls.log").exists():
            self.assertNotIn("config set secrets.command", self.calls())

    def test_the_flag_without_a_selected_devops_profile_says_so(self):
        proc = self.install("--only", "qa", UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("devops", proc.stdout + proc.stderr)
        self.assertNotIn("config set secrets.command", self.calls())


class ForgeTokenSecrecyTests(InstallerSetupMixin, unittest.TestCase):
    """The canary: the installer stores a COMMAND, never a token value.

    A sentinel token value must appear in no file the installer writes and in
    neither stdout nor stderr, under `--dry-run` as well, and the command must
    never be executed.
    """

    SENTINEL = "ghp_CANARYcanary0123456789abcdefgh"

    def setUp(self):
        super().setUp()
        self.marker = self.tmp / "command-was-executed"
        script = self.tmp / "token-source.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'touch "{self.marker}"\n'
            f'printf "{TOKEN_VAR}=%s\\n" "{self.SENTINEL}"\n',
            encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.command = str(script)

    def _files_written(self):
        for root in (self.root, self.home):
            for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
                for name in filenames:
                    path = Path(dirpath) / name
                    if path.is_symlink() or not path.is_file():
                        continue
                    yield path

    def assert_no_sentinel(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(self.SENTINEL, proc.stdout, "token value reached stdout")
        self.assertNotIn(self.SENTINEL, proc.stderr, "token value reached stderr")
        self.assertFalse(self.marker.exists(),
                         "the installer executed the token-source command")
        for path in self._files_written():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            with self.subTest(path=str(path)):
                self.assertNotIn(self.SENTINEL, text, f"token value written to {path}")

    def test_no_token_value_anywhere_on_a_real_run(self):
        self.assert_no_sentinel(self.install(UNIX_FLAG, self.command))

    def test_no_token_value_anywhere_under_dry_run(self):
        self.assert_no_sentinel(self.install("--dry-run", UNIX_FLAG, self.command))

    def test_the_command_string_itself_is_what_gets_stored(self):
        proc = self.install(UNIX_FLAG, self.command)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"dev-devops config set secrets.command {self.command}",
                      (self.root / "calls.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
