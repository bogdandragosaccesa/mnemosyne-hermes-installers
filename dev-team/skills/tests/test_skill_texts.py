"""Text-level tests for the dev-team-deciders skill and its cross-references.

These parse the shipped Markdown/YAML directly (stdlib only, no network, no
live model calls) -- they check that the skill says what the design requires,
not that `deciders` itself behaves correctly (see `dev-team/deciders/tests`
for that).
"""
from __future__ import annotations

import glob
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS_DIR = os.path.dirname(_HERE)
_SOULS_DIR = os.path.join(os.path.dirname(_SKILLS_DIR), "souls")

_DECIDERS_SKILL = os.path.join(_SKILLS_DIR, "dev-team-deciders", "SKILL.md")
_PLAN_SKILL = os.path.join(_SKILLS_DIR, "dev-team-stage-plan", "SKILL.md")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body) for a `---`-delimited Markdown file.

    Hand-rolled (stdlib only, no PyYAML dependency) -- good enough for the
    flat `key: value` and `- name: X` shapes these skills actually use.
    """
    assert text.startswith("---\n"), "file must start with a YAML frontmatter block"
    end = text.index("\n---", 4)
    frontmatter = text[4:end]
    body = text[end + len("\n---"):]
    return frontmatter, body


class DecidersSkillFrontmatterTests(unittest.TestCase):
    def setUp(self):
        self.frontmatter, self.body = _split_frontmatter(_read(_DECIDERS_SKILL))

    def test_deciders_skill_frontmatter(self):
        self.assertIn("name: dev-team-deciders", self.frontmatter)
        self.assertIn("required_environment_variables:", self.frontmatter)
        self.assertIn("OPENROUTER_DECIDERS_KEY", self.frontmatter)
        # The variable must be declared inside the required_environment_variables
        # block, not merely mentioned somewhere else in the frontmatter.
        env_block = self.frontmatter.split("required_environment_variables:", 1)[1]
        self.assertIn("name: OPENROUTER_DECIDERS_KEY", env_block)

    def test_deciders_skill_states_the_rules(self):
        for needle in (
            "never evidence",
            "exit 2",
            "secret exec OPENROUTER_DECIDERS_KEY=openrouter/deciders",
            "0.35",
            "0.65",
        ):
            self.assertIn(needle, self.body, f"missing required phrase: {needle!r}")


class PlanSkillTierStepTests(unittest.TestCase):
    def test_plan_skill_calls_tier(self):
        text = _read(_PLAN_SKILL)
        self.assertIn("deciders tier", text)
        self.assertIn("model-tiers.json", text)

    def test_plan_skill_names_the_decision_model_owner_not_an_example(self):
        # I3: "e.g. `liquid/d1.1`" cannot let an agent enumerate the full
        # denylist (typesafe/jev-1.13, upstage/solar-decide-flash, the
        # perplexity/pplx-decider-* family are not named by that example).
        text = _read(_PLAN_SKILL)
        self.assertIn("_is_decision_model", text)
        self.assertIn("DECISION_MODELS", text)
        self.assertNotIn("e.g. `liquid/d1.1`", text)


class EverySoulNamesTheSkillTests(unittest.TestCase):
    def test_every_soul_names_the_skill(self):
        soul_paths = sorted(glob.glob(os.path.join(_SOULS_DIR, "SOUL-*.md")))
        self.assertTrue(soul_paths, "expected at least one SOUL-*.md file")
        missing = [p for p in soul_paths if "dev-team-deciders" not in _read(p)]
        self.assertEqual(missing, [], f"souls missing dev-team-deciders reference: {missing}")


if __name__ == "__main__":
    unittest.main()
