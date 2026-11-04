"""Findings 2, 3, 4 and 5 (guard half) of the PR #15 independent review.

* **Finding 2** -- the prefix rule only covered the `kanban_create` TOOL.
  `hermes kanban create --assignee`, and the assignee argument of
  `hermes kanban assign` / `reassign`, were side doors: `create` was absent
  from `_SIDE_DOOR_VERBS` entirely, and `assign`/`reassign` refused only when
  the *target card* carried a stage skill -- so assigning an unprefixed,
  unguarded profile to any ordinary card went through.
* **Finding 3** -- `dev-team-prefix` was not a protected path, so a worker
  could rewrite the very file the prefix check reads. The unanchored variant
  is the right one: the file lives under `$HERMES_HOME`, not inside a repo.
* **Finding 4** -- the comparison was exact/case-sensitive, which refused an
  upper-case `--prefix` install's own cards. Hermes lowercases assignees
  before storing them, so both sides are normalised with `.strip().lower()`,
  matching `profiles.normalize_profile_name`.
* **Finding 5** -- `_describe(exc)` text reaches the agent verbatim; it is now
  routed through `forge_core.redact`.

Both `hermes kanban` parse paths are exercised: the real `hermes_cli` argparse
tree when it can be imported, and the strict fallback allowlist (forced by
pinning `_KANBAN_ARGPARSER` to False), which is what CI runs.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_guard import HomeMixin, guarded_board, terminal  # noqa: E402

import guard as guard_mod  # noqa: E402

PREFIX = "dev-"
PREFIX_FILE = "dev-team-prefix"
# Cards on `guarded_board`: a stage card and a card with no stage skill. The
# prefix rule must hold for BOTH -- that is the whole of finding 2.
CARDS = ("t_impl1", "t_misc")


class _Base(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.b = guarded_board("main", home=self.home)
        self.write_prefix()

    def write_prefix(self, text=PREFIX + "\n"):
        path = Path(self.home) / PREFIX_FILE
        path.write_text(text, encoding="utf-8")
        return path

    def refused(self, cmd):
        msg = terminal(self.b, "t_impl1", cmd)
        self.assertIsInstance(msg, str, f"allowed: {cmd!r}")
        return msg

    def allowed(self, cmd):
        self.assertIsNone(terminal(self.b, "t_impl1", cmd), cmd)


class _BothParsers(_Base):
    """Run each assertion under the real parser and under the fallback."""

    def both(self, check, cmd):
        with self.subTest(parser="fallback"):
            with mock.patch.object(guard_mod, "_KANBAN_ARGPARSER", False):
                check(cmd)
        with self.subTest(parser="real"):
            with mock.patch.object(guard_mod, "_KANBAN_ARGPARSER", None):
                if guard_mod._hermes_kanban_parser() is None:
                    self.skipTest("hermes_cli is not importable here")
                check(cmd)

    def refuses(self, cmd):
        self.both(self.refused, cmd)

    def allows(self, cmd):
        self.both(self.allowed, cmd)


# ---------------------------------------------------------------------------
# Finding 2: the terminal assignee paths
# ---------------------------------------------------------------------------
class TerminalCreateAssigneeTests(_BothParsers):
    def test_bare_role_is_refused(self):
        for role in ("qa", "reviewer", "devops", "backend-db", "pm"):
            with self.subTest(role=role):
                self.refuses(f"hermes kanban create 'card' --assignee {role}")

    def test_another_teams_prefix_is_refused(self):
        self.refuses("hermes kanban create 'card' --assignee t2-qa")

    def test_a_prefixed_assignee_is_allowed(self):
        self.allows("hermes kanban create 'card' --assignee dev-qa")

    def test_the_equals_form_is_covered_too(self):
        self.refuses("hermes kanban create 'card' --assignee=qa")
        self.allows("hermes kanban create 'card' --assignee=dev-qa")

    def test_a_create_with_no_assignee_is_refused(self):
        """Fail closed, exactly as the `kanban_create` tool path does: an
        unassigned card is one `hermes kanban assign` away from a bare role."""
        self.refuses("hermes kanban create 'card'")

    def test_an_empty_assignee_is_refused(self):
        self.refuses("hermes kanban create 'card' --assignee ''")

    def test_the_refusal_names_the_terminal_surface(self):
        msg = self.refused("hermes kanban create 'card' --assignee qa")
        self.assertIn("hermes kanban create", msg)
        self.assertIn("dev-qa", msg)
        self.assertIn('kanban_block(kind="needs_input")', msg)

    def test_a_board_create_is_not_a_card_create(self):
        """`hermes kanban boards create <slug>` makes a board, not a card."""
        self.allows("hermes kanban boards create some-board")

    def test_other_verbs_are_untouched(self):
        self.allows("hermes kanban list --assignee qa")
        self.allows("hermes kanban show t_impl1")


class TerminalAssignAssigneeTests(_BothParsers):
    def test_bare_role_refused_on_every_card_including_non_stage(self):
        for card in CARDS:
            for verb in ("assign", "reassign"):
                with self.subTest(card=card, verb=verb):
                    self.refuses(f"hermes kanban {verb} {card} qa")

    def test_prefixed_assignee_allowed_on_a_non_stage_card(self):
        self.allows("hermes kanban assign t_misc dev-qa")
        self.allows("hermes kanban reassign t_misc dev-qa")

    def test_a_stage_card_is_still_refused_whatever_the_assignee(self):
        """The pre-existing stage-card rule is unchanged: finding 2 adds to it."""
        msg = self.refused("hermes kanban assign t_impl1 dev-qa")
        self.assertIn("dev-team card", msg)

    def test_unassigning_is_allowed(self):
        """`none` is the CLI's unassign sentinel, not a profile name."""
        for value in ("none", "NONE", " none "):
            with self.subTest(value=value):
                self.allows(f"hermes kanban assign t_misc '{value}'")

    def test_reassign_with_its_reclaim_flag(self):
        self.refuses("hermes kanban reassign t_misc qa --reclaim")
        self.allows("hermes kanban reassign t_misc dev-qa --reclaim")

    def test_the_refusal_names_the_verb(self):
        msg = self.refused("hermes kanban assign t_misc qa")
        self.assertIn("hermes kanban assign", msg)


class TerminalAssigneeFailClosedTests(_BothParsers):
    def test_a_missing_prefix_file_refuses_rather_than_passing(self):
        (Path(self.home) / PREFIX_FILE).unlink()
        self.refuses("hermes kanban create 'card' --assignee dev-qa")
        self.refuses("hermes kanban assign t_misc dev-qa")

    def test_an_empty_prefix_file_refuses(self):
        self.write_prefix("")
        self.refuses("hermes kanban create 'card' --assignee dev-qa")

    def test_python_m_entry_point_is_covered(self):
        self.refuses("python3 -m hermes_cli.main kanban create 'c' --assignee qa")


# ---------------------------------------------------------------------------
# Finding 3: the prefix file is a protected path
# ---------------------------------------------------------------------------
class PrefixFileIsProtectedTests(_Base):
    def test_the_path_is_recognised(self):
        for token in (f"{self.home}/{PREFIX_FILE}",
                      f"~/.hermes/{PREFIX_FILE}",
                      f"$HERMES_HOME/{PREFIX_FILE}",
                      f"profiles/dev-qa/{PREFIX_FILE}",
                      PREFIX_FILE):
            with self.subTest(token=token):
                self.assertTrue(guard_mod._protected_path(token), token)

    def test_the_pattern_is_the_unanchored_one(self):
        """The file lives under `$HERMES_HOME`, not inside a repo, so a bare
        relative word must match too -- the anchored variant would not."""
        self.assertTrue(any(p.search(PREFIX_FILE) for p in guard_mod._PROTECTED_PATHS),
                        f"{PREFIX_FILE} is not in _PROTECTED_PATHS")

    def test_writes_are_refused(self):
        for cmd in (f"echo t2- > ~/.hermes/{PREFIX_FILE}",
                    f"printf 't2-' > {self.home}/{PREFIX_FILE}",
                    f"rm ~/.hermes/{PREFIX_FILE}",
                    f"cp /tmp/x ~/.hermes/{PREFIX_FILE}",
                    f"mv /tmp/x {self.home}/{PREFIX_FILE}",
                    f"tee ~/.hermes/{PREFIX_FILE}",
                    f"truncate -s 0 ~/.hermes/{PREFIX_FILE}"):
            with self.subTest(cmd=cmd):
                msg = self.refused(cmd)
                self.assertIn("the guard trusts", msg)

    def test_reads_are_still_allowed(self):
        for cmd in (f"cat ~/.hermes/{PREFIX_FILE}",
                    f"grep -c . {self.home}/{PREFIX_FILE}",
                    f"test -f ~/.hermes/{PREFIX_FILE}"):
            with self.subTest(cmd=cmd):
                self.allowed(cmd)

    def test_an_unrelated_similar_name_is_not_protected(self):
        self.assertFalse(guard_mod._protected_path("dev-team-prefixes.md"))
        self.assertFalse(guard_mod._protected_path("my-dev-team-prefix-notes"))


# ---------------------------------------------------------------------------
# Finding 4: case normalisation on both sides
# ---------------------------------------------------------------------------
class PrefixCaseNormalisationTests(_Base):
    def test_a_mis_cased_assignee_is_accepted(self):
        """Hermes lowercases an assignee before storing it, so `Dev-qa` IS the
        installed `dev-qa`. The old claim that it named a different profile
        directory was false."""
        for assignee in ("Dev-qa", "DEV-qa", "dEv-qa", "DEV-QA"):
            with self.subTest(assignee=assignee):
                self.assertIsNone(guard_mod.check_create(
                    {"title": "t", "assignee": assignee}))

    def test_an_upper_case_installed_prefix_accepts_its_own_cards(self):
        self.write_prefix("T2-\n")
        for assignee in ("T2-qa", "t2-qa", "T2-QA"):
            with self.subTest(assignee=assignee):
                self.assertIsNone(guard_mod.check_create(
                    {"title": "t", "assignee": assignee}))
        self.assertIsInstance(guard_mod.check_create(
            {"title": "t", "assignee": "dev-qa"}), str)

    def test_surrounding_whitespace_is_stripped_on_the_assignee(self):
        self.assertIsNone(guard_mod.check_create({"title": "t", "assignee": "  dev-qa  "}))

    def test_a_genuinely_different_prefix_is_still_refused(self):
        for assignee in ("qa", "t2-qa", "QA", "qa-dev-"):
            with self.subTest(assignee=assignee):
                self.assertIsInstance(guard_mod.check_create(
                    {"title": "t", "assignee": assignee}), str)

    def test_the_docstring_no_longer_claims_the_comparison_is_case_sensitive(self):
        doc = guard_mod.check_create.__doc__ or ""
        self.assertNotIn("case-sensitive", doc)
        self.assertNotIn("harmonize", doc)
        self.assertNotIn("names a profile that is not the installed one", doc)

    def test_the_changelog_no_longer_makes_the_false_claim(self):
        text = (Path(__file__).resolve().parents[4] / "CHANGELOG.md").read_text(
            encoding="utf-8")
        unreleased = text.split("## [1.3.2]")[0]
        claim = "names a profile directory that is not the installed one"
        self.assertNotIn(claim, unreleased,
                         f"CHANGELOG [Unreleased] still claims: {claim}")


# ---------------------------------------------------------------------------
# Finding 5 (guard half): _describe is redacted
# ---------------------------------------------------------------------------
class DescribeIsRedactedTests(_Base):
    def test_a_token_shape_in_an_exception_is_redacted(self):
        text = guard_mod._describe(
            RuntimeError("failed to authenticate with ghp_AAAABBBBCCCCDDDDEEEEFFFF"))
        self.assertNotIn("ghp_AAAABBBBCCCCDDDDEEEEFFFF", text)
        self.assertIn("RuntimeError", text)

    def test_a_literal_env_token_value_is_redacted(self):
        secret = "not-a-token-shape-at-all-123456"
        with mock.patch.dict(os.environ, {"DEV_TEAM_GH_TOKEN": secret}):
            text = guard_mod._describe(RuntimeError(f"git failed: {secret}"))
        self.assertNotIn(secret, text)

    def test_the_refusal_an_agent_sees_is_redacted(self):
        self.b.fail = RuntimeError("boom ghp_AAAABBBBCCCCDDDDEEEEFFFF")
        msg = terminal(self.b, "t_impl1", "hermes kanban complete t_misc")
        self.assertIsInstance(msg, str)
        self.assertNotIn("ghp_AAAABBBBCCCCDDDDEEEEFFFF", str(msg))

    def test_an_ordinary_description_is_untouched(self):
        self.assertEqual(guard_mod._describe(ValueError("the file is empty")),
                         "ValueError: the file is empty")

    def test_it_is_still_truncated(self):
        text = guard_mod._describe(ValueError("x" * 1000))
        self.assertLessEqual(len(text), guard_mod._DETAIL_MAX)


if __name__ == "__main__":
    unittest.main()
