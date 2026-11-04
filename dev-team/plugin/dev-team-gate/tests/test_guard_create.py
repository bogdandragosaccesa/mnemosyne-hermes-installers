"""M3: `kanban_create` may only assign a card to a prefixed dev-team profile.

A bare `assignee="qa"` makes the dispatcher spawn the unprefixed `qa` profile
-- another team's worker, or an unguarded root profile -- so the new card would
run with no dev-team gate. The refusal reads
`$HERMES_HOME/dev-team-prefix` (what the installer writes) and fails closed:
a missing, unreadable or empty prefix file refuses the create rather than
becoming a bypass.

The check is deliberately NOT gated on the creating card's stage, so a card
with no skills or with an unknown `dev-team-stage-*` skill cannot bypass it,
and the comparison is exact and case-sensitive.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_guard import HomeMixin  # noqa: E402

import guard as guard_mod  # noqa: E402

PREFIX = "dev-"


class _Base(HomeMixin, unittest.TestCase):
    def write_prefix(self, text=PREFIX + "\n"):
        path = Path(self.home) / "dev-team-prefix"
        path.write_text(text, encoding="utf-8")
        return path

    def create(self, **args):
        return guard_mod.check_create(args)

    def hook(self, **args):
        return guard_mod.pre_tool_call("kanban_create", args)


class AssigneePrefixTests(_Base):
    def test_prefixed_assignee_allowed(self):
        self.write_prefix()
        for role in ("qa", "reviewer", "devops", "backend-db"):
            with self.subTest(role=role):
                self.assertIsNone(self.create(title="t", assignee=PREFIX + role))

    def test_bare_role_name_refused(self):
        self.write_prefix()
        msg = self.create(title="t", assignee="qa")
        self.assertIsInstance(msg, str)
        self.assertEqual(msg, guard_mod._prefix_refusal("qa", PREFIX))
        self.assertTrue(msg.startswith("guard: kanban_create assignee 'qa' "), msg)
        self.assertIn("does not start with the installed team prefix 'dev-'", msg)
        self.assertIn("Use 'dev-qa'", msg)
        self.assertIn('kanban_block(kind="needs_input")', msg)

    def test_every_bare_role_name_refused(self):
        self.write_prefix()
        for role in ("pm", "architect", "uiux", "backend-db", "backend", "frontend",
                     "qa", "reviewer", "devops"):
            with self.subTest(role=role):
                self.assertIsInstance(self.create(title="t", assignee=role), str)

    def test_mis_cased_prefix_allowed(self):
        """Finding 4: Hermes lowercases an assignee before storing it
        (`profiles.normalize_profile_name`), so `Dev-qa` IS the installed
        `dev-qa`. Both sides of the comparison are normalised with
        `.strip().lower()`."""
        self.write_prefix()
        for assignee in ("Dev-qa", "DEV-qa", "dEv-qa"):
            with self.subTest(assignee=assignee):
                self.assertIsNone(self.create(title="t", assignee=assignee))

    def test_a_mis_cased_installed_prefix_accepts_its_own_cards(self):
        self.write_prefix("T2-\n")
        self.assertIsNone(self.create(title="t", assignee="t2-qa"))
        self.assertIsNone(self.create(title="t", assignee="T2-qa"))

    def test_other_team_prefix_refused(self):
        self.write_prefix()
        self.assertIsInstance(self.create(title="t", assignee="t2-qa"), str)

    def test_prefix_as_a_substring_is_not_enough(self):
        self.write_prefix()
        self.assertIsInstance(self.create(title="t", assignee="qa-dev-"), str)

    def test_missing_or_empty_assignee_refused(self):
        self.write_prefix()
        for assignee in (None, "", "   ", 7, ["dev-qa"]):
            with self.subTest(assignee=assignee):
                self.assertIsInstance(self.create(title="t", assignee=assignee), str)
        self.assertIsInstance(self.create(title="t"), str)  # key absent entirely

    def test_trailing_whitespace_in_the_prefix_file_is_stripped(self):
        for text in ("dev-\n", "dev-", "dev-\r\n", "  dev-  \n"):
            with self.subTest(text=text):
                self.write_prefix(text)
                self.assertIsNone(self.create(title="t", assignee="dev-qa"))

    def test_a_different_installed_prefix_is_honoured(self):
        self.write_prefix("t2-\n")
        self.assertIsNone(self.create(title="t", assignee="t2-qa"))
        self.assertEqual(self.create(title="t", assignee="dev-qa"),
                         guard_mod._prefix_refusal("dev-qa", "t2-"))


class PrefixFileFailClosedTests(_Base):
    """A missing prefix file must never become a bypass."""

    def _assert_unreadable(self, msg):
        path = Path(self.home) / "dev-team-prefix"
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith(
            f"guard: cannot read the installed team prefix from {path}, "), msg)
        self.assertIn("kanban_create cannot be verified; refusing (", msg)
        self.assertIn('kanban_block(kind="needs_input")', msg)

    def test_missing_prefix_file_refuses(self):
        self._assert_unreadable(self.create(title="t", assignee="dev-qa"))

    def test_empty_prefix_file_refuses(self):
        self.write_prefix("")
        self._assert_unreadable(self.create(title="t", assignee="dev-qa"))

    def test_whitespace_only_prefix_file_refuses(self):
        self.write_prefix("   \n\t\n")
        self._assert_unreadable(self.create(title="t", assignee="dev-qa"))

    def test_directory_in_place_of_the_prefix_file_refuses(self):
        (Path(self.home) / "dev-team-prefix").mkdir()
        self._assert_unreadable(self.create(title="t", assignee="dev-qa"))

    def test_unreadable_prefix_file_refuses(self):
        path = self.write_prefix()
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        if os.access(path, os.R_OK):  # running as root: the mode is advisory
            self.skipTest("cannot make a file unreadable as this user")
        self._assert_unreadable(self.create(title="t", assignee="dev-qa"))

    def test_the_prefix_file_is_refused_before_the_assignee_is_judged(self):
        """Order matters: with no prefix there is nothing to compare against,
        so even an obviously-bad assignee gets the fail-closed message."""
        self._assert_unreadable(self.create(title="t", assignee="qa"))


class CreateNotBypassableTests(_Base):
    """The check runs for every `kanban_create`, whatever card makes it."""

    def test_refused_whatever_the_creating_card_is(self):
        self.write_prefix()
        for task_id in (None, "", "t_misc", "t_noskills", "t_unknown_stage", "t_nosuch"):
            with self.subTest(task_id=task_id):
                env = {} if task_id is None else {"HERMES_KANBAN_TASK": task_id}
                with mock.patch.dict(os.environ, env):
                    if task_id is None:
                        os.environ.pop("HERMES_KANBAN_TASK", None)
                    blocked = self.hook(title="t", assignee="qa")
                    allowed = self.hook(title="t", assignee="dev-qa")
                self.assertEqual(blocked, {"action": "block",
                                           "message": guard_mod._prefix_refusal("qa", PREFIX)})
                self.assertIsNone(allowed)

    def test_check_create_reads_no_board(self):
        """It must not touch the board: a card with no skills or an unknown
        stage skill has nothing the guard could consult, and a board read would
        also be a new failure mode on the create path."""
        self.write_prefix()
        with mock.patch.object(guard_mod, "_make_board",
                               side_effect=AssertionError("board was read")):
            self.assertIsNone(self.create(title="t", assignee="dev-qa"))
            self.assertIsInstance(self.create(title="t", assignee="qa"), str)


class PreToolCallRoutingTests(_Base):
    def test_hook_blocks_with_the_refusal_message(self):
        self.write_prefix()
        self.assertEqual(
            self.hook(title="t", assignee="qa"),
            {"action": "block", "message": guard_mod._prefix_refusal("qa", PREFIX)},
        )

    def test_hook_passes_a_good_create_through(self):
        self.write_prefix()
        self.assertIsNone(self.hook(title="t", assignee="dev-qa", parents=["t_impl1"]))

    def test_hook_fails_closed_on_a_non_dict_args(self):
        self.write_prefix()
        self.assertEqual(guard_mod.pre_tool_call("kanban_create", None),
                         {"action": "block", "message": guard_mod._prefix_refusal("", PREFIX)})

    def test_other_tools_still_pass_untouched(self):
        self.write_prefix()
        for tool in ("kanban_block", "kanban_comment", "kanban_show", "kanban_request_review",
                     "write_file", "read_file"):
            with self.subTest(tool=tool):
                self.assertIsNone(guard_mod.pre_tool_call(tool, {"assignee": "qa"}))


if __name__ == "__main__":
    unittest.main()
