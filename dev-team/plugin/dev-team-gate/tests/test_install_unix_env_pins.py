"""install-dev-team-unix.sh: .env pin safety is decided by
dev-team/scripts/env_pin_check.py on the FINAL, resolved pin value, in every
locale (PR #9 review, pr9-review.md section 2(b), section 3, section 4).

Every scenario runs four times, under four environments, and must get the
same verdict each time:
  * "default"     -- this test process's own environment (minus anything
                     Hermes/Mnemosyne/dev-team specific, which could point
                     the installer at a live profile or bank), plus the
                     harness variables;
  * "env -i"      -- `/usr/bin/env -i` with only HOME, PATH, TMPDIR and the
                     fake-hermes harness variables (no LANG/LC_*);
  * "LC_ALL=C"    -- harness environment plus LC_ALL=C;
  * "LC_ALL=C.UTF-8".
Refusal means: exit 2, no profile created, no bank directory created at the
path the resolver would have used (or at the decoy it would have truncated
to), and the operator bank byte-identical.
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from test_install_unix import INSTALLER, InstallerSetupMixin, _read_env_value_via_mnemosyne_reader

_SCRUB_PREFIXES = ("HERMES", "MNEMOSYNE", "DEV_TEAM", "LC_", "LANG", "PYTHON")
VARIANTS = ("default", "env -i", "LC_ALL=C", "LC_ALL=C.UTF-8")


@unittest.skipUnless(shutil.which("python3"), "the installer's pin check needs python3 on PATH")
class EnvPinSafetyTests(InstallerSetupMixin, unittest.TestCase):

    def fresh(self):
        """A brand-new sandbox (fake Hermes root, HOME, fake CLI) for one
        variant run, so no run can see another's leftovers."""
        InstallerSetupMixin.setUp(self)
        self.T = self.tmp / "t"
        self.T.mkdir()
        return self.T

    def run_variant(self, variant, args, cwd=None):
        harness = {k: v for k, v in self.env.items() if k != "LANG"}
        harness["TMPDIR"] = str(self.tmp)
        argv = [b"bash", os.fsencode(str(INSTALLER)), b"--skip-model",
                *[a if isinstance(a, bytes) else os.fsencode(a) for a in args]]
        if variant == "env -i":
            argv = [b"/usr/bin/env", b"-i",
                    *[f"{k}={v}".encode() for k, v in sorted(harness.items())], *argv]
            env = {}
        elif variant == "default":
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(_SCRUB_PREFIXES[:3]) and k != "TMPDIR"}
            env.update(harness)
        else:
            env = {**harness, "LC_ALL": variant.split("=", 1)[1]}
        return subprocess.run(argv, env=env, cwd=cwd, capture_output=True, timeout=120)

    def assert_refused_everywhere(self, build, must_not_exist=()):
        """build(T) creates fixtures under T and returns (args, cwd).
        must_not_exist: callables T -> path that a wrong resolution would
        have created or linked."""
        verdicts = {}
        for variant in VARIANTS:
            T = self.fresh()
            args, cwd = build(T)
            before = self.snapshot_tree(self.root / "mnemosyne")
            proc = self.run_variant(variant, args, cwd=cwd)
            err = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
            with self.subTest(variant=variant):
                self.assertEqual(proc.returncode, 2, err)
                self.assertFalse((self.root / "profiles").exists(), err)
                self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before)
                for p in must_not_exist:
                    self.assertFalse(os.path.lexists(os.fsencode(str(p(T)))), f"{p(T)!r} was created\n{err}")
            verdicts[variant] = proc.returncode
        self.assertEqual(len(set(verdicts.values())), 1, verdicts)
        return verdicts

    # --- review section 2(b), Unix rows: refused by the checker, not by bash
    def _bank_case(self, suffix):
        def build(T):
            return ["--team-memory", str(T) + suffix, "--only", "qa"], None
        verdicts = self.assert_refused_everywhere(build, [lambda T: str(T) + suffix])
        return verdicts

    def test_u2028_line_separator_is_refused(self):
        self._bank_case("/a\u2028b")

    def test_x1c_file_separator_is_refused(self):
        self._bank_case("/a\x1cb")

    def test_x0b_vertical_tab_is_refused(self):
        self._bank_case("/a\x0bb")

    def test_nbsp_then_hash_is_refused(self):
        self._bank_case("/a\u00a0#b")

    def test_trailing_nbsp_is_refused(self):
        self._bank_case("/bank\u00a0")

    def test_trailing_x1f_is_refused(self):
        self._bank_case("/bank\x1f")

    def test_resolved_symlink_target_with_u2028_is_refused(self):
        def build(T):
            (T / "z\u2028y").mkdir()
            (T / "lnk").symlink_to(T / "z\u2028y")
            return ["--team-memory", str(T / "lnk"), "--only", "qa"], None
        self.assert_refused_everywhere(build)

    def test_non_utf8_byte_path_is_refused(self):
        def build(T):
            return [b"--team-memory", os.fsencode(str(T)) + b"/bank\xff", b"--only", b"qa"], None
        self.assert_refused_everywhere(build, [lambda T: os.fsdecode(os.fsencode(str(T)) + b"/bank\xff")])

    # --- review section 3: newline guard after the $PWD join
    def test_relative_team_memory_from_a_cwd_with_a_newline_is_refused(self):
        def build(T):
            other = T / "other-team-bank"
            (other / "data").mkdir(parents=True)
            (other / "data" / "mnemosyne.db").write_text("someone else's")
            cwd = T / "other-team-bank\n" / "w"
            cwd.mkdir(parents=True)
            return ["--team-memory", "bank", "--only", "qa"], cwd
        self.assert_refused_everywhere(build, [lambda T: T / "other-team-bank" / "bank",
                                               lambda T: T / "other-team-bank\n" / "w" / "bank"])

    # --- known issue from PR #9: readlink's $(...) ate a trailing newline
    def test_symlink_target_with_a_trailing_newline_is_refused(self):
        def build(T):
            (T / "decoy").mkdir()
            (T / "lnk").symlink_to(str(T) + "/decoy\n")
            return ["--team-memory", str(T / "lnk" / "bank"), "--only", "qa"], None
        self.assert_refused_everywhere(build, [lambda T: T / "decoy" / "bank",
                                               lambda T: str(T) + "/decoy\n"])

    # --- fail closed: a checker that errors (or answers 1 with no reason)
    # must refuse exactly like an unsafe value, before anything is written
    def test_checker_internal_error_or_silent_refusal_fails_closed(self):
        for rc in (2, 1, 70):
            with self.subTest(checker_exit=rc):
                T = self.fresh()
                shim = self.tmp / "bin" / "python3"
                shim.write_text(f"#!/bin/sh\nexit {rc}\n")
                shim.chmod(0o755)
                proc = self.run_variant("LC_ALL=C.UTF-8", ["--team-memory", str(T / "bank"), "--only", "qa"])
                err = proc.stderr.decode(errors="replace")
                self.assertEqual(proc.returncode, 2, err)
                self.assertFalse((self.root / "profiles").exists(), err)
                self.assertFalse((T / "bank").exists(), err)
                self.assertTrue("checker failed" in err or "no reason given" in err, err)

    # --- ordinary non-ASCII must still be accepted, byte-exact
    def test_ordinary_non_ascii_bank_is_accepted_and_pins_byte_exact(self):
        name = "Drăgoș ąÅх 团队バンク"
        for variant in VARIANTS:
            T = self.fresh()
            bank = T / name / "bank"
            proc = self.run_variant(variant, ["--team-memory", str(bank), "--only", "qa"])
            err = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
            with self.subTest(variant=variant):
                self.assertEqual(proc.returncode, 0, err)
                prof = self.profile("qa")
                self.assertEqual(os.readlink(prof / "mnemosyne"), str(bank))
                lines = (prof / ".env").read_bytes().split(b"\n")
                self.assertIn(b"MNEMOSYNE_HOME=" + os.fsencode(str(bank)), lines)
                self.assertIn(b"MNEMOSYNE_DATA_DIR=" + os.fsencode(str(bank / "data")), lines)
                value, why = _read_env_value_via_mnemosyne_reader(prof / ".env", "MNEMOSYNE_HOME")
                if why is None:  # real Mnemosyne reader installed on this host
                    self.assertEqual(value, str(bank))


if __name__ == "__main__":
    unittest.main()
