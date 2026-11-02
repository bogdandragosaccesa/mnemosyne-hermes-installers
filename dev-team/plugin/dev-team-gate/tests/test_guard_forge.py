"""PR #5 review fix round: #5 (a green pr-ci is tied to the PR) and #4 (the
guard trusts only the forge code next to the plugin, and refuses terminal
writes into it, the plugin and the project records)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_guard import (  # noqa: E402
    MERGE, PLUGIN_DIR, SHA, SLUG, FakeBoard, ForgeSpy, HomeMixin, chain, guarded_board,
    no_forge, terminal,
)

import guard as guard_mod  # noqa: E402
from guard import check_complete  # noqa: E402

PR_URL = "https://github.com/acme/w/pull/7"
OTHER = "0123456789abcdef0123456789abcdef01234567"
FORGE_DIR = PLUGIN_DIR.parent.parent / "forge"


def pr_ci_board():
    b = FakeBoard()
    chain(b)
    b.add("t_val", "validate", ["t_impl1"], md={"head_sha": SHA, "verdict": "pass", "evidence": []})
    b.add("t_rev", "review", ["t_val"], md={"head_sha": SHA, "verdict": "approve"})
    b.add("t_pr", "pr-ci", ["t_rev"])
    return b


class PrSpy:
    def __init__(self, facts=None, exc=None, log=None):
        self.facts = facts if facts is not None else {
            "pr": "7", "state": "open", "pr_url": PR_URL, "head_sha": SHA, "ready": True}
        self.exc, self.calls, self.log = exc, [], log

    def __call__(self, slug, pr_url):
        self.calls.append((slug, pr_url))
        if self.log is not None:
            self.log.append("pr")
        if self.exc:
            raise self.exc
        return self.facts


def green(**over):
    md = {"pr_url": PR_URL, "head_sha": SHA, "ci": "green"}
    md.update(over)
    return {"metadata": md}


# ---------------------------------------------------------------------------
# #5
# ---------------------------------------------------------------------------
class PrCiTiedToPrTests(unittest.TestCase):
    def setUp(self):
        self.b = pr_ci_board()

    def test_green_checks_project_then_head_then_ci(self):
        log = []
        pr = PrSpy(log=log)

        def ci(slug, sha):
            log.append("ci")
            return "green"

        self.assertIsNone(check_complete(self.b, "t_pr", green(), ci, pr))
        self.assertEqual(log, ["pr", "ci"])
        self.assertEqual(pr.calls, [(SLUG, PR_URL)])

    def test_green_pr_of_another_repository_refused_before_ci(self):
        pr = PrSpy(dict(PrSpy().facts, pr_url="https://github.com/acme/w/pull/7"))
        msg = check_complete(self.b, "t_pr", green(pr_url="https://github.com/evil/x/pull/7"),
                             no_forge, pr)
        self.assertTrue(msg.startswith("pr: not-in-project"), msg)

    def test_green_pr_url_compared_canonically(self):
        for claimed in ("https://GitHub.com/acme/w/pull/7/", "https://github.com/acme/w/pull/7"):
            with self.subTest(claimed=claimed):
                self.assertIsNone(check_complete(self.b, "t_pr", green(pr_url=claimed),
                                                 ForgeSpy("green"), PrSpy()))

    def test_green_pr_head_not_head_sha_refused_before_ci(self):
        pr = PrSpy(dict(PrSpy().facts, head_sha=OTHER))
        msg = check_complete(self.b, "t_pr", green(), no_forge, pr)
        self.assertTrue(msg.startswith("pr: head-mismatch"), msg)
        self.assertIn(OTHER, msg)

    def test_green_pr_not_ready_refused(self):
        pr = PrSpy(dict(PrSpy().facts, ready=False))
        msg = check_complete(self.b, "t_pr", green(), ForgeSpy("green"), pr)
        self.assertTrue(msg.startswith("pr: not-ready"), msg)

    def test_green_ci_red_on_the_pr_head_refused(self):
        msg = check_complete(self.b, "t_pr", green(), ForgeSpy("red"), PrSpy())
        self.assertTrue(msg.startswith("ci:"), msg)

    def test_green_pr_status_unreachable_refused(self):
        msg = check_complete(self.b, "t_pr", green(), no_forge,
                             PrSpy(exc=RuntimeError("gh: HTTP 502")))
        self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_green_pr_status_malformed_refused(self):
        for facts in ({}, {"pr_url": PR_URL}, {"pr_url": PR_URL, "head_sha": SHA}, "x"):
            with self.subTest(facts=facts):
                msg = check_complete(self.b, "t_pr", green(), no_forge, PrSpy(facts))
                self.assertIsInstance(msg, str)

    def test_green_without_pr_check_fails_closed(self):
        msg = check_complete(self.b, "t_pr", green(), ForgeSpy("green"))
        self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_red_claim_reads_no_forge(self):
        self.b.add("t_fix", "implement", ["t_pr"])
        md = green(ci="red", findings=["broken"])
        md["created_cards"] = ["t_fix"]

        def boom(*a):
            raise AssertionError("forge must not be consulted")

        self.assertIsNone(check_complete(self.b, "t_pr", md, boom, boom))


# ---------------------------------------------------------------------------
# #4 trusted forge: imported by path from the plugin's sibling forge/ dir
# ---------------------------------------------------------------------------
class FakeCi:
    name = "fake"

    def __init__(self, rec, state="green", sleep=0, exc=None, pr=None):
        self.rec, self.state, self.sleep, self.exc, self.pr = rec, state, sleep, exc, pr

    def ci_status(self, sha):
        if self.sleep:
            time.sleep(self.sleep)
        if self.exc:
            raise self.exc
        return self.state

    def pr_status(self, number):
        return self.pr or {"pr": number, "state": "open", "pr_url": PR_URL, "head_sha": SHA,
                           "ready": True}


class TrustedForgeTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        rec = Path(self.home) / "dev-team" / "projects" / f"{SLUG}.json"
        rec.parent.mkdir(parents=True)
        rec.write_text(json.dumps({
            "slug": SLUG, "repo_url": "https://github.com/acme/w.git", "pr_adapter": "fake",
            "ci_adapter": "fake", "default_branch": "main", "ci_hint": {}}))

    def reader(self, **kw):
        return guard_mod.ForgeReader(adapters={"fake": lambda rec: FakeCi(rec, **kw)})

    def test_forge_dir_is_the_plugins_sibling(self):
        self.assertEqual(guard_mod.FORGE_DIR, FORGE_DIR.resolve())
        self.assertTrue((guard_mod.FORGE_DIR / "forge_core.py").is_file())

    def test_forge_budget_fits_the_hook_timeout(self):
        self.assertEqual(guard_mod.FORGE_TIMEOUT_S, 20)
        self.assertLess(guard_mod.FORGE_TIMEOUT_S, 30)  # plugins.hook_callback_timeout

    def test_forge_missing_next_to_plugin_fails_closed(self):
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.object(guard_mod, "FORGE_DIR", Path(empty)), \
                mock.patch.object(guard_mod, "_FORGE", None):
            with self.assertRaises(FileNotFoundError):
                guard_mod.ForgeReader().ci_status(SLUG, SHA)
            msg = check_complete(pr_ci_board(), "t_pr", green(), guard_mod.forge_ci_status,
                                 guard_mod.forge_pr_status)
        self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_guard_never_looks_forge_up_on_path(self):
        source = (PLUGIN_DIR / "guard.py").read_text(encoding="utf-8")
        self.assertNotIn("shutil", source)
        self.assertNotIn("which(", source)

    def test_load_forge_imports_by_path_not_from_sys_path(self):
        with tempfile.TemporaryDirectory() as decoy:
            (Path(decoy) / "forge_core.py").write_text("raise RuntimeError('decoy imported')\n")
            (Path(decoy) / "adapters").mkdir()
            (Path(decoy) / "adapters" / "__init__.py").write_text("raise RuntimeError('decoy')\n")
            sys.path.insert(0, decoy)
            self.addCleanup(sys.path.remove, decoy)
            with mock.patch.object(guard_mod, "_FORGE", None):
                core, registry = guard_mod._load_forge()
        self.assertEqual(Path(core.__file__).resolve(), FORGE_DIR.resolve() / "forge_core.py")
        self.assertTrue({"github", "gitlab", "gitea", "jenkins"} <= set(registry))
        for cls in registry.values():
            self.assertEqual(Path(sys.modules[cls.__module__].__file__).resolve().parent,
                             FORGE_DIR.resolve() / "adapters")

    def test_ci_status_runs_in_process_without_a_subprocess(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("no CLI")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("no CLI")):
            self.assertEqual(self.reader().ci_status(SLUG, SHA), "green")
            self.assertEqual(self.reader().pr_status(SLUG, PR_URL)["head_sha"], SHA)

    def test_ci_status_negative_states_returned(self):
        for state in ("red", "pending", "none"):
            with self.subTest(state=state):
                self.assertEqual(self.reader(state=state).ci_status(SLUG, SHA), state)

    def test_ci_status_cannot_verify_raises(self):
        for kw in ({"state": "weird"}, {"exc": RuntimeError("gh: auth")}):
            with self.subTest(kw=kw):
                with self.assertRaises(Exception):
                    self.reader(**kw).ci_status(SLUG, SHA)
        with self.assertRaises(Exception):
            self.reader().ci_status("ghost", SHA)
        with self.assertRaises(Exception):
            self.reader().ci_status("../escape", SHA)

    def test_real_registry_used_by_default(self):
        reader = guard_mod.ForgeReader()
        with self.assertRaises(Exception) as ctx:  # "fake" is not a real adapter
            reader.ci_status(SLUG, SHA)
        self.assertIn("fake", str(ctx.exception))

    def test_forge_timeout_becomes_forge_unreachable(self):
        b = pr_ci_board()
        with mock.patch.object(guard_mod, "FORGE_TIMEOUT_S", 0.2):
            reader = self.reader(sleep=2)
            start = time.monotonic()
            msg = check_complete(b, "t_pr", green(), reader.ci_status, reader.pr_status)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_pre_tool_call_uses_the_trusted_reader(self):
        b = pr_ci_board()
        os.environ["HERMES_KANBAN_TASK"] = "t_pr"
        with mock.patch.object(guard_mod, "_make_board", lambda name: b), \
                mock.patch.object(guard_mod, "_adapter_registry",
                                  lambda: {"fake": lambda rec: FakeCi(rec, state="red")}):
            out = guard_mod.pre_tool_call("kanban_complete", green())
        self.assertTrue(out["message"].startswith("ci:"), out)


# ---------------------------------------------------------------------------
# #4 terminal writes into trusted paths
# ---------------------------------------------------------------------------
class ProtectedWriteTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.b = guarded_board("main", home=self.home)

    REFUSED = [
        "printf '#!/bin/sh\\necho \"{\\\"state\\\":\\\"green\\\"}\"\\n' > ~/.local/bin/forge",
        "cp /tmp/evil ~/.local/bin/forge",
        "ln -sf /tmp/evil $HOME/.local/bin/forge",
        "cat x | tee ~/.local/bin/forge",
        "echo '{}' > $HERMES_HOME/dev-team/projects/widgets.json",
        "echo '{}' >> ~/.hermes/dev-team/projects/widgets.json",
        "sed -i s/main/x/ ~/.hermes/dev-team/projects/widgets.json",
        "mv /tmp/w.json ~/.hermes/dev-team/projects/",
        "python3 -c \"open('/home/u/.hermes/dev-team/projects/w.json','w').write('{}')\"",
        "rm -rf ~/.hermes/profiles/dev-qa/plugins/dev-team-gate",
        "cd ~/.hermes/dev-team/projects && echo {} > w.json",
        "set-content C:\\Users\\u\\AppData\\Local\\hermes\\forge.cmd x",
        "forge detect --repo-dir ~/.hermes/dev-team/repos/w --slug w --force",
        "hermes plugins disable dev-team-gate",
        "hermes -p dev-qa plugins remove dev-team-gate",
        "hermes config set plugins.enabled []",
    ]
    ALLOWED = [
        "cat ~/.hermes/dev-team/projects/widgets.json",
        "ls $HERMES_HOME/dev-team/projects",
        "jq . ~/.hermes/dev-team/projects/widgets.json",
        "cat ~/.hermes/dev-team/projects/widgets.json > /tmp/copy.json",
        "forge detect --repo-dir ~/.hermes/dev-team/repos/w --slug w",
        "forge ci-status --slug w --sha abc1234",
        "echo hi > out.txt",
        "make test > \"$LOG\" 2>&1",
        "make 2>&1 | tee build.log",
        "hermes plugins list",
    ]

    def test_writes_into_trusted_paths_refused(self):
        extra = [f"cp x.py {PLUGIN_DIR}/guard.py", f"echo x >> {FORGE_DIR}/forge_core.py",
                 f"git -C {FORGE_DIR} checkout -- ."]
        for cmd in self.REFUSED + extra:
            for caller in (None, "t_misc", "t_impl1"):
                with self.subTest(cmd=cmd, caller=caller):
                    msg = terminal(self.b, caller, cmd)
                    self.assertIsInstance(msg, str, f"allowed: {cmd}")
                    self.assertTrue(msg.startswith("guard:"), msg)

    def test_reads_of_trusted_paths_allowed(self):
        for cmd in self.ALLOWED:
            for caller in (None, "t_misc", "t_impl1"):
                with self.subTest(cmd=cmd, caller=caller):
                    self.assertIsNone(terminal(self.b, caller, cmd))


if __name__ == "__main__":
    unittest.main()
