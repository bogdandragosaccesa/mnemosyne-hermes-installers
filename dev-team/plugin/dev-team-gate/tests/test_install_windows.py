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


if __name__ == "__main__":
    unittest.main()
