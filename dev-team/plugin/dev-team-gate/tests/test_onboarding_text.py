"""Static skill-text checks: unattended onboarding, no global board switch.

Task 1 of the brief (t_9f980dfe): the pm creates a repository's board with
`--default-workdir <team home>/repos/<slug>` at creation time. Onboarding no
longer has a `set-default-workdir` step — devops only clones, excludes
`.worktrees/`, and runs `forge detect`. Nothing in the team may change the
machine-wide current board, so no skill, soul or README may instruct an
agent to run `hermes kanban boards switch` or
`hermes kanban boards set-default-workdir`.
"""
import re
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]

FORBIDDEN = re.compile(r"boards switch|set-default-workdir")

# A sentence that explicitly *forbids* running the command is tolerated
# deliberately (acceptance criterion 1 allows it) because it does not tell
# an agent to run the command — it tells the agent never to. Only lines
# that pair the forbidden phrase with one of these negation markers count
# as a prohibition rather than an instruction.
PROHIBITION_MARKER = re.compile(
    r"never (run|switch)|nothing in the team (runs|may run)"
    r"|must not run|may not run|never run",
    re.IGNORECASE,
)

# Text files this test scans: every skill, every soul, and the README.
TEXT_FILES = sorted(DEV_TEAM.glob("skills/*/SKILL.md")) + sorted(
    DEV_TEAM.glob("souls/*.md")
) + [DEV_TEAM / "README.md"]

PIPELINE_SKILL = DEV_TEAM / "skills" / "running-the-pipeline" / "SKILL.md"


class NoGlobalBoardSwitchTest(unittest.TestCase):
    def test_text_files_exist(self):
        # Guard against a glob/path typo silently matching nothing.
        self.assertGreaterEqual(len(TEXT_FILES), 9, TEXT_FILES)
        for path in TEXT_FILES:
            self.assertTrue(path.is_file(), path)

    def test_no_file_instructs_boards_switch_or_set_default_workdir(self):
        offenders = []
        for path in TEXT_FILES:
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if FORBIDDEN.search(line) and not PROHIBITION_MARKER.search(line):
                    offenders.append(f"{path.relative_to(DEV_TEAM)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "no skill, soul or README may instruct `boards switch` or "
            "`set-default-workdir`; found:\n" + "\n".join(offenders),
        )

    def test_pipeline_skill_board_creation_includes_default_workdir(self):
        text = PIPELINE_SKILL.read_text(encoding="utf-8")
        create_lines = [
            line for line in text.splitlines() if "kanban boards create" in line
        ]
        self.assertTrue(create_lines, "no `hermes kanban boards create` command found")
        self.assertTrue(
            any("--default-workdir" in line for line in create_lines),
            "the pipeline skill's board-creation command must include "
            f"--default-workdir; found: {create_lines}",
        )


if __name__ == "__main__":
    unittest.main()
