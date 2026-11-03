"""Behaviour of install-dev-team-unix.sh against a fake `hermes` CLI.

Brief items 9-11 and review #2 (Minor): the prefix file, the shared Mnemosyne
link on profiles this run creates, the first-run plugin-list race, and never
overwriting a `forge` the installer did not write. The fake records nothing
outside a temporary HOME.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
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
INSTALLER = DEV_TEAM / "install-dev-team-unix.sh"
ROLES = ("pm", "architect", "uiux", "backend-db", "frontend", "qa", "reviewer", "devops")


def _quote_env(value):
    """Mirror install-dev-team-unix.sh's quote_env_value(): python-dotenv's
    single-quote escapes (\\ -> \\\\, ' -> \\'), wrapped in single quotes.
    Item 4a (review r3)."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

# A stand-in for the parts of the hermes CLI the installer calls. A fresh
# `profile create --clone` makes a real mnemosyne/ directory, as Hermes does.
# `plugins list --enabled` lags LAG calls behind `plugins enable`, like the
# gateway's plugin reload.
FAKE_HERMES = textwrap.dedent(r"""
    #!/usr/bin/env bash
    set -u
    root="$FAKE_HERMES_ROOT"
    profile=""
    if [[ "${1:-}" == "-p" ]]; then profile="$2"; shift 2; fi
    # Mirrors the real CLI: with no -p, "config path" reflects whichever
    # profile HERMES_HOME currently points the process at (set by the
    # gateway when a profile is active) -- an explicit -p always wins.
    dir="$root"
    if [[ -n "${HERMES_HOME:-}" ]]; then dir="$HERMES_HOME"; fi
    [[ -n $profile ]] && dir="$root/profiles/$profile"
    echo "$profile $*" >> "$root/calls.log"
    case "$1 ${2:-}" in
      "config path") echo "$dir/config.yaml" ;;
      "config get")
        case "$3" in
          memory.provider) echo mnemosyne ;;
          *) echo False ;;
        esac ;;
      "config set") ;;
      "profile show") [[ -d "$root/profiles/$3" ]] ;;
      "profile create")
        mkdir -p "$root/profiles/$3/mnemosyne/data"
        echo clone > "$root/profiles/$3/mnemosyne/data/mnemosyne.db"
        touch "$root/profiles/$3/config.yaml" ;;
      "tools enable") ;;
      "plugins enable") touch "$dir/.gate-enabled" ;;
      "plugins doctor") ;;
      "plugins list")
        n=$(cat "$dir/.list-calls" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$dir/.list-calls"
        if [[ -f "$dir/.gate-enabled" && $n -gt ${FAKE_PLUGIN_LIST_LAG:-0} ]]; then
          echo dev-team-gate
        fi ;;
      "memory status") echo "Status: available" ;;
      *) echo "fake hermes: unhandled: $profile $*" >&2; exit 3 ;;
    esac
""").lstrip()


@unittest.skipUnless(shutil.which("bash") and os.name == "posix", "needs bash")
class InstallerSetupMixin:
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.root = self.tmp / "hermes"
        (self.root / "plugins" / "mnemosyne").mkdir(parents=True)
        (self.root / "mnemosyne" / "data").mkdir(parents=True)
        (self.root / "mnemosyne" / "data" / "mnemosyne.db").write_text("shared")
        # review finding #5: more than one file in the tree, so a
        # whole-tree snapshot comparison is actually exercised.
        (self.root / "mnemosyne" / "data" / "mnemosyne.db-wal").write_text("wal-bytes")
        (self.root / "config.yaml").touch()
        self.home.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        fake = bindir / "hermes"
        fake.write_text(FAKE_HERMES)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "HOME": str(self.home), "PATH": f"{bindir}:/usr/bin:/bin",
            "FAKE_HERMES_ROOT": str(self.root), "LANG": "C.UTF-8",
            "DEV_TEAM_VERIFY_RETRIES": "5", "DEV_TEAM_VERIFY_DELAY": "0",
        }

    def install(self, *args, **env):
        return subprocess.run(["bash", str(INSTALLER), "--skip-model", *args],
                              env={**self.env, **env}, capture_output=True, text=True,
                              timeout=120)

    def profile(self, role, prefix="dev-"):
        return self.root / "profiles" / f"{prefix}{role}"

    def snapshot_tree(self, root):
        """Every entry under root, review finding #5: a 'byte-identical'
        claim must cover the whole tree, not one file's bytes plus its
        mtime. Records symlinks (by target, never followed) and
        directories as well as regular files (review r3 R3-3: the original
        F2 bug's self-link $R/plugins/mnemosyne/mnemosyne is a symlink to a
        directory, which a files-only snapshot never sees)."""
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
                elif p.is_file():
                    snap[rel] = ("file", hashlib.sha256(p.read_bytes()).hexdigest(),
                                 p.stat().st_mtime_ns)
                else:
                    snap[rel] = ("other",)
        return snap


class UnixInstallerTests(InstallerSetupMixin, unittest.TestCase):
    def test_fresh_profiles_link_the_team_bank_not_the_operator_bank(self):  # Task 2
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        team_home = self.root / "dev-team-mnemosyne"
        for role in ROLES:
            link = self.profile(role) / "mnemosyne"
            with self.subTest(role=role):
                self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
                self.assertEqual(link.resolve(), team_home.resolve())
                # Never the operator's own bank.
                self.assertNotEqual(link.resolve(), (self.root / "mnemosyne").resolve())
        self.assertTrue(team_home.is_dir())

    def test_operator_bank_is_never_touched(self):  # Task 2, acceptance #2
        before = self.snapshot_tree(self.root / "mnemosyne")  # whole tree (finding #5)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before)
        for role in ROLES:
            link = self.profile(role) / "mnemosyne"
            self.assertNotEqual(link.resolve(), (self.root / "mnemosyne").resolve())

    def test_mnemosyne_home_env_var_does_not_select_the_team_bank(self):  # Task 2, acc #1
        decoy = self.tmp / "decoy-mnemosyne-home"
        decoy.mkdir()
        (decoy / "data").mkdir()
        (decoy / "data" / "mnemosyne.db").write_text("operator-relocated-bank")  # non-empty (finding #5)
        before_operator = self.snapshot_tree(self.root / "mnemosyne")
        before_decoy = self.snapshot_tree(decoy)
        proc = self.install(MNEMOSYNE_HOME=str(decoy))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot_tree(decoy), before_decoy, "MNEMOSYNE_HOME must not be written to")
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before_operator)
        link = self.profile("pm") / "mnemosyne"
        self.assertEqual(link.resolve(), (self.root / "dev-team-mnemosyne").resolve())

    def test_team_memory_flag_refuses_mnemosyne_home(self):  # Task 2 r2, finding #1
        decoy = self.tmp / "decoy-mnemosyne-home"
        decoy.mkdir()
        proc = self.install("--team-memory", str(decoy), MNEMOSYNE_HOME=str(decoy))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("MNEMOSYNE_HOME", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())
        self.assertFalse(any(decoy.iterdir()))

    def test_team_memory_flag_refuses_a_path_nested_in_the_operator_bank(self):  # finding #2
        nested = self.root / "mnemosyne" / "team"
        before = self.snapshot_tree(self.root / "mnemosyne")
        proc = self.install("--team-memory", str(nested))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)
        self.assertFalse(nested.exists(), "the operator bank must not gain a new subdirectory")
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before)
        self.assertFalse((self.root / "profiles").exists())

    def test_team_memory_flag_refuses_a_path_containing_the_operator_bank(self):  # finding #2
        # --team-memory the Hermes root itself: the operator bank
        # ($HERMES_ROOT/mnemosyne) sits INSIDE the requested team home, the
        # reverse direction from the nested-path probe above.
        proc = self.install("--team-memory", str(self.root))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_team_memory_flag_is_made_absolute_before_use(self):  # finding #3
        # A relative --team-memory used to be taken verbatim: `ln -sfn
        # relbank` made a dangling relative symlink in every profile, and
        # the bank was created relative to the installer's cwd.
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        proc = subprocess.run(["bash", str(INSTALLER), "--skip-model", "--team-memory", "relbank"],
                              cwd=str(cwd), env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        link = self.profile("pm") / "mnemosyne"
        self.assertTrue(link.is_symlink())
        target = os.readlink(link)
        self.assertTrue(os.path.isabs(target), f"link target {target!r} is not absolute")
        self.assertEqual(Path(target).resolve(), (cwd / "relbank").resolve())
        self.assertTrue((cwd / "relbank").is_dir())

    # --- r3 review finding #1: '..' / '.' / '//' in a not-yet-existing suffix
    def assert_refused_untouched(self, proc, needle="operator's own bank"):
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(needle, proc.stderr)
        self.assertFalse((self.root / "profiles").exists(), "no profile may be created")

    def test_team_memory_flag_refuses_dotdot_after_a_missing_component(self):  # r3 #1 (B3)
        before = self.snapshot_tree(self.root / "mnemosyne")
        proc = self.install("--team-memory", f"{self.root}/nonexist/../mnemosyne")
        self.assert_refused_untouched(proc)
        self.assertFalse((self.root / "nonexist").exists(), "no stray directory may be created")
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before)

    def test_team_memory_flag_refuses_dot_and_repeated_slashes(self):  # r3 #1
        proc = self.install("--team-memory", f"{self.root}//a/./b/../..//./mnemosyne/./")
        self.assert_refused_untouched(proc)
        self.assertFalse((self.root / "a").exists())

    def test_team_memory_flag_is_lexically_normalised_before_mkdir_and_ln(self):  # r3 #1
        proc = self.install("--team-memory", f"{self.tmp}/x/../y//./bank/")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        target = os.readlink(self.profile("pm") / "mnemosyne")
        self.assertEqual(target, str((self.tmp / "y" / "bank").resolve()))
        self.assertFalse((self.tmp / "x").exists(), "the '..'-cancelled component must not be created")
        self.assertTrue((self.tmp / "y" / "bank").is_dir())

    # --- links anywhere on the path (parity with Windows r3 findings #2/#3)
    def test_team_memory_flag_refuses_a_link_to_the_operator_bank(self):
        link = self.tmp / "oplink"
        link.symlink_to(self.root / "mnemosyne")
        self.assert_refused_untouched(self.install("--team-memory", str(link)))

    def test_team_memory_flag_refuses_a_path_below_a_link_to_the_operator_bank(self):
        link = self.tmp / "oplink"
        link.symlink_to(self.root / "mnemosyne")
        before = self.snapshot_tree(self.root / "mnemosyne")
        self.assert_refused_untouched(self.install("--team-memory", str(link / "data" / "team")))
        self.assertFalse((self.root / "mnemosyne" / "data" / "team").exists())
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before)

    # --- D4: a bind mount gives a path identical to the operator bank
    # with no symlink in it anywhere, so the string/symlink-only check
    # must not be fooled by it.
    def test_team_memory_flag_refuses_an_alias_via_bind_mount(self):  # D4
        if not _unprivileged_bind_mount_works():
            self.skipTest("needs an unprivileged bind mount (unshare --mount --map-root-user); not permitted here")
        import shlex
        alias = self.tmp / "bind-alias"
        alias.mkdir()
        script = (
            f"mount --bind {shlex.quote(str(self.root / 'mnemosyne'))} {shlex.quote(str(alias))} "
            f"|| exit 99\n"
            f"exec bash {shlex.quote(str(INSTALLER))} --skip-model --team-memory {shlex.quote(str(alias))}\n"
        )
        proc = subprocess.run(
            ["unshare", "--mount", "--map-root-user", "--propagation", "private", "bash", "-c", script],
            env=self.env, capture_output=True, text=True, timeout=120)
        if proc.returncode == 99:
            self.skipTest("unprivileged bind mount unavailable in this sandbox")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_team_memory_flag_refuses_operator_bank_through_a_link_to_the_root(self):
        link = self.tmp / "hlink"
        link.symlink_to(self.root)
        self.assert_refused_untouched(self.install("--team-memory", str(link / "mnemosyne")))

    def test_team_memory_flag_refuses_a_dangling_link_into_the_operator_bank(self):
        link = self.tmp / "dangling"
        link.symlink_to(self.root / "mnemosyne" / "not-yet")
        self.assert_refused_untouched(self.install("--team-memory", str(link)))
        self.assertFalse((self.root / "mnemosyne" / "not-yet").exists())

    def test_team_memory_flag_refuses_mnemosyne_home_given_as_a_link(self):
        real = self.tmp / "relocated-bank"
        real.mkdir()
        link = self.tmp / "mhlink"
        link.symlink_to(real)
        proc = self.install("--team-memory", str(real), MNEMOSYNE_HOME=str(link))
        self.assert_refused_untouched(proc, needle="MNEMOSYNE_HOME")
        self.assertFalse(any(real.iterdir()))

    def test_team_memory_flag_refuses_a_link_cycle_without_spinning(self):  # r3 #1: bounded
        (self.tmp / "loop1").symlink_to(self.tmp / "loop2")
        (self.tmp / "loop2").symlink_to(self.tmp / "loop1")
        proc = self.install("--team-memory", str(self.tmp / "loop1" / "bank"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Too many levels of symbolic links", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_team_memory_flag_with_spaces_and_a_link_resolves_to_the_real_path(self):  # r3 #1
        real = self.tmp / "dir with space"
        real.mkdir()
        (self.tmp / "sp link").symlink_to(real)
        proc = self.install("--team-memory", str(self.tmp / "sp link" / "bank"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.readlink(self.profile("pm") / "mnemosyne"),
                         os.path.realpath(real / "bank"))

    # --- r3 review finding #4: the relink NOTE covers every operator location
    def _prelinked_profile(self, role, target):
        pdir = self.profile(role)
        pdir.mkdir(parents=True)
        (pdir / "config.yaml").touch()
        (pdir / "mnemosyne").symlink_to(target)
        return pdir

    def test_relink_note_fires_for_a_link_under_the_operator_bank(self):  # r3 #4
        self._prelinked_profile("qa", self.root / "mnemosyne" / "data")
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"{self.profile('qa')}/mnemosyne was linked to the operator's own bank", proc.stdout)
        self.assertEqual((self.profile("qa") / "mnemosyne").resolve(),
                         (self.root / "dev-team-mnemosyne").resolve())

    def test_relink_note_fires_for_a_link_to_mnemosyne_home(self):  # r3 #4
        relocated = self.tmp / "relocated-bank"
        relocated.mkdir()
        self._prelinked_profile("qa", relocated)
        proc = self.install(MNEMOSYNE_HOME=str(relocated))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"{self.profile('qa')}/mnemosyne was linked to the operator's own bank", proc.stdout)

    def test_relink_note_is_silent_for_an_unrelated_previous_target(self):  # r3 #4
        other = self.tmp / "other-team-bank"
        other.mkdir()
        self._prelinked_profile("qa", other)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("was linked to the operator's own bank", proc.stdout)

    def test_team_memory_flag_overrides_the_default_location(self):  # Task 2, acc #1
        custom = self.tmp / "custom-team-bank"
        proc = self.install("--team-memory", str(custom))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for role in ROLES:
            link = self.profile(role) / "mnemosyne"
            with self.subTest(role=role):
                self.assertEqual(link.resolve(), custom.resolve())
        self.assertFalse((self.root / "dev-team-mnemosyne").exists())

    def test_team_memory_flag_refuses_the_operators_own_bank(self):  # Task 2, acc #2
        proc = self.install("--team-memory", str(self.root / "mnemosyne"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("operator's own bank", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_preexisting_profile_keeps_its_own_mnemosyne(self):  # item 10
        own = self.profile("qa") / "mnemosyne" / "data"
        own.mkdir(parents=True)
        (own / "mnemosyne.db").write_text("qa's own memories")
        (self.profile("qa") / "config.yaml").touch()
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.profile("qa") / "mnemosyne").is_symlink())
        self.assertEqual((own / "mnemosyne.db").read_text(), "qa's own memories")
        self.assertIn("dev-qa", proc.stdout)  # named in the not-shared summary
        self.assertIn("--adopt-team-memory", proc.stdout)

    def test_adopt_team_memory_backs_up_and_relinks(self):  # Task 2, acc #3
        own = self.profile("qa") / "mnemosyne" / "data"
        own.mkdir(parents=True)
        (own / "mnemosyne.db").write_text("qa's own memories")
        (self.profile("qa") / "config.yaml").touch()
        proc = self.install("--adopt-team-memory")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        qa_dir = self.profile("qa")
        link = qa_dir / "mnemosyne"
        self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
        self.assertEqual(link.resolve(), (self.root / "dev-team-mnemosyne").resolve())

        backups = list(qa_dir.glob("mnemosyne.pre-team-memory-*"))
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual((backups[0] / "data" / "mnemosyne.db").read_text(),
                          "qa's own memories")
        self.assertIn("ADOPTED", proc.stdout)
        self.assertIn("dev-qa", proc.stdout)
        self.assertIn("out of scope", proc.stdout)  # merging old memories

    def test_separate_memory_is_unchanged(self):  # Task 2, acc #4
        proc = self.install("--separate-memory")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for role in ROLES:
            link = self.profile(role) / "mnemosyne"
            with self.subTest(role=role):
                self.assertFalse(link.is_symlink(), f"{link} must stay a real directory")
                self.assertTrue(link.is_dir())
        # Not even created when every profile keeps its own database.
        self.assertFalse((self.root / "dev-team-mnemosyne").exists())
        self.assertIn("separate per profile", proc.stdout)
        # R3-1 (Unix side): each profile's .env pins its OWN bank, so an
        # inherited MNEMOSYNE_DATA_DIR/MNEMOSYNE_HOME cannot redirect it.
        for role in ROLES:
            own = self.profile(role) / "mnemosyne"
            with self.subTest(role=role):
                lines = (self.profile(role) / ".env").read_text().splitlines()
                self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_DATA_DIR=")],
                                 [f"MNEMOSYNE_DATA_DIR={_quote_env(str(own / 'data'))}"])
                self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_HOME=")],
                                 [f"MNEMOSYNE_HOME={_quote_env(str(own))}"])

    def test_team_bank_path_with_ampersand_and_pipe_is_pinned_literally(self):  # R3-2 / F4
        """F4: the .env rewrite used to be `sed` with the path in the
        pattern/replacement, where & and | are special. Install twice (the
        second run takes the rewrite-in-place path) and require the literal
        value, exactly one line per variable, and other lines preserved."""
        bank = self.tmp / "team & co | bank"
        self.profile("qa").mkdir(parents=True)
        (self.profile("qa") / ".env").write_text(
            "FOO=bar\nMNEMOSYNE_DATA_DIR=/stale/value\nLAST=no-newline")
        for run in (1, 2):
            proc = self.install("--team-memory", str(bank), "--only", "qa")
            self.assertEqual(proc.returncode, 0, f"run {run}: " + proc.stdout + proc.stderr)
            lines = (self.profile("qa") / ".env").read_text().splitlines()
            with self.subTest(run=run):
                self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_DATA_DIR=")],
                                 [f"MNEMOSYNE_DATA_DIR={_quote_env(str(bank / 'data'))}"])
                self.assertEqual([l for l in lines if l.startswith("MNEMOSYNE_HOME=")],
                                 [f"MNEMOSYNE_HOME={_quote_env(str(bank))}"])
                self.assertIn("FOO=bar", lines)
                self.assertIn("LAST=no-newline", lines)

    def test_team_bank_path_with_hash_is_quoted_and_round_trips(self):  # item 4a (review r3)
        """python-dotenv truncates an UNQUOTED value at ' #' (its
        _unquoted_value then strips r'\\s+#.*'), so a bank path containing
        ' #' used to come back silently shortened on the next load. The
        installer now writes every pin single-quoted with python-dotenv's
        own single-quote escapes (dotenv/parser.py:25,32): '\\\\' -> '\\\\\\\\',
        "'" -> "\\\\'". Decoded here two ways: a stdlib-only reimplementation
        of that exact rule (meaningful even in a dotenv-less venv), and, if
        importable, the real python-dotenv parser."""
        bank = self.tmp / "team # tricky bank"
        proc = self.install("--team-memory", str(bank), "--only", "qa")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = (self.profile("qa") / ".env").read_text().splitlines()
        data_lines = [l for l in lines if l.startswith("MNEMOSYNE_DATA_DIR=")]
        home_lines = [l for l in lines if l.startswith("MNEMOSYNE_HOME=")]
        self.assertEqual(len(data_lines), 1)
        self.assertEqual(len(home_lines), 1)
        expected_data = str(bank / "data")
        expected_home = str(bank)
        # The raw on-disk line must actually be quoted (this is the RED
        # case against the unfixed installer: an unquoted line here means
        # python-dotenv would truncate expected_data/_home at ' #').
        self.assertTrue(data_lines[0][len("MNEMOSYNE_DATA_DIR="):].startswith("'"),
                         f"pin not quoted: {data_lines[0]!r}")

        def stdlib_single_quote_decode(raw_value):
            """Reimplements dotenv/parser.py's _single_quoted_value +
            _single_quote_escapes using only stdlib re/codecs -- no
            python-dotenv import required."""
            import re
            assert raw_value[0] == "'" and raw_value[-1] == "'", raw_value
            inner = raw_value[1:-1]
            return re.sub(r"\\[\\']", lambda m: m.group(0)[1], inner)

        self.assertEqual(stdlib_single_quote_decode(data_lines[0][len("MNEMOSYNE_DATA_DIR="):]),
                          expected_data)
        self.assertEqual(stdlib_single_quote_decode(home_lines[0][len("MNEMOSYNE_HOME="):]),
                          expected_home)

        try:
            from dotenv import dotenv_values  # noqa: PLC0415
        except ImportError:
            self.skipTest("python-dotenv not importable here; stdlib-only decode above already passed")
            return
        values = dotenv_values(str(self.profile("qa") / ".env"))
        self.assertEqual(values.get("MNEMOSYNE_DATA_DIR"), expected_data)
        self.assertEqual(values.get("MNEMOSYNE_HOME"), expected_home)

    def test_first_run_exits_0_when_the_plugin_list_lags(self):  # item 11
        proc = self.install(FAKE_PLUGIN_LIST_LAG="2")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_plugin_never_enabled_still_fails(self):  # item 11: the retry is bounded
        proc = self.install(FAKE_PLUGIN_LIST_LAG="99")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("dev-team-gate NOT enabled", proc.stdout)

    def test_refuses_to_overwrite_a_forge_it_did_not_write(self):  # #2 / Minor
        forge = self.home / ".local" / "bin" / "forge"
        forge.parent.mkdir(parents=True)
        forge.write_text("#!/bin/sh\necho laravel forge\n")
        proc = self.install()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("forge", proc.stderr)
        self.assertEqual(forge.read_text(), "#!/bin/sh\necho laravel forge\n")
        # refused before any profile was touched: no partial install
        self.assertFalse((self.root / "profiles").exists())

    def test_rerun_replaces_its_own_forge(self):
        self.assertEqual(self.install().returncode, 0)
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dev-team forge wrapper",
                      (self.home / ".local" / "bin" / "forge").read_text())

    def test_writes_the_prefix_file(self):  # item 9
        proc = self.install("--prefix", "t2-")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for role in ROLES:
            with self.subTest(role=role):
                self.assertEqual(
                    (self.profile(role, "t2-") / "dev-team-prefix").read_text(), "t2-\n")

    def test_dry_run_changes_nothing(self):
        proc = self.install("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.root / "profiles").exists())
        self.assertFalse((self.home / ".local" / "bin" / "forge").exists())

    def test_I5_never_tells_the_operator_to_use_a_plaintext_env_file(self):
        # Without `secret` on PATH the installer used to say "add
        # OPENROUTER_DECIDERS_KEY to each profile's .env" -- eight plaintext
        # copies of the key for the default team.
        no_secret_bin = self.tmp / "no-secret-bin"
        no_secret_bin.mkdir()
        for name in ("hermes",):
            src = Path(self.env["PATH"].split(":")[0]) / name
            dst = no_secret_bin / name
            shutil.copy(src, dst)
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR)
        proc = self.install(PATH=f"{no_secret_bin}:/usr/bin:/bin")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn(".env", proc.stdout)
        self.assertIn("OPENROUTER_DECIDERS_KEY", proc.stdout)

    def test_M8_refuses_an_empty_model_with_a_colon(self):
        # "--tier-fast anthropic:" has a colon (passes the PROVIDER:MODEL
        # shape check) but an empty model -- must not silently write
        # {"provider": "anthropic", "model": ""} to model-tiers.json.
        proc = self.install("--tier-fast", "anthropic:")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--tier-fast", proc.stderr)
        self.assertFalse((self.root / "dev-team" / "model-tiers.json").exists())

    def test_refuses_a_decisions_only_model_as_a_tier(self):
        # liquid/d1.1 and pplx-decider only answer /api/alpha/decisions; an
        # agent worker on a card routed to one of them cannot run at all.
        for flag, value in (("--tier-fast", "openrouter:liquid/d1.1"),
                            ("--tier-standard", "openrouter:perplexity/pplx-decider-v1.2"),
                            ("--tier-deep", "openrouter:liquid/d1.1-20261027")):
            with self.subTest(flag=flag):
                proc = self.install(flag, value)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("decisions-only", proc.stderr)
                self.assertIn(flag, proc.stderr)
                self.assertFalse((self.root / "profiles").exists())
                self.assertFalse((self.root / "dev-team" / "model-tiers.json").exists())

    def test_I3_refuses_additional_decision_models_and_the_pplx_family(self):
        for flag, value in (
            ("--tier-fast", "typesafe:jev-1.13"),
            ("--tier-standard", "upstage:solar-decide-flash"),
            ("--tier-deep", "perplexity:pplx-decider-v2"),  # family, not just v1.2
        ):
            with self.subTest(flag=flag, value=value):
                proc = self.install(flag, value)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("decisions-only", proc.stderr)

    def test_I3_refuses_a_variant_suffix_on_a_decision_model(self):
        proc = self.install("--tier-fast", "openrouter:liquid/d1.1:nitro")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("decisions-only", proc.stderr)

    def test_I3_refuses_a_decisions_only_model_via_the_model_flag(self):
        # self.install() always prepends --skip-model (which contradicts
        # --model), so invoke the installer directly here.
        proc = subprocess.run(
            ["bash", str(INSTALLER), "--model", "liquid/d1.1"],
            env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("decisions-only", proc.stderr)
        self.assertIn("--model", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_default_fast_tier_is_a_chat_model(self):
        proc = self.install("--only", "pm")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        import json
        table = json.loads((self.root / "dev-team" / "model-tiers.json").read_text())
        self.assertEqual(table["fast"], {"provider": "anthropic", "model": "claude-haiku-4-5"})

    def test_help_does_not_suggest_a_decisions_model_for_a_tier(self):
        proc = self.install("--help")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("liquid", proc.stdout)

    def test_only_installs_the_named_roles(self):
        proc = self.install("--only", "qa,devops")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "profiles").iterdir()),
                         ["dev-devops", "dev-qa"])

    # --- D1: MNEMOSYNE_DATA_DIR is the variable that actually controls
    # Mnemosyne's data directory (mnemosyne/cli.py:20-23,
    # mnemosyne/core/beam.py:358-366), so it must be treated as an operator
    # location exactly like MNEMOSYNE_HOME, and each profile must pin its
    # own value in .env so an inherited one cannot redirect it.
    def test_team_memory_flag_refuses_mnemosyne_data_dir(self):  # D1
        decoy = self.tmp / "decoy-data-dir"
        decoy.mkdir()
        proc = self.install("--team-memory", str(decoy), MNEMOSYNE_DATA_DIR=str(decoy))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("MNEMOSYNE_DATA_DIR", proc.stderr)
        self.assertFalse((self.root / "profiles").exists())

    def test_mnemosyne_data_dir_env_var_does_not_select_the_team_bank(self):  # D1
        decoy = self.tmp / "decoy-data-dir"
        decoy.mkdir()
        (decoy / "mnemosyne.db").write_text("operator-relocated-bank")
        before_decoy = self.snapshot_tree(decoy)
        proc = self.install(MNEMOSYNE_DATA_DIR=str(decoy))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot_tree(decoy), before_decoy, "MNEMOSYNE_DATA_DIR must not be written to")
        link = self.profile("pm") / "mnemosyne"
        self.assertEqual(link.resolve(), (self.root / "dev-team-mnemosyne").resolve())

    def test_profile_env_pins_mnemosyne_data_dir_to_the_team_bank(self):  # D1
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env_file = self.profile("pm") / ".env"
        self.assertTrue(env_file.exists(), ".env must be written")
        text = env_file.read_text()
        self.assertIn(f"MNEMOSYNE_DATA_DIR={_quote_env(str(self.root / 'dev-team-mnemosyne' / 'data'))}", text)

    def test_profile_env_pins_mnemosyne_home_to_the_team_bank(self):  # F1 (review r3)
        """mnemosyne/mcp_tools.py:167-173 (shared-surface DB), plus
        core/fleet_census.py and core/federation_handshake.py, read
        MNEMOSYNE_HOME directly to build a DB path -- not MNEMOSYNE_DATA_DIR.
        (mcp_server.py also reads MNEMOSYNE_HOME, but only to locate a .env
        file to auto-load for the standalone MCP server binary, not to build
        a DB path -- see README item 2 / review r3.) Pinning only
        MNEMOSYNE_DATA_DIR (the original D1 fix) left MNEMOSYNE_HOME free to
        leak the operator's shared-surface DB path into a dev-team profile."""
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env_file = self.profile("pm") / ".env"
        text = env_file.read_text()
        self.assertIn(f"MNEMOSYNE_HOME={_quote_env(str(self.root / 'dev-team-mnemosyne'))}", text)
        # The team bank's /data suffix must not bleed into MNEMOSYNE_HOME.
        self.assertNotIn(f"MNEMOSYNE_HOME={self.root / 'dev-team-mnemosyne'}/data", text)

    def test_mnemosyne_data_dir_pin_wins_over_an_inherited_value_under_real_precedence(self):  # D1
        """This is a stdlib-only, in-process MODEL of the precedence rule
        Hermes applies (hermes_cli/env_loader.py:502-562 loads a profile's
        own .env with python-dotenv's override=True, so its values beat
        whatever is already in the process environment): it re-implements
        that merge with `dict.update()` against a throwaway dict seeded
        with a stale, wrong MNEMOSYNE_DATA_DIR (standing in for the
        operator's `~/.profile` export), using the installer's actual
        written .env content, without ever touching this test process's
        real os.environ. It proves the .env line is correct and would win
        under that rule; it does not invoke Hermes' own loader code --
        see test_mnemosyne_pins_win_over_inherited_values_under_the_real_hermes_loader
        below for that, where the real loader is reachable."""
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env_file = self.profile("pm") / ".env"
        simulated_environ = {"MNEMOSYNE_DATA_DIR": str(self.tmp / "stale-inherited-shell-value"),
                              "MNEMOSYNE_HOME": str(self.tmp / "stale-inherited-shell-home")}
        # Stdlib only (the suites must run with no third-party packages, as
        # CI does): the installer writes plain KEY=VALUE lines, optionally
        # quoted, which is all this reader needs to understand.
        dotenv_values = {}
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            dotenv_values[key] = value
        simulated_environ.update(dotenv_values)  # override=True: dotenv values win
        self.assertEqual(simulated_environ["MNEMOSYNE_DATA_DIR"],
                          str(self.root / "dev-team-mnemosyne" / "data"))
        self.assertEqual(simulated_environ["MNEMOSYNE_HOME"],
                          str(self.root / "dev-team-mnemosyne"))

    def test_mnemosyne_pins_win_over_inherited_values_under_the_real_hermes_loader(self):  # F1/D1
        """Runs the installer, then loads the pinned .env with Hermes' own
        ``load_hermes_dotenv`` (hermes_cli/env_loader.py) against a stale,
        inherited MNEMOSYNE_DATA_DIR/MNEMOSYNE_HOME pre-seeded in a saved
        and restored os.environ -- the real precedence rule, not a
        re-implementation of it. ``load_hermes_dotenv`` imports
        python-dotenv and this repo's bundled ``agent``/``hermes_constants``
        modules at module scope (env_loader.py:17-18), none of which exist
        in the stdlib-only CI-equivalence venv, so this test skips there
        with an explicit reason instead of failing or silently doing
        nothing."""
        hermes_agent_dir = Path.home() / ".hermes" / "hermes-agent"
        if not (hermes_agent_dir / "hermes_cli" / "env_loader.py").exists():
            self.skipTest(f"real Hermes install not found at {hermes_agent_dir}; "
                           "cannot load the real env loader here")
        import sys
        path_entry = str(hermes_agent_dir)
        pre_existing_modules = set(sys.modules)
        sys.path.insert(0, path_entry)
        self.addCleanup(lambda: path_entry in sys.path and sys.path.remove(path_entry))

        def _unimport_newly_loaded_modules():
            for name in set(sys.modules) - pre_existing_modules:
                del sys.modules[name]
        self.addCleanup(_unimport_newly_loaded_modules)
        try:
            import hermes_cli.env_loader as real_env_loader  # noqa: PLC0415
        except ImportError as exc:
            self.skipTest(f"real env_loader is not importable here ({exc!r}); "
                           "this is expected in the stdlib-only venv, which has "
                           "neither python-dotenv nor this repo's bundled 'agent' "
                           "package -- both imported at env_loader.py module scope")
            return

        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        profile_home = self.profile("pm")
        saved_environ = dict(os.environ)
        os.environ["MNEMOSYNE_DATA_DIR"] = str(self.tmp / "stale-inherited-shell-value")
        os.environ["MNEMOSYNE_HOME"] = str(self.tmp / "stale-inherited-shell-home")
        try:
            real_env_loader.load_hermes_dotenv(hermes_home=profile_home, load_external_secrets=False)
            self.assertEqual(os.environ.get("MNEMOSYNE_DATA_DIR"),
                              str(self.root / "dev-team-mnemosyne" / "data"))
            self.assertEqual(os.environ.get("MNEMOSYNE_HOME"),
                              str(self.root / "dev-team-mnemosyne"))
        finally:
            os.environ.clear()
            os.environ.update(saved_environ)

    # --- F2 (review r3): the profile-dir canonicalisation check must run
    # BEFORE SOUL, the prefix file, or the plugins/mnemosyne link -- not
    # just before the mnemosyne/ data link as D2 originally had it.
    def test_profile_dir_outside_profiles_leaves_operator_root_untouched_without_adopt(self):  # F2
        """Reviewer's repro: profiles/dev-qa -> $root, a default run (no
        --adopt-team-memory). Before this fix the canonicalisation check
        ran only right before the mnemosyne/ link, so SOUL.md was
        overwritten, dev-team-prefix was written, and plugins/mnemosyne
        was (re)linked onto the operator's own plugin directory before the
        check ever fired."""
        profiles_dir = self.root / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "dev-qa").symlink_to(self.root)
        (self.root / "SOUL.md").write_text("operator soul\n")
        before_soul = (self.root / "SOUL.md").read_bytes()
        # R3-3: real plugin content, so a recursive delete or a self-link
        # created inside it ($R/plugins/mnemosyne/mnemosyne, what
        # `ln -sfn` does onto an existing directory) shows up.
        (self.root / "plugins" / "mnemosyne" / "__init__.py").write_text("plugin code\n")
        before_plugins = self.snapshot_tree(self.root / "plugins")
        proc = self.install("--only", "qa")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("outside", proc.stderr)
        self.assertEqual((self.root / "SOUL.md").read_bytes(), before_soul,
                          "operator SOUL.md must be byte-identical")
        self.assertEqual(self.snapshot_tree(self.root / "plugins"), before_plugins,
                          "operator plugins/ must be byte-identical")
        self.assertFalse((self.root / "dev-team-prefix").exists(),
                          "dev-team-prefix must not have been written to the operator root")

    # --- F2 equality (review r3, item 4b): a profile dir that resolves to
    # EXACTLY $HERMES_ROOT/profiles (not merely nested under it) must also
    # be refused. path_is_or_under() treats "is" and "is under" the same,
    # so profiles/ itself used to be accepted as a valid profile directory.
    def test_profile_dir_equal_to_profiles_itself_is_refused(self):  # F2 equality
        profiles_dir = self.root / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "dev-qa").symlink_to(profiles_dir)
        proc = self.install("--only", "qa")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((profiles_dir / "SOUL.md").exists(),
                          "SOUL.md must not be written into profiles/ itself")
        self.assertFalse((profiles_dir / "dev-team-prefix").exists(),
                          "dev-team-prefix must not be written into profiles/ itself")
        self.assertFalse((profiles_dir / "plugins" / "mnemosyne").exists(),
                          "the plugin link must not be created under profiles/ itself")

    # --- D2: the adopt branch must canonicalise before touching anything
    def test_adopt_team_memory_refuses_when_profile_dir_resolves_outside_profiles(self):  # D2
        # Reviewer's repro: make the profile directory itself a symlink to
        # the Hermes root, so "$profile_dir/mnemosyne" is really the
        # operator's own bank.
        profiles_dir = self.root / "profiles"
        profiles_dir.mkdir(parents=True)
        (profiles_dir / "dev-qa").symlink_to(self.root)
        before = self.snapshot_tree(self.root / "mnemosyne")
        proc = self.install("--adopt-team-memory")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("outside", proc.stderr)
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before, "operator bank must be untouched")

    # --- D3: an existing backup path must never be merged into
    def test_adopt_team_memory_never_merges_into_an_existing_backup_directory(self):  # D3
        own = self.profile("qa") / "mnemosyne" / "data"
        own.mkdir(parents=True)
        (own / "mnemosyne.db").write_text("qa's own memories")
        (self.profile("qa") / "config.yaml").touch()
        # Freeze `date` so the backup name is predictable, then pre-create
        # that exact path as a real directory with unrelated contents
        # (review D3's first repro).
        fake_date = self.tmp / "bin2" / "date"
        fake_date.parent.mkdir()
        fake_date.write_text("#!/usr/bin/env bash\necho 20261103105748\n")
        fake_date.chmod(fake_date.stat().st_mode | stat.S_IXUSR)
        env = dict(self.env)
        env["PATH"] = f"{fake_date.parent}:{self.env['PATH']}"
        collide = self.profile("qa") / "mnemosyne.pre-team-memory-20261103105748"
        collide.mkdir(parents=True)
        (collide / "pre-existing.txt").write_text("unrelated contents")
        proc = subprocess.run(["bash", str(INSTALLER), "--skip-model", "--adopt-team-memory", "--only", "qa"],
                               env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # The pre-existing backup must be untouched...
        self.assertEqual((collide / "pre-existing.txt").read_text(), "unrelated contents")
        self.assertFalse((collide / "data").exists(), "qa's bank must not have landed inside the colliding dir")
        # ...and qa's own bank must have landed at a NEW, unique path.
        backups = sorted(p.name for p in self.profile("qa").glob("mnemosyne.pre-team-memory-20261103105748*"))
        self.assertEqual(backups, ["mnemosyne.pre-team-memory-20261103105748",
                                    "mnemosyne.pre-team-memory-20261103105748.1"])
        moved = self.profile("qa") / "mnemosyne.pre-team-memory-20261103105748.1"
        self.assertEqual((moved / "data" / "mnemosyne.db").read_text(), "qa's own memories")

    def test_adopt_team_memory_never_merges_into_an_existing_backup_link(self):  # D3
        own = self.profile("qa") / "mnemosyne" / "data"
        own.mkdir(parents=True)
        (own / "mnemosyne.db").write_text("qa's own memories")
        (self.profile("qa") / "config.yaml").touch()
        fake_date = self.tmp / "bin3" / "date"
        fake_date.parent.mkdir()
        fake_date.write_text("#!/usr/bin/env bash\necho 20261103105748\n")
        fake_date.chmod(fake_date.stat().st_mode | stat.S_IXUSR)
        env = dict(self.env)
        env["PATH"] = f"{fake_date.parent}:{self.env['PATH']}"
        collide_link = self.profile("qa") / "mnemosyne.pre-team-memory-20261103105748"
        collide_link.symlink_to(self.root / "mnemosyne")  # link to the OPERATOR bank
        before_operator = self.snapshot_tree(self.root / "mnemosyne")
        proc = subprocess.run(["bash", str(INSTALLER), "--skip-model", "--adopt-team-memory", "--only", "qa"],
                               env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.snapshot_tree(self.root / "mnemosyne"), before_operator,
                          "the operator bank must never receive qa's moved data")
        moved = self.profile("qa") / "mnemosyne.pre-team-memory-20261103105748.1"
        self.assertEqual((moved / "data" / "mnemosyne.db").read_text(), "qa's own memories")

    # --- D5: --team-memory inside profiles/ or dev-team/ breaks every link
    def test_team_memory_flag_refuses_a_path_under_profiles_directory(self):  # D5
        proc = self.install("--team-memory", str(self.root / "profiles" / "dev-qa" / "mnemosyne"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("profile directory", proc.stderr)
        self.assertFalse((self.root / "profiles" / "dev-qa" / "mnemosyne").exists())

    def test_team_memory_flag_refuses_a_path_under_dev_team_directory(self):  # D5
        proc = self.install("--team-memory", str(self.root / "dev-team" / "mnemosyne"))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("dev-team", proc.stderr)

    # --- D8: --dry-run with --adopt-team-memory must report the backup it would make
    def test_dry_run_with_adopt_reports_would_back_up_for_each_private_bank(self):  # D8
        own = self.profile("qa") / "mnemosyne" / "data"
        own.mkdir(parents=True)
        (own / "mnemosyne.db").write_text("qa's own memories")
        (self.profile("qa") / "config.yaml").touch()
        proc = self.install("--dry-run", "--adopt-team-memory", "--only", "qa")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"would back up: {self.profile('qa')}/mnemosyne ->", proc.stdout)
        self.assertIn("would link:", proc.stdout)
        self.assertFalse((own / "mnemosyne.db").exists() and False)  # dry run: nothing moved
        self.assertEqual((own / "mnemosyne.db").read_text(), "qa's own memories")


class T4TeamHomeRootTests(InstallerSetupMixin, unittest.TestCase):
    """T4: the team home must live at the Hermes root, not inside whatever
    profile happens to be running the installer. Reproduces the live bug:
    the installer was run with HERMES_HOME pointing at a profile directory
    (as the gateway does for any named profile), so `hermes config path`
    with no -p resolves to that profile's own config.yaml.
    """

    def builder_profile(self, name="devops"):
        p = self.root / "profiles" / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "config.yaml").touch()
        return p

    def test_root_climbs_out_of_the_active_profile(self):
        builder = self.builder_profile()
        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"Team state:    {self.root}/dev-team", proc.stdout)
        self.assertTrue((self.root / "dev-team" / "model-tiers.json").exists())
        self.assertFalse((builder / "dev-team").exists())
        for role in ROLES:
            link = self.profile(role) / "dev-team"
            with self.subTest(role=role):
                self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
                self.assertEqual(link.resolve(), (self.root / "dev-team").resolve())

    def test_migrates_an_old_profile_rooted_team_home(self):
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "model-tiers.json").write_text("old-tiers")
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")

        # dev-pm already exists from an earlier, profile-rooted install run
        # and still links at the old, wrong location.
        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Migrated team state:", proc.stdout)

        new_home = self.root / "dev-team"
        self.assertEqual((new_home / "deciders-ledger.jsonl").read_text(), "ledger-data")
        # D3 (review): the old path is never left dangling or duplicated --
        # it becomes a symlink to the new home, so anything still resolving
        # it (including a link outside the scanned set) keeps working.
        self.assertTrue(old_home.is_symlink(), "old_home must become a symlink, not vanish")
        self.assertEqual(old_home.resolve(), new_home.resolve())
        backups = list(builder.glob("dev-team.pre-root-migration-*"))
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual((backups[0] / "deciders-ledger.jsonl").read_text(), "ledger-data")

        link = pm_dir / "dev-team"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), new_home.resolve())

    def test_N1_existing_empty_root_does_not_nest_the_data(self):
        # Review r2 N1: with the root dev-team already present as an EMPTY
        # directory, `mv OLD NEW` moved OLD *into* it (dev-team/dev-team/...).
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")
        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)
        new_home = self.root / "dev-team"
        new_home.mkdir(parents=True)  # exists, empty

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((new_home / "deciders-ledger.jsonl").read_text(), "ledger-data")
        self.assertFalse((new_home / "dev-team").exists(), "data nested one level too deep")
        self.assertEqual((pm_dir / "dev-team" / "deciders-ledger.jsonl").read_text(), "ledger-data")

    def test_refuses_when_old_and_new_homes_both_hold_data(self):
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("old-ledger")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        new_home = self.root / "dev-team"
        new_home.mkdir(parents=True)
        (new_home / "model-tiers.json").write_text("new-existing")

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Team home conflict", proc.stderr)
        self.assertIn(f'mv "{new_home}"', proc.stderr)
        self.assertIn(f'mv "{old_home}"', proc.stderr)
        # Neither side was touched.
        self.assertEqual((old_home / "deciders-ledger.jsonl").read_text(), "old-ledger")
        self.assertEqual((new_home / "model-tiers.json").read_text(), "new-existing")
        self.assertFalse((self.root / "profiles" / "dev-architect").exists())

    def test_migration_relinks_every_profile_left_out_by_only(self):
        # Review finding #1: dev-pm and dev-qa both link at the old,
        # profile-rooted home. Running with --only pm must not leave
        # dev-qa's dev-team link dangling once the old home is renamed away.
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        qa_dir = self.profile("qa")
        qa_dir.mkdir(parents=True)
        (qa_dir / "config.yaml").touch()
        (qa_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        proc = self.install("--only", "pm", HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        new_home = self.root / "dev-team"
        # D3 (review): the old path becomes a symlink to the new home
        # rather than disappearing (see test_migrates_an_old_profile_rooted
        # _team_home for the direct D3 coverage).
        self.assertTrue(old_home.is_symlink())
        self.assertEqual(old_home.resolve(), new_home.resolve())

        pm_link = pm_dir / "dev-team"
        self.assertTrue(pm_link.is_symlink())
        self.assertEqual(pm_link.resolve(), new_home.resolve())

        # dev-qa was not in --only but must still be relinked, not dangling.
        qa_link = qa_dir / "dev-team"
        self.assertTrue(qa_link.is_symlink())
        self.assertTrue(qa_link.exists(), "dev-qa/dev-team is a dangling link")
        self.assertEqual(qa_link.resolve(), new_home.resolve())
        self.assertIn("relinked:", proc.stdout)
        self.assertIn(str(qa_dir), proc.stdout)

    def test_refuses_when_two_distinct_old_homes_both_hold_data(self):
        # Review finding #2: the scan must not stop at the first non-empty
        # old target. dev-pm and dev-uiux link at two different, both
        # populated, old locations; the new root is empty. Must refuse and
        # list every one of them rather than silently relinking to one and
        # orphaning the other.
        builder = self.builder_profile()
        old_home_pm = builder / "dev-team"
        old_home_pm.mkdir(parents=True)
        (old_home_pm / "deciders-ledger.jsonl").write_text("pm-ledger")

        old_home_uiux = self.tmp / "elsewhere" / "dev-team"
        old_home_uiux.mkdir(parents=True)
        (old_home_uiux / "model-tiers.json").write_text("uiux-tiers")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home_pm, target_is_directory=True)

        uiux_dir = self.profile("uiux")
        uiux_dir.mkdir(parents=True)
        (uiux_dir / "config.yaml").touch()
        (uiux_dir / "dev-team").symlink_to(old_home_uiux, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Team home conflict", proc.stderr)
        self.assertIn(str(old_home_pm), proc.stderr)
        self.assertIn(str(old_home_uiux), proc.stderr)
        # Neither old location was touched, and no new root was created.
        self.assertEqual((old_home_pm / "deciders-ledger.jsonl").read_text(), "pm-ledger")
        self.assertEqual((old_home_uiux / "model-tiers.json").read_text(), "uiux-tiers")
        self.assertFalse((self.root / "dev-team").exists())

    def test_dry_run_migration_lists_entries_that_would_move(self):
        # Review finding #4: 'what moved' must list the top-level entries,
        # not only the source/destination paths, and must do so under
        # --dry-run too.
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")
        (old_home / "model-tiers.json").write_text("old-tiers")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        proc = self.install("--dry-run", HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Would migrate team state:", proc.stdout)
        self.assertIn("entries that would move:", proc.stdout)
        self.assertIn("deciders-ledger.jsonl", proc.stdout)
        self.assertIn("model-tiers.json", proc.stdout)
        self.assertIn(f"Team state:    {self.root}/dev-team", proc.stdout)
        # Nothing actually changed.
        self.assertTrue(old_home.exists())
        self.assertEqual((old_home / "deciders-ledger.jsonl").read_text(), "ledger-data")
        self.assertFalse((self.root / "dev-team").exists())

    def test_migration_lists_entries_moved(self):
        # Review finding #4, the non-dry-run side: the real run prints the
        # same per-entry listing once the move has happened.
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")
        (old_home / "model-tiers.json").write_text("old-tiers")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("entries moved:", proc.stdout)
        self.assertIn("deciders-ledger.jsonl", proc.stdout)
        self.assertIn("model-tiers.json", proc.stdout)

    def test_D1_refuses_repro_F_dev_pm_linked_to_its_own_profile_dir(self):
        # Repro F: a dev-team link resolves to the profile directory that
        # contains it -- migrating it would copy the whole profile (config,
        # memories) into the team home and rename the profile away.
        builder = self.builder_profile()
        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(pm_dir, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Refusing to migrate", proc.stderr)
        self.assertFalse((self.root / "dev-team").exists())
        # Tree unchanged: the profile is still exactly what it was.
        self.assertTrue((pm_dir / "config.yaml").exists())
        self.assertTrue((pm_dir / "dev-team").is_symlink())

    def test_D1_refuses_repro_H_dev_pm_linked_to_the_hermes_root(self):
        # Repro H: a dev-team link resolves to the Hermes root itself.
        builder = self.builder_profile()
        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(self.root, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("Refusing to migrate", proc.stderr)
        self.assertFalse((self.root / "dev-team").exists())
        # Tree unchanged: nothing from the root was copied or renamed away.
        self.assertTrue((self.root / "config.yaml").exists())
        self.assertTrue((self.root / "plugins" / "mnemosyne").exists())

    def test_D3_unscanned_external_link_still_resolves_after_migration(self):
        # An out-of-band consumer outside the installer's $PREFIX$role scan
        # entirely (never a profile at all) linked straight at the old
        # location must not be left dangling once that location is renamed.
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("ledger-data")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        external_dir = self.tmp / "external"
        external_dir.mkdir()
        external_link = external_dir / "dev-team"
        external_link.symlink_to(old_home, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        new_home = self.root / "dev-team"
        self.assertTrue(old_home.is_symlink())
        self.assertEqual(old_home.resolve(), new_home.resolve())
        self.assertTrue(external_link.exists(), "external_link is dangling")
        self.assertEqual(external_link.resolve(), new_home.resolve())
        self.assertEqual((external_link / "deciders-ledger.jsonl").read_text(), "ledger-data")

    def test_D4_repro_E_root_link_into_a_profile_is_migrated(self):
        # Repro E: the root dev-team is itself a link into a profile that
        # physically holds the data. The data must move to a real directory
        # at the root, not be reported as "already migrated" in place.
        builder = self.builder_profile()  # devops
        devops_data = builder / "dev-team"
        devops_data.mkdir(parents=True)
        (devops_data / "deciders-ledger.jsonl").write_text("devops-ledger")

        (self.root / "dev-team").symlink_to(devops_data, target_is_directory=True)

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(devops_data, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        new_home = self.root / "dev-team"
        self.assertTrue(new_home.is_dir() and not new_home.is_symlink(),
                         "the root dev-team must become a real directory")
        self.assertEqual((new_home / "deciders-ledger.jsonl").read_text(), "devops-ledger")
        self.assertTrue(devops_data.is_symlink())
        self.assertEqual(devops_data.resolve(), new_home.resolve())
        pm_link = pm_dir / "dev-team"
        self.assertTrue(pm_link.is_symlink())
        self.assertEqual(pm_link.resolve(), new_home.resolve())

    def test_D5_conflict_refusal_prints_concrete_commands_no_placeholder(self):
        builder = self.builder_profile()
        old_home = builder / "dev-team"
        old_home.mkdir(parents=True)
        (old_home / "deciders-ledger.jsonl").write_text("old-ledger")

        pm_dir = self.profile("pm")
        pm_dir.mkdir(parents=True)
        (pm_dir / "config.yaml").touch()
        (pm_dir / "dev-team").symlink_to(old_home, target_is_directory=True)

        # The new root has NO data (the commonest conflict shape, D5):
        # an old location plus some *other* unrelated conflicting candidate.
        old_home_2 = self.tmp / "elsewhere" / "dev-team"
        old_home_2.mkdir(parents=True)
        (old_home_2 / "model-tiers.json").write_text("other-tiers")
        uiux_dir = self.profile("uiux")
        uiux_dir.mkdir(parents=True)
        (uiux_dir / "config.yaml").touch()
        (uiux_dir / "dev-team").symlink_to(old_home_2, target_is_directory=True)

        proc = self.install(HERMES_HOME=str(builder))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("<", proc.stderr)  # no "<placeholder>" text anywhere
        self.assertIn(f'[ -e "{self.root}/dev-team" ] &&', proc.stderr)
        self.assertIn(f'mv "{old_home}" "{self.root}/dev-team"', proc.stderr)
        self.assertIn(f'mv "{old_home_2}" "{self.root}/dev-team"', proc.stderr)
        # Review r2 N2: after the chosen location is moved to the root, every
        # affected profile must be relinked to the ROOT, never to the moved
        # (now nonexistent) chosen path.
        self.assertIn(f'ln -sfn "{self.root}/dev-team" "{uiux_dir}/dev-team"', proc.stderr)
        self.assertIn(f'ln -sfn "{self.root}/dev-team" "{pm_dir}/dev-team"', proc.stderr)
        self.assertNotIn(f'ln -sfn "{old_home}"', proc.stderr)
        self.assertNotIn(f'ln -sfn "{old_home_2}"', proc.stderr)
        # Review r2 N4: the internal sentinel never reaches the user.
        self.assertNotIn("__root_link__", proc.stderr)
        # Review r3: prose inside the copy-paste block must be a comment.
        self.assertIn("    # or simply re-run this installer", proc.stderr)
        # Nothing touched.
        self.assertEqual((old_home / "deciders-ledger.jsonl").read_text(), "old-ledger")
        self.assertEqual((old_home_2 / "model-tiers.json").read_text(), "other-tiers")
        self.assertFalse((self.root / "dev-team").exists())


if __name__ == "__main__":
    unittest.main()
