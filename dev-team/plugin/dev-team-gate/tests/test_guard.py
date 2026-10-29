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

    def test_plan_cards_must_be_implement_children_of_the_plan(self):
        # review Minor (guard.py:115-118): any stage skill was accepted
        b = FakeBoard()
        b.add("t_intake", "intake")
        b.add("t_plan", "plan", ["t_intake"])
        b.add("t_impl", "implement", ["t_plan"])
        b.add("t_val", "validate", ["t_plan"])          # wrong stage
        b.add("t_stray", "implement", ["t_intake"])     # not the plan's child
        for bad in ("t_val", "t_stray"):
            with self.subTest(bad=bad):
                cards = ["t_impl", bad]
                msg = check_complete(b, "t_plan", complete_args({"cards": cards}, ["t_impl"]),
                                     no_forge)
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith("plan:"), msg)
                self.assertIn(bad, msg)

    def test_plan_may_name_a_split_unit(self):
        b = FakeBoard()
        b.add("t_intake", "intake")
        b.add("t_plan", "plan", ["t_intake"])
        b.add("t_be", "implement", ["t_plan"])
        b.add("t_fe", "implement", ["t_plan", "t_be"])
        self.assertIsNone(check_complete(
            b, "t_plan", complete_args({"cards": ["t_be", "t_fe"]}, ["t_be"]), no_forge))


class RoundTests(unittest.TestCase):
    def test_split_unit_starts_at_round_one(self):
        # review Minor (board.py:118-122): a unit split into backend and
        # frontend implement cards started at round 2 and lost a rework round.
        b = FakeBoard()
        b.add("t_plan", "plan")
        b.add("t_be", "implement", ["t_plan"])
        b.add("t_fe", "implement", ["t_plan", "t_be"])
        b.add("t_val", "validate", ["t_be", "t_fe"])
        self.assertEqual(implement_rounds(b, "t_fe"), 1)
        b.add("t_r2", "implement", ["t_val"])
        self.assertEqual(implement_rounds(b, "t_r2"), 2)
        b.add("t_val2", "validate", ["t_r2"])
        b.add("t_r3", "implement", ["t_val2"])
        self.assertEqual(implement_rounds(b, "t_r3"), 3)

    def test_split_unit_still_gets_two_rework_rounds_from_the_guard(self):
        b = FakeBoard()
        b.add("t_intake", "intake", md={"repo": "widgets"})
        b.add("t_plan", "plan", ["t_intake"])
        b.add("t_be", "implement", ["t_plan"])
        b.add("t_fe", "implement", ["t_plan", "t_be"])
        b.add("t_val", "validate", ["t_be", "t_fe"])
        b.add("t_r2", "implement", ["t_val"])
        b.add("t_val2", "validate", ["t_r2"], md={})
        b.add("t_r3", "implement", ["t_val2"])
        b.metadata["t_r2"] = impl_md()
        args = complete_args({"verdict": "fail", "head_sha": SHA, "evidence": [],
                              "findings": ["x"]}, ["t_r3"])
        self.assertIsNone(check_complete(b, "t_val2", args, no_forge))
        # ...and a fourth round is still refused
        b.add("t_val3", "validate", ["t_r3"])
        b.add("t_r4", "implement", ["t_val3"])
        b.metadata["t_r3"] = impl_md()
        args = complete_args({"verdict": "fail", "head_sha": SHA, "evidence": [],
                              "findings": ["x"]}, ["t_r4"])
        msg = check_complete(b, "t_val3", args, no_forge)
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("review: rounds-exhausted"), msg)

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
        # #5: a green claim is checked against the PR first (in the project,
        # head == head_sha, ready); these tests are about the CI read after it.
        self.pr = lambda slug, url: {"pr": "7", "state": "open", "pr_url": url,
                                     "head_sha": SHA, "ready": True}

    def test_pr_ci_green_with_no_child_passes(self):
        forge = ForgeSpy("green")
        self.assertIsNone(check_complete(self.b, "t_pr", complete_args(self.green), forge,
                                         self.pr))
        self.assertEqual(forge.calls, [(SLUG, SHA)])

    def test_pr_ci_red_blocks(self):
        for state in ("red", "pending", "none", None, "GREEN"):
            with self.subTest(state=state):
                msg = check_complete(
                    self.b, "t_pr", complete_args(self.green), ForgeSpy(state), self.pr)
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith("ci:"), msg)

    def test_pr_ci_forge_unreachable_blocks(self):
        for exc in (OSError("connection refused"), TimeoutError("20s"),
                    RuntimeError("adapter missing")):
            with self.subTest(exc=exc):
                msg = check_complete(
                    self.b, "t_pr", complete_args(self.green), ForgeSpy(exc=exc), self.pr)
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith("forge: unreachable"), msg)

    def test_pr_ci_no_intake_repo_blocks(self):
        self.b.metadata["t_intake"] = {}
        msg = check_complete(self.b, "t_pr", complete_args(self.green), ForgeSpy(), self.pr)
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


# ---------------------------------------------------------------------------
# Task 6: terminal guard and pre_tool_call wiring.
#
# `guard` is imported as a module and its Task 6 names are read at call time,
# so the Task 5 tests above keep running while these are red.
# ---------------------------------------------------------------------------
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

import guard as guard_mod  # noqa: E402

MERGE = "merge: human-only"
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def terminal(board, task_id, command):
    return guard_mod.check_terminal(board, task_id, command)


def guarded_board(default_branch: str | None = "develop", home=None):
    """intake(repo=widgets) -> plan -> implement t_impl1, plus an unguarded t_misc.

    Writes the project record into `home` when both are given.
    """
    b = FakeBoard()
    chain(b)
    b.add("t_misc", None, extra_skills=["github-code-review"])
    if home is not None and default_branch is not None:
        rec = Path(home) / "dev-team" / "projects" / f"{SLUG}.json"
        rec.parent.mkdir(parents=True)
        rec.write_text(json.dumps({
            "slug": SLUG, "repo_url": "", "pr_adapter": "github", "ci_adapter": "github",
            "default_branch": default_branch, "ci_hint": {},
        }))
    return b


class HomeMixin:
    """A temporary HERMES_HOME, and HERMES_KANBAN_TASK unset unless a test sets it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERMES_KANBAN_TASK", None)
        os.environ.pop("HERMES_KANBAN_BOARD", None)
        self.addCleanup(self._tmp.cleanup)


class MergeDeployTests(HomeMixin, unittest.TestCase):
    BLOCKED = [
        "gh pr merge 12",
        "gh pr merge --squash --delete-branch 12",
        "/usr/local/bin/gh pr merge 12",
        "gh pr -R acme/widgets merge 12",
        "glab mr merge 3",
        "tea pr merge 4",
        "tea prs merge 4",
        "tea pulls merge 4",
        "kubectl apply -f deploy.yaml",
        "kubectl -n prod rollout restart deploy/web",
        "helm upgrade web ./chart",
        "helm install web ./chart",
        "terraform apply -auto-approve",
        "terraform -chdir=infra apply",
        "docker push registry/web:1",
        # chains and nesting: every simple command is checked
        "cd x && gh pr merge 1",
        "make test; kubectl apply -f k.yaml",
        "true || helm upgrade web .",
        "echo ok | docker push img",
        "echo ok\ngh pr merge 1",
        "(cd x; gh pr merge 1)",
        "bash -c 'gh pr merge 1'",
        "sh -lc \"cd x && glab mr merge 2\"",
        "bash --login -c 'gh pr merge 1'",       # long options before -c
        "bash --norc --noprofile -c 'gh pr merge 1'",
        "bash -o pipefail -c 'gh pr merge 1'",   # option values before -c
        "bash -O extglob +o posix -ec 'gh pr merge 1'",
        'echo "$(gh pr merge 1)"',
        "echo `gh pr merge 1`",
        "eval gh pr merge 1",
        # wrappers and prefixes
        "FOO=1 helm upgrade web .",
        "sudo -u deploy kubectl apply -f k.yaml",
        "env GH_TOKEN=x gh pr merge 1",
        "timeout 60 terraform apply",
    ]
    ALLOWED = [
        "gh pr view 12",
        "gh pr create --title t --body-file b.md",
        "gh pr checks 12",
        "glab mr view 3",
        "kubectl get pods",
        "helm list",
        "terraform plan",
        "docker build -t web .",
        "git merge feature",
        "echo merge",
        "",
    ]

    def test_merge_commands_blocked(self):
        b = guarded_board(home=self.home)
        for task_id in (None, "t_misc", "t_impl1"):
            for cmd in self.BLOCKED:
                with self.subTest(task_id=task_id, cmd=cmd):
                    msg = terminal(b, task_id, cmd)
                    self.assertIsInstance(msg, str)
                    self.assertTrue(msg.startswith(MERGE), msg)

    def test_non_merge_commands_allowed(self):
        b = guarded_board(home=self.home)
        for task_id in (None, "t_misc", "t_impl1"):
            for cmd in self.ALLOWED:
                with self.subTest(task_id=task_id, cmd=cmd):
                    self.assertIsNone(terminal(b, task_id, cmd))

    def test_merge_blocked_without_board(self):
        """The always-block rules need no board: a broken board still blocks them."""
        b = FakeBoard()
        b.fail = OSError("database is locked")
        msg = terminal(b, "t_impl1", "gh pr merge 1")
        self.assertTrue(msg.startswith(MERGE), msg)

    def test_quoted_merge_in_commit_message_not_blocked(self):
        b = guarded_board(home=self.home)
        for task_id in (None, "t_misc", "t_impl1"):
            for cmd in (
                'git commit -m "gh pr merge"',
                "git commit -m 'kubectl apply -f x'",
                'git commit -m "hermes kanban complete t_impl1"',
                'git commit -m "git push origin main"',
                'echo "a; gh pr merge 1"',
            ):
                with self.subTest(task_id=task_id, cmd=cmd):
                    self.assertIsNone(terminal(b, task_id, cmd))


class PushTests(HomeMixin, unittest.TestCase):
    PUSH_BLOCKED = [
        "git push origin develop",          # the record's default branch
        "git push origin main",             # main and master are always protected
        "git push origin master",
        "git push origin HEAD:develop",
        "git push origin HEAD:main",
        "git push origin feature:refs/heads/develop",
        "git push origin +develop",
        "git push --force origin feat/x:develop",
        "git push origin :develop",         # deleting the default branch
        "git push --delete origin main",
        "git -C repo push origin develop",
        "git -c push.default=current push origin main",
        "git push origin feat/x develop",   # any refspec
        # destination cannot be verified: no refspec, HEAD, matching, all
        "git push",
        "git push origin",
        "git push -u origin",
        "git push origin HEAD",
        "git push origin :",
        "git push --all origin",
        "git push --mirror origin",
        "git push --prune origin feat/x",   # prunes unnamed remote branches
        "git push origin feat/x # done\ngit push",  # a comment never joins lines
        "cd repo && git push",
        "make && git push origin main",
        # destination is a shell expansion: the real branch is unknown
        'git push origin "$(git branch --show-current)"',
        'git push -u origin "$BRANCH"',
        "B=main; git push origin $B",
        "git push origin ${B}",
        "git push origin {main,}",
        "git push origin feat/x:$B",
        "git push origin refs/heads/$B",
        "git push origin ~",
        "git push origin mai?",
        "git push origin ma[i]n",
        "git push origin 'm*'",
        # nested shells with options before -c are parsed again
        "bash -o pipefail -c 'git push origin develop'",
        "bash --login -c 'git push origin develop'",
    ]
    PUSH_ALLOWED = [
        "git push origin feat/x",
        "git push -u origin feat/x",
        "git push origin HEAD:feat/x",
        "git push origin HEAD:refs/heads/feat/x",
        "git push --force-with-lease origin feat/x",
        "git push -o ci.skip origin feat/x",
        "git push origin --tags",
        "git push origin refs/tags/v1",
        "git status && git push origin feat/x",
        "bash -o pipefail -c 'git push origin feat/x'",
    ]

    def test_push_to_default_branch_blocked_feature_branch_allowed(self):
        b = guarded_board("develop", home=self.home)
        for cmd in self.PUSH_BLOCKED:
            with self.subTest(cmd=cmd):
                msg = terminal(b, "t_impl1", cmd)
                self.assertIsInstance(msg, str)
                self.assertTrue(msg.startswith(MERGE), msg)
        for cmd in self.PUSH_ALLOWED:
            with self.subTest(cmd=cmd):
                self.assertIsNone(terminal(b, "t_impl1", cmd))

    def test_push_without_card_protects_main_and_master(self):
        b = guarded_board("develop", home=self.home)
        for task_id in (None, "t_misc"):
            with self.subTest(task_id=task_id):
                self.assertTrue(terminal(b, task_id, "git push origin main").startswith(MERGE))
                self.assertTrue(terminal(b, task_id, "git push origin master").startswith(MERGE))
                self.assertTrue(terminal(b, task_id, "git push").startswith(MERGE))
                self.assertIsNone(terminal(b, task_id, "git push origin feat/x"))

    def test_push_on_guarded_card_without_record_fails_closed(self):
        b = guarded_board(default_branch=None, home=self.home)  # no record written
        msg = terminal(b, "t_impl1", "git push origin feat/x")
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("guard:"), msg)
        self.assertIn(SLUG, msg)

    def test_push_on_guarded_card_with_bad_slug_fails_closed(self):
        b = guarded_board(home=self.home)
        b.metadata["t_intake"] = dict(b.metadata["t_intake"], repo="../escape")
        msg = terminal(b, "t_impl1", "git push origin feat/x")
        self.assertTrue(msg.startswith("guard:"), msg)

    def test_push_with_board_failure_fails_closed(self):
        b = guarded_board(home=self.home)
        b.fail = OSError("database is locked")
        msg = terminal(b, "t_impl1", "git push origin feat/x")
        self.assertTrue(msg.startswith("guard:"), msg)
        self.assertIn("database is locked", msg)


class SideDoorTests(HomeMixin, unittest.TestCase):
    VERBS = ("complete", "request-changes", "edit", "unblock", "reopen-review")

    def test_kanban_cli_side_door_blocked_on_guarded_card_only(self):
        b = guarded_board(home=self.home)
        for verb in self.VERBS:
            for cmd in (
                f"hermes kanban {verb} t_impl1",
                f"hermes -p dev-qa kanban --board b {verb} t_impl1",
                f"cd /x && hermes kanban {verb} t_impl1",
                f"bash -c 'hermes kanban {verb} t_impl1'",
            ):
                with self.subTest(cmd=cmd):
                    msg = terminal(b, "t_impl1", cmd)
                    self.assertIsInstance(msg, str)
                    self.assertTrue(msg.startswith("guard:"), msg)
                    self.assertIn("kanban_", msg)  # points at the guarded tool instead
                    # Decided by the TARGET card (#7), not the caller: an unguarded caller
                    # or no card at all must not open the side door onto a guarded card.
                    for caller in ("t_misc", None):
                        other = terminal(b, caller, cmd)
                        self.assertIsInstance(other, str, f"{cmd!r} allowed from {caller}")
                        self.assertTrue(other.startswith("guard:"), other)

    def test_other_kanban_cli_verbs_allowed_on_guarded_card(self):
        b = guarded_board(home=self.home)
        for cmd in (
            "hermes kanban show t_impl1",
            "hermes kanban block t_impl1 'review: rounds-exhausted'",
            "hermes kanban comment t_impl1 'please edit the title'",
            "hermes kanban list",
        ):
            with self.subTest(cmd=cmd):
                self.assertIsNone(terminal(b, "t_impl1", cmd))

    def test_side_door_with_board_failure_fails_closed(self):
        b = FakeBoard()
        b.fail = OSError("database is locked")
        msg = terminal(b, "t_impl1", "hermes kanban complete t_impl1")
        self.assertTrue(msg.startswith("guard:"), msg)

    def test_side_door_on_unknown_card_fails_closed(self):
        msg = terminal(FakeBoard(), "t_gone", "hermes kanban complete t_gone")
        self.assertTrue(msg.startswith("guard:"), msg)
        self.assertIn("t_gone", msg)

    def test_plain_commands_do_not_touch_the_board(self):
        b = FakeBoard()
        b.fail = OSError("database is locked")
        self.assertIsNone(terminal(b, "t_impl1", "make test && git status"))


class UnparseableTests(HomeMixin, unittest.TestCase):
    def test_unparseable_command_fails_closed_on_guarded_card(self):
        b = guarded_board(home=self.home)
        msg = terminal(b, "t_impl1", 'git commit -m "unbalanced')
        self.assertIsInstance(msg, str)
        self.assertTrue(msg.startswith("guard:"), msg)

    def test_unparseable_command_keeps_always_block_rules(self):
        b = guarded_board(home=self.home)
        for task_id in (None, "t_misc", "t_impl1"):
            with self.subTest(task_id=task_id):
                msg = terminal(b, task_id, "gh pr merge 1 'oops")
                self.assertTrue(msg.startswith(MERGE), msg)

    def test_unparseable_command_allowed_off_guarded_card(self):
        b = guarded_board(home=self.home)
        self.assertIsNone(terminal(b, "t_misc", 'echo "unbalanced'))
        self.assertIsNone(terminal(b, None, 'echo "unbalanced'))


class PreToolCallTests(HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.board = guarded_board(home=self.home)
        self.made = []

        def make_board(slug):
            self.made.append(slug)
            return self.board

        p = mock.patch.object(guard_mod, "_make_board", make_board)
        p.start()
        self.addCleanup(p.stop)
        f = mock.patch.object(guard_mod, "forge_ci_status", no_forge)
        f.start()
        self.addCleanup(f.stop)

    def test_complete_defaults_task_id_from_env(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_impl1"  # a guarded implement card
        out = guard_mod.pre_tool_call("kanban_complete", {"summary": "s", "metadata": {}})
        self.assertEqual(out["action"], "block")
        self.assertTrue(out["message"].startswith("metadata:"), out)
        # an explicit task_id wins over the environment
        out = guard_mod.pre_tool_call(
            "kanban_complete", {"task_id": "t_misc", "summary": "s", "metadata": {}})
        self.assertIsNone(out)

    def test_complete_without_any_task_id_fails_closed(self):
        out = guard_mod.pre_tool_call("kanban_complete", {"summary": "s"})
        self.assertEqual(out["action"], "block")
        self.assertTrue(out["message"].startswith("guard:"), out)

    def test_complete_reads_the_board_named_in_args(self):
        guard_mod.pre_tool_call("kanban_complete", {"task_id": "t_misc", "board": "proj-b"})
        self.assertEqual(self.made, ["proj-b"])

    def test_kanban_block_never_guarded(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_impl1"
        self.board.fail = OSError("database is locked")
        for tool in ("kanban_block", "kanban_comment", "kanban_create", "read_file"):
            with self.subTest(tool=tool):
                self.assertIsNone(guard_mod.pre_tool_call(
                    tool, {"task_id": "t_impl1", "reason": "review: rounds-exhausted",
                           "command": "hermes kanban complete t_impl1"}))
        self.assertEqual(self.made, [])

    def test_terminal_dispatch(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_impl1"
        out = guard_mod.pre_tool_call("terminal", {"command": "gh pr merge 1"})
        self.assertEqual(out, {"action": "block", "message": out["message"]})
        self.assertTrue(out["message"].startswith(MERGE))
        out = guard_mod.pre_tool_call("terminal", {"command": "hermes kanban complete t_impl1"})
        self.assertTrue(out["message"].startswith("guard:"))
        self.assertIsNone(guard_mod.pre_tool_call("terminal", {"command": "make test"}))

    def test_terminal_without_card_still_checks_side_door_target(self):
        # #7: no HERMES_KANBAN_TASK does not skip the check; t_x is not on the
        # board, so the target cannot be verified and the call is refused.
        out = guard_mod.pre_tool_call("terminal", {"command": "hermes kanban complete t_x"})
        self.assertEqual(out["action"], "block")
        self.assertTrue(out["message"].startswith("guard:"), out)
        out = guard_mod.pre_tool_call("terminal", {"command": "hermes kanban complete t_impl1"})
        self.assertTrue(out["message"].startswith("guard:"), out)
        self.assertIsNone(guard_mod.pre_tool_call(
            "terminal", {"command": "hermes kanban complete t_misc"}))

    def test_hook_never_raises(self):
        os.environ["HERMES_KANBAN_TASK"] = "t_impl1"

        def broken(slug):
            raise RuntimeError("no hermes here")

        with mock.patch.object(guard_mod, "_make_board", broken):
            out = guard_mod.pre_tool_call("kanban_complete", {"metadata": {}})
        self.assertEqual(out["action"], "block")
        self.assertTrue(out["message"].startswith("guard:"), out)
        self.assertIn("no hermes here", out["message"])

    def test_terminal_non_string_command_blocks(self):
        out = guard_mod.pre_tool_call("terminal", {"command": ["gh", "pr", "merge"]})
        self.assertEqual(out["action"], "block")


# The forge read (formerly a `forge ci-status` subprocess found on PATH) is
# tested in test_guard_forge.TrustedForgeTests: since #4 the guard imports
# forge_core by path and never runs a `forge` it finds on PATH.


class PluginWiringTests(unittest.TestCase):
    def test_manifest(self):
        text = (PLUGIN_DIR / "plugin.yaml").read_text()
        lines = [ln.strip() for ln in text.splitlines()]
        self.assertIn("name: dev-team-gate", lines)
        self.assertIn("provides_hooks:", lines)
        self.assertIn("- pre_tool_call", lines)

    def test_register_wires_pre_tool_call(self):
        spec = importlib.util.spec_from_file_location(
            "dev_team_gate_under_test", PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = pkg
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(pkg)

        calls = []

        class Ctx:
            def register_hook(self, name, cb):
                calls.append((name, cb))

        pkg.register(Ctx())
        self.assertEqual([name for name, _ in calls], ["pre_tool_call"])
        self.assertIs(calls[0][1], sys.modules["dev_team_gate_under_test.guard"].pre_tool_call)


if __name__ == "__main__":
    unittest.main()
