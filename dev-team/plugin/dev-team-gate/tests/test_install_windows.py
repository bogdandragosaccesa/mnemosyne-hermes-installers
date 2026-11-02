"""Static checks of install-dev-team-windows.ps1 and the CI workflow (review #2
and the Minor items). The behaviour itself runs in CI on Windows PowerShell
5.1 (the `install-windows-ps51` job); these keep the source honest between
runs, and parse the script with PowerShell when `pwsh` is installed.

Assertion messages are short on purpose: a failing assertIn would print the
whole 27 kB script."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]
PS1 = DEV_TEAM / "install-dev-team-windows.ps1"
WORKFLOW = DEV_TEAM.parent / ".github" / "workflows" / "dev-team.yml"


def _function(text: str, name: str) -> str:
    """The body of a top-level PowerShell function: up to its closing `}` at
    column 0."""
    start = text.index(f"function {name}")
    end = text.index("\n}\n", start)
    return text[start:end + 2]


class WindowsInstallerTests(unittest.TestCase):
    def setUp(self):
        # read_text translates CRLF, so "\n" works for either line ending
        self.text = PS1.read_text(encoding="utf-8")

    def has(self, needle, haystack, what):
        self.assertTrue(needle in haystack, what)

    def test_new_dirlink_deletes_a_link_without_recursing(self):
        body = _function(self.text, "New-DirLink")
        self.has(".LinkType", body, "New-DirLink ignores .LinkType")
        self.has("[System.IO.Directory]::Delete($LinkPath)", body, "no Directory.Delete")
        for line in body.splitlines():
            if "::Delete(" in line:
                self.assertFalse("$true" in line, "Directory.Delete(path, $true) recurses")
        # -Recurse only on the branch for a real directory, never for a link
        link_branch = body[body.index(".LinkType"): body.index("::Delete(")]
        code = [ln for ln in link_branch.splitlines() if not ln.strip().startswith("#")]
        self.assertFalse(any("-Recurse" in ln for ln in code), "link branch recurses")

    def test_refuses_a_forge_cmd_it_did_not_write_before_touching_profiles(self):
        self.has("REM dev-team forge wrapper (install-dev-team-windows.ps1)", self.text,
                 "forge.cmd marker missing")
        self.has("was not written by this installer", self.text, "no forge.cmd refusal")
        refusal = self.text.index("was not written by this installer")
        first_loop = self.text.index("\nforeach ($role in $Profiles.Keys) {\n    if (-not (Test-Selected")
        self.assertLess(refusal, first_loop, "forge.cmd refusal comes after profiles are touched")

    def test_writes_the_forge_marker_into_forge_cmd(self):
        self.assertTrue(re.search(r"\$forgeCmdContent = .*\$ForgeMarker", self.text),
                        "forge.cmd content lacks the marker")

    def test_plugin_enabled_check_is_retried(self):  # item 11, Windows side
        self.has("function Test-Eventually", self.text, "no bounded retry helper")
        self.assertTrue(re.search(r"Test-Eventually \{[^}]*plugins' 'list'", self.text),
                        "plugins list check not retried")

    def test_tier_table_defaults_fast_to_a_chat_model(self):
        self.has("'anthropic/claude-haiku-4-5'", self.text, "no haiku fast default")
        self.assertFalse("liquid" in self.text, "a decisions model is still suggested")

    def test_I5_never_tells_the_operator_to_use_a_plaintext_env_file(self):
        # Both the dry-run and real-run key notes used to say "add
        # OPENROUTER_DECIDERS_KEY to each profile's .env".
        self.assertNotIn(".env", self.text, "still points at a plaintext .env file")
        self.has("setx OPENROUTER_DECIDERS_KEY", self.text,
                 "no setx guidance for a user environment variable")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_refuses_a_decisions_only_model_as_a_tier(self):
        # The refusal runs before anything is read or written, so the script
        # can be run as-is; HOME points at an empty temp dir all the same.
        for param, value in (("-TierFast", "openrouter:liquid/d1.1"),
                             ("-TierDeep", "openrouter:perplexity/pplx-decider-v1.2")):
            with self.subTest(param=param), tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                     "-DryRun", param, value],
                    capture_output=True, text=True, timeout=120,
                    env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                         "USERPROFILE": tmp})
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("decisions-only", proc.stderr)
                self.assertIn(param, proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_I3_refuses_additional_decision_models_and_the_pplx_family(self):
        for param, value in (
            ("-TierFast", "typesafe:jev-1.13"),
            ("-TierStandard", "upstage:solar-decide-flash"),
            ("-TierDeep", "perplexity:pplx-decider-v2"),  # family, not just v1.2
        ):
            with self.subTest(param=param, value=value), tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                     "-DryRun", param, value],
                    capture_output=True, text=True, timeout=120,
                    env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                         "USERPROFILE": tmp})
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("decisions-only", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_I3_refuses_a_variant_suffix_on_a_decision_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                 "-DryRun", "-TierFast", "openrouter:liquid/d1.1:nitro"],
                capture_output=True, text=True, timeout=120,
                env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                     "USERPROFILE": tmp})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("decisions-only", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_I3_refuses_a_decisions_only_model_via_the_model_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                 "-DryRun", "-Model", "liquid/d1.1"],
                capture_output=True, text=True, timeout=120,
                env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                     "USERPROFILE": tmp})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("decisions-only", proc.stderr)
            self.assertIn("-Model", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_M8_refuses_an_empty_tier_fast_value(self):
        # -TierFast '' used to be treated as "not passed" ($TierFast is
        # falsy for an empty string in PowerShell) and silently default --
        # must now refuse, same as the Unix installer's empty-model case.
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                 "-DryRun", "-TierFast", ""],
                capture_output=True, text=True, timeout=120,
                env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                     "USERPROFILE": tmp})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("-TierFast", proc.stderr)

    def test_wrappers_name_a_resolved_python_not_a_bare_python(self):
        # A bare `python` hits the Microsoft Store alias stub on machines with
        # no system Python ("Python was not found", exit 9009), which failed
        # both Windows CI jobs. Both wrappers must call the resolved
        # interpreter, and the installer must refuse when none is found.
        for var in ("forgeCmdContent", "decidersCmdContent"):
            line = re.search(var + r" = (.*)", self.text).group(1)
            self.assertIn("$WrapperPython", line, f"{var} does not use $WrapperPython")
            self.assertNotRegex(line, r"`r`npython ", f"{var} still calls a bare python")
        self.assertIn("function Resolve-WrapperPython", self.text)
        self.assertIn("WindowsApps", self.text, "resolver does not skip the Store alias stub")
        self.assertIn("No Python 3 found for the forge and deciders wrappers", self.text)

    def test_deciders_cmd_propagates_exit_code_explicitly(self):
        # install-windows-ps51 (run 41451251672): `deciders --help` FAILED
        # with no explanation, because (a) the .cmd wrapper let cmd.exe's
        # own exit code stand in for python's instead of propagating it
        # explicitly, and (b) the check threw everything away with `*>
        # $null`. A .cmd whose last line is a bare command relies on
        # cmd.exe's ERRORLEVEL propagation, which is not reliable across
        # shells launching it; an explicit `exit /b %ERRORLEVEL%` is.
        self.assertTrue(
            re.search(r"decidersCmdContent = .*exit /b %ERRORLEVEL%", self.text),
            "deciders.cmd does not explicitly propagate python's exit code")

    def test_deciders_help_check_prints_output_instead_of_discarding_it(self):
        # The old check piped everything to `*> $null`, so a failure said
        # only "FAILED" with no way to tell python-not-found from a bad
        # argument from an import error.
        self.assertNotIn("*> $null", self.text, "a check still discards all output")
        body = _function(self.text, "Invoke-CheckedCommand")
        self.has("2>&1", body, "Invoke-CheckedCommand does not capture stderr")
        verify_section = self.text[self.text.index("Write-Host 'Verifying'"):]
        deciders_check = verify_section[:verify_section.index("$ModelTiersPath -Raw")]
        self.has("Invoke-CheckedCommand", deciders_check,
                  "deciders --help check does not use Invoke-CheckedCommand")
        # On failure it must call the helper that prints the captured
        # output, not just say "FAILED" with nothing to explain it.
        self.has("Write-CheckFailure", deciders_check,
                  "deciders --help check never prints the output on failure")
        failure_printer = _function(self.text, "Write-CheckFailure")
        self.has("FAILED", failure_printer, "no FAILED label")
        self.has("Write-Host", failure_printer, "failure printer never writes the output")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_invoke_checked_command_reports_exit_code_and_combined_output(self):
        # Exercises the actual helper function (not a stand-in): a fake
        # "python" that writes to both stdout and stderr then exits
        # non-zero must come back with that exit code and both lines in
        # Output, proving the check can explain a future failure instead
        # of only saying "FAILED".
        func_body = _function(self.text, "Invoke-CheckedCommand")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake_tool.sh"
            fake.write_text(
                "#!/bin/sh\necho 'to stdout'\necho 'boom: python not found' >&2\nexit 7\n")
            fake.chmod(0o755)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                func_body + "\n"
                f"$r = Invoke-CheckedCommand -FilePath '{fake}' -Arguments @('--help')\n"
                "Write-Output \"CODE=$($r.Code)\"\n"
                "Write-Output \"OUT=$($r.Output)\"\n")
            proc = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CODE=7", proc.stdout)
        self.assertIn("boom: python not found", proc.stdout)
        self.assertIn("to stdout", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_parses_under_powershell(self):
        with tempfile.TemporaryDirectory() as tmp:
            checker = Path(tmp) / "parse.ps1"
            checker.write_text(
                "$errors = $null\n"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', "
                "[ref]$null, [ref]$errors)\n"
                "if ($errors.Count) { $errors | ForEach-Object { Write-Output $_.ToString() }; exit 1 }\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(checker)],
                                  capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def job(self, name):
        m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", self.text, re.M | re.S)
        if m is None:
            self.fail(f"no job {name}")
        return m.group(1)

    def has(self, needle, haystack, what):
        self.assertTrue(needle in haystack, what)

    def test_windows_powershell_51_job_reruns_and_checks_the_link_targets(self):
        job = self.job("install-windows-ps51")
        self.has("shell: powershell", job, "not run on Windows PowerShell 5.1")
        self.assertFalse("shell: pwsh" in job, "uses pwsh, not 5.1")
        self.assertEqual(job.count("install-dev-team-windows.ps1"), 2, "installer not run twice")
        self.has("mnemosyne.db", job, "no Mnemosyne DB check")
        self.has("dev-team-gate\\guard.py", job, "no guard.py check")

    def test_linux_job_asserts_first_run_exit_0_and_shared_mnemosyne_links(self):
        job = self.job("install-linux")
        self.has("first run must exit 0", job, "no first-run exit check")
        self.has("is not a symlink to the shared bank", job, "no shared-bank link check")

    def test_linux_job_runs_dry_run_and_only(self):
        job = self.job("install-linux")
        self.has("--dry-run", job, "no --dry-run run")
        self.has("--only", job, "no --only run")

    def test_mnemosyne_install_steps_pass_github_token(self):
        # run 41451251672: install-windows and install-windows-ps51 hit
        # "GitHub API rate limit" installing Mnemosyne, unauthenticated.
        for job_name in ("install-linux", "install-windows", "install-windows-ps51"):
            job = self.job(job_name)
            step = job[job.index("Install Mnemosyne memory"):]
            step = step[:step.index("\n      - name:") if "\n      - name:" in step else len(step)]
            self.has("GITHUB_TOKEN: ${{ github.token }}", step,
                      f"{job_name}: Install Mnemosyne memory step has no GITHUB_TOKEN env")


if __name__ == "__main__":
    unittest.main()
