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
import sys
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


def _snapshot_tree(root):
    """Every entry under root -- files by content, symlinks by target
    (never followed), directories by presence -- so a recursive delete or
    a self-link created inside a directory both show (review r3 R3-3)."""
    import hashlib
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                snap[rel] = ("symlink", os.readlink(p))
            elif p.is_dir():
                snap[rel] = ("dir",)
            else:
                snap[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest())
    return snap


def _function(text: str, name: str) -> str:
    """The body of a top-level PowerShell function: up to its closing `}` at
    column 0."""
    start = text.index(f"function {name}")
    end = text.index("\n}\n", start)
    return text[start:end + 2]


# Round 2 (review): every function below Resolve-WrapperPython calls into
# (directly or transitively) since M1-M3 -- a harness that extracts
# Resolve-WrapperPython alone now fails with "term ... is not recognized"
# under pwsh, not a clean red/green test result. Centralised here once so
# every call site stays in sync with the real dependency chain instead of
# each guessing its own subset.
#
# CMD_WRAPPER_FUNCS is the same idea one layer down: Resolve-CmdEmbeddablePath
# itself (used directly by several tests, not only via Resolve-WrapperPython)
# calls Test-AsciiOrdinalRange and Get-LongPathName (round 2: 8.3-short-path
# canonicalisation), and Get-CmdPrefixCandidates calls Get-PersistedEnvVar.
# A harness extracting Get-CmdPrefixCandidates/Resolve-CmdEmbeddablePath
# without these three is exactly the stale-dependency-list bug fixed here --
# every call site must use this one tuple instead of hand-rolling its own.
#
# N4 (round-7 review): Test-ContainsUnsafeCmdEmbedChar was MISSING from this
# tuple even though Resolve-CmdEmbeddablePath's literal-embed gate calls it.
# Every existing test passed only because the `-and` on the ASCII check
# short-circuited on non-ASCII input before that call was ever reached; an
# ASCII metachar path (e.g. "C:\Program Files (x86)\Python312\python.exe")
# made the harness die with rc=1, "... is not recognized as ... function" --
# not a clean red/green result. Test-ContainsCmdShellMetachar is gone from
# both the installer and this tuple: round 7 (D2) collapsed the two drifting
# copies of the literal-embed rule into Test-ContainsUnsafeCmdEmbedChar
# alone, so the wider A3 round-5 classifier has no call site left.
CMD_WRAPPER_FUNCS = (
    "Test-AsciiOrdinalRange", "Test-ContainsUnsafeCmdEmbedChar", "Get-LongPathName",
    "Get-PersistedEnvVar", "Get-CmdPrefixCandidates", "Resolve-CmdEmbeddablePath",
    "Assert-WrapperTextAscii", "Get-CmdGuardBlock",
)
# Format-EnvPinForDisplay: Resolve-WrapperPython itself calls it since N3
# (the skipped-py-launcher warning), so it is part of the real chain now,
# not only of the cmd-wrapper harnesses below.
RESOLVE_WRAPPER_PYTHON_DEPS = ("Format-EnvPinForDisplay",) + CMD_WRAPPER_FUNCS + \
    ("Test-WrapperPythonEmbeddable",)


def _resolve_wrapper_python_prelude(text: str) -> str:
    """Resolve-WrapperPython plus its full real dependency chain, extracted
    verbatim (not reimplemented) -- see RESOLVE_WRAPPER_PYTHON_DEPS."""
    return "".join(_function(text, f) + "\n" for f in RESOLVE_WRAPPER_PYTHON_DEPS) + \
        _function(text, "Resolve-WrapperPython") + "\n"


def _cmd_wrapper_prelude(text: str) -> str:
    """Format-EnvPinForDisplay + the full Resolve-CmdEmbeddablePath dependency
    chain (CMD_WRAPPER_FUNCS), extracted verbatim. Shared by every test that
    builds forge.cmd/deciders.cmd guard/embed logic directly (not through
    Resolve-WrapperPython)."""
    return _function(text, "Format-EnvPinForDisplay") + "\n" + \
        "".join(_function(text, f) + "\n" for f in CMD_WRAPPER_FUNCS)


ENV_PIN_CHECK_PY = DEV_TEAM / "scripts" / "env_pin_check.py"


def _pin_prelude(text, python=None):
    """What every harness that pins (or runs the -TeamMemory setup slice)
    needs from outside its slice (A3): the interpreter the installer
    resolves before any pin logic -- here the one running this suite -- the
    checker's path, and the pin gate's functions. Redefining a function the
    slice also defines is harmless: both definitions are the same text."""
    import sys
    py = (python or sys.executable).replace("'", "''")
    return (f"$WrapperPython = '{py}'\n"
            f"$EnvPinCheckPy = '{str(ENV_PIN_CHECK_PY).replace(chr(39), chr(39) * 2)}'\n"
            + "".join(_function(text, f) + "\n" for f in (
                "Format-EnvPinForDisplay", "Get-EnvPinCheckerReason",
                "Get-EnvUnsafeValueReason", "Assert-EnvPinSafe")))


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

    _PS51_LEGACY_ENCODING_PRELUDE = (
        "$PSDefaultParameterValues['Set-Content:Encoding'] = 'Latin1'\n"
        "$PSDefaultParameterValues['Add-Content:Encoding'] = 'Latin1'\n"
        "$PSDefaultParameterValues['Get-Content:Encoding'] = 'Latin1'\n"
    )

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_env_pin_writes_utf8_even_under_ps51_ansi_defaults(self):  # review r3 minor #1
        """pwsh 7 on Linux already defaults Get/Set/Add-Content to UTF-8, so a
        naive version of this test is green before the fix. Model PS 5.1's
        ANSI-code-page default explicitly via $PSDefaultParameterValues so an
        unfixed Set-EnvVarPin (relying on those cmdlets' defaults) fails here
        the same way it would on the real CI target. An explicit encoding or
        a .NET call is unaffected by these defaults, which is the fix."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profiles" / "dev-qa"
            profile_dir.mkdir(parents=True)
            bank = Path(tmp) / "Dr\u0103go\u0219" / "bank"
            harness = Path(tmp) / "env_pin.ps1"
            harness.write_text(
                self._PS51_LEGACY_ENCODING_PRELUDE
                + _pin_prelude(self.text)
                + _function(self.text, "Set-EnvVarPin") + "\n"
                + f"Set-EnvVarPin -ProfileDir '{profile_dir}' -VarName 'MNEMOSYNE_HOME' -Value '{bank}'\n"
                + "Write-Output 'PIN_DONE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PIN_DONE", proc.stdout)
            raw = (profile_dir / ".env").read_bytes()
            decoded = raw.decode("utf-8")  # strict: raises/mismatches on ANSI-mangled bytes
            self.assertEqual(decoded.strip(), f"MNEMOSYNE_HOME={bank}")
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), ".env must have no UTF-8 BOM")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_env_pin_rewrite_keeps_non_ascii_existing_lines_byte_exact(self):  # review r3 minor #1
        """--clone copies the operator's root .env, and Set-EnvVarPin rewrites
        that file in place -- a pre-existing non-ASCII line (comment or an
        unrelated key) must survive the rewrite byte-exact, under the same
        PS 5.1 ANSI-default modelling as the write test above."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profiles" / "dev-qa"
            profile_dir.mkdir(parents=True)
            env_file = profile_dir / ".env"
            env_file.write_bytes("# Dr\u0103go\u0219\nOTHER=\u0103\u0219\nMNEMOSYNE_HOME=/old\n".encode("utf-8"))
            harness = Path(tmp) / "env_pin_rewrite.ps1"
            harness.write_text(
                self._PS51_LEGACY_ENCODING_PRELUDE
                + _pin_prelude(self.text)
                + _function(self.text, "Set-EnvVarPin") + "\n"
                + f"Set-EnvVarPin -ProfileDir '{profile_dir}' -VarName 'MNEMOSYNE_HOME' -Value '/new'\n"
                + "Write-Output 'PIN_DONE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            raw = env_file.read_bytes()
            lines = raw.decode("utf-8").splitlines()
            self.assertIn("# Dr\u0103go\u0219", lines, "non-ASCII comment line not preserved byte-exact")
            self.assertIn("OTHER=\u0103\u0219", lines, "non-ASCII value line not preserved byte-exact")
            self.assertIn("MNEMOSYNE_HOME=/new", lines)
            self.assertNotIn("MNEMOSYNE_HOME=/old", lines)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_env_pin_writes_special_characters_verbatim_and_round_trips(self):
        """item 2/4a (operator round-2 decision): the pin format is
        unquoted KEY=value, UTF-8, no escaping -- Set-EnvVarPin never
        regex-substitutes or quotes the value (same concern F4 raised for
        the Unix sed version). A bank path containing '&', '|', a
        backslash, a UNC prefix, a space and non-ASCII characters must
        come back byte-exact, and -- because there's no escaping -- the
        stored value IS the raw value, so it round-trips through both
        python-dotenv's real parser (if importable) and a trivial
        identity decode standing in for Mnemosyne's naive
        strip-bounding-quotes reader (a no-op here: there are no
        bounding quote characters to strip)."""
        bank = "C:\\Users\\Dr\u0103go\u0219\\team & co |pipe\\\\server\\share"
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profiles" / "dev-qa"
            profile_dir.mkdir(parents=True)
            harness = Path(tmp) / "env_pin_special.ps1"
            harness.write_text(
                _pin_prelude(self.text)
                + _function(self.text, "Set-EnvVarPin") + "\n"
                + f"Set-EnvVarPin -ProfileDir '{profile_dir}' -VarName 'MNEMOSYNE_HOME' -Value \"{bank}\"\n"
                + "Write-Output 'PIN_DONE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("PIN_DONE", proc.stdout)
            lines = (profile_dir / ".env").read_text(encoding="utf-8").splitlines()
            matching = [l for l in lines if l.startswith("MNEMOSYNE_HOME=")]
            self.assertEqual(len(matching), 1)
            stored_value = matching[0][len("MNEMOSYNE_HOME="):]
            self.assertEqual(stored_value, bank, "value was not written verbatim")
            # Mnemosyne's naive reader (mcp_server.py:_load_dotenv) only
            # strips one bounding quote character off each end; there are
            # none here, so it is a no-op and the value is unchanged.
            mnemosyne_naive_decoded = stored_value.strip().strip("'\"")
            self.assertEqual(mnemosyne_naive_decoded, bank)
            try:
                from dotenv.main import DotEnv  # noqa: PLC0415
            except ImportError:
                self.skipTest("python-dotenv not importable here; naive-reader decode above already passed")
                return
            env_file = profile_dir / ".env"
            parsed = DotEnv(str(env_file), encoding="utf-8").dict()
            self.assertEqual(parsed.get("MNEMOSYNE_HOME"), bank,
                              "real python-dotenv did not round-trip the value byte-exact")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_env_pin_rerun_replaces_the_line_without_duplicating(self):
        """item 2: a re-run (e.g. the installer run twice, or two vars
        pinned back to back under the same key due to a relocated bank)
        must replace the existing pin line, never duplicate it -- whether
        the first write happened to look quoted or not."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profiles" / "dev-qa"
            profile_dir.mkdir(parents=True)
            env_file = profile_dir / ".env"
            # Seed a pre-existing line that LOOKS quoted (e.g. left over
            # from a previous, since-reverted quoting scheme) to prove the
            # prefix match replaces it regardless of its own shape.
            env_file.write_text("MNEMOSYNE_HOME='/old/quoted'\nOTHER=keep\n", encoding="utf-8")
            harness = Path(tmp) / "env_pin_rerun.ps1"
            harness.write_text(
                _pin_prelude(self.text)
                + _function(self.text, "Set-EnvVarPin") + "\n"
                + f"Set-EnvVarPin -ProfileDir '{profile_dir}' -VarName 'MNEMOSYNE_HOME' -Value '/new/bank'\n"
                + f"Set-EnvVarPin -ProfileDir '{profile_dir}' -VarName 'MNEMOSYNE_HOME' -Value '/new/bank2'\n"
                + "Write-Output 'PIN_DONE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            lines = env_file.read_text(encoding="utf-8").splitlines()
            matching = [l for l in lines if l.startswith("MNEMOSYNE_HOME=")]
            self.assertEqual(matching, ["MNEMOSYNE_HOME=/new/bank2"],
                              "re-run must replace the existing pin, not duplicate it")
            self.assertIn("OTHER=keep", lines)

    def _team_memory_harness_from_env(self, tmp, hermes_root):
        """Like _team_memory_harness, but takes -TeamMemory from the
        TEAM_MEMORY_ARG environment variable instead of interpolating it
        into a PS single-quoted literal -- needed to pass values
        containing a literal quote character, trailing whitespace, or a
        newline without reimplementing PowerShell's string-literal
        escaping rules in the test itself."""
        start = self.text.index("$PluginSrcMnemo = Join-Path $HermesRoot")
        end = self.text.index("# Team state (forge project records")
        snippet = self.text[start:end]
        norm_fn = _function(self.text, "Get-NormalizedPath")
        harness = Path(tmp) / "harness_env.ps1"
        harness.write_text(
            f"$HermesRoot = '{hermes_root}'\n"
            "$TeamMemory = $env:TEAM_MEMORY_ARG\n"
            + _pin_prelude(self.text) + norm_fn + "\n" + snippet +
            "\nWrite-Output \"TEAM_HOME=$TeamMemoryHome\"\n")
        return harness

    def _run_team_memory_with_value(self, tmp, root, value):
        harness = self._team_memory_harness_from_env(tmp, root)
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "TEAM_MEMORY_ARG": value})

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_value_with_space_then_hash(self):  # item 4a, operator round-2 decision
        """python-dotenv's real parser truncates an unquoted value at
        ' #' (confirmed against the real loader elsewhere in this suite);
        Mnemosyne's naive reader does not. The two readers would disagree,
        so the installer must refuse this shape up front rather than
        silently pin a value one of them would misread."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = str(Path(tmp) / "team # tricky bank")
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("whitespace immediately followed by", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_value_with_a_leading_quote(self):  # item 4a, operator round-2 decision
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = "'" + str(Path(tmp) / "bank")
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("quote character", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_value_with_trailing_whitespace(self):  # item 4a, operator round-2 decision
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = str(Path(tmp) / "bank") + " "
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("whitespace", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_value_with_an_embedded_newline(self):  # item 4a, operator round-2 decision
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = str(Path(tmp) / "bank") + "\nEVIL=1"
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("newline", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_value_containing_dollar_brace(self):  # round-1 review coverage finding #2
        """python-dotenv (via env_loader's _resolve_dotenv_assignments)
        expands `${VAR}` in an unquoted value; Mnemosyne's reader does
        not expand it at all. Verified elsewhere in this suite:
        'C:\\a\\${HOME}\\b' comes back expanded under the real Hermes
        loader and verbatim under Mnemosyne's. The two readers would
        disagree, so refuse this shape up front like the others."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = str(Path(tmp) / "bank-${HOME}-suffix")
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("${", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_accepts_a_value_with_safe_special_characters(self):  # item 4a: no false positives
        """'&', '|', backslashes and non-ASCII are all safe under the
        unquoted-no-escaping format (F4's concern) and must NOT be
        refused by the new round-2 guard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            value = str(Path(tmp) / "a&b|c d" / "Dr\u0103go\u0219")
            proc = self._run_team_memory_with_value(tmp, root, value)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"TEAM_HOME={value}", proc.stdout)

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
            + _pin_prelude(self.text) + norm_fn + "\n" + dirlink_fn + "\n" + helpers +
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
            # R3-3: the real layout -- $PluginSrcMnemo IS
            # $HermesRoot\plugins\mnemosyne, with real content -- so the
            # PS 5.1 Remove-Item -Recurse (or a self-link inside it) shows.
            plugin_src = root / "plugins" / "mnemosyne"
            plugin_src.mkdir(parents=True)
            (plugin_src / "marker.txt").write_text("plugin code\n")
            before_plugins = _snapshot_tree(root / "plugins")
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
            self.assertEqual(_snapshot_tree(root / "plugins"), before_plugins,
                             "operator plugins\\ must be byte-identical (no delete, no self-link)")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_profile_dir_guard_refuses_profile_dir_equal_to_profiles_root(self):  # F2 equality, review r3 minor #4
        """profiles\\dev-qa -> profiles (the profiles root itself, not merely
        a path nested under it) must also be refused. Test-PathIsOrUnder
        treats Desc -ieq Anc as is-or-under, so the guard used to ACCEPT a
        profile dir that resolves to exactly $HermesRoot\\profiles and write
        SOUL.md / the prefix file / the plugin link straight into it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            (root / "SOUL.md").write_text("operator soul\n")
            plugin_src = root / "plugins" / "mnemosyne"
            plugin_src.mkdir(parents=True)
            (plugin_src / "marker.txt").write_text("plugin code\n")
            before_plugins = _snapshot_tree(root / "plugins")
            profiles_dir = root / "profiles"
            profiles_dir.mkdir()
            evil_profile = profiles_dir / "dev-qa"
            evil_profile.symlink_to(profiles_dir)
            harness = self._profile_dir_guard_harness(tmp, root, evil_profile, plugin_src)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("outside", proc.stderr)
            self.assertNotIn("REACHED_PLUGIN_LINK", proc.stdout)
            self.assertFalse((profiles_dir / "SOUL.md").exists(),
                              "SOUL.md must not have been written into profiles\\ itself")
            self.assertFalse((profiles_dir / "dev-team-prefix").exists(),
                              "dev-team-prefix must not have been written into profiles\\ itself")
            self.assertEqual(_snapshot_tree(root / "plugins"), before_plugins,
                             "operator's plugins\\ (root\\plugins) must be byte-identical (no plugin link written)")
            self.assertFalse((profiles_dir / "plugins").exists(),
                              "no plugins\\ must have been created inside profiles\\ itself")

    def _separate_memory_harness(self, tmp, profile_dir, dry_run):
        """R3-1: the REAL per-profile-loop slice that runs right after the
        prefix file -- the DryRun block (with its `continue`) and the real
        plugin-link + data-link block -- with $SeparateMemory = $true, so
        the F3 else-branches are what execute. Wrapped in a one-pass
        foreach so the DryRun branch's `continue` is legal."""
        start = self.text.index('    if ($DryRun) {\n        Write-Host "  would link: $profileDir\\plugins\\mnemosyne')
        end = self.text.index("    # 4. dev-team-gate link")
        slice_ = self.text[start:end]
        self.assertIn("$SeparateMemory", slice_, "slice lost the -SeparateMemory branches")
        plugin_src = Path(tmp) / "plugin-src"
        plugin_src.mkdir(exist_ok=True)
        harness = Path(tmp) / "separate_memory.ps1"
        harness.write_text(
            _function(self.text, "New-DirLink") + "\n"
            + _pin_prelude(self.text)
            + _function(self.text, "Set-EnvVarPin") + "\n"
            + _function(self.text, "Set-MnemosyneDataDirPin") + "\n"
            + f"$DryRun = ${'true' if dry_run else 'false'}\n$SeparateMemory = $true\n"
            + f"$name = 'dev-qa'\n$profileDir = '{profile_dir}'\n$exists = $false\n"
            + f"$PluginSrcMnemo = '{plugin_src}'\n$PluginSrcGate = '{plugin_src}'\n"
            + f"$DevTeamHome = '{tmp}'\n$SkillsDir = '{tmp}'\n$copied = @()\n"
            # Must never be reached with -SeparateMemory.
            + "function Invoke-MnemosyneDataLink { Write-Output 'TEAM_LINK_ATTEMPTED' }\n"
            + "foreach ($__once in 1) {\n" + slice_ + "}\nWrite-Output 'SLICE_DONE'\n")
        return harness

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_separate_memory_pins_each_profiles_own_bank(self):  # R3-1 / F3
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), tempfile.TemporaryDirectory() as tmp:
                profile_dir = Path(tmp) / "hermes" / "profiles" / "dev-qa"
                profile_dir.mkdir(parents=True)
                harness = self._separate_memory_harness(tmp, profile_dir, dry_run)
                proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("TEAM_LINK_ATTEMPTED", proc.stdout)
                own = profile_dir / "mnemosyne"
                env_file = profile_dir / ".env"
                if dry_run:
                    self.assertIn(f"would pin in .env: MNEMOSYNE_DATA_DIR={own / 'data'}", proc.stdout)
                    self.assertIn(f"would pin in .env: MNEMOSYNE_HOME={own}", proc.stdout)
                    self.assertFalse(env_file.exists(), "a dry run must not write .env")
                else:
                    self.assertIn("SLICE_DONE", proc.stdout)
                    lines = env_file.read_text().splitlines()
                    self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_DATA_DIR=")],
                                     [f"MNEMOSYNE_DATA_DIR={own / 'data'}"])
                    self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_HOME=")],
                                     [f"MNEMOSYNE_HOME={own}"])

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


def _root_lines(stdout):
    """The ROOT= lines a root-climb harness printed, each whole."""
    return [line for line in stdout.splitlines() if line.startswith("ROOT=")]


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
        # Stop before $WrapperPython = Resolve-WrapperPython is actually
        # invoked: the root-climb logic this test exercises is fully
        # computed by $HermesRoot above, and this sandbox has no 'python'
        # (or Hermes tools / py launcher) to resolve, so running the
        # wrapper-python resolution here would fail closed on a host that
        # legitimately lacks a 'python' command, unrelated to what this
        # test checks.
        end = self.text.index("$WrapperPython = Resolve-WrapperPython")
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
        # Compare the whole ROOT= line, not a substring: without the climb
        # the output is ROOT=<root>/profiles/devops, which still contains
        # "ROOT=<root>", so an assertIn on stdout cannot see the climb go.
        self.assertEqual(_root_lines(proc.stdout), [f"ROOT={root}"], proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_root_climb_logic_leaves_the_true_root_alone(self):
        start = self.text.index("$RootConfig = Get-HermesLastLine")
        # See the sibling test above: stop before the wrapper-python
        # resolution is invoked, so this test doesn't require a 'python'
        # command on the host.
        end = self.text.index("$WrapperPython = Resolve-WrapperPython")
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
        # Whole-line match (see the sibling test): an over-climb that keeps
        # the <root> prefix must fail too.
        self.assertEqual(_root_lines(proc.stdout), [f"ROOT={root}"], proc.stdout)

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
            + _pin_prelude(self.text) + norm_fn + "\n" + snippet +
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

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_symlink_whose_target_is_env_unsafe(self):  # round-1 review safety finding #1
        """Reviewer repro (a): a safe-looking -TeamMemory argument that is
        itself a symlink to an unsafe target. The raw argument
        (str(link)) contains no unsafe shape and used to pass the F4a
        refusal untouched; Set-EnvVarPin then wrote the RESOLVED
        $TeamMemoryHome (the symlink's target), which the .env readers
        would not agree on. Must now be refused before the symlink is
        ever followed into a pin."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            unsafe_target_parent = Path(tmp) / "x #y"
            unsafe_target_parent.mkdir()
            unsafe_target = unsafe_target_parent / "bank"
            link = Path(tmp) / "safe-link"
            link.symlink_to(unsafe_target)
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(link))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("whitespace immediately followed by", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_a_relative_value_resolved_under_an_unsafe_cwd(self):  # round-1 review safety finding #1
        """Reviewer repro (b): a safe-looking relative -TeamMemory
        argument ('bank') resolved, via GetFullPath, against a cwd that
        itself contains an unsafe shape. The raw argument is safe; only
        the RESOLVED $TeamMemoryHome (cwd + 'bank') is not."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            unsafe_cwd = Path(tmp) / "cwd #1"
            unsafe_cwd.mkdir()
            harness = self._team_memory_harness(tmp, root, team_memory_arg="bank")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60, cwd=str(unsafe_cwd))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("whitespace immediately followed by", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_team_memory_refuses_when_hermes_root_itself_is_env_unsafe(self):  # round-1 review safety finding #1
        """Reviewer note on the same finding: "the same hole applies when
        $HermesRoot itself contains ' #' and an explicit safe -TeamMemory
        is given: the profile-local pins are never checked." Every
        profile-local Set-MnemosyneDataDirPin call derives its DataDir
        from $HermesRoot\\profiles\\<name>, so an unsafe $HermesRoot must
        be refused even when -TeamMemory itself is perfectly safe."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "x #y" / "hermes"
            root.mkdir(parents=True)
            safe_team_memory = Path(tmp) / "safe-bank"
            harness = self._team_memory_harness(tmp, root, team_memory_arg=str(safe_team_memory))
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("whitespace immediately followed by", proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)

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
            + _pin_prelude(self.text) + norm_fn + "\n" + dirlink_fn + "\n" + snippet + "\n" + fn_body +
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
    def test_D2_refuses_when_profile_dir_resolves_equal_to_profiles_root(self):  # round-1 review coverage #3
        """Defense-in-depth companion to test_profile_dir_guard_refuses_
        profile_dir_equal_to_profiles_root: that test only exercises the
        main-loop guard site (the equality check right before this
        function is even called). This function has the SAME equality
        check (:1272-1274) as its own defense-in-depth, and it had no
        direct test of its own."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            (root / "mnemosyne" / "data").mkdir(parents=True)
            profiles_dir = root / "profiles"
            profiles_dir.mkdir()
            evil_profile = profiles_dir / "dev-qa"
            evil_profile.symlink_to(profiles_dir)
            kwargs = {
                "ProfileDirArg": f"'{evil_profile}'", "NameArg": "'dev-qa'", "ExistsArg": "$true",
                "AdoptArg": "$true", "TsArg": "'20261103000000'",
            }
            harness = self._datalink_harness(tmp, root, kwargs)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("outside, or equal to", proc.stderr)
            self.assertFalse((profiles_dir / ".env").exists(),
                              "nothing must have been pinned into profiles\\ itself")

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
    def test_new_profile_with_no_existing_mnemosyne_pins_the_team_bank(self):  # review r3 minor #4c, round-1 rename (#5)
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


class WindowsCmdWrapperNonAsciiTests(unittest.TestCase):
    """D1-D9 (pr10-review.md section 4): forge.cmd/deciders.cmd stay pure
    ASCII under a non-ASCII checkout/Python path via %ENVVAR% prefix
    rewriting, and py -3 discovery survives a non-ASCII sys.executable
    under PowerShell 5.1."""

    def setUp(self):
        self.text = PS1.read_text(encoding="utf-8")

    _WRAPPER_FUNCS = ("Format-EnvPinForDisplay",) + CMD_WRAPPER_FUNCS

    def _wrapper_prelude(self):
        return "".join(_function(self.text, f) + "\n" for f in self._WRAPPER_FUNCS)

    def _assert_cmd_wrapper_paths_ascii_fn(self):
        return _function(self.text, "Assert-CmdWrapperPathsAscii") + "\n"

    def _write_build_harness(self, tmp, hermes_root, wrapper_python, forge_py, deciders_py,
                              extra_env_lines=""):
        """Runs real path resolution (Assert-CmdWrapperPathsAscii) through
        the real forge.cmd/deciders.cmd content-building snippet (the exact
        source slice from the installer, not a reimplementation), writing
        both files under tmp. Returns the completed subprocess."""
        start = self.text.index("$ForgeEmbedPairs = @(")
        end = self.text.index('Write-Host "deciders installed: $decidersCmdPath"') + \
            len('Write-Host "deciders installed: $decidersCmdPath"')
        build_snippet = self.text[start:end]
        forge_path = Path(tmp) / "forge.cmd"
        deciders_path = Path(tmp) / "deciders.cmd"

        def q(s):
            return s.replace("'", "''")

        harness = Path(tmp) / "wrapper_harness.ps1"
        harness.write_text(
            extra_env_lines
            + f"$HermesRoot = '{q(str(hermes_root))}'\n"
            + f"$WrapperPython = '{q(wrapper_python)}'\n"
            + f"$ForgePy = '{q(forge_py)}'\n"
            + f"$DecidersPy = '{q(deciders_py)}'\n"
            + f"$ForgeMarker = 'REM dev-team forge wrapper (install-dev-team-windows.ps1)'\n"
            + f"$DecidersMarker = 'REM dev-team deciders wrapper (install-dev-team-windows.ps1)'\n"
            + f"$forgeCmdPath = '{q(str(forge_path))}'\n"
            + f"$decidersCmdPath = '{q(str(deciders_path))}'\n"
            + self._wrapper_prelude()
            + self._assert_cmd_wrapper_paths_ascii_fn()
            + "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
            + "$WrapperPythonCmd = $CmdPaths.WrapperPython\n"
            + "$ForgePyCmd = $CmdPaths.ForgePy\n"
            + "$DecidersPyCmd = $CmdPaths.DecidersPy\n"
            + build_snippet + "\n"
            + "Write-Output 'WRAPPERS_WRITTEN'\n")
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                              capture_output=True, text=True, timeout=60)
        return proc, forge_path, deciders_path

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_python_under_localappdata_is_rewritten_and_guarded(self):
        # Acceptance #1: Python at
        # C:\Users\Dragos\AppData\Local\hermes\tools\python-3.12\python.exe
        # with LOCALAPPDATA set to that AppData\Local.
        local = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local"
        wrapper_python = local + "\\hermes\\tools\\python-3.12\\python.exe"
        forge_py = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local\\dev-team\\forge\\forge.py"
        deciders_py = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            for p in (forge_path, deciders_path):
                raw = p.read_bytes()
                raw.decode("ascii")  # raises on any non-ASCII byte
                text = raw.decode("ascii")
                self.assertIn("%LOCALAPPDATA%\\hermes\\tools\\python-3.12\\python.exe", text)
                self.assertIn("if not exist", text, f"{p} missing the D4 guard")
                self.assertIn("exit /b 1", text, f"{p} missing the guard's error exit")
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%LOCALAPPDATA%\\dev-team\\forge\\forge.py", forge_text)
            deciders_text = deciders_path.read_bytes().decode("ascii")
            self.assertIn("%LOCALAPPDATA%\\dev-team\\deciders\\deciders.py", deciders_text)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_checkout_under_userprofile_is_rewritten(self):
        # Acceptance #1 (second half): checkout under %USERPROFILE%.
        userprofile = "C:\\Users\\Dr\u0103go\u0219"
        wrapper_python = "C:\\Python312\\python.exe"
        forge_py = userprofile + "\\dev-team\\forge\\forge.py"
        deciders_py = userprofile + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = f"$env:USERPROFILE = '{userprofile}'\n$env:LOCALAPPDATA = ''\n$env:APPDATA = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%USERPROFILE%\\dev-team\\forge\\forge.py", forge_text)
            self.assertIn("if not exist", forge_text)
            # The wrapper python here is plain ASCII, so it must be embedded
            # literally with no guard line for it.
            self.assertIn('"C:\\Python312\\python.exe"', forge_text)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_longest_prefix_wins_and_boundary_is_respected(self):
        # Acceptance #2: LOCALAPPDATA (longer, nested under USERPROFILE)
        # must win over USERPROFILE; case-insensitive; and a lookalike
        # directory name must not match at a false boundary.
        userprofile = "C:\\Users\\Dr\u0103go\u0219"
        local = userprofile + "\\AppData\\Local"
        wrapper_python = local.upper() + "\\hermes\\tools\\python-3.12\\python.exe"  # case-insensitive match
        forge_py = userprofile + "\\dev-team\\forge\\forge.py"
        deciders_py = userprofile + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = '{userprofile}'\n$env:APPDATA = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%LOCALAPPDATA%\\hermes\\tools\\python-3.12\\python.exe", forge_text,
                          "longest prefix (LOCALAPPDATA) did not win over USERPROFILE")
            self.assertNotIn("%USERPROFILE%\\AppData\\Local", forge_text)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_boundary_check_rejects_a_lookalike_sibling_directory(self):
        # "C:\Users\Dragos2\..." must NOT match USERPROFILE "C:\Users\Dragos".
        userprofile = "C:\\Users\\Dr\u0103go\u0219"
        sibling = "C:\\Users\\Dr\u0103go\u02192"
        wrapper_python = "C:\\Python312\\python.exe"
        forge_py = sibling + "\\dev-team\\forge\\forge.py"
        deciders_py = sibling + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = f"$env:USERPROFILE = '{userprofile}'\n$env:LOCALAPPDATA = ''\n$env:APPDATA = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not printable ASCII", proc.stderr)
        self.assertFalse(forge_path.exists(), "forge.cmd must not be written on refusal")
        self.assertFalse(deciders_path.exists(), "deciders.cmd must not be written on refusal")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_8_3_short_path_is_canonicalized_before_the_ascii_fast_path(self):
        # F1 (round 3 review): an 8.3 SHORT name is printable ASCII by
        # construction even when the LONG form it denotes is not -- the
        # OLD code tested $Path itself for ASCII-ness and took the
        # "already ASCII, embed literally" fast path before
        # Get-LongPathName ever ran, so a short-named $Path (e.g. a
        # non-ASCII checkout surfaced through 8.3 names, as can happen
        # under some launchers/CI runners) silently embedded the SHORT
        # name verbatim in forge.cmd instead of rewriting it to
        # %LOCALAPPDATA%. Exact repro from the review: a stub
        # Get-LongPathName mapping the short form below to its non-ASCII
        # long form, with LOCALAPPDATA set to that same long non-ASCII
        # form.
        short_path = "C:\\DEV-TE~1\\DRGO~1\\AppData\\Local\\Python\\python.exe"
        long_local = "C:\\dev-team\\Dr\u0103go\u0219\\AppData\\Local"
        long_path = long_local + "\\Python\\python.exe"

        def q(s):
            return s.replace("'", "''")

        stub = (
            "function Get-LongPathName {\n"
            "    param([string] $Path)\n"
            f"    if ($Path -eq '{q(short_path)}') {{ return '{q(long_path)}' }}\n"
            f"    if ($Path -eq '{q(long_local)}') {{ return '{q(long_local)}' }}\n"
            "    return $Path\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{q(long_local)}'\n"
                "$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                + self._wrapper_prelude() + stub +
                "$prefixes = Get-CmdPrefixCandidates\n"
                f"$r = Resolve-CmdEmbeddablePath -Label 'python' -Path '{q(short_path)}' -Prefixes $prefixes\n"
                "Write-Output \"EMBED=$($r.Embed)\"\n"
                "Write-Output \"REWRITTEN=$($r.Rewritten)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("EMBED=%LOCALAPPDATA%\\Python\\python.exe", proc.stdout, proc.stdout)
        self.assertIn("REWRITTEN=True", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_ascii_short_path_with_no_tilde_stays_on_the_fast_path(self):
        # F1 regression guard: a path with NO '~' at all must never pay
        # for a GetLongPathName call on the ASCII fast path, and must
        # stay byte-identical (Rewritten = $false, Embed = the literal
        # input) -- the '~'-gated canonicalization must not widen to
        # ordinary ASCII paths.
        path = "C:\\Python312\\python.exe"
        # A Get-LongPathName stub that FAILS the test if ever called for
        # this path proves the fast path never reaches it.
        stub = (
            "function Get-LongPathName {\n"
            "    param([string] $Path)\n"
            "    Write-Output 'LONGPATHNAME_CALLED'\n"
            "    return $Path\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                + self._wrapper_prelude() + stub +
                "$prefixes = Get-CmdPrefixCandidates\n"
                f"$r = Resolve-CmdEmbeddablePath -Label 'python' -Path '{path}' -Prefixes $prefixes\n"
                "Write-Output \"EMBED=$($r.Embed)\"\n"
                "Write-Output \"REWRITTEN=$($r.Rewritten)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"EMBED={path}", proc.stdout, proc.stdout)
        self.assertIn("REWRITTEN=False", proc.stdout, proc.stdout)
        self.assertNotIn("LONGPATHNAME_CALLED", proc.stdout,
                          "Get-LongPathName must not be called for a path with no '~'")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_F2_prefix_side_short_name_value_is_canonicalized_to_match(self):
        # F2: Get-LongPathName canonicalization on the PREFIX side (round
        # 2's existing behaviour, line "$val = (Get-LongPathName
        # ([string]$p.Value))...") was never directly exercised by a
        # test. Here $Path is already a long, non-ASCII form (no '~', so
        # the F1 fast-path gate never fires) and LOCALAPPDATA itself
        # holds an 8.3 SHORT value that must be canonicalized to the long
        # form before it can match $Path's prefix.
        long_local = "C:\\dev-team\\Dr\u0103go\u0219\\AppData\\Local"
        short_local = "C:\\DEV-TE~1\\DRGO~1\\AppData\\Local"
        path = long_local + "\\Python\\python.exe"

        def q(s):
            return s.replace("'", "''")

        stub = (
            "function Get-LongPathName {\n"
            "    param([string] $Path)\n"
            f"    if ($Path -eq '{q(short_local)}') {{ return '{q(long_local)}' }}\n"
            f"    if ($Path -eq '{q(path)}') {{ return '{q(path)}' }}\n"
            "    return $Path\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{q(short_local)}'\n"
                "$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                + self._wrapper_prelude() + stub +
                "$prefixes = Get-CmdPrefixCandidates\n"
                f"$r = Resolve-CmdEmbeddablePath -Label 'python' -Path '{q(path)}' -Prefixes $prefixes\n"
                "Write-Output \"EMBED=$($r.Embed)\"\n"
                "Write-Output \"REWRITTEN=$($r.Rewritten)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("EMBED=%LOCALAPPDATA%\\Python\\python.exe", proc.stdout, proc.stdout)
        self.assertIn("REWRITTEN=True", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_F5_assert_cmd_wrapper_paths_ascii_refuses_an_empty_embed(self):
        # F5: Assert-CmdWrapperPathsAscii's own defence-in-depth check
        # (round 2 review) -- if Resolve-CmdEmbeddablePath ever returned
        # an empty Embed (normally impossible: it either embeds the
        # literal path, embeds a non-empty "%NAME%\<remainder>", or exits
        # 2 itself), Assert-CmdWrapperPathsAscii must refuse (exit 2)
        # rather than let Set-Content write a wrapper with no interpreter
        # path at all. Shadow Resolve-CmdEmbeddablePath with a stub that
        # returns an empty Embed to force that path, since the real
        # function cannot be made to return one.
        def q(s):
            return s.replace("'", "''")

        stub = (
            "function Resolve-CmdEmbeddablePath {\n"
            "    param([string] $Label, [string] $Path, [object[]] $Prefixes, [switch] $NoRefuse)\n"
            "    return [ordered]@{ Embed = ''; Rewritten = $false; PrefixName = $null }\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$HermesRoot = '{q(str(Path(tmp) / 'hermes'))}'\n"
                "$WrapperPython = 'C:\\Python312\\python.exe'\n"
                "$ForgePy = 'C:\\dev-team\\forge\\forge.py'\n"
                "$DecidersPy = 'C:\\dev-team\\deciders\\deciders.py'\n"
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                + self._wrapper_prelude() + stub +
                self._assert_cmd_wrapper_paths_ascii_fn() +
                "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
                "Write-Output 'UNREACHABLE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("resolved empty", proc.stderr, proc.stderr)
        self.assertIn("refusing to write forge.cmd/deciders.cmd", proc.stderr, proc.stderr)
        self.assertNotIn("UNREACHABLE", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_path_with_no_usable_prefix_refuses_with_exit_2(self):
        # Acceptance #3: a non-ASCII path under none of the known folders.
        wrapper_python = "D:\\Pyth\u00f6n\\python.exe"
        forge_py = "C:\\dev-team\\forge\\forge.py"
        deciders_py = "C:\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not printable ASCII", proc.stderr)
        self.assertIn("is not under any of the folders", proc.stderr)
        self.assertFalse(forge_path.exists())
        self.assertFalse(deciders_path.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_ascii_path_wrapper_is_byte_identical_to_v1_3_1(self):
        # Acceptance #4: pin the exact v1.3.1 bytes for an all-ASCII install.
        wrapper_python = "C:\\Python312\\python.exe"
        forge_py = "C:\\dev-team\\forge\\forge.py"
        deciders_py = "C:\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            expected_forge = (
                b"@echo off\r\n"
                b"REM dev-team forge wrapper (install-dev-team-windows.ps1)\r\n"
                b'"C:\\Python312\\python.exe" "C:\\dev-team\\forge\\forge.py" %*\r\n'
                b"exit /b %ERRORLEVEL%\r\n")
            expected_deciders = (
                b"@echo off\r\n"
                b"REM dev-team deciders wrapper (install-dev-team-windows.ps1)\r\n"
                b'"C:\\Python312\\python.exe" "C:\\dev-team\\deciders\\deciders.py" %*\r\n'
                b"exit /b %ERRORLEVEL%\r\n")
            self.assertEqual(forge_path.read_bytes(), expected_forge)
            self.assertEqual(deciders_path.read_bytes(), expected_deciders)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_process_only_hermes_root_is_not_used_as_a_prefix(self):
        # Acceptance #6: HERMES_ROOT set only in this process's environment
        # (not persisted in the registry) must never be used as a prefix,
        # because a later cmd.exe would not see it either.
        hermes_root = "C:\\Users\\Dr\u0103go\u0219\\hermes"
        wrapper_python = "C:\\Python312\\python.exe"
        forge_py = hermes_root + "\\dev-team\\forge\\forge.py"
        deciders_py = hermes_root + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = (
                f"$env:HERMES_ROOT = '{hermes_root}'\n"
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n"
                "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, hermes_root, wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
        # Refused (exit 2): no persisted (registry) HERMES_ROOT matches, so
        # HERMES_ROOT must not have been used as a usable prefix despite
        # being set (and matching $HermesRoot) in this process's own
        # environment.
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("%HERMES_ROOT%", proc.stdout)
        self.assertFalse(forge_path.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_py_launcher_non_ascii_executable_is_decoded_correctly(self):
        # Acceptance #5: a stub `py` printing the UTF-16LE/surrogatepass hex
        # form of a non-ASCII sys.executable must be decoded and resolved.
        # M3 (round 2 review): the decoded path here is non-ASCII and not
        # embeddable (LOCALAPPDATA points elsewhere, no HERMES_ROOT), so it
        # only wins when PATH has nothing better -- PATH is cleared to
        # isolate the decode-and-fall-back-to-the-launcher-result behaviour
        # from this host's own, embeddable, system Python. (A PATH Python
        # winning INSTEAD of a non-embeddable py-launcher result is covered
        # by test_py_launcher_result_loses_to_an_embeddable_path_candidate.)
        func_body = _resolve_wrapper_python_prelude(self.text)
        with tempfile.TemporaryDirectory() as tmp:
            real_python = Path(tmp) / "Dr\u0103go\u0219" / "python.exe"
            real_python.parent.mkdir(parents=True)
            real_python.write_text("stub")
            hex_form = str(real_python).encode("utf-16-le").hex()
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{tmp}\\no-tools-here'\n"
                "$env:PATH = ''\n"
                "function py { Write-Output '" + hex_form + "'; $global:LASTEXITCODE = 0 }\n"
                + func_body + "\n"
                "$r = Resolve-WrapperPython\n"
                "Write-Output \"RESOLVED=$r\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"RESOLVED={real_python}", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_py_launcher_result_loses_to_an_embeddable_path_candidate(self):
        # M3 (round 2 review): the regression this fixed -- a non-ASCII,
        # non-embeddable py -3 result must NOT win outright over a PATH
        # Python that CAN be embedded; the installer used to hard-refuse
        # here even though PATH had a perfectly usable interpreter.
        func_body = _resolve_wrapper_python_prelude(self.text)
        with tempfile.TemporaryDirectory() as tmp:
            non_ascii_py = Path(tmp) / "Dr\u0103go\u0219" / "python.exe"
            non_ascii_py.parent.mkdir(parents=True)
            non_ascii_py.write_text("stub")
            hex_form = str(non_ascii_py).encode("utf-16-le").hex()
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            path_py = bindir / "python"
            path_py.write_text("#!/bin/sh\n")
            path_py.chmod(0o755)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{tmp}\\no-tools-here'\n"
                f"$env:PATH = '{bindir}'\n"
                "function py { Write-Output '" + hex_form + "'; $global:LASTEXITCODE = 0 }\n"
                + func_body + "\n"
                "$r = Resolve-WrapperPython\n"
                "Write-Output \"RESOLVED=$r\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"RESOLVED={path_py}", proc.stdout, proc.stdout)
        self.assertNotIn(str(non_ascii_py), proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_py_launcher_malformed_output_falls_through_to_path(self):
        func_body = _resolve_wrapper_python_prelude(self.text)
        with tempfile.TemporaryDirectory() as tmp:
            fallback_python = Path(tmp) / "fallback-python.exe"
            fallback_python.write_text("stub")
            fake_bin = Path(tmp) / "bin"
            fake_bin.mkdir()
            fake_python_path = fake_bin / "python"
            fake_python_path.write_text("#!/bin/sh\n")
            fake_python_path.chmod(0o755)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{tmp}\\no-tools-here'\n"
                "function py { Write-Output 'not-valid-hex!!'; $global:LASTEXITCODE = 0 }\n"
                "function global:Get-Command {\n"
                "    param([string] $Name, [switch] $All, $ErrorAction)\n"
                "    if ($Name -eq 'py') { return [pscustomobject]@{ Name = 'py' } }\n"
                f"    if ($Name -eq 'python') {{ return @([pscustomobject]@{{ Source = '{fallback_python}' }}) }}\n"
                "    return $null\n"
                "}\n"
                + func_body + "\n"
                "$r = Resolve-WrapperPython\n"
                "Write-Output \"RESOLVED=$r\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"RESOLVED={fallback_python}", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_never_says_no_python_3_found_when_launcher_reported_a_path(self):
        # Acceptance #5 / D8: the top-level call site must report the
        # launcher's path, not claim no Python 3 exists, when py -3
        # reported one that doesn't resolve on disk.
        start = self.text.index("$script:WrapperPythonLauncherPath = $null")
        end = self.text.index("$CmdPaths = Assert-CmdWrapperPathsAscii")
        snippet = self.text[start:end]
        func_body = _resolve_wrapper_python_prelude(self.text)
        format_fn = _function(self.text, "Format-EnvPinForDisplay")
        missing = "C:\\Users\\Dr\u0103go\u0219\\gone\\python.exe"
        hex_form = missing.encode("utf-16-le").hex()
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{tmp}\\no-tools-here'\n"
                "function py { Write-Output '" + hex_form + "'; $global:LASTEXITCODE = 0 }\n"
                "function global:Get-Command {\n"
                "    param([string] $Name, [switch] $All, $ErrorAction)\n"
                "    if ($Name -eq 'py') { return [pscustomobject]@{ Name = 'py' } }\n"
                "    return $null\n"
                "}\n"
                + format_fn + "\n" + func_body + "\n" + snippet + "\n"
                + "Write-Output 'UNREACHABLE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("No Python 3 found", proc.stderr)
        self.assertIn("py launcher reported", proc.stderr)
        self.assertIn("does not exist", proc.stderr)
        self.assertNotIn("UNREACHABLE", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_guard_echo_pins_quoted_doubled_percent_bytes_for_ampersand_remainder(self):
        # S1 (review round 1): pin the exact bytes of a rewritten forge.cmd
        # whose guarded remainder contains '&' (a legal filename char, and
        # the reviewer's repro used an ASCII remainder "R&D"). The OLD
        # unquoted/expanded echo would let cmd.exe split this line and run
        # the fragment after '&' as a command on the fail-closed path.
        local = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local"
        wrapper_python = "C:\\Python312\\python.exe"  # ASCII: embedded literally, no guard
        forge_py = local + "\\dev-team\\R&D (x)\\forge\\forge.py"
        deciders_py = local + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = (f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = ''\n"
                     "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            expected_forge = (
                "@echo off\r\n"
                "REM dev-team forge wrapper (install-dev-team-windows.ps1)\r\n"
                "setlocal DisableDelayedExpansion\r\n"
                "if not defined LOCALAPPDATA goto :dt_forge_missing_0\r\n"
                'if not exist "%LOCALAPPDATA%\\dev-team\\R&D (x)\\forge\\forge.py" goto :dt_forge_missing_0\r\n'
                '"C:\\Python312\\python.exe" "%LOCALAPPDATA%\\dev-team\\R&D (x)\\forge\\forge.py" %*\r\n'
                "exit /b %ERRORLEVEL%\r\n"
                ":dt_forge_missing_0\r\n"
                'echo dev-team: forge.py was not found at "%%LOCALAPPDATA%%\\dev-team\\R&D (x)\\forge\\forge.py" 1>&2\r\n'
                "echo This happens when this wrapper runs under a different Windows user than "
                "installed it, or after the Python moved -- re-run install-dev-team-windows.ps1 "
                "as this user. 1>&2\r\n"
                "exit /b 1\r\n")
            self.assertEqual(forge_text, expected_forge)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_guard_structure_guards_before_run_line_labels_after_errorlevel_exit(self):
        # S1/C3: structural shape of the guard block, not just substrings
        # -- every "if not exist" guard line comes before the run line
        # (and hence before "exit /b %ERRORLEVEL%"), every ":label" comes
        # after it, and every goto target has a matching label.
        local = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local"
        wrapper_python = local + "\\hermes\\tools\\python-3.12\\python.exe"
        forge_py = local + "\\dev-team\\forge\\forge.py"
        deciders_py = local + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = (f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = ''\n"
                     "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            for p in (forge_path, deciders_path):
                text = p.read_bytes().decode("ascii")
                lines = text.split("\r\n")
                errorlevel_idx = lines.index("exit /b %ERRORLEVEL%")
                guard_idxs = [i for i, l in enumerate(lines) if l.startswith("if not exist")]
                label_idxs = [i for i, l in enumerate(lines) if l.startswith(":")]
                self.assertTrue(guard_idxs, f"{p} has no guard lines")
                self.assertTrue(label_idxs, f"{p} has no labels")
                self.assertTrue(all(i < errorlevel_idx for i in guard_idxs),
                                 f"{p}: a guard line comes after the run line")
                self.assertTrue(all(i > errorlevel_idx for i in label_idxs),
                                 f"{p}: a label comes before exit /b %ERRORLEVEL%, so it "
                                 "could be fallen into on the success path")
                goto_targets = {t.lstrip(":") for t in re.findall(r"goto (\S+)", text)}
                label_names = {l[1:] for l in lines if l.startswith(":")}
                self.assertEqual(goto_targets, label_names, f"{p}: goto/label mismatch")
                self.assertEqual(len(goto_targets), 2, f"{p}: expected one guard per rewritten path")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_lookalike_i_with_dot_above_is_rewritten_not_corrupted(self):
        # S2: U+0130 (LATIN CAPITAL LETTER I WITH DOT ABOVE, Turkish "I")
        # folds to ASCII 'i' under PowerShell's default case-insensitive
        # -match, so the old check reported this path as already
        # printable ASCII, embedded it literally, and -Encoding ascii
        # then wrote '?' in its place.
        local = "C:\\Users\\\u0130smail\\AppData\\Local"
        wrapper_python = local + "\\hermes\\tools\\python-3.12\\python.exe"
        forge_py = local + "\\dev-team\\forge\\forge.py"
        deciders_py = local + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = (f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = ''\n"
                     "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%LOCALAPPDATA%\\hermes\\tools\\python-3.12\\python.exe", forge_text)
            self.assertNotIn("?", forge_text, "the non-ASCII İ was corrupted into '?'")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_lookalike_kelvin_sign_is_rewritten_not_corrupted(self):
        # S2: U+212A KELVIN SIGN folds to ASCII 'k' under the same bug.
        local = "C:\\Users\\\u212Aelvin\\AppData\\Local"
        wrapper_python = local + "\\hermes\\tools\\python-3.12\\python.exe"
        forge_py = local + "\\dev-team\\forge\\forge.py"
        deciders_py = local + "\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = (f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = ''\n"
                     "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%LOCALAPPDATA%\\hermes\\tools\\python-3.12\\python.exe", forge_text)
            self.assertNotIn("?", forge_text, "the non-ASCII Kelvin sign was corrupted into '?'")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_non_ascii_lookalike_with_no_usable_prefix_refuses_with_exit_2(self):
        # S2: the same lookalike under no known prefix must still hard-refuse
        # (exit 2, nothing written), not silently embed it as "ASCII".
        wrapper_python = "D:\\P\u0130thon\\python.exe"
        forge_py = "C:\\dev-team\\forge\\forge.py"
        deciders_py = "C:\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = ("$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n"
                     "$env:APPDATA = ''\n$env:ProgramFiles = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("not printable ASCII", proc.stderr)
        self.assertFalse(forge_path.exists())
        self.assertFalse(deciders_path.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_d9_defense_catches_a_case_insensitive_ascii_lookalike(self):
        # S2: Assert-WrapperTextAscii (D9, defence in depth) has the same
        # case-insensitive -match bug as Resolve-CmdEmbeddablePath; feed it
        # text containing U+0130 directly (bypassing path resolution) and
        # confirm it still refuses with exit 2.
        ordinal_fn = _function(self.text, "Test-AsciiOrdinalRange")
        assert_fn = _function(self.text, "Assert-WrapperTextAscii")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "d9.ps1"
            harness.write_text(
                ordinal_fn + "\n" + assert_fn + "\n"
                + "Assert-WrapperTextAscii 'test' \"malicious \u0130 text\"\n"
                "Write-Output 'UNREACHABLE'\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Internal error", proc.stderr)
        self.assertNotIn("UNREACHABLE", proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_hermes_root_prefix_uses_effective_value_not_any_matching_scope(self):
        # C1: the effective HERMES_ROOT a later cmd.exe sees is the User
        # value when it's non-empty, else the Machine value. A DIFFERENT
        # User value must not be shadowed by a Machine value that happens
        # to match $HermesRoot -- Get-PersistedEnvVar is the seam this
        # fix introduced so the registry read can be stubbed in a test.
        hermes_root = "C:\\dev-team\\hermes"
        ordinal_fn = _function(self.text, "Test-AsciiOrdinalRange")
        func_body = _function(self.text, "Get-CmdPrefixCandidates")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "c1.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n"
                "$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                "function Get-PersistedEnvVar {\n"
                "    param([string] $Name, [string] $Scope)\n"
                "    if ($Scope -eq 'User') { return 'C:\\dev-team\\OTHER' }\n"
                f"    if ($Scope -eq 'Machine') {{ return '{hermes_root}' }}\n"
                "    return $null\n"
                "}\n"
                + ordinal_fn + "\n" + func_body + "\n"
                + f"$r = Get-CmdPrefixCandidates -HermesRoot '{hermes_root}'\n"
                "Write-Output \"NAMES=$($r | ForEach-Object { $_.Name })\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("HERMES_ROOT", proc.stdout,
                         "a Machine value shadowed by a different User value must not be used")

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_hermes_root_candidate_is_tier_2_os_profile_vars_are_tier_1(self):
        # M2 (round 2 review): Get-CmdPrefixCandidates must mark every OS
        # profile variable (LOCALAPPDATA/APPDATA/USERPROFILE/ProgramW6432)
        # as Tier 1 and a persisted, matching HERMES_ROOT as Tier 2 -- the
        # seam Resolve-CmdEmbeddablePath relies on to never prefer
        # HERMES_ROOT over an OS variable just because it is longer.
        hermes_root = "C:\\Users\\Drag\\dev-team\\hermes"
        ordinal_fn = _function(self.text, "Test-AsciiOrdinalRange")
        func_body = _function(self.text, "Get-CmdPrefixCandidates")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "m2_tier.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = 'C:\\Users\\Drag'\n"
                "$env:APPDATA = ''\n$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n"
                "function Get-PersistedEnvVar {\n"
                "    param([string] $Name, [string] $Scope)\n"
                f"    if ($Scope -eq 'User') {{ return '{hermes_root}' }}\n"
                "    return $null\n"
                "}\n"
                + ordinal_fn + "\n" + func_body + "\n"
                + f"$r = Get-CmdPrefixCandidates -HermesRoot '{hermes_root}'\n"
                "foreach ($c in $r) { Write-Output \"$($c.Name)=$($c.Tier)\" }\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("USERPROFILE=1", proc.stdout, proc.stdout)
        self.assertIn("HERMES_ROOT=2", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_os_profile_variable_wins_over_a_matching_hermes_root(self):
        # M2 (round 2 review) acceptance: a path sits under BOTH a
        # persisted, matching HERMES_ROOT and %USERPROFILE% (HERMES_ROOT is
        # a subdirectory of the user's non-ASCII profile, the common case)
        # -- the rewritten wrapper must use %USERPROFILE%, never
        # %HERMES_ROOT%, even though HERMES_ROOT is the LONGER (and so,
        # pre-M2, winning) prefix. Hermes itself never defines HERMES_ROOT
        # (it uses HERMES_HOME), so a user who stops setting/persisting it
        # later must not silently break a wrapper that preferred it.
        userprofile = "C:\\Users\\Dr\u0103go\u0219"
        hermes_root = userprofile + "\\dev-team\\hermes"
        forge_py = hermes_root + "\\forge\\forge.py"  # ASCII remainder either way
        deciders_py = hermes_root + "\\deciders\\deciders.py"
        wrapper_python = "C:\\Python312\\python.exe"
        get_persisted_stub = (
            "function Get-PersistedEnvVar {\n"
            "    param([string] $Name, [string] $Scope)\n"
            f"    if ($Scope -eq 'User') {{ return '{hermes_root}' }}\n"
            "    return $null\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            extra = (get_persisted_stub +
                     f"$env:USERPROFILE = '{userprofile}'\n"
                     "$env:LOCALAPPDATA = ''\n$env:APPDATA = ''\n"
                     "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(hermes_root), wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            forge_text = forge_path.read_bytes().decode("ascii")
            self.assertIn("%USERPROFILE%\\dev-team\\hermes\\forge\\forge.py", forge_text,
                          "the OS profile variable (tier 1) must win over HERMES_ROOT (tier 2)")
            self.assertNotIn("%HERMES_ROOT%", forge_text)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_programw6432_prefix_used_ahead_of_programfiles(self):
        # m2 (round 2 review): %ProgramW6432% always names the true 64-bit
        # Program Files, unlike %ProgramFiles% which WOW64-redirects under a
        # 32-bit cmd.exe -- when both are set, Get-CmdPrefixCandidates must
        # offer %ProgramW6432%, not %ProgramFiles% (tested at this level,
        # not through a full non-ASCII path: both variables' own values are
        # always plain ASCII, so neither can ever be the matched prefix for
        # a genuinely non-ASCII $Path -- only their PRESENCE as a candidate,
        # and the refusal message's folder list, are observable).
        ordinal_fn = _function(self.text, "Test-AsciiOrdinalRange")
        func_body = _function(self.text, "Get-CmdPrefixCandidates")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "m2_w6432.ps1"
            harness.write_text(
                "$env:ProgramW6432 = 'C:\\Program Files'\n"
                "$env:ProgramFiles = 'C:\\Program Files (x86)'\n"
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                + ordinal_fn + "\n" + func_body + "\n"
                + "$r = Get-CmdPrefixCandidates -HermesRoot $null\n"
                "Write-Output \"NAMES=$($r | ForEach-Object { $_.Name })\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ProgramW6432", proc.stdout, proc.stdout)
        self.assertNotIn("ProgramFiles", proc.stdout.replace("ProgramW6432", ""), proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_programfiles_is_the_fallback_when_programw6432_is_absent(self):
        # m2 (round 2 review): on a 32-bit Windows OS, %ProgramW6432% does
        # not exist at all -- plain %ProgramFiles% must still be offered
        # there (see test above for why only presence, not an actual
        # rewrite, is observable here).
        ordinal_fn = _function(self.text, "Test-AsciiOrdinalRange")
        func_body = _function(self.text, "Get-CmdPrefixCandidates")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "m2_fallback.ps1"
            harness.write_text(
                "$env:ProgramFiles = 'C:\\Program Files'\n"
                "$env:ProgramW6432 = ''\n"
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                + ordinal_fn + "\n" + func_body + "\n"
                + "$r = Get-CmdPrefixCandidates -HermesRoot $null\n"
                "Write-Output \"NAMES=$($r | ForEach-Object { $_.Name })\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ProgramFiles", proc.stdout, proc.stdout)

    # ---- N1/N2/N3/N4 + D1 (round-7 review, pr12-review-r2.md §3) ----

    def _embeddable_prelude(self):
        """_wrapper_prelude plus Test-WrapperPythonEmbeddable itself, which
        probes through the real Resolve-CmdEmbeddablePath (-NoRefuse)."""
        return self._wrapper_prelude() + _function(self.text, "Test-WrapperPythonEmbeddable") + "\n"

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N4_ascii_x86_path_with_no_prefix_falls_back_to_a_literal_embed(self):
        # N4: the round-6 literal fallback had no test AND the harness could
        # not even reach it -- CMD_WRAPPER_FUNCS was missing
        # Test-ContainsUnsafeCmdEmbedChar, so this exact path died with
        # rc=1 ("... is not recognized as ... function") instead of
        # producing a verdict. "C:\Program Files (x86)\..." is pure ASCII,
        # matches no %ENVVAR% prefix (there is deliberately no
        # ProgramFiles(x86) candidate) and contains only quote-safe
        # metacharacters, so it must be embedded literally, unrewritten.
        path = "C:\\Program Files (x86)\\Python312\\python.exe"
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n"
                + self._wrapper_prelude() +
                "$prefixes = Get-CmdPrefixCandidates\n"
                f"$r = Resolve-CmdEmbeddablePath -Label 'python' -Path '{path}' -Prefixes $prefixes\n"
                "Write-Output \"EMBED=$($r.Embed)\"\n"
                "Write-Output \"REWRITTEN=$($r.Rewritten)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"EMBED={path}", proc.stdout, proc.stdout)
        self.assertIn("REWRITTEN=False", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N1_ascii_path_with_quote_safe_metachars_under_a_prefix_stays_literal(self):
        # N1: the A3 round-5 gate put EVERY cmd.exe metacharacter
        # (& ^ % ! ( ) " < >) through the %ENVVAR%-rewrite branch, so an
        # all-ASCII path containing only the quote-safe ones (& ^ ( ))
        # started being rewritten the moment it happened to sit under a
        # profile folder -- gaining variable guards, an exit 1 under a
        # trimmed environment, and following %USERPROFILE% to whichever
        # user runs the wrapper. 1.3.1 embedded all three of these
        # literally. Only '%', '"' and '!' are genuinely unsafe in a
        # quoted literal embed, so only those may force the rewrite.
        local = "C:\\Users\\bob\\AppData\\Local"
        userprofile = "C:\\Users\\bob"
        cases = (
            ("AMP", local + "\\Programs\\R&D\\python.exe"),
            ("PARENS", userprofile + "\\src (copy)\\dev-team\\forge\\forge.py"),
            ("CARET", userprofile + "\\a^b\\python.exe"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            body = (
                f"$env:LOCALAPPDATA = '{local}'\n$env:USERPROFILE = '{userprofile}'\n"
                "$env:APPDATA = ''\n$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n"
                + self._wrapper_prelude() +
                "$prefixes = Get-CmdPrefixCandidates\n")
            for name, p in cases:
                body += (f"$r = Resolve-CmdEmbeddablePath -Label 'x' -Path '{p}' -Prefixes $prefixes\n"
                         f"Write-Output \"{name}_EMBED=$($r.Embed)\"\n"
                         f"Write-Output \"{name}_REWRITTEN=$($r.Rewritten)\"\n")
            harness.write_text(body)
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name, p in cases:
            with self.subTest(case=name):
                self.assertIn(f"{name}_EMBED={p}", proc.stdout, proc.stdout)
                self.assertIn(f"{name}_REWRITTEN=False", proc.stdout, proc.stdout)
        self.assertNotIn("%LOCALAPPDATA%", proc.stdout, proc.stdout)
        self.assertNotIn("%USERPROFILE%", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N4_refuses_an_ascii_path_containing_a_bang_or_a_percent(self):
        # N4 + N1's boundary: the three characters that are NOT safe in a
        # quoted literal embed must still be refused (exit 2) when no
        # %ENVVAR% prefix covers the path -- widening the literal fallback
        # to '& ^ ( )' must not widen it to '%', '!' or '"'. '"' is not
        # testable here: it is illegal in a Windows file name.
        for name, path in (("BANG", "D:\\dev!\\python.exe"),
                           ("PERCENT", "D:\\50%off\\python.exe")):
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as tmp:
                    harness = Path(tmp) / "harness.ps1"
                    harness.write_text(
                        "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                        "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n"
                        + self._wrapper_prelude() +
                        "$prefixes = Get-CmdPrefixCandidates\n"
                        f"$r = Resolve-CmdEmbeddablePath -Label 'python' -Path '{path}' -Prefixes $prefixes\n"
                        "Write-Output 'UNREACHABLE'\n")
                    proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                          capture_output=True, text=True, timeout=60)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("UNREACHABLE", proc.stdout, proc.stdout)
                self.assertIn("cannot be safely embedded", proc.stderr, proc.stderr)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N2_wrapper_python_embeddable_agrees_with_the_probe_for_an_8_3_short_path(self):
        # N2: Test-WrapperPythonEmbeddable kept its OWN fast path, which
        # skipped the 8.3 long-form canonicalisation the real
        # Resolve-CmdEmbeddablePath does. An 8.3 short candidate is
        # printable ASCII by construction, so this function said
        # "embeddable" and Resolve-WrapperPython committed to it -- then
        # the later, real, non-probe call refused the whole install with
        # exit 2 instead of falling back to the next source. That is
        # exactly the M3 regression this function exists to prevent.
        # Review repro: D:\PYTHN~1\python.exe -> D:\Pythoen\python.exe.
        short_path = "D:\\PYTHN~1\\python.exe"
        long_path = "D:\\Pyth\u00f6n\\python.exe"

        def q(s):
            return s.replace("'", "''")

        stub = (
            "function Get-LongPathName {\n"
            "    param([string] $Path)\n"
            f"    if ($Path -eq '{q(short_path)}') {{ return '{q(long_path)}' }}\n"
            "    return $Path\n"
            "}\n")
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n$HermesRoot = $null\n"
                + self._embeddable_prelude() + stub +
                f"$e = Test-WrapperPythonEmbeddable '{q(short_path)}'\n"
                f"$p = Resolve-CmdEmbeddablePath -Label 'probe' -Path '{q(short_path)}' "
                "-Prefixes (Get-CmdPrefixCandidates -HermesRoot $HermesRoot) -NoRefuse\n"
                "Write-Output \"EMBEDDABLE=$e\"\n"
                "Write-Output \"PROBE=$([bool]$p)\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PROBE=False", proc.stdout, proc.stdout)
        self.assertIn("EMBEDDABLE=False", proc.stdout,
                      "Test-WrapperPythonEmbeddable disagreed with its own -NoRefuse probe: "
                      + proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N4_wrapper_python_embeddable_for_metachar_paths(self):
        # N4: Test-WrapperPythonEmbeddable with a metacharacter path -- the
        # quote-safe ones are embeddable (literally), the unsafe ones are
        # not, and in both cases the answer must come from the single
        # -NoRefuse probe, not a second copy of the rule.
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                "$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n$HermesRoot = $null\n"
                + self._embeddable_prelude() +
                "Write-Output \"X86=$(Test-WrapperPythonEmbeddable 'C:\\Program Files (x86)\\Python312\\python.exe')\"\n"
                "Write-Output \"AMP=$(Test-WrapperPythonEmbeddable 'C:\\R&D\\python.exe')\"\n"
                "Write-Output \"BANG=$(Test-WrapperPythonEmbeddable 'D:\\dev!\\python.exe')\"\n"
                "Write-Output \"PERCENT=$(Test-WrapperPythonEmbeddable 'D:\\50%off\\python.exe')\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("X86=True", proc.stdout, proc.stdout)
        self.assertIn("AMP=True", proc.stdout, proc.stdout)
        self.assertIn("BANG=False", proc.stdout, proc.stdout)
        self.assertIn("PERCENT=False", proc.stdout, proc.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_D1_literal_embed_containing_a_caret_gets_setlocal(self):
        # D1 (round-7 decision): a LITERAL (unrewritten) embed containing
        # '^' had no `setlocal DisableDelayedExpansion`. Under `cmd /v:on`,
        # cmd's delayed-expansion pass strips carets inside quotes when the
        # line also contains a '!' (which `%*` can supply from a caller's
        # argument), so the path actually run differs from the path
        # embedded. The fix widens the EXISTING setlocal trigger rather
        # than pushing '^' paths into the rewrite branch -- that would
        # reintroduce N1 for caret paths. Pinned as exact bytes: the
        # wrapper must be 1.3.1's wrapper plus that one line, and nothing
        # else (no guards, no tail, since nothing was rewritten).
        wrapper_python = "C:\\P^y\\python.exe"
        forge_py = "C:\\dev-team\\forge\\forge.py"
        deciders_py = "C:\\dev-team\\deciders\\deciders.py"
        with tempfile.TemporaryDirectory() as tmp:
            extra = ("$env:LOCALAPPDATA = ''\n$env:USERPROFILE = ''\n$env:APPDATA = ''\n"
                     "$env:ProgramFiles = ''\n$env:ProgramW6432 = ''\n")
            proc, forge_path, deciders_path = self._write_build_harness(
                tmp, Path(tmp) / "hermes", wrapper_python, forge_py, deciders_py,
                extra_env_lines=extra)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            expected_forge = (
                b"@echo off\r\n"
                b"REM dev-team forge wrapper (install-dev-team-windows.ps1)\r\n"
                b"setlocal DisableDelayedExpansion\r\n"
                b'"C:\\P^y\\python.exe" "C:\\dev-team\\forge\\forge.py" %*\r\n'
                b"exit /b %ERRORLEVEL%\r\n")
            self.assertEqual(forge_path.read_bytes(), expected_forge)
            self.assertNotIn(b"if not exist", deciders_path.read_bytes(),
                             "a literal caret embed must not gain existence guards")
            self.assertIn(b"setlocal DisableDelayedExpansion", deciders_path.read_bytes())

    @unittest.skipUnless(shutil.which("pwsh"), "pwsh not installed")
    def test_N3_warns_when_the_py_launcher_choice_is_skipped_for_an_unsafe_char(self):
        # N3: an ASCII `py -3` path containing '!' (or '%') is not
        # embeddable, so Resolve-WrapperPython silently switched to the
        # PATH Python -- 1.3.1 used the launcher's one. The switch is
        # defensible (a '!' path breaks under `cmd /v:on`) but it must not
        # be silent: the operator has to be told which interpreter they
        # actually got and why.
        func_body = _resolve_wrapper_python_prelude(self.text)
        with tempfile.TemporaryDirectory() as tmp:
            bang_py = Path(tmp) / "Tools!" / "python.exe"
            bang_py.parent.mkdir(parents=True)
            bang_py.write_text("stub")
            hex_form = str(bang_py).encode("utf-16-le").hex()
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            path_py = bindir / "python"
            path_py.write_text("#!/bin/sh\n")
            path_py.chmod(0o755)
            harness = Path(tmp) / "harness.ps1"
            harness.write_text(
                f"$env:LOCALAPPDATA = '{tmp}\\no-tools-here'\n"
                "$env:USERPROFILE = ''\n$env:APPDATA = ''\n$env:ProgramFiles = ''\n"
                "$env:ProgramW6432 = ''\n$HermesRoot = $null\n"
                f"$env:PATH = '{bindir}'\n"
                "function py { Write-Output '" + hex_form + "'; $global:LASTEXITCODE = 0 }\n"
                + func_body + "\n"
                "$r = Resolve-WrapperPython\n"
                "Write-Output \"RESOLVED=$r\"\n")
            proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                  capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"RESOLVED={path_py}", proc.stdout, proc.stdout)
        # Write-Warning goes to PowerShell's warning stream; which OS stream
        # that lands on depends on the host, so check both.
        out = proc.stdout + proc.stderr
        self.assertIn("WARNING", out, out)
        self.assertIn("cannot be safely embedded", out, out)
        self.assertIn(str(bang_py), out, out)
        self.assertIn("PY_PYTHON", out, out)

    def test_D2_the_literal_embed_rule_has_exactly_one_deciding_call_site(self):
        # D2 (round-7 decision): round-6 F1, N1 and N2 were all the same
        # bug -- two copies of the literal-embed rule drifted apart. The
        # rule is Test-ContainsUnsafeCmdEmbedChar, and exactly ONE place may
        # DECIDE embeddability from it: Resolve-CmdEmbeddablePath's fast
        # path. Test-WrapperPythonEmbeddable must reach the same answer
        # through that function's -NoRefuse probe and never ask the
        # classifier itself (that second copy WAS N2). The superseded,
        # wider Test-ContainsCmdShellMetachar must have no call site left
        # at all.
        # Not a ban on every mention: N3's skipped-py-launcher warning
        # reads the classifier to explain WHY a candidate was declined,
        # which decides nothing about an embed.
        gate = _function(self.text, "Resolve-CmdEmbeddablePath")
        code = [ln for ln in gate.splitlines() if not ln.strip().startswith("#")]
        calls = [ln.strip() for ln in code if "Test-ContainsUnsafeCmdEmbedChar $" in ln]
        self.assertEqual(len(calls), 1,
                         "Resolve-CmdEmbeddablePath consults the literal-embed gate "
                         + f"{len(calls)} times, not once: " + "; ".join(calls))
        probe = _function(self.text, "Test-WrapperPythonEmbeddable")
        probe_code = [ln for ln in probe.splitlines() if not ln.strip().startswith("#")]
        own = [ln.strip() for ln in probe_code
               if "Test-ContainsUnsafeCmdEmbedChar" in ln or "Test-AsciiOrdinalRange" in ln]
        self.assertEqual(own, [], "Test-WrapperPythonEmbeddable has its own copy of the rule "
                                  "again instead of using the -NoRefuse probe: " + "; ".join(own))
        stale = re.findall(r"Test-ContainsCmdShellMetachar\s+\$\w+", self.text)
        self.assertEqual(stale, [], "the superseded, wider metachar gate is called again: "
                                    + "; ".join(stale))

    def test_py_launcher_c_program_has_no_double_quotes_and_decodes_round_trip(self):
        # C4: the py -3 -c program (never directly asserted before) must
        # contain no '"' (PS 5.1's native-argument quoting of a literal
        # '"' is unreliable -- see the comment at the call site), and must
        # actually decode a non-ASCII sys.executable end to end: real
        # python3 runs the EXACT extracted program (with sys.executable
        # patched to a non-ASCII value), and the installer's OWN decode
        # snippet (also extracted verbatim, run under pwsh) must recover
        # the exact original string.
        m = re.search(r"& py -3 -c '(.*)' 2>\$null", self.text)
        self.assertIsNotNone(m, "could not find the py -3 -c invocation")
        program = m.group(1).replace("''", "'")
        self.assertNotIn('"', program, "the -c program must stay free of double quotes")
        non_ascii_exec = "C:\\Users\\Dr\u0103go\u0219\\python.exe"
        full_program = f"import sys; sys.executable = {non_ascii_exec!r}\n" + program
        py_proc = subprocess.run([sys.executable, "-c", full_program],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(py_proc.returncode, 0, py_proc.stderr)
        hex_out = py_proc.stdout.strip()
        self.assertRegex(hex_out, r"^([0-9a-f]{4})+$")
        if not shutil.which("pwsh"):
            self.skipTest("pwsh not installed")
        start = self.text.index("$hexLines = @(@($hex)")
        end = self.text.index("$decoded = [Text.Encoding]::Unicode.GetString($bytes)") + \
            len("$decoded = [Text.Encoding]::Unicode.GetString($bytes)")
        decode_snippet = self.text[start:end] + "\n}\n"  # close the `if` block (slice already closes the `for`)
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "decode.ps1"
            # m3 (round 2 review): $hex can be a multi-line array (py -3
            # printing a deprecation notice before its own hex line) -- the
            # decode snippet must use the LAST non-empty line, not join or
            # take the first. Exercise that here with a stray leading line.
            harness.write_text("$hex = @('deprecation warning', '" + hex_out + "')\n" + decode_snippet +
                               "\nWrite-Output \"DECODED=$decoded\"\n")
            ps_proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(harness)],
                                     capture_output=True, text=True, timeout=60)
        self.assertEqual(ps_proc.returncode, 0, ps_proc.stdout + ps_proc.stderr)
        self.assertIn(f"DECODED={non_ascii_exec}", ps_proc.stdout, ps_proc.stdout)


if __name__ == "__main__":
    unittest.main()
