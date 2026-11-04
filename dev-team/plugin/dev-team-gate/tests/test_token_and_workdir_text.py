"""M4/M5 static skill-text checks: the forge token, and the board's default workdir.

M4 -- `dev-team-stage-pr-ci` must tell its worker how the forge credential
reaches it (`DEV_TEAM_GH_TOKEN`), how to hand it to git
(`export GH_TOKEN="$DEV_TEAM_GH_TOKEN"` then `gh auth setup-git`), and that a
missing token is a `setup: no-forge-token` block with **no** credential
fallback. The token must only ever be passed by reference: no skill may show
the literal value being echoed, piped into `gh auth login`, or interpolated
into a URL.

M5 -- `running-the-pipeline` must state `--default-workdir` as a requirement
and say why `pr-ci` depends on it, so an operator reading only that skill
cannot create a board that dies at the last stage.
"""
import re
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]

PR_CI = DEV_TEAM / "skills" / "dev-team-stage-pr-ci" / "SKILL.md"
PIPELINE = DEV_TEAM / "skills" / "running-the-pipeline" / "SKILL.md"
SKILLS = sorted(DEV_TEAM.glob("skills/*/SKILL.md"))
SOULS = sorted(DEV_TEAM.glob("souls/*.md"))

# Ways the token's VALUE would end up somewhere it can be read: echoed or
# piped into `gh auth login --with-token`, or interpolated into a remote URL.
# `$DEV_TEAM_GH_TOKEN` / `$GH_TOKEN` as an expansion is how it is passed by
# reference and is exactly what we DO want, so the patterns below match the
# dereferenced-into-a-sink shapes, not the variable itself.
TOKEN_LEAKS = (
    re.compile(r"gh\s+auth\s+login[^\n]*--with-token"),
    re.compile(r"echo[^\n]*\$(?:DEV_TEAM_)?GH_TOKEN"),
    re.compile(r"https://\$[A-Za-z_][A-Za-z0-9_]*@"),
    re.compile(r"x-access-token:\$"),
)


class PrCiTokenTextTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PR_CI.is_file(), PR_CI)
        self.text = PR_CI.read_text(encoding="utf-8")

    def test_names_the_env_var(self):
        self.assertIn("DEV_TEAM_GH_TOKEN", self.text)

    def test_specifies_the_push_mechanism(self):
        self.assertIn('export GH_TOKEN="$DEV_TEAM_GH_TOKEN"', self.text)
        self.assertIn("gh auth setup-git", self.text)

    def test_records_why_not_git_askpass(self):
        self.assertIn("GIT_ASKPASS", self.text)
        self.assertIn("credential.", self.text)

    def test_states_the_block_label_for_a_missing_token(self):
        self.assertIn("setup: no-forge-token", self.text)

    def test_forbids_every_credential_fallback(self):
        for fallback in ("gh auth login", "keyring", "SSH", "GITHUB_TOKEN"):
            with self.subTest(fallback=fallback):
                self.assertIn(fallback, self.text,
                              f"the skill must name {fallback!r} as forbidden")
        self.assertIn("no fallback", self.text.lower())

    def test_says_the_token_is_passed_by_reference(self):
        self.assertIn("by reference", self.text)


class NoSkillLeaksTheTokenTests(unittest.TestCase):
    """No skill or soul may demonstrate the token's value reaching a sink."""

    def test_files_exist(self):
        self.assertGreaterEqual(len(SKILLS) + len(SOULS), 9, SKILLS + SOULS)

    def test_no_text_file_shows_the_token_value_being_used(self):
        offenders = []
        for path in SKILLS + SOULS:
            text = path.read_text(encoding="utf-8")
            for pattern in TOKEN_LEAKS:
                for m in pattern.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    # A line that forbids the shape is the point of M4.
                    whole = text.splitlines()[line - 1]
                    if "Never" in whole or "never" in whole or "forbidden" in whole:
                        continue
                    offenders.append(f"{path.name}:{line}: {m.group(0)!r}")
        self.assertEqual(offenders, [], "token value reaches a sink: " + "; ".join(offenders))


class PipelineDefaultWorkdirTextTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PIPELINE.is_file(), PIPELINE)
        self.text = PIPELINE.read_text(encoding="utf-8")

    def test_default_workdir_is_stated_as_required(self):
        self.assertRegex(
            self.text,
            r"`--default-workdir`\*{0,2} is required",
            "running-the-pipeline must state --default-workdir as a requirement, "
            "not merely show it in an example",
        )

    def test_says_why_pr_ci_depends_on_it(self):
        # The causal link, not just the word "pr-ci" somewhere in the file.
        window = re.search(r"--default-workdir`\*{0,2} is required(?:.|\n){0,1600}", self.text)
        self.assertIsNotNone(window)
        chunk = window.group(0)
        self.assertIn("pr-ci", chunk)
        self.assertIn("clone to push from", chunk)

    def test_still_shows_the_canonical_creation_command(self):
        self.assertIn('--default-workdir "$TEAM_HOME/repos/<repo-slug>"', self.text)


class ChangelogUnreleasedTests(unittest.TestCase):
    """M6: the Unreleased section carries this release's entries, each with a PR
    number, in Keep a Changelog form."""

    def setUp(self):
        self.path = DEV_TEAM.parent / "CHANGELOG.md"
        self.assertTrue(self.path.is_file(), self.path)
        text = self.path.read_text(encoding="utf-8")
        m = re.search(r"\n## \[Unreleased\]\n(.*?)(?=\n## \[)", text, re.S)
        self.assertIsNotNone(m, "no [Unreleased] section followed by a released one")
        self.section = m.group(1)

    def test_unreleased_is_not_empty(self):
        self.assertTrue(self.section.strip(), "[Unreleased] has no entries")

    def test_only_keep_a_changelog_headings(self):
        headings = re.findall(r"^### (.+)$", self.section, re.M)
        self.assertTrue(headings, "[Unreleased] has no ### category heading")
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertIn(heading, ("Added", "Changed", "Deprecated", "Removed",
                                        "Fixed", "Security"))

    def test_every_entry_carries_a_pr_number(self):
        bullets = [b for b in re.findall(r"^- (?:.|\n(?!\n|- |### ))*", self.section, re.M)]
        self.assertGreaterEqual(len(bullets), 4, bullets)
        for bullet in bullets:
            with self.subTest(bullet=bullet.splitlines()[0][:60]):
                self.assertRegex(bullet, r"\(#\d+(?:, #\d+)*\)")

    def test_the_four_scope_items_are_described(self):
        for token in ("DEV_TEAM_GH_TOKEN", "gh auth setup-git", "kanban_create",
                      "--default-workdir", "redact"):
            with self.subTest(token=token):
                self.assertIn(token, self.section)


if __name__ == "__main__":
    unittest.main()
