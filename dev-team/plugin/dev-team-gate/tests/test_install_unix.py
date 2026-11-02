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


class UnixInstallerTests(InstallerSetupMixin, unittest.TestCase):
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
        # Relinking the profile that was NOT chosen is named concretely.
        self.assertIn(f'ln -sfn "{old_home}" "{uiux_dir}/dev-team"', proc.stderr)
        self.assertIn(f'ln -sfn "{old_home_2}" "{pm_dir}/dev-team"', proc.stderr)
        # Nothing touched.
        self.assertEqual((old_home / "deciders-ledger.jsonl").read_text(), "old-ledger")
        self.assertEqual((old_home_2 / "model-tiers.json").read_text(), "other-tiers")
        self.assertFalse((self.root / "dev-team").exists())


if __name__ == "__main__":
    unittest.main()
