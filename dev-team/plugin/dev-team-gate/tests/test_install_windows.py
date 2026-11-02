"""Static checks of install-dev-team-windows.ps1 and the CI workflow (review #2
and the Minor items). The behaviour itself runs in CI on Windows PowerShell
5.1 (the `install-windows-ps51` job); these keep the source honest between
runs, and parse the script with PowerShell when `pwsh` is installed.

Assertion messages are short on purpose: a failing assertIn would print the
whole 27 kB script."""
import os
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

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_T2_refuses_an_empty_provider_or_model_half(self):
        # -TierFast 'anthropic:' (empty model) and -TierFast ':model' (empty
        # provider) both split cleanly on ':' and so slipped past
        # Split-TierPM's "no colon" check -- unlike the Unix installer,
        # which requires both halves non-empty. Same contract here: exit 2
        # with "must be PROVIDER:MODEL with both non-empty".
        for param, value in (("-TierFast", "anthropic:"),
                              ("-TierFast", ":model"),
                              ("-TierStandard", "anthropic:"),
                              ("-TierDeep", ":model")):
            with self.subTest(param=param, value=value), tempfile.TemporaryDirectory() as tmp:
                proc = subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(PS1),
                     "-DryRun", param, value],
                    capture_output=True, text=True, timeout=120,
                    env={"HOME": tmp, "PATH": "/usr/bin:/bin:/snap/bin",
                         "USERPROFILE": tmp})
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("must be PROVIDER:MODEL with both non-empty", proc.stderr)
                self.assertIn(param, proc.stderr)

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


class WindowsT4TeamHomeRootTests(unittest.TestCase):
    """T4: the team home must live at the Hermes root, not inside whatever
    profile happens to be running the installer. install-dev-team-windows.ps1
    has no fake-hermes harness (a full run needs real junctions, which this
    sandbox cannot create), so these exercise the two pure pieces of the fix
    in isolation the same way test_invoke_checked_command_... does above:
    the root-climb Split-Path logic, and the migration decision function.
    """

    def setUp(self):
        self.text = PS1.read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_root_climb_logic_climbs_out_of_a_profile_directory(self):
        start = self.text.index("$RootConfig = Get-HermesLastLine")
        end = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        snippet = self.text[start:end]
        self.assertIn("-eq 'profiles'", snippet, "no profiles-climb check")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            profile_dir = root / "profiles" / "devops"
            profile_dir.mkdir(parents=True)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "function Invoke-Hermes { param([Parameter(ValueFromRemainingArguments=$true)]$a) "
                f"$global:LASTEXITCODE = 0; '{profile_dir / 'config.yaml'}' }}\n"
                "function Get-HermesLastLine { param($r) $r }\n"
                + snippet +
                "\nWrite-Output \"ROOT=$HermesRoot\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"ROOT={root}", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_root_climb_logic_leaves_the_true_root_alone(self):
        start = self.text.index("$RootConfig = Get-HermesLastLine")
        end = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        snippet = self.text[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir(parents=True)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "function Invoke-Hermes { param([Parameter(ValueFromRemainingArguments=$true)]$a) "
                f"$global:LASTEXITCODE = 0; '{root / 'config.yaml'}' }}\n"
                "function Get-HermesLastLine { param($r) $r }\n"
                + snippet +
                "\nWrite-Output \"ROOT=$HermesRoot\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"ROOT={root}", proc.stdout)

    def _migration_harness(self, tmp):
        norm_fn = _function(self.text, "Get-NormalizedPath")
        link_fn = _function(self.text, "New-DirLink")
        test_fn = _function(self.text, "Test-DirHasData")
        migrate_fn = _function(self.text, "Invoke-TeamHomeMigration")
        harness = Path(tmp) / "harness.ps1"
        harness.write_text(norm_fn + "\n" + link_fn + "\n" + test_fn + "\n" + migrate_fn + "\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_migration_moves_data_when_the_new_home_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-team-home"
            old.mkdir()
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            new = Path(tmp) / "new-team-home"
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"$r = Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome '{old}'\n")
                f.write("Write-Output \"CHOICE=$($r.Choice)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("CHOICE=move", proc.stdout)
            self.assertEqual((new / "deciders-ledger.jsonl").read_text(), "ledger-data")
            # D3 (review): the old path must keep resolving to the data via
            # a junction. Only verifiable on real Windows -- Linux pwsh's
            # New-Item -ItemType Junction reports success without creating
            # anything Test-Path recognizes, a platform limitation the
            # install-windows-ps51 CI job exists to cover on real Windows.
            if os.name == "nt":
                self.assertTrue(old.exists(), "old path must still resolve after migration")
                self.assertEqual((old / "deciders-ledger.jsonl").read_text(), "ledger-data")
            backups = list(Path(tmp).glob("old-team-home.pre-root-migration-*"))
            self.assertEqual(len(backups), 1, backups)
            self.assertEqual((backups[0] / "deciders-ledger.jsonl").read_text(), "ledger-data")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N1_migration_into_an_existing_empty_root_does_not_nest(self):
        # Review r2 N1: Move-Item into an existing (empty) folder moves the
        # source INSIDE it. The empty root must be removed first.
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-team-home"
            old.mkdir()
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            new = Path(tmp) / "new-team-home"
            new.mkdir()  # exists, empty
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"$r = Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome '{old}'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((new / "deciders-ledger.jsonl").read_text(), "ledger-data")
            self.assertFalse((new / "old-team-home").exists(), "data nested one level too deep")

    def test_N2_N4_conflict_text_relinks_to_the_root_and_hides_the_sentinel(self):
        # Review r2 N2/N4 (static): the conflict block must not print a
        # comment naming an installer-internal function as the relink step,
        # and must not show the '__root_link__' sentinel to the user.
        self.assertNotIn("# relink ${profileName}: New-DirLink", self.text)
        self.assertNotIn("linked from: $($entry.Profiles -join ' ')", self.text)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_migration_refuses_when_both_homes_hold_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-team-home"
            old.mkdir()
            (old / "deciders-ledger.jsonl").write_text("old-ledger")
            new = Path(tmp) / "new-team-home"
            new.mkdir()
            (new / "model-tiers.json").write_text("new-existing")
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome '{old}' | Out-Null\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("Team home conflict", proc.stderr)
            self.assertIn(str(old), proc.stderr)
            self.assertIn(str(new), proc.stderr)
            self.assertEqual((old / "deciders-ledger.jsonl").read_text(), "old-ledger")
            self.assertEqual((new / "model-tiers.json").read_text(), "new-existing")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_migration_is_a_noop_when_no_old_home_was_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "new-team-home"
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"$r = Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome $null\n")
                f.write("Write-Output \"CHOICE=$($r.Choice)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("CHOICE=none", proc.stdout)
            self.assertFalse(new.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_migration_lists_entries_that_would_move_in_dry_run(self):
        # Review finding #4: 'what moved' must list the top-level entries,
        # not only the source/destination paths, under -DryRun too.
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-team-home"
            old.mkdir()
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            (old / "model-tiers.json").write_text("old-tiers")
            new = Path(tmp) / "new-team-home"
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome '{old}' -DryRun | Out-Null\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("Would migrate team state:", proc.stdout)
            self.assertIn("entries that would move:", proc.stdout)
            self.assertIn("deciders-ledger.jsonl", proc.stdout)
            self.assertIn("model-tiers.json", proc.stdout)
            # Nothing changed.
            self.assertTrue(old.exists())
            self.assertFalse(new.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_migration_lists_entries_moved(self):
        # Review finding #4, non-dry-run side.
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-team-home"
            old.mkdir()
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            (old / "model-tiers.json").write_text("old-tiers")
            new = Path(tmp) / "new-team-home"
            harness = self._migration_harness(tmp)
            with harness.open("a") as f:
                f.write(f"Invoke-TeamHomeMigration -DevTeamHome '{new}' -OldTeamHome '{old}' | Out-Null\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("entries moved:", proc.stdout)
            self.assertIn("deciders-ledger.jsonl", proc.stdout)
            self.assertIn("model-tiers.json", proc.stdout)

    def _normalized_path_harness(self, tmp):
        norm_fn = _function(self.text, "Get-NormalizedPath")
        harness = Path(tmp) / "harness.ps1"
        harness.write_text(norm_fn + "\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_normalized_path_strips_nt_namespace_prefix(self):
        # Review finding #3: a junction's .Target can come back with the
        # '\??\' NT-namespace prefix; without stripping it, a junction that
        # already points at the correct root would be read as a different,
        # old location on every re-run.
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._normalized_path_harness(tmp)
            target = str(Path(tmp) / "hermes" / "dev-team")
            with harness.open("a") as f:
                f.write(f"$a = Get-NormalizedPath '{target}'\n")
                f.write(f"$b = Get-NormalizedPath '\\??\\{target}'\n")
                f.write("Write-Output \"EQUAL=$($a -eq $b)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("EQUAL=True", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_normalized_path_strips_trailing_backslash(self):
        with tempfile.TemporaryDirectory() as tmp:
            harness = self._normalized_path_harness(tmp)
            target = str(Path(tmp) / "hermes" / "dev-team")
            with harness.open("a") as f:
                f.write(f"$a = Get-NormalizedPath '{target}'\n")
                f.write(f"$b = Get-NormalizedPath '{target}\\'\n")
                f.write("Write-Output \"EQUAL=$($a -eq $b)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("EQUAL=True", proc.stdout)

    def _plan_harness(self, tmp):
        norm_fn = _function(self.text, "Get-NormalizedPath")
        anc_fn = _function(self.text, "Test-IsAncestorOrEqualPath")
        reason_fn = _function(self.text, "Get-UnsafeOldHomeReason")
        data_fn = _function(self.text, "Test-DirHasData")
        plan_fn = _function(self.text, "Resolve-TeamHomeMigrationPlan")
        harness = Path(tmp) / "harness.ps1"
        harness.write_text(norm_fn + "\n" + anc_fn + "\n" + reason_fn + "\n" + data_fn + "\n" + plan_fn + "\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_plan_relinks_every_profile_not_only_the_selected_one(self):
        # Review finding #1: dev-pm and dev-qa both link at the same old,
        # populated location. The plan must list both as AffectedProfiles
        # even though only one of them need be in -Only's selection.
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old-area" / "dev-team"
            old.mkdir(parents=True)
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            new = Path(tmp) / "new-team-home"
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '" + str(old) + "'; 'dev-qa' = '" + str(old) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output \"OLD=$($r.OldTeamHome)\"\n"
                    "Write-Output \"AFFECTED=$(($r.AffectedProfiles | Sort-Object) -join ',')\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=move", proc.stdout)
            self.assertIn(f"OLD={old}", proc.stdout)
            self.assertIn("AFFECTED=dev-pm,dev-qa", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_plan_refuses_when_two_distinct_old_homes_both_hold_data(self):
        # Review finding #2: the plan must not stop at the first populated
        # old target; a second, distinct populated location must also be
        # reported, not silently orphaned.
        with tempfile.TemporaryDirectory() as tmp:
            old_pm = Path(tmp) / "pm-area" / "dev-team"
            old_pm.mkdir(parents=True)
            (old_pm / "deciders-ledger.jsonl").write_text("pm-ledger")
            old_uiux = Path(tmp) / "uiux-area" / "dev-team"
            old_uiux.mkdir(parents=True)
            (old_uiux / "model-tiers.json").write_text("uiux-tiers")
            new = Path(tmp) / "new-team-home"
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '" + str(old_pm) + "'; 'dev-uiux' = '" + str(old_uiux) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output $r.Message\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=conflict", proc.stdout)
            self.assertIn(str(old_pm), proc.stdout)
            self.assertIn(str(old_uiux), proc.stdout)
            # Neither old location was touched.
            self.assertEqual((old_pm / "deciders-ledger.jsonl").read_text(), "pm-ledger")
            self.assertEqual((old_uiux / "model-tiers.json").read_text(), "uiux-tiers")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_plan_treats_a_junction_already_at_the_root_as_not_old(self):
        # Review finding #3: a candidate whose target, once normalized,
        # already equals the new root (even with a '\??\' prefix or a
        # trailing backslash) must not be treated as an old location.
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "new-team-home"
            new.mkdir()
            (new / "model-tiers.json").write_text("current")
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '\\??\\" + str(new) + "\\' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=none", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D1_refuses_a_candidate_not_named_dev_team(self):
        # Repro F/H equivalent: a candidate junction resolves to something
        # not literally named "dev-team" -- migrating it would move an
        # entire profile or other directory.
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "new-team-home"
            not_dev_team = Path(tmp) / "profiles" / "dev-pm"
            not_dev_team.mkdir(parents=True)
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '" + str(not_dev_team) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output $r.Message\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=invalid", proc.stdout)
            self.assertIn("not 'dev-team'", proc.stdout)
            self.assertFalse(new.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D1_refuses_a_candidate_equal_to_a_profile_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "hermes" / "dev-team"
            profile_dir = Path(tmp) / "hermes" / "profiles" / "dev-team"
            profile_dir.mkdir(parents=True)
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '" + str(profile_dir) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' "
                    f"-CandidateTargets $candidates -HermesRoot '{Path(tmp) / 'hermes'}' "
                    f"-ProfileDirs @('{profile_dir}')\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=invalid", proc.stdout)
            self.assertFalse(new.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D4_root_link_target_is_treated_as_an_old_home_candidate(self):
        # D4: when $DevTeamHome itself is a link, its target must be fed in
        # as the "__root_link__" candidate and not double-counted against
        # itself as a second populated location.
        with tempfile.TemporaryDirectory() as tmp:
            new = Path(tmp) / "hermes" / "dev-team"
            root_target = Path(tmp) / "hermes" / "profiles" / "devops" / "dev-team"
            root_target.mkdir(parents=True)
            (root_target / "deciders-ledger.jsonl").write_text("devops-ledger")
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ '__root_link__' = '" + str(root_target) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' "
                    f"-CandidateTargets $candidates -HermesRoot '{Path(tmp) / 'hermes'}' "
                    "-DevTeamHomeIsLink\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output \"OLD=$($r.OldTeamHome)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=move", proc.stdout)
            self.assertIn(f"OLD={root_target}", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D5_conflict_message_has_no_placeholder_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "pm-area" / "dev-team"
            old.mkdir(parents=True)
            (old / "deciders-ledger.jsonl").write_text("ledger-data")
            new = Path(tmp) / "new-team-home"
            new.mkdir()
            (new / "model-tiers.json").write_text("new-existing")
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ 'dev-pm' = '" + str(old) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output $r.Message\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=conflict", proc.stdout)
            self.assertNotIn("<", proc.stdout)  # no "<placeholder>" text
            self.assertIn(f"if (Test-Path -LiteralPath '{new}')", proc.stdout)
            self.assertIn(f"Move-Item -LiteralPath '{old}'", proc.stdout)
            # N2 (review r2): the relink step points every profile at the root
            # (via a re-run), never at the just-moved chosen path.
            self.assertIn("re-run this installer: it relinks every team profile", proc.stdout)
            self.assertNotIn("New-DirLink against", proc.stdout)


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

    def test_windows_powershell_51_job_tests_junction_based_migration(self):
        # D6 (review): the scan loop and the junction relink had no test
        # exercising them, even with pwsh. Must build a junction-based old
        # home under a profile, run the installer, and assert the data
        # moved, the junctions point at the root, the old path still
        # resolves (D3), and the backup is intact.
        job = self.job("install-windows-ps51")
        self.has("New-Item -ItemType Junction -Path (Join-Path $pmDir 'dev-team')", job,
                  "no junction-based old home built under a profile")
        self.has("deciders-ledger.jsonl", job, "no data-moved assertion")
        self.has("LinkType", job, "no junction-points-at-the-root assertion")
        self.has("D3: old path", job, "no old-path-still-resolves (D3) assertion")
        self.has("pre-root-migration-", job, "no backup-intact assertion")
        # The migration test runs between the two installer invocations.
        first_run = job.index("install-dev-team-windows.ps1")
        junction_build = job.index("New-Item -ItemType Junction -Path (Join-Path $pmDir 'dev-team')")
        second_run = job.rindex("install-dev-team-windows.ps1")
        self.assertLess(junction_build, first_run, "old home must be built before the first run")
        self.assertLess(first_run, job.index("D3: old path"), "D3 assertion must run after the first (migrating) run")
        self.assertLess(job.index("D3: old path"), second_run, "D6 assertions must run before the idempotency re-run")

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
