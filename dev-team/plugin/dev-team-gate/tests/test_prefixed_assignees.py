"""Brief item 9: installed profiles are prefixed (`dev-` by default, `--prefix`),
so no skill or soul may tell an agent to assign a card to a bare role name.

A bare `assignee="qa"` makes the dispatcher spawn the unprefixed `qa` profile:
a different team's worker, or an unguarded root profile, runs the card.
"""
import re
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]
ROLES = ("pm", "architect", "uiux", "backend-db", "backend", "frontend", "qa", "reviewer", "devops")
PREFIX_FILE = "dev-team-prefix"

# A role word standing alone: not part of a longer word or hyphenated name.
_ROLE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(r) for r in ROLES) + r")(?![\w-])")
_ASSIGNEE_LITERAL = re.compile(r"""assignee\s*=\s*["']([^"']*)["']""")


def _docs():
    files = sorted((DEV_TEAM / "skills").glob("*/SKILL.md")) + sorted(
        (DEV_TEAM / "souls").glob("SOUL-*.md"))
    assert files, f"no skills or souls under {DEV_TEAM}"
    return files


class PrefixedAssigneeTests(unittest.TestCase):
    def test_no_assignee_literal_is_a_bare_role(self):
        for path in _docs():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for value in _ASSIGNEE_LITERAL.findall(line):
                    with self.subTest(file=path.name, line=n):
                        self.assertTrue(value.startswith("<prefix>"),
                                        f"{path}:{n}: assignee={value!r} is not prefixed")

    def test_no_line_about_assignees_names_a_bare_role(self):
        for path in _docs():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "assignee" not in line.lower() and "kanban_create" not in line:
                    continue
                for m in _ROLE.finditer(line):
                    with self.subTest(file=path.name, line=n, role=m.group(1)):
                        self.assertTrue(line[: m.start()].endswith("<prefix>"),
                                        f"{path}:{n}: bare role {m.group(1)!r}: {line.strip()}")

    def test_docs_that_assign_say_where_the_prefix_comes_from(self):
        for path in _docs():
            text = path.read_text(encoding="utf-8")
            if "<prefix>" in text:
                with self.subTest(file=path.name):
                    self.assertIn(PREFIX_FILE, text, f"{path} uses <prefix> without naming "
                                  f"the {PREFIX_FILE} file it is read from")

    def test_every_stage_that_creates_a_child_uses_a_prefixed_assignee(self):
        for stage in ("intake", "plan", "implement", "validate", "review", "pr-ci"):
            text = (DEV_TEAM / "skills" / f"dev-team-stage-{stage}" / "SKILL.md").read_text(
                encoding="utf-8")
            with self.subTest(stage=stage):
                self.assertTrue('assignee="<prefix>' in text, f"{stage}: no prefixed assignee")

    def test_installers_write_the_prefix_file(self):
        unix = (DEV_TEAM / "install-dev-team-unix.sh").read_text(encoding="utf-8")
        win = (DEV_TEAM / "install-dev-team-windows.ps1").read_text(encoding="utf-8")
        self.assertTrue(re.search(
            r'printf\s+.%s\\n.\s+"\$PREFIX"\s*>\s*"\$profile_dir/dev-team-prefix"', unix),
            "unix installer does not write $profile_dir/dev-team-prefix")
        self.assertTrue("'dev-team-prefix'" in win, "windows installer does not write dev-team-prefix")


if __name__ == "__main__":
    unittest.main()
