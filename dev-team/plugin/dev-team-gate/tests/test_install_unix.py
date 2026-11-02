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

DEV_TEAM = Path(__file__).resolve().parents[3]
INSTALLER = DEV_TEAM / "install-dev-team-unix.sh"
ROLES = ("pm", "architect", "uiux", "backend-db", "frontend", "qa", "reviewer", "devops")

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
    dir="$root"; [[ -n $profile ]] && dir="$root/profiles/$profile"
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
class UnixInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.root = self.tmp / "hermes"
        (self.root / "plugins" / "mnemosyne").mkdir(parents=True)
        (self.root / "mnemosyne" / "data").mkdir(parents=True)
        (self.root / "mnemosyne" / "data" / "mnemosyne.db").write_text("shared")
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

    def test_fresh_profiles_link_the_shared_mnemosyne_bank(self):  # item 10
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for role in ROLES:
            link = self.profile(role) / "mnemosyne"
            with self.subTest(role=role):
                self.assertTrue(link.is_symlink(), f"{link} is not a symlink")
                self.assertEqual(link.resolve(), (self.root / "mnemosyne").resolve())
        self.assertEqual((self.root / "mnemosyne" / "data" / "mnemosyne.db").read_text(),
                         "shared")

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

    def test_only_installs_the_named_roles(self):
        proc = self.install("--only", "qa,devops")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(p.name for p in (self.root / "profiles").iterdir()),
                         ["dev-devops", "dev-qa"])


if __name__ == "__main__":
    unittest.main()
