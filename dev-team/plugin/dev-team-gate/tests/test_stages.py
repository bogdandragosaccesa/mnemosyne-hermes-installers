"""Tests for stages: the dev-team stage table, stage detection and child routing."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stages import (  # noqa: E402
    STAGES,
    expected_child,
    stage_from_skills,
    validate_metadata,
)

SHA = "9f4cbf1a2b3c4d5e6f708192a3b4c5d6e7f80912"

VALID = {
    "intake": {
        "definition_of_done": ["tests pass", "PR opened"],
        "budget": {"amount": 40, "unit": "usd"},
        "repo": "git@github.com:acme/widgets.git",
    },
    "plan": {"cards": ["t_aaaa1111", "t_bbbb2222"]},
    "implement": {
        "branch": "feature/widgets",
        "head_sha": SHA,
        "changed_files": ["src/widgets.py"],
        "tests_run": [{"command": "python3 -m unittest", "exit_code": 0}],
    },
    "validate": {"head_sha": SHA, "verdict": "pass", "evidence": ["ran suite: OK"]},
    "review": {"head_sha": SHA, "verdict": "approve"},
    "pr-ci": {
        "pr_url": "https://github.com/acme/widgets/pull/7",
        "head_sha": SHA,
        "ci": "green",
    },
}

REQUIRED = {
    "intake": ("definition_of_done", "budget", "repo"),
    "plan": ("cards",),
    "implement": ("branch", "head_sha", "changed_files", "tests_run"),
    "validate": ("head_sha", "verdict", "evidence"),
    "review": ("head_sha", "verdict"),
    "pr-ci": ("pr_url", "head_sha", "ci"),
}


def md(stage, **overrides):
    out = copy.deepcopy(VALID[stage])
    out.update(overrides)
    return out


class StageTableBase(unittest.TestCase):
    def assertValid(self, stage, metadata):
        self.assertEqual(validate_metadata(stage, metadata), [])

    def assertRejectsNaming(self, stage, metadata, field):
        errors = validate_metadata(stage, metadata)
        self.assertTrue(errors, f"{stage}: expected rejection naming {field!r}")
        self.assertTrue(
            any(field in e for e in errors),
            f"{stage}: {field!r} not named in {errors!r}",
        )

    def _missing_each_field(self, stage):
        self.assertEqual(set(REQUIRED[stage]) - set(VALID[stage]), set())
        for field in REQUIRED[stage]:
            with self.subTest(stage=stage, dropped=field):
                metadata = md(stage)
                del metadata[field]
                self.assertRejectsNaming(stage, metadata, field)


class TestStagesConstant(unittest.TestCase):
    def test_stages_exact(self):
        self.assertEqual(
            STAGES, ("intake", "plan", "implement", "validate", "review", "pr-ci")
        )


class TestIntake(StageTableBase):
    def test_intake_valid_passes(self):
        self.assertValid("intake", md("intake"))

    def test_intake_missing_each_field(self):
        self._missing_each_field("intake")

    def test_intake_wrong_types_rejected(self):
        cases = [
            ("definition_of_done", "tests pass"),
            ("budget", 40),
            ("budget", {"amount": 40}),
            ("budget", {"unit": "usd"}),
            ("budget", {"amount": "40", "unit": "usd"}),
            ("budget", {"amount": True, "unit": "usd"}),
            ("budget", {"amount": -1, "unit": "usd"}),
            ("budget", {"amount": float("nan"), "unit": "usd"}),
            ("budget", {"amount": float("inf"), "unit": "usd"}),
            ("budget", {"amount": 40, "unit": ""}),
            ("repo", ""),
            ("repo", 7),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assertRejectsNaming("intake", md("intake", **{field: value}), field)


class TestPlan(StageTableBase):
    def test_plan_valid_passes(self):
        self.assertValid("plan", md("plan"))

    def test_plan_missing_each_field(self):
        self._missing_each_field("plan")

    def test_plan_cards_must_be_nonempty_list_of_ids(self):
        for value in ([], "t_aaaa1111", [""], [7], None):
            with self.subTest(value=value):
                self.assertRejectsNaming("plan", md("plan", cards=value), "cards")


class TestImplement(StageTableBase):
    def test_implement_valid_passes(self):
        self.assertValid("implement", md("implement"))

    def test_implement_missing_each_field(self):
        self._missing_each_field("implement")

    def test_implement_tests_run_nonzero_exit_rejected(self):
        runs = [
            {"command": "python3 -m unittest", "exit_code": 0},
            {"command": "npm test", "exit_code": 1},
        ]
        self.assertRejectsNaming("implement", md("implement", tests_run=runs), "tests_run")

    def test_implement_tests_run_malformed_rejected(self):
        cases = [
            [],
            {"command": "x", "exit_code": 0},
            [{"command": "x"}],
            [{"exit_code": 0}],
            [{"command": "", "exit_code": 0}],
            [{"command": "x", "exit_code": "0"}],
            [{"command": "x", "exit_code": False}],
            ["python3 -m unittest"],
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertRejectsNaming(
                    "implement", md("implement", tests_run=value), "tests_run"
                )

    def test_implement_changed_files_must_be_nonempty(self):
        for value in ([], "src/widgets.py", [""], [3]):
            with self.subTest(value=value):
                self.assertRejectsNaming(
                    "implement", md("implement", changed_files=value), "changed_files"
                )

    def test_implement_bad_sha_and_branch_rejected(self):
        for value in ("", "HEAD", "zzzzzzz", "abc", 1234567):
            with self.subTest(head_sha=value):
                self.assertRejectsNaming(
                    "implement", md("implement", head_sha=value), "head_sha"
                )
        for value in ("", None, 5):
            with self.subTest(branch=value):
                self.assertRejectsNaming("implement", md("implement", branch=value), "branch")


class TestValidate(StageTableBase):
    def test_validate_valid_passes(self):
        self.assertValid("validate", md("validate"))
        self.assertValid(
            "validate", md("validate", verdict="fail", findings=["a.py:3 off by one"])
        )

    def test_validate_missing_each_field(self):
        self._missing_each_field("validate")

    def test_validate_fail_requires_findings(self):
        self.assertRejectsNaming("validate", md("validate", verdict="fail"), "findings")
        self.assertRejectsNaming(
            "validate", md("validate", verdict="fail", findings=[]), "findings"
        )
        self.assertRejectsNaming(
            "validate", md("validate", verdict="fail", findings="broken"), "findings"
        )

    def test_validate_bad_verdict_and_evidence_rejected(self):
        for value in ("approve", "PASS", "", None):
            with self.subTest(verdict=value):
                self.assertRejectsNaming("validate", md("validate", verdict=value), "verdict")
        self.assertRejectsNaming("validate", md("validate", evidence="ok"), "evidence")


class TestReview(StageTableBase):
    def test_review_valid_passes(self):
        self.assertValid("review", md("review"))
        self.assertValid(
            "review", md("review", verdict="changes", findings=["rename foo"])
        )

    def test_review_missing_each_field(self):
        self._missing_each_field("review")

    def test_review_changes_requires_findings(self):
        self.assertRejectsNaming("review", md("review", verdict="changes"), "findings")
        self.assertRejectsNaming(
            "review", md("review", verdict="changes", findings=[]), "findings"
        )

    def test_review_bad_verdict_rejected(self):
        for value in ("pass", "approved", "", 1):
            with self.subTest(verdict=value):
                self.assertRejectsNaming("review", md("review", verdict=value), "verdict")


class TestPrCi(StageTableBase):
    def test_pr_ci_valid_passes(self):
        self.assertValid("pr-ci", md("pr-ci"))

    def test_pr_ci_missing_each_field(self):
        self._missing_each_field("pr-ci")

    def test_pr_ci_red_with_findings_passes(self):
        self.assertValid("pr-ci", md("pr-ci", ci="red", findings=["test_x fails"]))

    def test_pr_ci_red_requires_findings(self):
        self.assertRejectsNaming("pr-ci", md("pr-ci", ci="red"), "findings")
        self.assertRejectsNaming("pr-ci", md("pr-ci", ci="red", findings=[]), "findings")

    def test_pr_ci_other_ci_states_rejected(self):
        for value in ("pending", "none", "GREEN", "", None):
            with self.subTest(ci=value):
                self.assertRejectsNaming("pr-ci", md("pr-ci", ci=value), "ci")

    def test_pr_ci_bad_pr_url_rejected(self):
        for value in ("", "github.com/acme/widgets/pull/7", 7):
            with self.subTest(pr_url=value):
                self.assertRejectsNaming("pr-ci", md("pr-ci", pr_url=value), "pr_url")


class TestValidateMetadataFailsClosed(unittest.TestCase):
    def test_unknown_stage_rejected(self):
        errors = validate_metadata("deploy", {})
        self.assertTrue(errors)
        self.assertTrue(any("deploy" in e for e in errors))

    def test_non_dict_metadata_rejected(self):
        for value in (None, [], "x"):
            with self.subTest(value=value):
                self.assertTrue(validate_metadata("review", value))

    def test_extra_fields_allowed(self):
        self.assertEqual(validate_metadata("review", md("review", notes=["x"])), [])

    def test_errors_are_strings(self):
        errors = validate_metadata("implement", {})
        self.assertEqual(len(errors), 4)
        self.assertTrue(all(isinstance(e, str) for e in errors))


class TestExpectedChild(unittest.TestCase):
    def test_expected_child_routes(self):
        routes = [
            ("intake", md("intake"), "plan"),
            ("plan", md("plan"), "implement"),
            ("implement", md("implement"), "validate"),
            ("validate", md("validate"), "review"),
            ("validate", md("validate", verdict="fail", findings=["x"]), "implement"),
            ("review", md("review"), "pr-ci"),
            ("review", md("review", verdict="changes", findings=["x"]), "implement"),
            ("pr-ci", md("pr-ci"), None),
            ("pr-ci", md("pr-ci", ci="red", findings=["x"]), "implement"),
        ]
        for stage, metadata, child in routes:
            with self.subTest(stage=stage, metadata=metadata):
                self.assertEqual(expected_child(stage, metadata), child)

    def test_expected_child_non_success_goes_to_implement(self):
        # Fail closed: anything that is not the success verdict routes to rework,
        # never onward and never to "no child required".
        self.assertEqual(expected_child("validate", {"verdict": "bogus"}), "implement")
        self.assertEqual(expected_child("validate", {}), "implement")
        self.assertEqual(expected_child("review", {}), "implement")
        self.assertEqual(expected_child("pr-ci", {"ci": "pending"}), "implement")
        self.assertEqual(expected_child("pr-ci", {}), "implement")

    def test_expected_child_unknown_stage_raises(self):
        with self.assertRaises(ValueError):
            expected_child("deploy", {})


class TestStageFromSkills(unittest.TestCase):
    def test_stage_from_skills_each_stage(self):
        for stage in STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stage_from_skills([f"dev-team-stage-{stage}"]), stage)

    def test_stage_from_skills_ignores_other_skills(self):
        self.assertEqual(
            stage_from_skills(
                ["github", "dev-team-stage-review", "test-driven-development"]
            ),
            "review",
        )
        self.assertIsNone(stage_from_skills(["github", "running-the-pipeline"]))
        self.assertIsNone(stage_from_skills(["dev-team-stage"]))
        self.assertIsNone(stage_from_skills([]))
        self.assertIsNone(stage_from_skills(None))

    def test_stage_from_skills_duplicate_same_stage_ok(self):
        self.assertEqual(
            stage_from_skills(["dev-team-stage-plan", "dev-team-stage-plan"]), "plan"
        )

    def test_stage_from_skills_ambiguous_raises(self):
        # Fail closed: two stages on one card cannot be resolved to an unguarded card.
        with self.assertRaises(ValueError):
            stage_from_skills(["dev-team-stage-plan", "dev-team-stage-review"])

    def test_stage_from_skills_unknown_stage_raises(self):
        # A dev-team stage skill naming no known stage is a malformed guarded card.
        with self.assertRaises(ValueError):
            stage_from_skills(["dev-team-stage-deploy"])

    def test_stage_from_skills_non_string_entries_ignored(self):
        self.assertEqual(stage_from_skills([None, 3, "dev-team-stage-intake"]), "intake")


if __name__ == "__main__":
    unittest.main()
