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


def _unprivileged_bind_mount_works():
    """True only if `unshare --mount --map-root-user` actually runs here.
    GitHub's ubuntu runners have the binary but forbid unprivileged user
    namespaces ("write failed /proc/self/uid_map: Operation not permitted"),
    so checking for the binary alone is not enough."""
    import shutil, subprocess
    if not shutil.which("unshare"):
        return False
    try:
        return subprocess.run(["unshare", "--mount", "--map-root-user", "true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

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

    def test_team_bank_is_a_dedicated_directory_not_the_operator_bank(self):  # Task 2
        self.has("'dev-team-mnemosyne'", self.text, "no dedicated team bank directory name")
        self.assertNotIn("$TeamMemoryHome = $env:MNEMOSYNE_HOME", self.text,
                         "MNEMOSYNE_HOME must no longer select the team bank")
        self.has("$env:MNEMOSYNE_HOME", self.text,
                 "MNEMOSYNE_HOME must still be checked by the refusal guard (review finding #1)")
        self.has("-TeamMemory", self.text, "no -TeamMemory override parameter")
        self.has("-AdoptTeamMemory", self.text, "no -AdoptTeamMemory opt-in parameter")
        self.has("OperatorMnemoHome", self.text, "no explicit operator-bank variable")

    def test_adopt_team_memory_backs_up_before_linking(self):  # Task 2, acc #3
        self.has("mnemosyne.pre-team-memory-", self.text, "no backup directory naming")
        self.has("Move-Item -LiteralPath $dataLink -Destination $backupDir", self.text,
                 "adopted directory is not moved aside as a backup")
        self.has("out of scope", self.text, "no out-of-scope note for merging old memories")

    def _profile_dir_guard_harness(self, tmp, hermes_root, profile_dir, plugin_src_mnemo):
        """F2 (review r3): the real per-profile-loop prefix, from
        $profileDir's assignment through the plugins\\mnemosyne link --
        the same slice of the real script, not a reimplementation of its
        logic -- so this proves the canonicalisation check now runs
        before SOUL, the prefix file, AND the plugin link (which used to
        run New-DirLink -- a Remove-Item -Recurse branch for a real
        directory target -- before the check ever fired)."""
        helpers_start = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        helpers_end = self.text.index("# Team state (forge project records")
        helpers = self.text[helpers_start:helpers_end]
        norm_fn = _function(self.text, "Get-NormalizedPath")
        dirlink_fn = _function(self.text, "New-DirLink")
        loop_start = self.text.index("    # F2 (review r3): canonicalise $profileDir")
        loop_end = self.text.index("# The plugin link above shares Mnemosyne's")
        loop_body = self.text[loop_start:loop_end]
        harness = Path(tmp) / "profile_dir_guard.ps1"
        harness.write_text(
            f"$HermesRoot = '{hermes_root}'\n$TeamMemory = $null\n"
            + norm_fn + "\n" + dirlink_fn + "\n" + helpers +
            f"\n$name = 'dev-qa'\n$profileDir = '{profile_dir}'\n"
            f"$KeepSoul = $false\n$DryRun = $false\n$soul = '{hermes_root}\\SOUL.md'\n"
            f"$Prefix = 'dev-'\n$PluginSrcMnemo = '{plugin_src_mnemo}'\n"
            + loop_body + "\nWrite-Output 'REACHED_PLUGIN_LINK'\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_profile_dir_guard_runs_before_soul_prefix_and_plugin_link(self):  # F2
        """Reviewer's repro: profiles\\dev-qa -> $HermesRoot, no
        -AdoptTeamMemory. Before this fix the canonicalisation check only
        ran inside Invoke-MnemosyneDataLink -- called AFTER the SOUL
        copy, the prefix write, and the plugins\\mnemosyne New-DirLink
        (whose Remove-Item -Recurse branch deleted the operator's real
        plugin directory in this exact layout under PS 5.1)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            (root / "SOUL.md").write_text("operator soul\n")
            before_soul = (root / "SOUL.md").read_bytes()
            plugin_src = Path(tmp) / "mnemosyne-plugin-src"
            plugin_src.mkdir()
            (plugin_src / "marker.txt").write_text("plugin code\n")
            profiles_dir = root / "profiles"
            profiles_dir.mkdir()
            evil_profile = profiles_dir / "dev-qa"
            evil_profile.symlink_to(root)
            harness = self._profile_dir_guard_harness(tmp, root, evil_profile, plugin_src)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("outside", proc.stderr)
            self.assertNotIn("REACHED_PLUGIN_LINK", proc.stdout)
            self.assertEqual((root / "SOUL.md").read_bytes(), before_soul,
                              "operator SOUL.md must be byte-identical")
            self.assertFalse((root / "dev-team-prefix").exists(),
                              "dev-team-prefix must not have been written to the operator root")
            self.assertFalse((root / "plugins").exists(),
                              "plugins\\mnemosyne link must never have been attempted")

    def test_I5_never_tells_the_operator_to_use_a_plaintext_env_file(self):
        # Both the dry-run and real-run key notes used to say "add
        # OPENROUTER_DECIDERS_KEY to each profile's .env". Checks the
        # OpenRouter guidance specifically rather than banning the bare
        # substring ".env" everywhere: D1's fix legitimately writes
        # MNEMOSYNE_DATA_DIR (never a secret) into each profile's own .env,
        # which is an unrelated, non-credential use of the same file name.
        occurrences = [m.start() for m in re.finditer(r"OpenRouter key for deciders", self.text)]
        self.assertTrue(occurrences, "no OpenRouter key guidance found")
        for idx_start in occurrences:
            window = self.text[idx_start: idx_start + 200]
            self.assertNotIn(".env", window, f"OpenRouter guidance still points at .env: {window!r}")
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

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N2_N4_conflict_text_relinks_to_the_root_and_hides_the_sentinel(self):
        # Review r2 N2/N4 (behavioural): drive a real conflict where one
        # candidate is keyed '__root_link__' (the root dev-team link
        # itself, not a named profile) and assert on
        # Resolve-TeamHomeMigrationPlan's actual output -- not on static
        # source-text patterns that could pass while the real message
        # still leaks the sentinel or names an internal function.
        #
        # Old (static) assertions this replaces, and their behavioural
        # equivalents below:
        #   1. assertNotIn("# relink ${profileName}: New-DirLink", self.text)
        #      -> replaced by TWO behavioural checks: (a) every relink
        #      line in the real conflict message is a '#' comment
        #      telling the operator to re-run the installer, AND
        #      (b) 'New-DirLink' never appears anywhere in the real
        #      output at all, so a comment that names the
        #      installer-internal function as the relink step (e.g.
        #      "# relink <profile>: New-DirLink" added next to the
        #      correct re-run line) still fails even though it also
        #      starts with '#' (proves the fixed relink step is what the
        #      function actually emits, not merely absent from a
        #      hand-picked source string, and that no sibling line names
        #      the internal function either).
        #   2. assertNotIn("linked from: $($entry.Profiles -join ' ')", self.text)
        #      -> replaced by: '__root_link__' never appears anywhere in
        #      the real message, and "the root dev-team link" does
        #      appear (proves the sentinel is translated for the
        #      operator in the function's actual output).
        # Both old checks are preserved as behaviour, not weakened: the
        # old test could only ever fail to notice a reintroduced literal
        # with a slightly different spelling; this one fails whenever
        # the real conflict text leaks the sentinel or drops the comment
        # prefix, regardless of exact source wording.
        with tempfile.TemporaryDirectory() as tmp:
            root_old = Path(tmp) / "root-area" / "dev-team"
            root_old.mkdir(parents=True)
            (root_old / "deciders-ledger.jsonl").write_text("root-ledger")
            pm_old = Path(tmp) / "pm-area" / "dev-team"
            pm_old.mkdir(parents=True)
            (pm_old / "model-tiers.json").write_text("pm-tiers")
            new = Path(tmp) / "new-team-home"
            harness = self._plan_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    "$candidates = @{ '__root_link__' = '" + str(root_old) + "'; "
                    "'dev-pm' = '" + str(pm_old) + "' }\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' -CandidateTargets $candidates\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output $r.Message\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=conflict", proc.stdout, "no conflict was produced")
            self.assertNotIn("__root_link__", proc.stdout,
                              "the sentinel key leaked into the operator-facing message")
            self.assertIn("the root dev-team link", proc.stdout,
                           "the root-link candidate was not described in plain language")
            self.assertNotIn("New-DirLink", proc.stdout,
                              "conflict text names an installer-internal function as the relink step")
            relink_lines = [ln.strip() for ln in proc.stdout.splitlines()
                             if "re-run this installer" in ln]
            self.assertTrue(relink_lines, "no relink-and-re-run line in the conflict message")
            for ln in relink_lines:
                self.assertTrue(ln.startswith("#"), f"relink line is not a '#' comment: {ln!r}")

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

    def _profile_dirs_harness(self, tmp):
        norm_fn = _function(self.text, "Get-NormalizedPath")
        anc_fn = _function(self.text, "Test-IsAncestorOrEqualPath")
        all_fn = _function(self.text, "Get-AllProfileDirs")
        reason_fn = _function(self.text, "Get-UnsafeOldHomeReason")
        data_fn = _function(self.text, "Test-DirHasData")
        plan_fn = _function(self.text, "Resolve-TeamHomeMigrationPlan")
        harness = Path(tmp) / "profile_dirs_harness.ps1"
        harness.write_text(
            norm_fn + "\n" + anc_fn + "\n" + all_fn + "\n" + reason_fn + "\n" + data_fn + "\n" + plan_fn + "\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N3_get_all_profile_dirs_scans_every_profile_not_only_team_ones(self):
        # N3 (review): Get-AllProfileDirs must return every directory under
        # $HermesRoot\profiles, mirroring the Unix installer's
        # "$HERMES_ROOT"/profiles/*/ glob -- not only dev-team-prefixed ones.
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            (hermes_root / "profiles" / "dev-pm").mkdir(parents=True)
            (hermes_root / "profiles" / "t14-other").mkdir(parents=True)
            harness = self._profile_dirs_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    f"$dirs = Get-AllProfileDirs -HermesRoot '{hermes_root}'\n"
                    "Write-Output \"COUNT=$($dirs.Count)\"\n"
                    "Write-Output ($dirs -join ',')\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("COUNT=2", proc.stdout)
            self.assertIn("t14-other", proc.stdout)
            self.assertIn("dev-pm", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N3_profile_dirs_scan_refuses_a_non_team_profile_directory(self):
        # N3 (review r2): the earlier name check in Get-UnsafeOldHomeReason
        # already refuses any candidate not literally named "dev-team", so
        # a candidate like profiles/t14-other is refused by THAT check
        # before the ProfileDirs loop ever runs -- it does not actually
        # exercise acceptance criterion 2. The only candidate that can
        # reach the ProfileDirs loop is one literally named "dev-team":
        # here, a decoy *profile* directory at profiles/dev-team (not the
        # real new team home, which lives at $HermesRoot\dev-team),
        # reached through another profile's dev-team junction -- exactly
        # the "migrating it would move a whole profile" case N3 exists to
        # stop. Only Get-AllProfileDirs feeding the ProfileDirs loop can
        # refuse this: the earlier name check passes (its name IS
        # "dev-team") and the DevTeamHome-equality check passes too (it is
        # not that path). Reviewer probe: fixed code -> OUTCOME=invalid
        # with "equals or contains profile directory"; mutant A
        # (`foreach ($pd in @())`, i.e. the loop disabled) -> OUTCOME=move.
        with tempfile.TemporaryDirectory() as tmp:
            hermes_root = Path(tmp) / "hermes"
            new = hermes_root / "dev-team"  # the real, correct new team home
            decoy = hermes_root / "profiles" / "dev-team"  # a DIFFERENT path, same leaf name
            decoy.mkdir(parents=True)
            (decoy / "deciders-ledger.jsonl").write_text("decoy-ledger")
            (hermes_root / "profiles" / "dev-pm").mkdir(parents=True)
            harness = self._profile_dirs_harness(tmp)
            with harness.open("a") as f:
                f.write(
                    # dev-pm's dev-team junction happens to point at a
                    # *profile* directory that is itself literally named
                    # "dev-team" -- a misconfiguration, not the real team
                    # home, and not something the name check alone catches.
                    "$candidates = @{ 'dev-pm' = '" + str(decoy) + "' }\n"
                    f"$allDirs = Get-AllProfileDirs -HermesRoot '{hermes_root}'\n"
                    f"$r = Resolve-TeamHomeMigrationPlan -DevTeamHome '{new}' "
                    "-CandidateTargets $candidates "
                    f"-HermesRoot '{hermes_root}' -ProfileDirs $allDirs\n"
                    "Write-Output \"OUTCOME=$($r.Outcome)\"\n"
                    "Write-Output $r.Message\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("OUTCOME=invalid", proc.stdout)
            self.assertIn("equals or contains profile directory", proc.stdout)
            self.assertIn(str(decoy), proc.stdout)
            self.assertFalse(new.exists())

    def test_N3_call_site_sets_profile_dirs_from_get_all_profile_dirs_and_never_appends(self):
        # N3 (review r2): acceptance criterion 1 -- the main body's call
        # site (~line 871) must set $ProfileDirs from Get-AllProfileDirs,
        # not only the role loop's $Prefix$role scan -- had no test at
        # all, so mutant B ($ProfileDirs = @() at the call site) survived
        # every other test. This script has no harness for a full run, so
        # a source check is used instead (matching this file's existing
        # static-check tests, e.g. test_team_bank_is_a_dedicated_directory...).
        self.assertRegex(
            self.text, r"\$ProfileDirs\s*=\s*Get-AllProfileDirs\s+-HermesRoot\s+\$HermesRoot",
            "main body does not set $ProfileDirs from Get-AllProfileDirs -HermesRoot $HermesRoot")
        self.assertNotIn("$ProfileDirs = @()", self.text,
                         "main body still resets $ProfileDirs to an empty array (mutant B)")
        self.assertNotIn("$ProfileDirs +=", self.text,
                         "main body appends to $ProfileDirs instead of relying on the full scan")
        # Exactly one assignment to $ProfileDirs in the whole file (the
        # call site itself) -- anything else is either the mutant or a
        # second, conflicting assignment.
        self.assertEqual(len(re.findall(r"^\$ProfileDirs\s*=", self.text, re.MULTILINE)), 1,
                         "expected exactly one top-level $ProfileDirs assignment")

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
            self.assertIn("    # then re-run this installer: it relinks every team profile", proc.stdout)
            self.assertNotIn("New-DirLink against", proc.stdout)


class WindowsTeamMemoryTests(unittest.TestCase):
    """Task 2: the team bank is a dedicated directory, never the operator's
    own bank. Exercises the pure refusal/selection logic the same way the
    T4 tests above exercise the root-climb and migration-plan pieces."""

    def setUp(self):
        self.text = PS1.read_text(encoding="utf-8")

    def _team_memory_harness(self, tmp, hermes_root, team_memory_arg=None):
        start = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        end = self.text.index("# Team state (forge project records")
        snippet = self.text[start:end]
        norm_fn = _function(self.text, "Get-NormalizedPath")
        harness = Path(tmp) / "harness.ps1"
        team_memory_param = f"$TeamMemory = '{team_memory_arg}'" if team_memory_arg else "$TeamMemory = $null"
        harness.write_text(
            f"$HermesRoot = '{hermes_root}'\n"
            f"{team_memory_param}\n"
            + norm_fn + "\n" + snippet +
            "\nWrite-Output \"TEAM_HOME=$TeamMemoryHome\"\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_default_team_home_is_dev_team_mnemosyne_not_mnemosyne(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            harness = self._team_memory_harness(tmp, root)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"TEAM_HOME={root / 'dev-team-mnemosyne'}", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_param_overrides_the_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            custom = Path(tmp) / "custom-bank"
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(custom))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"TEAM_HOME={custom}", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_the_operators_own_bank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            operator_bank = root / "mnemosyne"
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(operator_bank))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_mnemosyne_home_env_var(self):  # r2 review finding #1
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            decoy = Path(tmp) / "decoy-mnemosyne-home"
            decoy.mkdir()
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(decoy))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "MNEMOSYNE_HOME": str(decoy)})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("MNEMOSYNE_HOME", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_path_nested_in_the_operator_bank(self):  # r2 review finding #2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            nested = root / "mnemosyne" / "team"
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(nested))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)
        self.assertFalse(nested.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_path_containing_the_operator_bank(self):  # r2 review finding #2
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(root))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_param_is_made_absolute_before_use(self):  # r2 review finding #3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            harness = self._team_memory_harness(tmp, root, team_memory_arg="relbank")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60, cwd=tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"TEAM_HOME={Path(tmp) / 'relbank'}", proc.stdout)

    # --- r3 review findings #2/#3: a link ANYWHERE on the path is followed.
    # Real symlinks under Linux pwsh exercise the same link-resolution code
    # (LinkTarget / .Target) a junction takes on Windows.
    def _run_harness(self, tmp, root, team_memory_arg, env_extra=None):
        harness = self._team_memory_harness(tmp, root, team_memory_arg=str(team_memory_arg))
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, **(env_extra or {})})

    def _root_with_operator_bank(self, tmp):
        root = Path(tmp) / "hermes"
        (root / "mnemosyne" / "data").mkdir(parents=True)
        return root

    def _assert_refused(self, proc, needle="operator's own bank"):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_link_to_the_operator_bank(self):  # r3 #3 (W1)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            link = Path(tmp) / "oplink"
            link.symlink_to(root / "mnemosyne")
            self._assert_refused(self._run_harness(tmp, root, link))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_subdir_of_a_link_to_the_operator_bank(self):  # r3 #3 (W2)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            link = Path(tmp) / "oplink"
            link.symlink_to(root / "mnemosyne")
            self._assert_refused(self._run_harness(tmp, root, link / "sub"))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_the_operator_bank_through_a_link_to_the_root(self):  # r3 #2 (W9)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            link = Path(tmp) / "hlink"
            link.symlink_to(root)
            self._assert_refused(self._run_harness(tmp, root, link / "mnemosyne"))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_deep_path_through_a_link_to_the_operator_bank(self):  # r3 #2 (W3)
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            link = Path(tmp) / "oplink"
            link.symlink_to(root / "mnemosyne")
            self._assert_refused(self._run_harness(tmp, root, link / "data" / "team"))
            self.assertFalse((root / "mnemosyne" / "data" / "team").exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_chain_of_links_with_a_relative_hop(self):  # r3 #2
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            (Path(tmp) / "rel").symlink_to("hermes")          # relative target
            (Path(tmp) / "outer").symlink_to(Path(tmp) / "rel")
            self._assert_refused(self._run_harness(tmp, root, Path(tmp) / "outer" / "mnemosyne" / "x"))

    # --- D1: MNEMOSYNE_DATA_DIR is the variable that actually controls
    # Mnemosyne's data directory, so it must be treated as an operator
    # location exactly like MNEMOSYNE_HOME.
    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_mnemosyne_data_dir_env_var(self):  # D1
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            decoy = Path(tmp) / "decoy-data-dir"
            decoy.mkdir()
            proc = self._run_harness(tmp, root, decoy, env_extra={"MNEMOSYNE_DATA_DIR": str(decoy)})
        self._assert_refused(proc, needle="MNEMOSYNE_DATA_DIR")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_mnemosyne_data_dir_user_scope_value(self):  # D1
        """Genuinely behavioural attempt first (review r3 nit: this used
        to be a pure source-text check): set the decoy path as a real
        User-scope env var via [Environment]::SetEnvironmentVariable and
        run the harness unmodified, so the installer's own
        `[Environment]::GetEnvironmentVariable('MNEMOSYNE_DATA_DIR',
        'User')` call (install-dev-team-windows.ps1 near
        `_mnemosyneDataDirRaw`) picks it up for real -- exactly what a
        relocated Windows Mnemosyne install leaves behind.

        Non-Windows pwsh (what this suite and CI's Linux dev-team.yml
        jobs run under) does not implement persistent User-scope
        variables: confirmed by probe --
        `[Environment]::SetEnvironmentVariable($k, $v, 'User')` followed
        immediately by `[Environment]::GetEnvironmentVariable($k,
        'User')` in the same process comes back empty, not $v, with no
        exception raised. This test detects that round-trip failure and
        skips with an explicit reason instead of reporting a false pass;
        it does not silently fall back to a source check, because a
        source check proves the wiring exists but not that it works --
        see test_team_memory_data_dir_user_scope_is_wired_to_the_refusal
        below for that weaker, always-on guarantee."""
        marker = "MNEMOSYNE_DATA_DIR"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            decoy = Path(tmp) / "decoy-data-dir"
            decoy.mkdir()
            probe = subprocess.run(
                ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                 f"[Environment]::SetEnvironmentVariable('{marker}', '{decoy}', 'User'); "
                 f"Write-Output ([Environment]::GetEnvironmentVariable('{marker}', 'User')); "
                 f"[Environment]::SetEnvironmentVariable('{marker}', $null, 'User')"],
                capture_output=True, text=True, timeout=30)
            if probe.stdout.strip() != str(decoy):
                self.skipTest(
                    "this pwsh does not persist User-scope environment variables "
                    f"(set {decoy!s} then read back {probe.stdout.strip()!r}); a real "
                    "Windows PowerShell 5.1/7 host is needed to exercise this behaviourally")
            try:
                proc = self._run_harness(tmp, root, decoy)
                self._assert_refused(proc, needle="MNEMOSYNE_DATA_DIR")
            finally:
                subprocess.run(
                    ["pwsh", "-NoProfile", "-NonInteractive", "-Command",
                     f"[Environment]::SetEnvironmentVariable('{marker}', $null, 'User')"],
                    capture_output=True, text=True, timeout=30)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_data_dir_user_scope_is_wired_to_the_refusal(self):  # D1
        # The behavioural test above is skipped on every non-Windows pwsh
        # this suite has access to (confirmed: User-scope env vars are a
        # no-op there). This static check is the fallback it names: it
        # proves the User-scope read is wired into the same refusal path
        # as the live env-var case tested above, even though it cannot
        # prove the read itself works on a real Windows host.
        source_text = self.text[max(0, self.text.index("_mnemosyneDataDirRaw") - 400):
                                 self.text.index("_mnemosyneDataDirRaw") + 400]
        self.assertIn("GetEnvironmentVariable('MNEMOSYNE_DATA_DIR', 'User')", source_text)
        self.assertIn("_mnemosyneDataDirReal", source_text)

    # --- D5: -TeamMemory inside profiles\ or dev-team\ breaks every link
    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_path_under_profiles_directory(self):  # D5
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            proc = self._run_harness(tmp, root, root / "profiles" / "dev-qa" / "mnemosyne")
        self._assert_refused(proc, needle="profile directory")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_path_under_dev_team_directory(self):  # D5
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            proc = self._run_harness(tmp, root, root / "dev-team" / "mnemosyne")
        self._assert_refused(proc, needle="dev-team")

    # --- D4: a bind mount (device+inode identity, read via `stat` on the
    # non-Windows pwsh this suite runs under) gives a path identical to the
    # operator bank with no symlink in it anywhere.
    @unittest.skipUnless(shutil.which("pwsh") and _unprivileged_bind_mount_works(),
                         "needs pwsh and an unprivileged bind mount (not permitted on CI runners)")
    def test_team_memory_refuses_an_alias_via_bind_mount(self):  # D4
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            alias = Path(tmp) / "bind-alias"
            alias.mkdir()
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(alias))
            script = (
                f"export HOME={shlex.quote(str(Path(tmp)))}\n"
                f"mount --bind {shlex.quote(str(root / 'mnemosyne'))} {shlex.quote(str(alias))} || exit 99\n"
                f"exec pwsh -NoProfile -NonInteractive -File {shlex.quote(str(harness))}\n"
            )
            proc = subprocess.run(
                ["unshare", "--mount", "--map-root-user", "--propagation", "private", "bash", "-c", script],
                capture_output=True, text=True, timeout=60)
        if proc.returncode == 99 or "cannot create snap home dir" in proc.stderr:
            # Not the a1f3afb probe's job: the probe proves unprivileged
            # bind mounts work, but a snap-confined pwsh (this dev box's
            # /snap/bin/pwsh) still cannot start inside that mount
            # namespace, and only launching it shows that. On CI runners the
            # decorator's probe already skips (user namespaces forbidden).
            self.skipTest("unprivileged bind mount + snap-confined pwsh unavailable in this sandbox")
        self._assert_refused(proc)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_dangling_link_into_the_operator_bank(self):  # r3 #2
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            link = Path(tmp) / "dangling"
            link.symlink_to(root / "mnemosyne" / "not-yet")
            self._assert_refused(self._run_harness(tmp, root, link))

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_survives_a_link_cycle(self):  # r3 #2: bounded loop
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            (Path(tmp) / "a").symlink_to(Path(tmp) / "b")
            (Path(tmp) / "b").symlink_to(Path(tmp) / "a")
            proc = self._run_harness(tmp, root, Path(tmp) / "a" / "bank")
        # Must terminate (timeout above) and refuse, never hand back a
        # half-resolved path that could dodge the containment check.
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Too many levels of symbolic links", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_mnemosyne_home_given_as_a_link(self):  # r3 #3
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            real = Path(tmp) / "relocated-bank"
            real.mkdir()
            link = Path(tmp) / "mhlink"
            link.symlink_to(real)
            proc = self._run_harness(tmp, root, real, env_extra={"MNEMOSYNE_HOME": str(link)})
            self._assert_refused(proc, needle="MNEMOSYNE_HOME")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_dotdot_after_a_missing_component(self):  # r3 #1 parity
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            self._assert_refused(self._run_harness(tmp, root, f"{root}/nonexist/../mnemosyne"))
            self.assertFalse((root / "nonexist").exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_through_an_unrelated_link_resolves_to_the_real_path(self):  # r3 #2
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            real = Path(tmp) / "elsewhere"
            real.mkdir()
            (Path(tmp) / "elink").symlink_to(real)
            proc = self._run_harness(tmp, root, Path(tmp) / "elink" / "bank")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"TEAM_HOME={Path(os.path.realpath(real)) / 'bank'}", proc.stdout)

    def test_relink_note_checks_both_operator_locations_by_containment(self):  # r3 #4
        start = self.text.index("function Invoke-MnemosyneDataLink")
        end = self.text.index("\n}\n", start)
        block = self.text[start:end]
        self.assertIn("Test-TouchesOperatorBank", block)
        self.assertIn("Resolve-TeamMemoryRealPath", block)
        # Test-TouchesOperatorBank itself (defined alongside the refusal
        # logic, above) is what actually checks both locations -- assert
        # that on its own body too, so a future refactor that drops one of
        # them out of touches_operator_bank still fails this test.
        touches_body = _function(self.text, "Test-TouchesOperatorBank")
        self.assertIn("$_operatorHomeReal", touches_body)
        self.assertIn("$_mnemosyneHomeReal", touches_body)
        self.assertIn("$_mnemosyneDataDirReal", touches_body)

    def _note_harness(self, tmp, root, data_link):
        """The setup snippet plus the real relink-NOTE block (now inside
        Invoke-MnemosyneDataLink, D6), run live."""
        harness = self._team_memory_harness(tmp, root)
        start = self.text.index("    if ($isLink) {\n        $prevTarget = $null")
        end = self.text.index("$kind = New-DirLink -LinkPath $dataLink -Target $TeamMemoryHome", start)
        harness.write_text(harness.read_text() +
                           f"$dataLink = '{data_link}'\n$isLink = $true\n" + self.text[start:end] + "\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_relink_note_fires_live_for_under_bank_and_mnemosyne_home_links(self):  # r3 #4
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root_with_operator_bank(tmp)
            relocated = Path(tmp) / "relocated-bank"
            relocated.mkdir()
            other = Path(tmp) / "other"
            other.mkdir()
            cases = [(root / "mnemosyne" / "data", {}, True),
                     (relocated, {"MNEMOSYNE_HOME": str(relocated)}, True),
                     (other, {}, False)]
            for i, (target, env_extra, expect) in enumerate(cases):
                link = Path(tmp) / f"plink{i}"
                link.symlink_to(target)
                harness = self._note_harness(tmp, root, link)
                proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                      capture_output=True, text=True, timeout=60,
                                      env={**os.environ, **env_extra})
                with self.subTest(target=str(target)):
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    note = f"NOTE: {link} was linked to the operator's own bank"
                    (self.assertIn if expect else self.assertNotIn)(note, proc.stdout)


class WindowsMnemosyneDataLinkTests(unittest.TestCase):
    """D2/D3/D6/D8: Invoke-MnemosyneDataLink, run directly with pwsh (the
    extraction D6 asked for -- the same move already made for
    Resolve-TeamHomeMigrationPlan)."""

    def setUp(self):
        self.text = PS1.read_text(encoding="utf-8")

    def _datalink_harness(self, tmp, hermes_root, call_kwargs, dry_run=False):
        start = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        end = self.text.index("# Team state (forge project records")
        snippet = self.text[start:end]
        fn_start = self.text.index("function Invoke-MnemosyneDataLink")
        fn_end = self.text.index("\n}\n", fn_start) + 2
        fn_body = self.text[fn_start:fn_end]
        norm_fn = _function(self.text, "Get-NormalizedPath")
        dirlink_fn = _function(self.text, "New-DirLink")
        harness = Path(tmp) / "datalink_harness.ps1"
        kwargs_lines = "\n".join(f"${k} = {v}" for k, v in call_kwargs.items())
        harness.write_text(
            f"$HermesRoot = '{hermes_root}'\n$TeamMemory = $null\n"
            + norm_fn + "\n" + dirlink_fn + "\n" + snippet + "\n" + fn_body +
            "\nNew-Item -ItemType Directory -Path $TeamMemoryHome -Force | Out-Null\n"
            f"\n{kwargs_lines}\n"
            f"$r = Invoke-MnemosyneDataLink -ProfileDir $ProfileDirArg -Name $NameArg -Exists $ExistsArg "
            f"-TeamMemoryHome $TeamMemoryHome -AdoptTeamMemory:$AdoptArg -AdoptTimestamp $TsArg "
            f"-DryRun:${'true' if dry_run else 'false'} -HermesRootProfilesReal $HermesRootProfilesReal\n"
            f"Write-Output \"RESULT_COPIED=$($r.Copied)\"\nWrite-Output \"RESULT_ADOPTED=$($r.Adopted)\"\n"
            f"Write-Output \"RESULT_UNSHARED=$($r.Unshared)\"\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D2_refuses_when_profile_dir_resolves_outside_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            # Reviewer's repro: the profile dir is itself a symlink to the root.
            profiles_dir = root / "profiles"
            profiles_dir.mkdir()
            evil_profile = profiles_dir / "dev-qa"
            evil_profile.symlink_to(root)
            kwargs = {
                "ProfileDirArg": f"'{evil_profile}'", "NameArg": "'dev-qa'", "ExistsArg": "$true",
                "AdoptArg": "$true", "TsArg": "'20261103000000'",
            }
            harness = self._datalink_harness(tmp, root, kwargs)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("outside", proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D3_backup_collision_gets_a_unique_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            profile_dir = root / "profiles" / "dev-qa"
            (profile_dir / "mnemosyne" / "data").mkdir(parents=True)
            (profile_dir / "mnemosyne" / "data" / "mnemosyne.db").write_text("qa's own memories")
            # Pre-create the exact backup path a fixed timestamp would use.
            collide = profile_dir / "mnemosyne.pre-team-memory-20261103105748"
            collide.mkdir()
            (collide / "pre-existing.txt").write_text("unrelated contents")
            kwargs = {
                "ProfileDirArg": f"'{profile_dir}'", "NameArg": "'dev-qa'", "ExistsArg": "$true",
                "AdoptArg": "$true", "TsArg": "'20261103105748'",
            }
            harness = self._datalink_harness(tmp, root, kwargs)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((collide / "pre-existing.txt").read_text(), "unrelated contents")
            moved = profile_dir / "mnemosyne.pre-team-memory-20261103105748.1"
            self.assertEqual((moved / "data" / "mnemosyne.db").read_text(), "qa's own memories")
            self.assertIn("RESULT_ADOPTED=True", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D8_dry_run_with_adopt_reports_would_back_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            profile_dir = root / "profiles" / "dev-qa"
            (profile_dir / "mnemosyne" / "data").mkdir(parents=True)
            (profile_dir / "mnemosyne" / "data" / "mnemosyne.db").write_text("qa's own memories")
            kwargs = {
                "ProfileDirArg": f"'{profile_dir}'", "NameArg": "'dev-qa'", "ExistsArg": "$true",
                "AdoptArg": "$true", "TsArg": "'20261103105748'",
            }
            harness = self._datalink_harness(tmp, root, kwargs, dry_run=True)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"would back up: {profile_dir / 'mnemosyne'} ->", proc.stdout)
            self.assertIn("would link:", proc.stdout)
            # Dry run: nothing actually moved.
            self.assertTrue((profile_dir / "mnemosyne" / "data" / "mnemosyne.db").exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_keep_private_path_pins_the_profiles_own_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            profile_dir = root / "profiles" / "dev-qa"
            (profile_dir / "mnemosyne" / "data").mkdir(parents=True)
            (profile_dir / "mnemosyne" / "data" / "mnemosyne.db").write_text("qa's own memories")
            kwargs = {
                "ProfileDirArg": f"'{profile_dir}'", "NameArg": "'dev-qa'", "ExistsArg": "$true",
                "AdoptArg": "$false", "TsArg": "'20261103105748'",
            }
            harness = self._datalink_harness(tmp, root, kwargs)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("RESULT_UNSHARED=True", proc.stdout)
            env_file = profile_dir / ".env"
            self.assertIn(f"MNEMOSYNE_DATA_DIR={profile_dir / 'mnemosyne' / 'data'}", env_file.read_text())
            self.assertIn(f"MNEMOSYNE_HOME={profile_dir / 'mnemosyne'}", env_file.read_text())  # F1 (review r3)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_separate_memory_path_links_and_pins_the_team_bank(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            profile_dir = root / "profiles" / "dev-qa"
            profile_dir.mkdir(parents=True)
            kwargs = {
                "ProfileDirArg": f"'{profile_dir}'", "NameArg": "'dev-qa'", "ExistsArg": "$false",
                "AdoptArg": "$false", "TsArg": "'20261103105748'",
            }
            harness = self._datalink_harness(tmp, root, kwargs)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # New-Item -ItemType Junction is a documented Windows-only
            # reparse point; on this non-Windows pwsh sandbox it returns
            # success without creating anything on disk (a platform
            # limitation of PowerShell Core on Linux, not of this
            # installer -- WindowsInstallerTests.test_new_dirlink_... only
            # checks New-DirLink's source text for the same reason). The
            # D1 pin this test exists for is independent of that and is
            # checked below.
            env_file = profile_dir / ".env"
            self.assertIn(f"MNEMOSYNE_DATA_DIR={root / 'dev-team-mnemosyne' / 'data'}", env_file.read_text())
            self.assertIn(f"MNEMOSYNE_HOME={root / 'dev-team-mnemosyne'}", env_file.read_text())  # F1 (review r3)


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
        # The DB/link checks moved into a script the unit suite executes
        # (test_ci_team_bank_checks.py, PR #8 fix round 4 C1-C4).
        self.has("check-team-bank.ps1", job, "no team-bank / Mnemosyne DB check")
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

    def test_linux_job_asserts_first_run_exit_0_and_team_mnemosyne_links(self):
        job = self.job("install-linux")
        self.has("first run must exit 0", job, "no first-run exit check")
        self.has("check-team-bank.sh", job, "no team-bank link check")
        script = (WORKFLOW.parent.parent / "scripts" / "check-team-bank.sh").read_text(encoding="utf-8")
        self.has("is not a symlink to the team bank", script, "team-bank check lost its link assertion")

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
