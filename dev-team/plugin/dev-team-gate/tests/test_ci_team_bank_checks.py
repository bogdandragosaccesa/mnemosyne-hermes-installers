"""Behavioural tests for the CI team-bank checks (.github/scripts/
check-team-bank.sh and check-team-bank.ps1), which the install-linux and
install-windows-ps51 jobs in .github/workflows/dev-team.yml run after the
installer.

Why these exist (PR #8 fix round 4, review t_899815da C1-C4): the checks
used to live inline in the workflow, where no local test could execute
them, and three of them were wrong in ways only a real CI run exposed:

  C1  they asserted the team bank's mnemosyne.db exists, but nothing in a
      CI install creates it (the installer only mkdirs the bank, and
      `hermes memory status` creates no DB). Red run 41632104823 failed
      ps51 on exactly this. The checks now create it deliberately, through
      one linked profile's own .env pins, the way a real profile would.
  C2  the Windows operator/team collision guard compared Resolve-Path
      strings, which never follow junctions or symlinks, so it could
      never fire. The checks now compare real (link-resolved) paths.
  C3  the Windows per-profile loop only checked that SOME mnemosyne.db was
      reachable through the profile's link, never that the link resolves
      to the team bank. A profile linked to the operator's bank passed.
  C4  dev-pm in the ps51 job is created by `hermes profile create --clone`
      before the installer runs, so it keeps a private bank by design.
      That is now an explicit, asserted expectation (--private), not an
      accident of the DB-existence check.

The fake `mnemosyne` CLI below resolves its data directory with the same
precedence as the real one (mnemosyne/cli.py _default_data_dir:
MNEMOSYNE_DATA_DIR, then $HERMES_HOME/mnemosyne/data), and appends to the
DB file so a write into the wrong bank changes that bank's bytes.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]
REPO = DEV_TEAM.parent
SH = REPO / ".github" / "scripts" / "check-team-bank.sh"
PS1 = REPO / ".github" / "scripts" / "check-team-bank.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "dev-team.yml"

ROLES = ("dev-pm", "dev-architect", "dev-qa")

FAKE_CLI = """#!/bin/bash
# Same precedence as mnemosyne/cli.py _default_data_dir.
data="${MNEMOSYNE_DATA_DIR:-${HERMES_HOME:?}/mnemosyne/data}"
[ "$1" = stats ] || { echo "unexpected args: $*" >&2; exit 64; }
mkdir -p "$data"
printf 'x' >> "$data/mnemosyne.db"
echo "  DB path: $data/mnemosyne.db"
"""


class Layout:
    """A fake Hermes root as the installer leaves it on a CI runner."""

    def __init__(self, tmp, *, team_links_to_operator=False, private=(),
                 profile_links_to=None, pin_to_operator=(), no_team_bank=False,
                 quote_pins=False, root_name="hermes"):
        self.tmp = Path(tmp)
        self.root = self.tmp / root_name
        self.operator = self.root / "mnemosyne"
        self.team = self.root / "dev-team-mnemosyne"
        (self.operator / "data").mkdir(parents=True)
        self.operator_db = self.operator / "data" / "mnemosyne.db"
        self.operator_db.write_bytes(b"operator-bank")
        if team_links_to_operator:
            self.team.symlink_to(self.operator)
        elif not no_team_bank:
            self.team.mkdir()
        self.elsewhere = self.tmp / "elsewhere-bank"
        (self.elsewhere / "data").mkdir(parents=True)
        (self.elsewhere / "data" / "mnemosyne.db").write_bytes(b"elsewhere")
        profile_links_to = profile_links_to or {}
        self.dirs = {}
        for name in ROLES:
            d = self.root / "profiles" / name
            d.mkdir(parents=True)
            self.dirs[name] = d
            if name in private:
                (d / "mnemosyne" / "data").mkdir(parents=True)
                (d / "mnemosyne" / "data" / "mnemosyne.db").write_bytes(b"own")
                bank = d / "mnemosyne"
            else:
                target = {"operator": self.operator,
                          "elsewhere": self.elsewhere}.get(profile_links_to.get(name), self.team)
                (d / "mnemosyne").symlink_to(target)
                bank = self.team
            if name in pin_to_operator:
                bank = self.operator
            data_val = str(bank / "data")
            home_val = str(bank)
            if quote_pins:
                def q(v):  # item 4a (review r3): same escapes as the installer
                    return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
                data_val, home_val = q(data_val), q(home_val)
            # BOM-less UTF-8, as Set-EnvVarPin / set_env_var write it.
            (d / ".env").write_text(
                f"OTHER=1\nMNEMOSYNE_DATA_DIR={data_val}\nMNEMOSYNE_HOME={home_val}\n",
                encoding="utf-8")
        self.cli = self.tmp / "bin" / "mnemosyne"
        self.cli.parent.mkdir()
        self.cli.write_text(FAKE_CLI)
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)

    @property
    def team_db(self):
        return self.root / "dev-team-mnemosyne" / "data" / "mnemosyne.db"


class _CheckContract:
    """The contract both scripts must meet. Subclasses supply run()."""

    def check(self, layout, *, init="dev-architect", private=()):
        raise NotImplementedError

    def assertFails(self, proc, *needles):
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, f"expected failure, got rc=0:\n{out[-2000:]}")
        # A deliberate refusal, not a crash that happens to mention a path:
        # the needles must be on the script's own ::error:: line.
        errors = [ln for ln in out.splitlines() if ln.startswith("::error::")]
        self.assertTrue(errors, f"no ::error:: line (a crash?):\n{out[-2000:]}")
        for n in needles:
            self.assertTrue(any(n in ln for ln in errors), f"missing {n!r} in {errors!r}")

    def assertPasses(self, proc):
        self.assertEqual(proc.returncode, 0,
                         f"rc={proc.returncode}:\n{(proc.stdout + proc.stderr)[-3000:]}")

    # --- C1 ------------------------------------------------------------
    def test_creates_the_team_db_through_a_linked_profiles_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, private=("dev-pm",))
            self.assertFalse(lay.team_db.exists(), "precondition: a fresh install has no team DB")
            proc = self.check(lay, private=("dev-pm",))
            self.assertPasses(proc)
            self.assertTrue(lay.team_db.is_file(), "team DB was not created under dev-team-mnemosyne/data")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")

    def test_all_linked_profiles_pass_without_private(self):  # the install-linux shape
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp)
            self.assertPasses(self.check(lay))
            self.assertTrue(lay.team_db.is_file())

    def test_init_pin_pointing_at_the_operator_bank_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, pin_to_operator=("dev-architect",))
            proc = self.check(lay)
            self.assertFails(proc, "dev-architect")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")

    def test_missing_team_bank_directory_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, no_team_bank=True)
            self.assertFails(self.check(lay), "dev-team-mnemosyne")

    # --- C2 ------------------------------------------------------------
    def test_team_bank_that_is_a_link_to_the_operator_bank_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, team_links_to_operator=True)
            proc = self.check(lay)
            # The guard itself, not the per-profile check further down that
            # would also trip (every profile resolves to the operator bank).
            self.assertFails(proc, "the team bank (", "operator")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")

    # --- C3 ------------------------------------------------------------
    def test_profile_linked_to_the_operator_bank_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, profile_links_to={"dev-qa": "operator"})
            proc = self.check(lay)
            self.assertFails(proc, "dev-qa")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")

    def test_profile_linked_to_some_other_bank_with_a_db_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, profile_links_to={"dev-qa": "elsewhere"})
            self.assertFails(self.check(lay), "dev-qa")

    # --- C4 ------------------------------------------------------------
    def test_private_real_directory_fails_unless_declared_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, private=("dev-pm",))
            self.assertFails(self.check(lay), "dev-pm")

    def test_declared_private_profile_that_is_linked_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp)
            self.assertFails(self.check(lay, private=("dev-pm",)), "dev-pm")

    def test_declared_private_profile_that_is_a_link_elsewhere_fails(self):
        # Private means a real directory of its own, not any link.
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, profile_links_to={"dev-pm": "elsewhere"})
            self.assertFails(self.check(lay, private=("dev-pm",)), "dev-pm", "is a link")

    def test_init_through_a_private_profile_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, private=("dev-pm",))
            self.assertFails(self.check(lay, init="dev-pm", private=("dev-pm",)), "dev-pm")

    # --- item 4a (review r3) / operator decision (round 2): the installer
    # no longer writes pins quoted (reverted, see check-team-bank.sh's
    # Unquote-EnvPin/unquote_env_pin comments); this exercises the
    # legacy-only unquoting path in case a pin from that brief quoted
    # window is still on disk.
    # PR #9 review section 3: under Windows PowerShell 5.1 a Get-Content
    # with no -Encoding decodes these BOM-less UTF-8 pins as ANSI, so a
    # non-ASCII bank path failed the check falsely. The pwsh driver below
    # models 5.1's default (see _PS51_ANSI_DEFAULTS).
    def test_non_ascii_bank_path_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, private=("dev-pm",),
                         root_name="Dr\u0103go\u0219 \u0105\u00c5\u0445 \u56e2\u961f\u30d0\u30f3\u30af")
            proc = self.check(lay, private=("dev-pm",))
            self.assertPasses(proc)
            self.assertTrue(lay.team_db.is_file(), "team DB was not created under dev-team-mnemosyne/data")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")

    def test_quoted_env_pin_is_still_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            lay = Layout(tmp, private=("dev-pm",), quote_pins=True)
            self.assertFalse(lay.team_db.exists(), "precondition: a fresh install has no team DB")
            proc = self.check(lay, private=("dev-pm",))
            self.assertPasses(proc)
            self.assertTrue(lay.team_db.is_file(), "team DB was not created under dev-team-mnemosyne/data")
            self.assertEqual(lay.operator_db.read_bytes(), b"operator-bank", "operator DB was written")


class BashTeamBankCheckTests(_CheckContract, unittest.TestCase):
    def check(self, layout, *, init="dev-architect", private=()):
        args = ["bash", str(SH), "--root", str(layout.root), "--cli", str(layout.cli), "--init", init]
        for p in private:
            args += ["--private", p]
        args += [f"{n}={d}" for n, d in layout.dirs.items()]
        env = {k: v for k, v in os.environ.items() if not k.startswith("MNEMOSYNE_")}
        return subprocess.run(args, capture_output=True, text=True, timeout=60, env=env)


# pwsh 7 defaults Get-Content/Set-Content/Add-Content to UTF-8, Windows
# PowerShell 5.1 (the ps51 job's shell) to the ANSI code page. Model 5.1 so
# a cmdlet relying on that default reads these UTF-8 files the way 5.1 would;
# an explicit encoding or a .NET call is unaffected, which is the fix.
_PS51_ANSI_DEFAULTS = (
    "$PSDefaultParameterValues['Set-Content:Encoding'] = 'Latin1'\n"
    "$PSDefaultParameterValues['Add-Content:Encoding'] = 'Latin1'\n"
    "$PSDefaultParameterValues['Get-Content:Encoding'] = 'Latin1'\n"
)


@unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
class PwshTeamBankCheckTests(_CheckContract, unittest.TestCase):
    def check(self, layout, *, init="dev-architect", private=()):
        def q(s):
            return "'" + str(s).replace("'", "''") + "'"
        profiles = ",".join(q(f"{n}={d}") for n, d in layout.dirs.items())
        priv = ",".join(q(p) for p in private) or ""
        driver = Path(layout.tmp) / "driver.ps1"
        driver.write_text(
            _PS51_ANSI_DEFAULTS +
            f"& {q(PS1)} -HermesRoot {q(layout.root)} -MnemosyneCli {q(layout.cli)} "
            f"-InitProfile {q(init)} -ProfileDir @({profiles}) -PrivateProfile @({priv})\n"
            "exit $LASTEXITCODE\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MNEMOSYNE_")}
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                              capture_output=True, text=True, timeout=120, env=env)


class PwshScriptStaysPs51Compatible(unittest.TestCase):
    """The ps51 job runs check-team-bank.ps1 under Windows PowerShell 5.1,
    which this suite cannot execute. Keep 7-only syntax out of it."""

    def test_no_powershell7_only_operators(self):
        import re
        code = "\n".join(ln for ln in PS1.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith("#"))
        for pat, what in ((r"\?\?", "null-coalescing ??"), (r"\?\.", "null-conditional ?."),
                          (r"\s\?\s[^:]+\s:\s", "ternary ?:"), (r"&&|\|\|", "pipeline chain && / ||"),
                          (r"-AsHashtable", "ConvertFrom-Json -AsHashtable"),
                          (r"\$IsWindows", "$IsWindows (undefined on 5.1)")):
            self.assertIsNone(re.search(pat, code), f"check-team-bank.ps1 uses {what}")


class WorkflowUsesTheTestedChecks(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def job(self, name):
        start = self.text.index(f"\n  {name}:\n")
        nxt = self.text.find("\n  install-", start + 1)
        return self.text[start: nxt if nxt != -1 else len(self.text)]

    def test_linux_job_runs_the_bash_check_with_an_init_profile(self):
        job = self.job("install-linux")
        self.assertIn(".github/scripts/check-team-bank.sh", job)
        self.assertIn("--init dev-architect", job)
        self.assertIn("--cli \"$HOME/.mnemosyne-venv/bin/mnemosyne\"", job)
        # C1: the bare DB-existence assertion that nothing ever satisfied.
        self.assertNotIn('test -f "$shared/data/mnemosyne.db"', job)

    def test_ps51_job_runs_the_powershell_check_with_dev_pm_private(self):
        job = self.job("install-windows-ps51")
        self.assertIn("check-team-bank.ps1", job)
        self.assertIn("-InitProfile 'dev-architect'", job)
        self.assertIn("-PrivateProfile @('dev-pm')", job)
        self.assertIn(".mnemosyne-venv\\Scripts\\mnemosyne.exe", job)
        # C2: Resolve-Path never follows links.
        self.assertNotIn("Resolve-Path -LiteralPath $db", job)
        # C3: DB reachability alone is not a link-target check.
        self.assertNotIn("Join-Path $dir 'mnemosyne\\data\\mnemosyne.db'", job)

    def test_check_scripts_are_invoked_after_the_last_installer_run(self):
        for name, script in (("install-linux", "check-team-bank.sh"),
                             ("install-windows-ps51", "check-team-bank.ps1")):
            job = self.job(name)
            last_install = job.rindex("install-dev-team-")
            self.assertLess(last_install, job.index(script), f"{name}: check runs before the last install")


class EnvPinUnquotingTests(unittest.TestCase):
    """Item 4a (review r3) / operator decision (round 2): isolated contract
    tests for the LEGACY-ONLY escape decoding, run directly against the
    real function bodies extracted from each script (never a
    reimplementation), so a future edit to the real unquoting logic still
    has to pass here. The installer no longer writes pins in this format
    (reverted to unquoted); these functions only exist to still resolve a
    pin left over from the brief window where the quoted format shipped.
    Covers the escape cases the full-check Layout fixture cannot (its
    temp-dir paths never contain a backslash or an apostrophe)."""

    CASES = [
        ("plain/no/specials", "plain/no/specials"),
        ("/t/x #y", "/t/x #y"),
        ("a&b|c d", "a&b|c d"),
        ("path with 'quote' and \\backslash\\", "path with 'quote' and \\backslash\\"),
        ("ăș", "ăș"),
        ("a\\\\b", "a\\\\b"),  # already-escaped-looking literal backslashes, still round-trip
    ]

    @staticmethod
    def _quote(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

    def _extract_bash_function(self, name):
        lines = SH.read_text().splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith(f"{name}()"))
        end = next(i for i in range(start, len(lines)) if lines[i] == "}")
        return "\n".join(lines[start:end + 1])

    def test_bash_unquote_env_pin_round_trips_every_case(self):
        func_src = self._extract_bash_function("unquote_env_pin")
        for raw, expected in self.CASES:
            quoted = self._quote(raw)
            script = f'{func_src}\nunquote_env_pin "$1"\n'
            proc = subprocess.run(["bash", "-c", script, "_", quoted],
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, expected, f"input quoted={quoted!r}")

    def test_bash_unquote_env_pin_passes_through_legacy_unquoted(self):
        func_src = self._extract_bash_function("unquote_env_pin")
        legacy = "/legacy/unquoted/path"
        script = f'{func_src}\nunquote_env_pin "$1"\n'
        proc = subprocess.run(["bash", "-c", script, "_", legacy],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, legacy)

    @staticmethod
    def _extract_pwsh_function():
        ps1_text = PS1.read_text()
        start = ps1_text.index("function Unquote-EnvPin")
        end = ps1_text.index("\nfunction Get-EnvPin", start)
        return ps1_text[start:end]

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_pwsh_unquote_env_pin_round_trips_every_case(self):
        func_src = self._extract_pwsh_function()
        for raw, expected in self.CASES:
            quoted = self._quote(raw)
            with tempfile.TemporaryDirectory() as tmp:
                driver = Path(tmp) / "driver.ps1"
                driver.write_text(
                    func_src + "\n"
                    "$v = [Console]::In.ReadToEnd()\n"
                    "[Console]::Out.Write((Unquote-EnvPin $v))\n")
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                    input=quoted, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout, expected, f"input quoted={quoted!r}")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_pwsh_unquote_env_pin_passes_through_legacy_unquoted(self):
        func_src = self._extract_pwsh_function()
        legacy = "/legacy/unquoted/path"
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / "driver.ps1"
            driver.write_text(
                func_src + "\n"
                "$v = [Console]::In.ReadToEnd()\n"
                "[Console]::Out.Write((Unquote-EnvPin $v))\n")
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(driver)],
                input=legacy, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, legacy)


if __name__ == "__main__":
    unittest.main()
