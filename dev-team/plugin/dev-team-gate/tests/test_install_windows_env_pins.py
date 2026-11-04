"""A3 (PR #9 follow-ups): install-dev-team-windows.ps1 decides .env pin
safety through dev-team/scripts/env_pin_check.py, and refuses to write a
forge.cmd / deciders.cmd wrapper cmd.exe would misread.

Everything here runs the REAL installer code under pwsh: either a slice of
the script (the same "$PluginSrcMnemo = Join-Path $HermesRoot" .. "# Team
state" slice test_install_windows.py uses) or functions extracted from it.
The checker runs with the interpreter running this suite, standing in for
the installer's $WrapperPython.

Rows are review pr9-review.md section 2(b) (Windows rows plus the Unix-only
rows, which the Windows installer must refuse too) and a lone surrogate.
Values travel to pwsh through the environment (UTF-8 on Linux, so U+2028,
U+0085 etc. arrive intact), never through a PowerShell string literal: PS
treats U+2018/U+2019 as quote characters, and a literal can't carry a lone
surrogate. The lone surrogate is built inside PowerShell with [char]0xD800.

Windows-only evidence (cmd.exe, Windows PowerShell 5.1, a real UTF-16
environment block) lives in test_install_windows_nt.py, which the
install-windows-ps51 CI job runs.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_install_windows import DEV_TEAM, PS1, _function, _pin_prelude, _resolve_wrapper_python_prelude, CMD_WRAPPER_FUNCS  # noqa: E402

CHECKER = DEV_TEAM / "scripts" / "env_pin_check.py"
sys.path.insert(0, str(CHECKER.parent))
import env_pin_check as epc  # noqa: E402

PWSH = shutil.which("pwsh")
TEXT = PS1.read_text(encoding="utf-8")

def ps_q(s):
    """A PowerShell single-quoted literal -- ASCII-safe paths only."""
    return "'" + str(s).replace("'", "''") + "'"


def run_ps(script_path, env_extra=None, timeout=120):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MNEMOSYNE_", "DEV_TEAM_"))}
    env.update(env_extra or {})
    return subprocess.run([PWSH or "pwsh", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
                          capture_output=True, text=True, timeout=timeout, env=env)


def team_memory_harness(tmp, hermes_root, team_memory_expr="$env:TEAM_MEMORY_ARG",
                        tail="", python=None, roles=("qa",)):
    """The real setup slice, from $PluginSrcMnemo through the pin helpers,
    under $ErrorActionPreference = 'Stop' as in the real script, with
    -TeamMemory taken from a PowerShell expression."""
    start = TEXT.index("$PluginSrcMnemo = Join-Path $HermesRoot")
    end = TEXT.index("# Team state (forge project records")
    profiles = "; ".join(f"{ps_q(r)} = 'x'" for r in roles)
    harness = Path(tmp) / "a3_harness.ps1"
    harness.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"$HermesRoot = $env:HERMES_ROOT_ARG\n"
        f"$TeamMemory = {team_memory_expr}\n"
        "$Prefix = 'dev-'\n$Only = $null\n$DryRun = $false\n"
        f"$Profiles = [ordered]@{{ {profiles} }}\n"
        + _pin_prelude(TEXT, python)
        + _function(TEXT, "Test-Selected") + "\n"
        + _function(TEXT, "Get-NormalizedPath") + "\n"
        + TEXT[start:end]
        + "\nWrite-Output \"TEAM_HOME=$TeamMemoryHome\"\n" + tail,
        encoding="utf-8")
    return harness


# --- review section 2(b) rows: each must be refused (exit 2) -----------------
# (label, suffix appended to $T, refused by the checker only -- i.e. the
#  PowerShell subset alone accepted it before this change)
REFUSAL_ROWS = (
    ("U+2028 mid-path", "/a\u2028b", True),
    ("U+2029 mid-path", "/a\u2029b", True),
    ("U+0085 mid-path", "/a\x85b", True),
    ("U+001C mid-path", "/a\x1cb", True),
    ("U+000B mid-path", "/a\x0bb", True),    # .NET \s has \v, but only ends were checked
    ("NBSP before #", "/a\u00a0#b", False),  # .NET \s has \p{Z}
    ("NBSP at the end", "/bank\u00a0", False),
    ("U+001F at the end", "/bank\x1f", True),  # not in .NET \s
)


@unittest.skipUnless(PWSH, "pwsh not installed")
class TeamMemoryPinRefusalTests(unittest.TestCase):
    def _run(self, tmp, value_suffix):
        root = Path(tmp) / "hermes"
        root.mkdir(exist_ok=True)
        harness = team_memory_harness(tmp, root)
        return run_ps(harness, {"HERMES_ROOT_ARG": str(root),
                                "TEAM_MEMORY_ARG": str(Path(tmp)) + value_suffix})

    def test_review_2b_rows_are_refused_with_exit_2(self):
        for label, suffix, checker_only in REFUSAL_ROWS:
            with self.subTest(row=label), tempfile.TemporaryDirectory() as tmp:
                proc = self._run(tmp, suffix)
                self.assertEqual(proc.returncode, 2, f"{label}: {proc.stdout}{proc.stderr}")
                self.assertNotIn("TEAM_HOME=", proc.stdout)
                self.assertIn("-TeamMemory", proc.stderr)
                if checker_only:
                    self.assertIn("env_pin_check.py", proc.stderr,
                                  f"{label}: not refused by the checker: {proc.stderr}")
                # the refusal names the value without raw control characters
                self.assertFalse(re.search("[\x00-\x08\x0b-\x1f\x85\u2028\u2029]", proc.stderr),
                                 f"{label}: raw control character on stderr")
                # nothing created
                self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                                 ["a3_harness.ps1", "hermes"])

    def test_lone_surrogate_is_refused_with_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            harness = team_memory_harness(
                tmp, root, team_memory_expr="$env:TEAM_MEMORY_ARG + [char]0xD800 + 'b'")
            proc = run_ps(harness, {"HERMES_ROOT_ARG": str(root),
                                    "TEAM_MEMORY_ARG": str(Path(tmp) / "a")})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("TEAM_HOME=", proc.stdout)
        self.assertIn("surrogate", proc.stderr)
        self.assertIn("\\uD800", proc.stderr)  # shown escaped, never raw

    def test_unsafe_hermes_root_refuses_profile_local_pins_up_front(self):
        """-TeamMemory is fine, but every profile-local pin derives from
        $HermesRoot\\profiles, whose U+2028 only the checker sees."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "h\u2028r" / "hermes"
            root.mkdir(parents=True)
            harness = team_memory_harness(tmp, root)
            proc = run_ps(harness, {"HERMES_ROOT_ARG": str(root),
                                    "TEAM_MEMORY_ARG": str(Path(tmp) / "bank")})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("env_pin_check.py", proc.stderr)
        self.assertIn("dev-qa", proc.stderr)

    def test_link_whose_target_is_unsafe_is_refused(self):
        """review 2(b) `lnk -> $T/z\\u2028y`: the RESOLVED value is checked."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "z\u2028y"
            target.mkdir()
            (Path(tmp) / "lnk").symlink_to(target)
            root = Path(tmp) / "hermes"
            root.mkdir()
            harness = team_memory_harness(tmp, root)
            proc = run_ps(harness, {"HERMES_ROOT_ARG": str(root),
                                    "TEAM_MEMORY_ARG": str(Path(tmp) / "lnk" / "bank")})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("env_pin_check.py", proc.stderr)


@unittest.skipUnless(PWSH, "pwsh not installed")
class NonAsciiPinAcceptanceTests(unittest.TestCase):
    SAMPLE = "Dr\u0103go\u0219 \u0105\u00c5\u0445 \u56e2\u961f\u30d0\u30f3\u30af"

    def test_non_ascii_bank_is_accepted_and_reads_back_byte_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "hermes"
            root.mkdir()
            bank = str(Path(tmp) / self.SAMPLE / "bank")
            prof = Path(tmp) / "prof"
            tail = (f"Set-MnemosyneDataDirPin -ProfileDir {ps_q(prof)} "
                    "-DataDir (Join-Path $TeamMemoryHome 'data')\nWrite-Output 'PIN_DONE'\n")
            harness = team_memory_harness(tmp, root, tail=tail)
            proc = run_ps(harness, {"HERMES_ROOT_ARG": str(root), "TEAM_MEMORY_ARG": bank})
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"TEAM_HOME={bank}", proc.stdout)
            self.assertIn("PIN_DONE", proc.stdout)
            raw = (prof / ".env").read_bytes()
            want = f"MNEMOSYNE_DATA_DIR={bank}/data\nMNEMOSYNE_HOME={bank}\n".encode("utf-8")
            self.assertEqual(raw, want)
            expected = {"MNEMOSYNE_DATA_DIR": bank + "/data", "MNEMOSYNE_HOME": bank}
            # Both readers, as the checker transcribes them (A1 diffs those
            # transcriptions against the real readers when installed).
            results, _ = epc.hermes_read(raw, "\n")
            for got in results:
                self.assertEqual(got, expected)
            self.assertEqual(epc.mnemosyne_read(raw), expected)
            # The real python-dotenv too, where this interpreter has it.
            try:
                from dotenv import dotenv_values  # noqa: PLC0415
            except ImportError:
                dotenv_values = None
            if dotenv_values is not None:
                self.assertEqual(dict(dotenv_values(prof / ".env", interpolate=False)), expected)


@unittest.skipUnless(PWSH, "pwsh not installed")
class WriteSiteBackstopTests(unittest.TestCase):
    """The checker runs again on the exact value at the write site, so no
    call site -- present or future -- can pin an unsafe value."""

    def _harness(self, tmp, body, dry_run=False, python=None):
        h = Path(tmp) / "write_site.ps1"
        h.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$DryRun = ${'true' if dry_run else 'false'}\n"
            + _pin_prelude(TEXT, python)
            + _function(TEXT, "Set-EnvVarPin") + "\n"
            + _function(TEXT, "Set-MnemosyneDataDirPin") + "\n" + body
            + "\nWrite-Output 'WRITE_DONE'\n", encoding="utf-8")
        return h

    def test_set_env_var_pin_refuses_a_checker_only_value_and_leaves_env_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "prof"
            prof.mkdir()
            (prof / ".env").write_bytes(b"OTHER=1\n")
            h = self._harness(tmp, f"Set-EnvVarPin -ProfileDir {ps_q(prof)} "
                                   "-VarName 'MNEMOSYNE_HOME' -Value $env:PIN_ARG")
            proc = run_ps(h, {"PIN_ARG": str(Path(tmp) / "a\u2028b")})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertNotIn("WRITE_DONE", proc.stdout)
            self.assertIn("MNEMOSYNE_HOME", proc.stderr)
            self.assertIn("env_pin_check.py", proc.stderr)
            self.assertEqual((prof / ".env").read_bytes(), b"OTHER=1\n")

    def test_dry_run_refuses_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "prof"
            h = self._harness(tmp, f"Set-MnemosyneDataDirPin -ProfileDir {ps_q(prof)} "
                                   "-DataDir $env:PIN_ARG", dry_run=True)
            proc = run_ps(h, {"PIN_ARG": str(Path(tmp) / "a\x85b" / "data")})
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertNotIn("would pin", proc.stdout)
            self.assertFalse(prof.exists())

    def test_checker_failure_fails_closed(self):
        """No Python, a Python that errors (2), says 1 without a reason, or
        exits anything else: every one is exit 2, nothing written. The env
        variable carrying the value is cleared afterwards."""
        with tempfile.TemporaryDirectory() as tmp:
            fakes = {"missing": str(Path(tmp) / "no-such-python")}
            for name, script in (("rc2", "echo boom >&2; exit 2"),
                                 ("rc1_silent", "exit 1"),
                                 ("rc70", "exit 70")):
                f = Path(tmp) / f"py_{name}"
                f.write_text(f"#!/bin/sh\n{script}\n")
                f.chmod(0o755)
                fakes[name] = str(f)
            for name, py in fakes.items():
                with self.subTest(python=name):
                    prof = Path(tmp) / f"prof_{name}"
                    h = self._harness(
                        tmp, f"try {{ Set-EnvVarPin -ProfileDir {ps_q(prof)} -VarName 'MNEMOSYNE_HOME' "
                             f"-Value {ps_q(Path(tmp) / 'bank')} }} finally {{ "
                             "Write-Output \"LEFT=[$env:DEV_TEAM_ENV_CHECK_VALUE]\" }",
                        python=py)
                    proc = run_ps(h)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertNotIn("WRITE_DONE", proc.stdout)
                    self.assertIn("could not be checked", proc.stderr)
                    self.assertFalse((prof / ".env").exists())

    def test_value_env_var_is_cleared_after_a_passing_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = Path(tmp) / "prof"
            h = self._harness(tmp, f"Set-EnvVarPin -ProfileDir {ps_q(prof)} -VarName 'MNEMOSYNE_HOME' "
                                   f"-Value {ps_q(Path(tmp) / 'bank')}\n"
                                   "Write-Output \"LEFT=[$env:DEV_TEAM_ENV_CHECK_VALUE]\"")
            proc = run_ps(h)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("LEFT=[]", proc.stdout)
            self.assertIn("WRITE_DONE", proc.stdout)


class CheckerIsAuthoritativeStaticTests(unittest.TestCase):
    def test_checker_is_called_with_the_value_only_via_the_environment(self):
        body = _function(TEXT, "Get-EnvPinCheckerReason")
        self.assertIn("$env:DEV_TEAM_ENV_CHECK_VALUE = $Value", body)
        self.assertIn("Remove-Item -LiteralPath Env:DEV_TEAM_ENV_CHECK_VALUE", body)
        call = [ln for ln in body.splitlines() if "& $WrapperPython" in ln]
        self.assertEqual(len(call), 1, "exactly one checker invocation")
        self.assertNotIn("$Value", call[0], "the value must never be on argv")

    def test_set_env_var_pin_runs_the_checker(self):
        self.assertIn("Assert-EnvPinSafe", _function(TEXT, "Set-EnvVarPin"))
        self.assertIn("Assert-EnvPinSafe", _function(TEXT, "Set-MnemosyneDataDirPin"))

    def test_checker_python_is_resolved_before_any_pin_logic(self):
        resolved = TEXT.index("$WrapperPython = Resolve-WrapperPython")
        self.assertLess(resolved, TEXT.index("$PluginSrcMnemo = Join-Path $HermesRoot"))
        self.assertIn("$EnvPinCheckPy", TEXT[:resolved])

    def test_comments_describe_both_readers_correctly(self):
        """review 2(d): python-dotenv DOES strip surrounding whitespace, and
        `.strip("'\\"")` strips any number of quote characters."""
        body = _function(TEXT, "Get-EnvUnsafeValueReason")
        self.assertNotIn("python-dotenv's parser does not", body)
        self.assertNotIn("exactly one bounding pair", body)
        self.assertIn("_equal_sign", body)
        self.assertIn("rstrip()", body)
        self.assertIn("any number", body)


@unittest.skipUnless(PWSH, "pwsh not installed")
class CmdWrapperRefusalTests(unittest.TestCase):
    """D1-D9 (review follow-up, pr10-review.md section 4): a non-ASCII path
    under a known folder (LOCALAPPDATA/APPDATA/USERPROFILE/ProgramFiles/a
    persisted HERMES_ROOT) is rewritten to an %ENVVAR% form instead of
    refused; only a non-ASCII path under none of those is still refused
    (exit 2, before anything is written), per the comment above
    Assert-CmdWrapperPathsAscii in the installer."""

    _EXTRA_FUNCS = CMD_WRAPPER_FUNCS

    def _run(self, tmp, python_path, forge_py="/ascii/forge/forge.py",
             deciders_py="/ascii/deciders/deciders.py", env_extra=None):
        h = Path(tmp) / "wrapper.ps1"
        h.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"$HermesRoot = '{tmp}/hermes'\n"
            + _function(TEXT, "Format-EnvPinForDisplay") + "\n"
            + "".join(_function(TEXT, f) + "\n" for f in self._EXTRA_FUNCS)
            + _function(TEXT, "Assert-CmdWrapperPathsAscii") + "\n"
            + "$WrapperPython = $env:PY_ARG\n"
            + f"$ForgePy = $env:FORGE_ARG\n$DecidersPy = $env:DECIDERS_ARG\n"
            + "$CmdPaths = Assert-CmdWrapperPathsAscii\nWrite-Output 'WRAPPERS_OK'\n", encoding="utf-8")
        env = {"PY_ARG": python_path, "FORGE_ARG": forge_py, "DECIDERS_ARG": deciders_py}
        env.update(env_extra or {})
        return run_ps(h, env)

    def test_non_ascii_python_with_no_usable_prefix_is_refused_with_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No LOCALAPPDATA/USERPROFILE/etc set to cover this path, so
            # there is no %ENVVAR% form to rewrite it to.
            proc = self._run(tmp, "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local\\hermes\\tools\\python-3.12\\python.exe",
                             env_extra={"LOCALAPPDATA": "", "USERPROFILE": "", "APPDATA": "", "ProgramFiles": ""})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("WRAPPERS_OK", proc.stdout)
        self.assertIn("ASCII", proc.stderr)
        self.assertIn("python", proc.stderr)
        self.assertIn("Dr\\u0103go\\u0219", proc.stderr)

    def test_non_ascii_python_under_localappdata_is_rewritten_not_refused(self):
        local = "C:\\Users\\Dr\u0103go\u0219\\AppData\\Local"
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, local + "\\hermes\\tools\\python-3.12\\python.exe",
                             env_extra={"LOCALAPPDATA": local, "USERPROFILE": "", "APPDATA": "", "ProgramFiles": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WRAPPERS_OK", proc.stdout)

    def test_non_ascii_script_path_with_no_usable_prefix_is_refused_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, "C:\\Python312\\python.exe",
                             forge_py="C:\\Users\\\u0425\\dev-team\\forge\\forge.py",
                             env_extra={"LOCALAPPDATA": "", "USERPROFILE": "", "APPDATA": "", "ProgramFiles": ""})
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("forge.py", proc.stderr)

    def test_ascii_paths_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(tmp, "C:\\Python312\\python.exe",
                             env_extra={"LOCALAPPDATA": "", "USERPROFILE": "", "APPDATA": "", "ProgramFiles": ""})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WRAPPERS_OK", proc.stdout)


@unittest.skipUnless(PWSH, "pwsh not installed")
class WrapperPythonFirstCandidateWinsTests(unittest.TestCase):
    """D6 (review follow-up, pr10-review.md section 4): the FIRST EXISTING
    candidate wins, full stop -- no preference for an ASCII-representable
    path over a non-ASCII one (that preference, and its '$first'
    bookkeeping, were deleted). Whether the winning path can be embedded in
    forge.cmd/deciders.cmd is decided afterwards, separately, by
    Assert-CmdWrapperPathsAscii (which can now rewrite it via an %ENVVAR%
    prefix instead of refusing outright)."""

    def _layout(self, tmp, ascii_python_on_path):
        t = Path(tmp)
        local = t / "Dr\u0103go\u0219"
        # Join-Path turns 'hermes\tools' into hermes/tools under Linux pwsh.
        hermes_py_dir = local / "hermes" / "tools" / "python-3.12"
        hermes_py_dir.mkdir(parents=True)
        (hermes_py_dir / "python.exe").write_text("")
        bindir = t / ("asciibin" if ascii_python_on_path else "\u0445bin")
        bindir.mkdir()
        py = bindir / "python"
        py.write_text("#!/bin/sh\nexit 0\n")
        py.chmod(0o755)
        h = t / "resolve.ps1"
        h.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            + _resolve_wrapper_python_prelude(TEXT) + "\n"
            + "Write-Output (\"PY=\" + (Resolve-WrapperPython))\n", encoding="utf-8")
        env = {"LOCALAPPDATA": str(local), "PATH": str(bindir)}
        return h, env, hermes_py_dir / "python.exe", py

    def test_hermes_tools_python_wins_even_when_non_ascii_and_an_ascii_python_is_on_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            h, env, hermes_py, _path_py = self._layout(tmp, ascii_python_on_path=True)
            proc = run_ps(h, env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"PY={hermes_py}", proc.stdout)

    def test_only_non_ascii_candidates_still_resolves_the_hermes_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            h, env, hermes_py, _path_py = self._layout(tmp, ascii_python_on_path=False)
            proc = run_ps(h, env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"PY={hermes_py}", proc.stdout)


class CmdWrapperStaticTests(unittest.TestCase):
    def test_refusal_runs_before_any_profile_is_touched(self):
        call = TEXT.index("\n$CmdPaths = Assert-CmdWrapperPathsAscii\n")
        first_loop = TEXT.index("\nforeach ($role in $Profiles.Keys) {\n    if (-not (Test-Selected")
        self.assertLess(call, first_loop)
        self.assertLess(call, TEXT.index("$PluginSrcMnemo = Join-Path $HermesRoot"))

    def test_wrappers_are_still_written_as_ascii_after_the_guard(self):
        # -Encoding ascii is now safe by construction (the guard refused
        # every non-ASCII path); it stays so a future change that drops the
        # guard turns into '?' visibly in review rather than a UTF-8 file
        # cmd.exe would decode in the console code page.
        for var in ("forgeCmdContent", "decidersCmdContent"):
            self.assertRegex(TEXT, r"-Value \$" + var + r" -NoNewline -Encoding ascii")

    def test_windows_ci_runs_the_nt_only_evidence_under_ps51(self):
        wf = (DEV_TEAM.parent / ".github" / "workflows" / "dev-team.yml").read_text(encoding="utf-8")
        job = wf[wf.index("\n  install-windows-ps51:\n"):]
        step = job[job.index("Windows-only env-pin and cmd-wrapper evidence (A3)"):]
        self.assertIn("shell: powershell", step.split("\n      - ")[0])
        self.assertIn("-p test_install_windows_nt.py", step)
        self.assertTrue((HERE / "test_install_windows_nt.py").is_file())


if __name__ == "__main__":
    unittest.main()
