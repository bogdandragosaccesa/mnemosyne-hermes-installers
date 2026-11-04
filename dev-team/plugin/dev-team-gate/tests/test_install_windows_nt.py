"""Windows-only evidence for A3 (PR #9 follow-ups), run by the
install-windows-ps51 CI job (.github/workflows/dev-team.yml, step
"Windows-only env-pin and cmd-wrapper evidence (A3)"). Skipped elsewhere:
it needs cmd.exe, Windows PowerShell 5.1 and a real UTF-16 environment
block, none of which exist on Linux.

What it proves, on the CI target itself:
  1. Get-EnvPinCheckerReason under 5.1 hands env_pin_check.py the value
     exact through $env:DEV_TEAM_ENV_CHECK_VALUE: a non-ASCII bank is
     accepted, U+2028 is refused, and a lone surrogate -- which only a
     UTF-16 environment can carry -- reaches the checker and is refused by
     the checker itself.
  2. Assert-CmdWrapperPathsAscii under 5.1 refuses a real non-ASCII-path
     Python (a venv created under a non-ASCII directory) with exit 2.
  3. The premise of that refusal: the wrapper as the installer used to
     write it (-Encoding ascii) does not run that Python under cmd.exe.
  4. Both wrappers, built from the installer's own content lines, run an
     ASCII-path Python under cmd.exe and propagate its exit code.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_install_windows import PS1, _function, _pin_prelude, CMD_WRAPPER_FUNCS, _cmd_wrapper_prelude  # noqa: E402

TEXT = PS1.read_text(encoding="utf-8")
NON_ASCII_DIR = "Dr\u0103go\u0219 \u0105\u00c5\u0445 \u56e2\u961f"
SAMPLE = "C:\\Users\\Dr\u0103go\u0219\\\u0105\u00c5\u0445 \u56e2\u961f\u30d0\u30f3\u30af\\bank"


def _ps_q(s):
    return "'" + str(s).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt", "Windows only: needs cmd.exe, Windows PowerShell 5.1 "
                                      "and a UTF-16 environment block")
class WindowsNtEvidence(unittest.TestCase):
    def setUp(self):
        self.powershell = shutil.which("powershell")  # Windows PowerShell 5.1
        self.assertTrue(self.powershell, "powershell.exe (5.1) not found")

    def _ps51(self, tmp, body, env_extra=None):
        h = Path(tmp) / "a3_nt.ps1"
        # 5.1 reads a BOM-less script as ANSI; the BOM makes it UTF-8.
        h.write_text(body, encoding="utf-8-sig")
        env = dict(os.environ)
        env.update(env_extra or {})
        return subprocess.run(
            [self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(h)],
            capture_output=True, text=True, errors="replace", timeout=300, env=env)

    def _non_ascii_venv_python(self, tmp):
        d = Path(tmp) / NON_ASCII_DIR / "venv"
        venv.create(d, with_pip=False)
        py = d / "Scripts" / "python.exe"
        self.assertTrue(py.exists(), py)
        return py

    def test_checker_gets_the_value_exact_under_ps51(self):
        if not str(sys.executable).isascii():
            self.skipTest("this runner's own Python is on a non-ASCII path")
        with tempfile.TemporaryDirectory() as tmp:
            body = (
                "$ErrorActionPreference = 'Stop'\n"
                "if ($PSVersionTable.PSVersion.Major -ne 5) { Write-Output 'NOT_51'; exit 1 }\n"
                + _pin_prelude(TEXT) +
                "function Show($r) { if ($null -eq $r) { 'SAFE' } else { 'REFUSED ' + $r } }\n"
                "Write-Output ('SAMPLE=' + (Show (Get-EnvPinCheckerReason -Key 'MNEMOSYNE_HOME' -Value $env:A3_SAMPLE)))\n"
                "Write-Output ('LS=' + (Show (Get-EnvPinCheckerReason -Key 'MNEMOSYNE_HOME' -Value $env:A3_LS)))\n"
                "$sur = 'C:\\a' + [char]0xD800 + 'b'\n"
                "Write-Output ('SUR=' + (Show (Get-EnvPinCheckerReason -Key 'MNEMOSYNE_HOME' -Value $sur)))\n"
                "Write-Output ('LEFT=[' + $env:DEV_TEAM_ENV_CHECK_VALUE + ']')\n")
            proc = self._ps51(tmp, body, {"A3_SAMPLE": SAMPLE, "A3_LS": "C:\\a\u2028b"})
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("SAMPLE=SAFE", proc.stdout, out)
        self.assertRegex(proc.stdout, r"LS=REFUSED is refused by env_pin_check\.py: .*U\+2028", out)
        self.assertRegex(proc.stdout, r"SUR=REFUSED is refused by env_pin_check\.py: .*surrogate", out)
        self.assertIn("LEFT=[]", proc.stdout, out)

    def test_wrapper_guard_refuses_a_non_ascii_python_under_ps51(self):
        with tempfile.TemporaryDirectory() as tmp:
            py = self._non_ascii_venv_python(tmp)
            body = (
                "$ErrorActionPreference = 'Stop'\n"
                "$HermesRoot = $null\n"
                + _cmd_wrapper_prelude(TEXT)
                + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
                "$WrapperPython = $env:A3_PY\n$ForgePy = 'C:\\x\\forge.py'\n$DecidersPy = 'C:\\x\\deciders.py'\n"
                "$CmdPaths = Assert-CmdWrapperPathsAscii\nWrite-Output 'WRAPPERS_OK'\n")
            # This test's whole point is "no usable prefix -> still
            # refused", not "got rewritten". The venv lives under TEMP,
            # which on real Windows usually resolves under %LOCALAPPDATA%,
            # itself under %USERPROFILE% -- those two are the only ones
            # genuinely at risk of accidentally prefix-matching it (APPDATA
            # is Roaming, never an ancestor of TEMP; neither is
            # ProgramFiles), so only they need neutralising. Point them at
            # a dummy, non-existent, un-nested path rather than blanking
            # them to "": an empty value is dropped from the Win32
            # environment block entirely, and clearing LOCALAPPDATA,
            # USERPROFILE, APPDATA and ProgramFiles to "" TOGETHER crashes
            # Windows PowerShell 5.1 itself at startup (a real,
            # host-reproduced System.Management.Automation.Utils
            # type-initializer exception -- see t_b8be23cc) before any
            # installer code runs. A non-empty, ASCII, un-nested dummy
            # value keeps PS 5.1 happy while still matching no prefix.
            no_match = "C:\\NoPrefixMatchXYZ"
            proc = self._ps51(tmp, body, {"A3_PY": str(py), "LOCALAPPDATA": no_match,
                                          "USERPROFILE": no_match})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("WRAPPERS_OK", proc.stdout)
        self.assertIn("not printable ASCII", proc.stderr)

    def test_wrapper_guard_rewrites_a_non_ascii_python_under_localappdata_and_it_runs(self):
        # D1-D9 end-to-end on the real target: a non-ASCII venv Python
        # placed INSIDE a controlled %LOCALAPPDATA% is rewritten to
        # "%LOCALAPPDATA%\\..." in forge.cmd, and cmd.exe -- which only
        # this Windows job can exercise -- actually expands that back to
        # the real, non-ASCII path and runs it.
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / NON_ASCII_DIR / "AppData" / "Local"
            local.mkdir(parents=True)
            venv_dir = local / "venv"
            venv.create(venv_dir, with_pip=False)
            py = venv_dir / "Scripts" / "python.exe"
            self.assertTrue(py.exists(), py)
            script = local / "hello.py"
            script.write_text("print('WRAPPER_RAN')\n")
            body = (
                "$ErrorActionPreference = 'Stop'\n"
                f"$HermesRoot = $null\n"
                + _cmd_wrapper_prelude(TEXT)
                + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
                "$WrapperPython = $env:A3_PY\n"
                f"$ForgePy = $env:A3_SCRIPT\n$DecidersPy = $env:A3_SCRIPT\n"
                "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
                "$WrapperPythonCmd = $CmdPaths.WrapperPython\n$ForgePyCmd = $CmdPaths.ForgePy\n"
                + _function(TEXT, "Get-CmdGuardBlock") + "\n"
                "$ForgeGuard = Get-CmdGuardBlock -Prefix 'dt_forge' -Pairs @("
                "[ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd }, "
                "[ordered]@{ Label = 'forge.py'; Embed = $ForgePyCmd })\n"
                "$forgeCmdContent = \"@echo off`r`nREM t`r`n$($ForgeGuard.Guards)`\"$($WrapperPythonCmd.Embed)`\" `\"$($ForgePyCmd.Embed)`\" %*`r`nexit /b %ERRORLEVEL%`r`n$($ForgeGuard.Tail)\"\n"
                f"Set-Content -LiteralPath {_ps_q(Path(tmp) / 'forge.cmd')} -Value $forgeCmdContent -NoNewline -Encoding ascii\n"
                "Write-Output \"EMBED=$($WrapperPythonCmd.Embed)\"\n")
            # Only LOCALAPPDATA is under test here. Leave USERPROFILE/
            # APPDATA/ProgramFiles at their real, non-empty, inherited
            # values: `local` is a deeper (longer) directory than any of
            # those real profile paths can be, so the longest-prefix rule
            # in Resolve-CmdEmbeddablePath always picks LOCALAPPDATA
            # regardless of what the others hold. Blanking the others to
            # "" gains nothing for this test and, together with
            # LOCALAPPDATA, is what crashes Windows PowerShell 5.1 at
            # startup (see t_b8be23cc) before the installer code it's
            # meant to exercise ever runs.
            proc = self._ps51(tmp, body, {"A3_PY": str(py), "A3_SCRIPT": str(script),
                                          "LOCALAPPDATA": str(local)})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("%LOCALAPPDATA%\\venv\\Scripts\\python.exe", proc.stdout)
            cmd_path = Path(tmp) / "forge.cmd"
            self.assertTrue(cmd_path.read_bytes().isascii(), "forge.cmd must stay pure ASCII")
            run = subprocess.run(["cmd.exe", "/d", "/c", str(cmd_path)], capture_output=True,
                                 text=True, errors="replace", timeout=120,
                                 env={**os.environ, "LOCALAPPDATA": str(local)})
        self.assertIn("WRAPPER_RAN", run.stdout, run.stdout + run.stderr)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_premise_the_old_ascii_wrapper_cannot_run_a_non_ascii_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            py = self._non_ascii_venv_python(tmp)
            script = Path(tmp) / "hello.py"
            script.write_text("print('WRAPPER_RAN')\n")
            cmd = Path(tmp) / "old.cmd"
            body = (
                f"$c = \"@echo off`r`n`\"$env:A3_PY`\" `\"{script}`\" %*`r`nexit /b %ERRORLEVEL%`r`n\"\n"
                f"Set-Content -LiteralPath {_ps_q(cmd)} -Value $c -NoNewline -Encoding ascii\n")
            proc = self._ps51(tmp, body, {"A3_PY": str(py)})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(b"?", cmd.read_bytes(), "-Encoding ascii kept the non-ASCII path?")
            run = subprocess.run(["cmd.exe", "/d", "/c", str(cmd)], capture_output=True,
                                 text=True, errors="replace", timeout=120)
        self.assertNotIn("WRAPPER_RAN", run.stdout)
        self.assertNotEqual(run.returncode, 0)

    def test_guard_echo_does_not_run_a_fragment_when_localappdata_has_ampersand(self):
        # S1 (review round 1): the old unquoted/expanded echo let cmd.exe
        # split the guard's error line on '&' and run the trailing
        # fragment as a command, even though the whole point of this
        # branch is to fail closed. LOCALAPPDATA's VALUE contains '&'
        # here; the fixed echo must print the literal, UNEXPANDED
        # "%LOCALAPPDATA%" token (doubled '%', quoted), so the value is
        # never substituted into the line at all.
        # N1 (round 7): this needs a path that is actually REWRITTEN, since
        # only a rewritten path gets a guard (and therefore an echo). An
        # all-ASCII path whose only metacharacters are the quote-safe ones
        # ('&', '^', '(', ')') is now embedded literally and unguarded --
        # that is N1's fix, verified against real cmd.exe by
        # test_R1_a_literal_ascii_path_with_ampersand_and_parens_runs_under_cmd
        # below -- so the '&' must sit in a LOCALAPPDATA value that ALSO
        # carries a non-ASCII character, which is what forces the rewrite.
        # The '&' then appears only in the EXPANDED value, which is exactly
        # the S1 hazard: the echo must never expand it.
        with tempfile.TemporaryDirectory() as tmp:
            local_value = str(Path(tmp) / f"R&D (x) {NON_ASCII_DIR}" / "AppData" / "Local")
            self.assertFalse(local_value.isascii(), "LOCALAPPDATA must be non-ASCII to be rewritten")
            sentinel_dir = Path(tmp) / "sentinel_bin"
            sentinel_dir.mkdir(parents=True)
            marker = Path(tmp) / "SENTINEL_RAN.txt"
            # A buggy unquoted split of "...R&D (x)\AppData\Local\..."
            # would try to run something starting with "D" as a command.
            # If cmd.exe ever executes it, a marker file appears.
            sentinel_cmd = sentinel_dir / "D.cmd"
            sentinel_cmd.write_text(f"@echo off\r\necho SENTINEL_RAN> \"{marker}\"\r\n")
            missing_py = str(Path(local_value) / "hermes" / "tools" / "python-3.12" / "python.exe")
            forge_cmd_path = Path(tmp) / "forge.cmd"
            body = (
                "$ErrorActionPreference = 'Stop'\n"
                "$HermesRoot = $null\n"
                + _cmd_wrapper_prelude(TEXT)
                + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
                "$WrapperPython = $env:A3_PY\n$ForgePy = 'C:\\x\\forge.py'\n$DecidersPy = 'C:\\x\\deciders.py'\n"
                "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
                "$WrapperPythonCmd = $CmdPaths.WrapperPython\n$ForgePyCmd = $CmdPaths.ForgePy\n"
                "$ForgeGuard = Get-CmdGuardBlock -Prefix 'dt_forge' -Pairs @("
                "[ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd })\n"
                "$forgeCmdContent = \"@echo off`r`nREM t`r`n$($ForgeGuard.Guards)\" +\n"
                "    \"`\"$($WrapperPythonCmd.Embed)`\" `\"C:\\\\x\\\\forge.py`\" %*`r`n\" +\n"
                "    \"exit /b %ERRORLEVEL%`r`n$($ForgeGuard.Tail)\"\n"
                f"Set-Content -LiteralPath {_ps_q(forge_cmd_path)} -Value $forgeCmdContent -NoNewline -Encoding ascii\n"
                "Write-Output \"EMBED=$($WrapperPythonCmd.Embed)\"\n")
            # Same reasoning as the LOCALAPPDATA-rewrite test above: only
            # LOCALAPPDATA is under test, `local_value` outranks any real
            # USERPROFILE/APPDATA/ProgramFiles value by length so they
            # never need neutralising, and blanking all four together is
            # what crashes Windows PowerShell 5.1 at startup.
            proc = self._ps51(tmp, body, {"A3_PY": missing_py, "LOCALAPPDATA": local_value})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("%LOCALAPPDATA%\\hermes\\tools\\python-3.12\\python.exe", proc.stdout)
            self.assertTrue(forge_cmd_path.read_bytes().isascii(), "forge.cmd must stay pure ASCII")
            env = dict(os.environ)
            env.update({"LOCALAPPDATA": local_value, "PATH": f"{sentinel_dir}{os.pathsep}{env.get('PATH', '')}"})
            run = subprocess.run(["cmd.exe", "/d", "/c", str(forge_cmd_path)], capture_output=True,
                                 text=True, errors="replace", timeout=120, env=env)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertIn("%LOCALAPPDATA%", run.stderr, run.stderr)
            self.assertFalse(marker.exists(), "the '&' fragment ran a sentinel command")

    def test_both_wrappers_run_an_ascii_python_and_propagate_its_exit_code(self):
        if not str(sys.executable).isascii():
            self.skipTest("this runner's own Python is on a non-ASCII path")
        lines = {v: re.search(r"^\$" + v + r" = .*$", TEXT, re.M).group(0)
                 for v in ("forgeCmdContent", "decidersCmdContent")}
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "stub.py"
            stub.write_text("import sys\nprint('STUB_ARGS=' + '|'.join(sys.argv[1:]))\nsys.exit(3)\n")
            # Round 2 (review): this test used to leave $WrapperPythonCmd,
            # $ForgePyCmd, $DecidersPyCmd and $ForgeGuard/$DecidersGuard
            # undefined before using them in the extracted $forgeCmdContent/
            # $decidersCmdContent lines -- $null.Embed is $null, so the
            # wrapper was built with an EMPTY embedded interpreter path
            # ("" "<script>" %*), silently. Compute them via the real
            # Assert-CmdWrapperPathsAscii/Get-CmdGuardBlock chain, exactly as
            # the installer does immediately before those two lines
            # (install-dev-team-windows.ps1, around $CmdPaths/$ForgeGuard/
            # $DecidersGuard), so the embed is never empty.
            body = ("$ErrorActionPreference = 'Stop'\n"
                    "$HermesRoot = $null\n"
                    + _cmd_wrapper_prelude(TEXT)
                    + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
                    f"$WrapperPython = {_ps_q(sys.executable)}\n"
                    f"$ForgePy = {_ps_q(stub)}\n$DecidersPy = {_ps_q(stub)}\n"
                    "$ForgeMarker = 'REM forge'\n$DecidersMarker = 'REM deciders'\n"
                    "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
                    "$WrapperPythonCmd = $CmdPaths.WrapperPython\n"
                    "$ForgePyCmd = $CmdPaths.ForgePy\n$DecidersPyCmd = $CmdPaths.DecidersPy\n"
                    "$ForgeGuard = Get-CmdGuardBlock -Prefix 'dt_forge' -Pairs @("
                    "[ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd }, "
                    "[ordered]@{ Label = 'forge.py'; Embed = $ForgePyCmd })\n"
                    "$DecidersGuard = Get-CmdGuardBlock -Prefix 'dt_deciders' -Pairs @("
                    "[ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd }, "
                    "[ordered]@{ Label = 'deciders.py'; Embed = $DecidersPyCmd })\n"
                    + lines["forgeCmdContent"] + "\n" + lines["decidersCmdContent"] + "\n"
                    f"Set-Content -LiteralPath {_ps_q(Path(tmp) / 'forge.cmd')} -Value $forgeCmdContent -NoNewline -Encoding ascii\n"
                    f"Set-Content -LiteralPath {_ps_q(Path(tmp) / 'deciders.cmd')} -Value $decidersCmdContent -NoNewline -Encoding ascii\n")
            proc = self._ps51(tmp, body)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            for name in ("forge.cmd", "deciders.cmd"):
                with self.subTest(wrapper=name):
                    run = subprocess.run(["cmd.exe", "/d", "/c", str(Path(tmp) / name), "a", "b c"],
                                         capture_output=True, text=True, errors="replace", timeout=120)
                    self.assertIn("STUB_ARGS=a|b c", run.stdout, run.stdout + run.stderr)
                    self.assertEqual(run.returncode, 3)

    def test_R1_a_literal_ascii_path_with_ampersand_and_parens_runs_under_cmd(self):
        # R1 (round-7, load-bearing): commit 8650469's message claims a
        # literal "R&D (x)" path "fail[ed] closed (rc=3)" on a Windows VM,
        # while the round-6 literal fallback and N1's fix both depend on
        # exactly that literal form WORKING. One of the two claims is
        # wrong, and only a runner with a real cmd.exe can say which.
        # Settle it: an all-ASCII path containing '&', '(' and ')' is
        # embedded LITERALLY (not rewritten -- that is N1), the wrapper
        # stays pure ASCII, and cmd.exe runs that interpreter and
        # propagates its exit code.
        line = re.search(r"^\$forgeCmdContent = .*$", TEXT, re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "R&D (x)"
            venv_dir = base / "venv"
            venv.create(venv_dir, with_pip=False)
            py = venv_dir / "Scripts" / "python.exe"
            self.assertTrue(py.exists(), py)
            self.assertTrue(str(py).isascii(), f"this runner's TEMP is not ASCII: {py}")
            stub = base / "stub.py"
            stub.write_text("import sys\nprint('STUB_ARGS=' + '|'.join(sys.argv[1:]))\nsys.exit(3)\n")
            forge_cmd_path = Path(tmp) / "forge.cmd"
            body = ("$ErrorActionPreference = 'Stop'\n"
                    "$HermesRoot = $null\n"
                    + _cmd_wrapper_prelude(TEXT)
                    + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
                    "$WrapperPython = $env:A3_PY\n"
                    "$ForgePy = $env:A3_SCRIPT\n$DecidersPy = $env:A3_SCRIPT\n"
                    "$ForgeMarker = 'REM forge'\n"
                    "$CmdPaths = Assert-CmdWrapperPathsAscii\n"
                    "$WrapperPythonCmd = $CmdPaths.WrapperPython\n"
                    "$ForgePyCmd = $CmdPaths.ForgePy\n"
                    "$ForgeGuard = Get-CmdGuardBlock -Prefix 'dt_forge' -Pairs @("
                    "[ordered]@{ Label = 'the wrapper Python'; Embed = $WrapperPythonCmd }, "
                    "[ordered]@{ Label = 'forge.py'; Embed = $ForgePyCmd })\n"
                    + line + "\n"
                    f"Set-Content -LiteralPath {_ps_q(forge_cmd_path)} -Value $forgeCmdContent -NoNewline -Encoding ascii\n"
                    "Write-Output \"EMBED=$($WrapperPythonCmd.Embed)\"\n"
                    "Write-Output \"REWRITTEN=$($WrapperPythonCmd.Rewritten)\"\n")
            proc = self._ps51(tmp, body, {"A3_PY": str(py), "A3_SCRIPT": str(stub)})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # N1: literal, not rewritten -- no %ENVVAR%, no guard lines.
            self.assertIn(f"EMBED={py}", proc.stdout, proc.stdout)
            self.assertIn("REWRITTEN=False", proc.stdout, proc.stdout)
            raw = forge_cmd_path.read_bytes()
            self.assertTrue(raw.isascii(), "forge.cmd must stay pure ASCII")
            self.assertNotIn(b"if not exist", raw, "a literal embed must gain no existence guard")
            # R1, settled: does cmd.exe actually run this literal path?
            run = subprocess.run(["cmd.exe", "/d", "/c", str(forge_cmd_path), "a", "b c"],
                                 capture_output=True, text=True, errors="replace", timeout=120)
        self.assertIn("STUB_ARGS=a|b c", run.stdout, run.stdout + run.stderr)
        self.assertEqual(run.returncode, 3, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
