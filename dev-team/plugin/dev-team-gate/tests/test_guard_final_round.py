"""PR #5 final fix round: N1 (side-door parser), N4 (curl), N5 (over-blocking),
N7 (pr-ci requires an open PR)."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_guard import MERGE, FakeBoard, HomeMixin, guarded_board, terminal  # noqa: E402
from test_guard_forge import PR_URL, PrSpy, green, pr_ci_board  # noqa: E402

import guard as guard_mod  # noqa: E402
from guard import check_complete  # noqa: E402

CALLERS = (None, "t_misc", "t_impl1")


class _Base(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.b = guarded_board("main", home=self.home)

    def refused(self, cmd, label="guard:"):
        for caller in CALLERS:
            with self.subTest(caller=caller, cmd=cmd):
                msg = terminal(self.b, caller, cmd)
                self.assertIsInstance(msg, str, f"allowed: {cmd!r} from {caller}")
                if label:
                    self.assertTrue(msg.startswith(label), msg)

    def allowed(self, cmd):
        for caller in CALLERS:
            with self.subTest(caller=caller, cmd=cmd):
                self.assertIsNone(terminal(self.b, caller, cmd))


# ---------------------------------------------------------------------------
# N1: the side-door parser must match Hermes' own argparse, check every
# positional, and cover the card-moving verbs the round-2 review added.
# ---------------------------------------------------------------------------
class SideDoorParserTests(_Base):
    def test_abbreviated_option_target_refused(self):
        # argparse expands --ti to --title, which consumes "t_misc" as its
        # value; the real target is the second positional, t_impl1.
        self.refused("hermes kanban edit --ti t_misc t_impl1")

    @unittest.skipUnless(guard_mod._hermes_kanban_parser(),
                         "hermes_cli not importable; the fallback parser refuses abbreviations")
    def test_abbreviated_option_target_still_allowed_when_real_target_unguarded(self):
        # With Hermes' own parser, --ti expands to --title and consumes
        # t_impl1 as its value, so the real target is the unguarded t_misc:
        # allowed, proving the guard reads the correct (second) positional.
        self.allowed("hermes kanban edit --ti t_impl1 t_misc")

    def test_fallback_parser_refuses_abbreviated_option(self):
        # Without hermes_cli (the CI case) the guard cannot know how Hermes
        # would expand an abbreviation, so it must refuse rather than guess.
        with mock.patch.object(guard_mod, "_hermes_kanban_parser", return_value=None):
            self.refused("hermes kanban edit --ti t_impl1 t_misc")

    def test_promote_refused_on_guarded_card(self):
        self.refused("hermes kanban promote t_impl1")

    def test_promote_refused_on_needs_input_card_even_when_unguarded(self):
        self.b.add("t_ask", None, extra_skills=["devops-onboarding"])
        self.b.views["t_ask"].status = "blocked"
        self.b.views["t_ask"].block_kind = "needs_input"
        self.refused("hermes kanban promote t_ask")

    def test_promote_allowed_on_an_ordinary_unguarded_card(self):
        self.b.add("t_ready", None, extra_skills=["devops-onboarding"])
        self.b.views["t_ready"].status = "todo"
        self.allowed("hermes kanban promote t_ready")

    def test_new_verbs_refused_on_guarded_card(self):
        for cmd in (
            "hermes kanban request-review t_impl1",
            "hermes kanban archive t_impl1",
            "hermes kanban assign t_impl1 dev-pm",
            "hermes kanban reassign t_impl1 dev-pm",
            "hermes kanban reclaim t_impl1",
            "hermes kanban link t_misc t_impl1",
            "hermes kanban unlink t_misc t_impl1",
        ):
            with self.subTest(cmd=cmd):
                self.refused(cmd)

    def test_link_unlink_check_both_positionals(self):
        self.refused("hermes kanban link t_impl1 t_misc")  # first positional guarded
        self.refused("hermes kanban unlink t_impl1 t_misc")

    def test_python_dash_m_hermes_cli_main_treated_as_hermes(self):
        self.refused("python3 -m hermes_cli.main kanban complete t_impl1")
        self.refused("python3 -m hermes_cli.main kanban promote t_impl1")

    def test_show_and_complete_on_unguarded_card_still_allowed(self):
        self.allowed("hermes kanban show t_impl1")
        self.allowed("hermes kanban complete t_misc")


# ---------------------------------------------------------------------------
# N4: curl short-option clusters, gh api /contents/, GraphQL
# createCommitOnBranch, gh alias set hiding `pr merge`.
# ---------------------------------------------------------------------------
class CurlAndApiTests(_Base):
    def test_curl_short_cluster_method_refused(self):
        self.refused("curl -sXPUT https://api.github.com/repos/o/r/pulls/5/merge", MERGE)

    def test_curl_short_cluster_data_refused(self):
        self.refused("curl -sd '{}' https://api.github.com/repos/o/r/pulls/5/merge", MERGE)

    def test_curl_short_cluster_still_allows_reads(self):
        self.allowed("curl -sL https://api.github.com/repos/o/r/pulls/5")

    def test_gh_api_contents_write_refused(self):
        self.refused(
            "gh api -X PUT repos/o/r/contents/README.md -f message=x -f content=y "
            "-f branch=main",
            MERGE,
        )

    def test_gh_api_graphql_create_commit_on_branch_refused(self):
        self.refused(
            "gh api graphql -f query='mutation{createCommitOnBranch(input:{}){clientMutationId}}'",
            MERGE,
        )

    def test_gh_alias_set_hiding_pr_merge_refused(self):
        self.refused("gh alias set m 'pr merge'", MERGE)

    def test_gh_alias_set_unrelated_body_allowed(self):
        self.allowed("gh alias set co 'pr checkout'")


# ---------------------------------------------------------------------------
# N5: protected-path patterns anchored to a path context, not a bare
# relative substring, so ordinary work on this repo is not over-blocked.
# ---------------------------------------------------------------------------
class OverBlockingTests(_Base):
    def test_commit_message_mentioning_dev_team_forge_allowed(self):
        self.allowed("git commit -m 'fix dev-team/forge parsing'")

    def test_diff_against_dev_team_forge_allowed(self):
        self.allowed("git diff -- dev-team/forge/")

    def test_unittest_discover_dev_team_forge_tests_allowed(self):
        self.allowed("python3 -m unittest discover -s dev-team/forge/tests")

    def test_git_add_dev_team_plugin_guard_allowed(self):
        self.allowed("git add dev-team/plugin/dev-team-gate/guard.py")

    def test_absolute_write_to_forge_wrapper_still_blocked(self):
        self.refused("printf x > ~/.local/bin/forge")

    def test_absolute_write_to_real_project_record_still_blocked(self):
        self.refused("echo '{}' > $HERMES_HOME/dev-team/projects/widgets.json")


# ---------------------------------------------------------------------------
# N7: a green pr-ci completion must also check the PR is open.
# ---------------------------------------------------------------------------
class PrMustBeOpenTests(unittest.TestCase):
    def setUp(self):
        self.b = pr_ci_board()

    def test_closed_pr_refused_even_when_everything_else_is_green(self):
        pr = PrSpy(dict(PrSpy().facts, state="closed"))

        def ci(slug, sha):
            return "green"

        msg = check_complete(self.b, "t_pr", green(), ci, pr)
        self.assertTrue(msg.startswith("pr:"), msg)
        self.assertIn("not-open", msg)

    def test_merged_pr_refused(self):
        pr = PrSpy(dict(PrSpy().facts, state="merged"))
        msg = check_complete(self.b, "t_pr", green(), lambda s, h: "green", pr)
        self.assertTrue(msg.startswith("pr:"), msg)

    def test_open_pr_still_passes(self):
        pr = PrSpy(dict(PrSpy().facts, state="open"))
        self.assertIsNone(check_complete(self.b, "t_pr", green(), lambda s, h: "green", pr))


if __name__ == "__main__":
    unittest.main()
