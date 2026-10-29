"""Tests for guard.check_complete and board helpers, against an in-memory FakeBoard.

No test imports Hermes: KanbanBoard imports it lazily and is never built here.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from board import TaskView, implement_rounds, project_slug  # noqa: E402
from guard import check_complete  # noqa: E402

SHA = "9f4cbf1a2b3c4d5e6f708192a3b4c5d6e7f80912"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"
SLUG = "widgets"


def skill(stage):
    return [f"dev-team-stage-{stage}"]


class FakeBoard:
    """Board protocol over dicts. `fail` makes every call raise."""

    def __init__(self):
        self.views = {}
        self.metadata = {}
        self.fail: Exception | None = None

    def add(self, id, stage=None, parents=(), md=None, extra_skills=()):
        skills = list(extra_skills) + (skill(stage) if stage else [])
        self.views[id] = TaskView(id=id, skills=skills, parents=list(parents), children=[])
        for p in parents:
            if p in self.views:
                self.views[p].children.append(id)
        if md is not None:
            self.metadata[id] = md
        return id

    def task(self, id):
        if self.fail:
            raise self.fail
        return self.views.get(id)

    def last_metadata(self, id):
        if self.fail:
            raise self.fail
        return self.metadata.get(id)


def no_forge(slug, sha):
    raise AssertionError("forge must not be consulted")


class ForgeSpy:
    def __init__(self, result: object = "green", exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def __call__(self, slug, sha):
        self.calls.append((slug, sha))
        if self.exc:
            raise self.exc
        return self.result


def chain(board, rounds=1):
    """intake -> plan -> implement -> (validate -> implement)*(rounds-1).

    Returns the id of the last implement card.
    """
    board.add("t_intake", "intake", md={
        "definition_of_done": ["done"], "budget": {"amount": 10, "unit": "usd"},
        "repo": SLUG,
    })
    board.add("t_plan", "plan", ["t_intake"], md={"cards": ["t_impl1"]})
    board.add("t_impl1", "implement", ["t_plan"], md=impl_md())
    last = "t_impl1"
    for n in range(2, rounds + 1):
        board.add(f"t_val{n}", "validate", [last], md={
            "head_sha": SHA, "verdict": "fail", "evidence": [], "findings": ["x"]})
        last = board.add(f"t_impl{n}", "implement", [f"t_val{n}"], md=impl_md())
    return last


def impl_md(sha=SHA):
    return {
        "branch": "feature/w", "head_sha": sha, "changed_files": ["a.py"],
        "tests_run": [{"command": "make test", "exit_code": 0}],
    }


def complete_args(md, created=None):
    args = {"metadata": md}
    if created is not None:
        args["created_cards"] = created
    return args


class UnguardedTests(unittest.TestCase):
    def test_unguarded_card_passes(self):
        b = FakeBoard()
        b.add("t_misc", None, extra_skills=["github-code-review"])
        b.add("t_bare", None)
        for tid in ("t_misc", "t_bare"):
            for args in ({}, {"metadata": None}, {"metadata": {"anything": 1}}):
                with self.subTest(tid=tid, args=args):
                    self.assertIsNone(check_complete(b, tid, args, no_forge))

    def test_unknown_task_fails_closed(self):
        msg = check_complete(FakeBoard(), "t_nope", {}, no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("t_nope", msg)

    def test_board_exception_fails_closed(self):
        b = FakeBoard()
        chain(b)
        b.fail = OSError("database is locked")
        msg = check_complete(b, "t_impl1", complete_args(impl_md()), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("database is locked", msg)

    def test_malformed_stage_skill_fails_closed(self):
        b = FakeBoard()
        b.views["t_bad"] = TaskView("t_bad", ["dev-team-stage-deploy"], [], [])
        self.assertIsInstance(check_complete(b, "t_bad", {}, no_forge), str)


class MetadataTests(unittest.TestCase):
    def test_invalid_metadata_blocks_with_field_name(self):
        b = FakeBoard()
        chain(b)
        md = impl_md()
        del md["changed_files"]
        msg = check_complete(b, "t_impl1", complete_args(md, []), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("changed_files", msg)

    def test_missing_metadata_blocks(self):
        b = FakeBoard()
        chain(b)
        self.assertIsInstance(check_complete(b, "t_impl1", {}, no_forge), str)


class HeadShaTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBoard()
        chain(self.b)

    def test_head_sha_mismatch_blocks(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.b.add("t_rev", "review", ["t_val"])
        md = {"head_sha": OTHER_SHA, "verdict": "pass", "evidence": ["ok"]}
        msg = check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("head_sha", msg)

    def test_head_sha_match_passes_validate(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.b.add("t_rev", "review", ["t_val"])
        md = {"head_sha": SHA, "verdict": "pass", "evidence": ["ok"]}
        self.assertIsNone(
            check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge))

    def test_head_sha_checked_for_review_and_pr_ci(self):
        self.b.add("t_val", "validate", ["t_impl1"],
                   md={"head_sha": SHA, "verdict": "pass", "evidence": []})
        self.b.add("t_rev", "review", ["t_val"],
                   md={"head_sha": SHA, "verdict": "approve"})
        self.b.add("t_pr", "pr-ci", ["t_rev"])
        self.b.add("t_pr_child", "pr-ci", ["t_rev"])
        cases = [
            ("t_rev", {"head_sha": OTHER_SHA, "verdict": "approve"}, ["t_pr_child"]),
            ("t_pr", {"pr_url": "https://x/pr/1", "head_sha": OTHER_SHA, "ci": "green"}, []),
        ]
        for tid, md, created in cases:
            with self.subTest(tid=tid):
                msg = check_complete(self.b, tid, complete_args(md, created), ForgeSpy())
                self.assertIsInstance(msg, str)
                self.assertIn("head_sha", msg)

    def test_parent_without_metadata_blocks(self):
        self.b.metadata.pop("t_impl1")
        self.b.add("t_val", "validate", ["t_impl1"])
        self.b.add("t_rev", "review", ["t_val"])
        md = {"head_sha": SHA, "verdict": "pass", "evidence": []}
        self.assertIsInstance(
            check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge), str)

    def test_no_parent_stage_card_blocks(self):
        self.b.add("t_val", "validate", ["t_plan"])
        self.b.add("t_rev", "review", ["t_val"])
        md = {"head_sha": SHA, "verdict": "pass", "evidence": []}
        msg = check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("found 0", msg)

    def test_two_parent_stage_cards_block(self):
        self.b.add("t_impl_b", "implement", ["t_plan"], md=impl_md())
        self.b.add("t_val", "validate", ["t_impl1", "t_impl_b"])
        self.b.add("t_rev", "review", ["t_val"])
        md = {"head_sha": SHA, "verdict": "pass", "evidence": []}
        msg = check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge)
        self.assertIn("found 2", msg)


class ChildTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBoard()
        chain(self.b)

    def test_missing_child_blocks(self):
        for created in (None, []):
            with self.subTest(created=created):
                msg = check_complete(
                    self.b, "t_impl1", complete_args(impl_md(), created), no_forge)
                self.assertIsInstance(msg, str)
                self.assertIn("validate", msg)

    def test_wrong_child_stage_blocks(self):
        self.b.add("t_rev", "review", ["t_impl1"])
        msg = check_complete(self.b, "t_impl1", complete_args(impl_md(), ["t_rev"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("validate", msg)

    def test_child_must_list_task_as_parent(self):
        self.b.add("t_val", "validate", ["t_plan"])
        msg = check_complete(self.b, "t_impl1", complete_args(impl_md(), ["t_val"]), no_forge)
        self.assertIsInstance(msg, str)

    def test_two_children_of_expected_stage_block(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.b.add("t_val2", "validate", ["t_impl1"])
        msg = check_complete(
            self.b, "t_impl1", complete_args(impl_md(), ["t_val", "t_val2"]), no_forge)
        self.assertIsInstance(msg, str)

    def test_unknown_created_card_blocks(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        msg = check_complete(
            self.b, "t_impl1", complete_args(impl_md(), ["t_val", "t_ghost"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("t_ghost", msg)

    def test_created_cards_not_a_list_blocks(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.assertIsInstance(
            check_complete(self.b, "t_impl1", complete_args(impl_md(), "t_val"), no_forge), str)

    def test_right_child_passes(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.assertIsNone(
            check_complete(self.b, "t_impl1", complete_args(impl_md(), ["t_val"]), no_forge))

    def test_rework_child_after_validate_fail(self):
        self.b.add("t_val", "validate", ["t_impl1"])
        self.b.add("t_rev", "review", ["t_val"])
        self.b.add("t_fix", "implement", ["t_val"])
        md = {"head_sha": SHA, "verdict": "fail", "evidence": [], "findings": ["bug"]}
        self.assertIsInstance(
            check_complete(self.b, "t_val", complete_args(md, ["t_rev"]), no_forge), str)
        self.assertIsNone(
            check_complete(self.b, "t_val", complete_args(md, ["t_fix"]), no_forge))


class PlanTests(unittest.TestCase):
    def test_plan_cards_must_exist_with_stage_skill(self):
        b = FakeBoard()
        b.add("t_intake", "intake")
        b.add("t_plan", "plan", ["t_intake"])
        b.add("t_impl", "implement", ["t_plan"])
        b.add("t_plain", None, ["t_plan"])
        cases = [(["t_impl", "t_ghost"], "t_ghost"), (["t_impl", "t_plain"], "t_plain")]
        for cards, bad in cases:
            with self.subTest(bad=bad):
                msg = check_complete(
                    b, "t_plan", complete_args({"cards": cards}, cards), no_forge)
                self.assertIsInstance(msg, str)
                self.assertIn(bad, msg)
        self.assertIsNone(check_complete(
            b, "t_plan", complete_args({"cards": ["t_impl"]}, ["t_impl"]), no_forge))


class RoundTests(unittest.TestCase):
    def test_implement_rounds_counts_ancestry_including_self(self):
        b = FakeBoard()
        last = chain(b, rounds=3)
        self.assertEqual(implement_rounds(b, last), 3)
        self.assertEqual(implement_rounds(b, "t_impl1"), 1)
        self.assertEqual(implement_rounds(b, "t_plan"), 0)

    def test_implement_rounds_counts_shared_ancestor_once(self):
        b = FakeBoard()
        b.add("t_i1", "implement")
        b.add("t_a", "validate", ["t_i1"])
        b.add("t_b", "review", ["t_i1"])
        b.add("t_i2", "implement", ["t_a", "t_b"])
        self.assertEqual(implement_rounds(b, "t_i2"), 2)

    def test_implement_rounds_missing_ancestor_raises(self):
        b = FakeBoard()
        b.add("t_i2", "implement", ["t_gone"])
        with self.assertRaises(LookupError):
            implement_rounds(b, "t_i2")

    def test_fourth_round_refused_block_allowed(self):
        # Third implement card exists; QA fails it and creates a fourth.
        b = FakeBoard()
        third = chain(b, rounds=3)
        b.add("t_val4", "validate", [third])
        b.add("t_impl4", "implement", ["t_val4"])
        md = {"head_sha": SHA, "verdict": "fail", "evidence": [], "findings": ["still broken"]}
        msg = check_complete(b, "t_val4", complete_args(md, ["t_impl4"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("review: rounds-exhausted"), msg)
        # The third round itself was allowed. (kanban_block is never guarded: Task 6.)
        b2 = FakeBoard()
        second = chain(b2, rounds=2)
        b2.add("t_val3", "validate", [second])
        b2.add("t_impl3", "implement", ["t_val3"])
        self.assertIsNone(check_complete(b2, "t_val3", complete_args(md, ["t_impl3"]), no_forge))


class PrCiTests(unittest.TestCase):
    def setUp(self):
        self.b = FakeBoard()
        chain(self.b)
        self.b.add("t_val", "validate", ["t_impl1"],
                   md={"head_sha": SHA, "verdict": "pass", "evidence": []})
        self.b.add("t_rev", "review", ["t_val"],
                   md={"head_sha": SHA, "verdict": "approve"})
        self.b.add("t_pr", "pr-ci", ["t_rev"])
        self.green = {"pr_url": "https://github.com/acme/w/pull/7", "head_sha": SHA,
                      "ci": "green"}

    def test_pr_ci_green_with_no_child_passes(self):
        forge = ForgeSpy("green")
        self.assertIsNone(check_complete(self.b, "t_pr", complete_args(self.green), forge))
        self.assertEqual(forge.calls, [(SLUG, SHA)])

    def test_pr_ci_red_blocks(self):
        for state in ("red", "pending", "none", None, "GREEN"):
            with self.subTest(state=state):
                msg = check_complete(
                    self.b, "t_pr", complete_args(self.green), ForgeSpy(state))
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith("ci:"), msg)

    def test_pr_ci_forge_unreachable_blocks(self):
        for exc in (OSError("connection refused"), TimeoutError("20s"),
                    RuntimeError("adapter missing")):
            with self.subTest(exc=exc):
                msg = check_complete(
                    self.b, "t_pr", complete_args(self.green), ForgeSpy(exc=exc))
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_pr_ci_no_intake_repo_blocks(self):
        self.b.metadata["t_intake"] = {}
        msg = check_complete(self.b, "t_pr", complete_args(self.green), ForgeSpy())
        self.assertIsInstance(msg, str)

    def test_pr_ci_red_claim_requires_rework_child_without_forge(self):
        md = dict(self.green, ci="red", findings=["test_x fails on this change"])
        msg = check_complete(self.b, "t_pr", complete_args(md, []), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("implement", msg)
        self.b.add("t_fix", "implement", ["t_pr"])
        self.assertIsNone(check_complete(self.b, "t_pr", complete_args(md, ["t_fix"]), no_forge))

    def test_pr_ci_red_claim_without_findings_blocks(self):
        self.b.add("t_fix", "implement", ["t_pr"])
        md = dict(self.green, ci="red")
        msg = check_complete(self.b, "t_pr", complete_args(md, ["t_fix"]), no_forge)
        self.assertIsInstance(msg, str)
        self.assertIn("findings", msg)

    def test_pr_ci_red_claim_round_limit_applies(self):
        b = FakeBoard()
        third = chain(b, rounds=3)
        b.add("t_v", "validate", [third], md={"head_sha": SHA, "verdict": "pass",
                                               "evidence": []})
        b.add("t_r", "review", ["t_v"], md={"head_sha": SHA, "verdict": "approve"})
        b.add("t_p", "pr-ci", ["t_r"])
        b.add("t_fix", "implement", ["t_p"])
        md = dict(self.green, ci="red", findings=["broken"])
        msg = check_complete(b, "t_p", complete_args(md, ["t_fix"]), no_forge)
        self.assertTrue(msg.startswith("review: rounds-exhausted"), msg)


class SlugTests(unittest.TestCase):
    def test_project_slug_from_intake_ancestor(self):
        b = FakeBoard()
        last = chain(b, rounds=2)
        self.assertEqual(project_slug(b, last), SLUG)

    def test_project_slug_missing_raises(self):
        b = FakeBoard()
        b.add("t_impl", "implement")
        with self.assertRaises(LookupError):
            project_slug(b, "t_impl")

    def test_project_slug_conflicting_intakes_raise(self):
        b = FakeBoard()
        b.add("t_in1", "intake", md={"repo": "a"})
        b.add("t_in2", "intake", md={"repo": "b"})
        b.add("t_impl", "implement", ["t_in1", "t_in2"])
        with self.assertRaises(LookupError):
            project_slug(b, "t_impl")


if __name__ == "__main__":
    unittest.main()
