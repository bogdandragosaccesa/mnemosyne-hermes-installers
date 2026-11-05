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
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
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
        self.assertIn(f"dev-devops config set secrets.command.command {self.COMMAND}",
                      self.calls())

    def test_the_source_is_also_enabled(self):
        """Defect A: CommandSource defaults `enabled` to False, so writing
        only `secrets.command.command` would leave the source disabled."""
        proc = self.install(UNIX_FLAG, self.COMMAND)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dev-devops config set secrets.command.enabled true", self.calls())

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
        self.assertIn(f"t2-devops config set secrets.command.command {self.COMMAND}", self.calls())
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
        self.assertIn(f"dev-devops config set secrets.command.command {self.command}",
                      (self.root / "calls.log").read_text(encoding="utf-8"))


class WindowsRefusalTests(unittest.TestCase):
    """Defect B: `-ForgeTokenSource` must refuse on Windows (POSIX-only
    secret source), not silently record devops profile config Hermes will
    never read there. Static/text-level only -- per this file's own
    convention, real Windows execution lives in the install-windows /
    install-windows-ps51 CI jobs, so nothing here needs a live Windows host.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = PS1.read_text(encoding="utf-8")

    def test_the_flag_is_refused_before_Set_ForgeTokenSource_is_reachable(self):
        refusal_idx = self.text.index("is refused on Windows")
        func_idx = self.text.index("function Set-ForgeTokenSource")
        self.assertLess(refusal_idx, func_idx,
                        "the Windows refusal must run before Set-ForgeTokenSource "
                        "(the function that would write config) is ever called")

    def test_the_refusal_exits_2(self):
        refusal_idx = self.text.index("is refused on Windows")
        snippet = self.text[max(0, refusal_idx - 400):refusal_idx + 400]
        self.assertIn("exit 2", snippet,
                      "refusal must use the installers' existing bad-usage exit code")

    def test_the_refusal_names_the_posix_only_reason(self):
        self.assertIn("POSIX-only", self.text)

    def test_the_param_doc_no_longer_promises_it_works(self):
        doc = self.text[self.text.index(".PARAMETER ForgeTokenSource"):]
        doc = doc[:doc.index(".EXAMPLE")]
        self.assertIn("Refused", doc)
        self.assertNotIn("this installer never runs it", doc)

    def test_readme_and_changelog_state_the_posix_only_limit(self):
        readme = (DEV_TEAM / "README.md").read_text(encoding="utf-8")
        changelog = (DEV_TEAM.parent / "CHANGELOG.md").read_text(encoding="utf-8")
        for doc_name, text in (("README.md", readme), ("CHANGELOG.md", changelog)):
            with self.subTest(doc=doc_name):
                self.assertIn("POSIX-only", text,
                             f"{doc_name} does not state the POSIX-only limit")
                self.assertIn("refused", text.lower(),
                             f"{doc_name} does not say -ForgeTokenSource is refused")


class TokenShapeRefusalTests(InstallerSetupMixin, unittest.TestCase):
    """Defect D / D3: a COMMAND containing a literal token shape is refused,
    exit 2, nothing written, reusing forge_core.py's existing
    github_pat_/gh[pours]_ patterns (no new regexes)."""

    def _calls(self):
        p = self.root / "calls.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def test_a_classic_token_shape_is_refused(self):
        proc = self.install(UNIX_FLAG, "echo ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("config set secrets.command", self._calls())

    def test_a_fine_grained_pat_shape_is_refused(self):
        proc = self.install(UNIX_FLAG, "echo github_pat_aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("config set secrets.command", self._calls())

    def test_the_refusal_holds_under_dry_run_too(self):
        """So the shape can never even reach --dry-run's own echo of COMMAND."""
        canary = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        proc = self.install("--dry-run", UNIX_FLAG, f"echo {canary}")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn(canary, proc.stdout)
        self.assertNotIn(canary, proc.stderr)

    def test_a_command_with_no_token_shape_still_works(self):
        """The check must not be so broad it refuses ordinary commands."""
        proc = self.install(UNIX_FLAG, "pass show forge/github-token")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


def _find_agent_root():
    """Best-effort discovery of a checkout exposing `agent.secret_sources`,
    in the same spirit as test_install_unix.py's `_find_dotenv_python`:
    never assumed to exist, so this returns None (never raises) when it
    can't be found -- e.g. on CI, where only this installers repo is
    checked out and no Hermes source tree sits under the runner's home."""
    home = Path.home()
    for pattern in (".hermes/*/agent/secret_sources/registry.py",
                    ".hermes/installs/*/agent/secret_sources/registry.py"):
        for hit in home.glob(pattern):
            root = hit.parents[2]
            if (root / "agent" / "secret_sources" / "registry.py").is_file():
                return root
    return None


class RealHermesRoundTripTests(unittest.TestCase):
    """The non-negotiable, non-stub test for defect A: CI was green on a
    broken flag because every prior assertion ran only against a stub
    `hermes`. This runs the REAL `hermes config set` against a throwaway
    HERMES_HOME and reads the result back through Hermes' own registry
    (`enabled_source_names`), so a regression to the single-string
    `secrets.command` shape fails here even if every stub assertion above
    is satisfied by accident.

    Skips cleanly -- never fails red -- when either the real `hermes` CLI
    or the `agent` package it ships is not reachable from this host, so CI
    on an environment without a real Hermes install stays green.
    """

    @classmethod
    def setUpClass(cls):
        cls.hermes_bin = shutil.which("hermes")
        if not cls.hermes_bin:
            raise unittest.SkipTest("no real `hermes` CLI on PATH")
        agent_root = _find_agent_root()
        if agent_root is None:
            raise unittest.SkipTest("no importable `agent` package found under ~/.hermes")
        if str(agent_root) not in sys.path:
            sys.path.insert(0, str(agent_root))
        try:
            import agent.secret_sources.registry  # noqa: F401
        except Exception as exc:
            raise unittest.SkipTest(f"agent.secret_sources.registry not importable: {exc!r}")

    def _hermes(self, home, *args):
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        env.pop("HERMES_PROFILE", None)
        env.pop("HERMES_PROFILE_NAME", None)
        return subprocess.run([self.hermes_bin, "config", *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_the_two_leaf_keys_make_the_real_registry_report_command_enabled(self):
        from agent.secret_sources.registry import enabled_source_names

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            r1 = self._hermes(home, "set", "secrets.command.command", "echo hi")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            r2 = self._hermes(home, "set", "secrets.command.enabled", "true")
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

            r3 = self._hermes(home, "get", "secrets", "--json")
            self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
            secrets_cfg = json.loads(r3.stdout)

            names = enabled_source_names(secrets_cfg, home)
            self.assertIn("command", names,
                          "writing secrets.command.command and secrets.command.enabled "
                          "must enable the real CommandSource -- a regression to the "
                          "single-string shape (defect A) would make this fail")

    def test_writing_only_the_command_leaf_leaves_the_source_disabled(self):
        """Pins WHY both leaf keys are required: `enabled` defaults to False,
        which is the exact bug defect A shipped (only `secrets.command` was
        written, as a plain string)."""
        from agent.secret_sources.registry import enabled_source_names

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            r1 = self._hermes(home, "set", "secrets.command.command", "echo hi")
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)

            r3 = self._hermes(home, "get", "secrets", "--json")
            self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
            secrets_cfg = json.loads(r3.stdout)

            names = enabled_source_names(secrets_cfg, home)
            self.assertNotIn("command", names)


if __name__ == "__main__":
    unittest.main()
