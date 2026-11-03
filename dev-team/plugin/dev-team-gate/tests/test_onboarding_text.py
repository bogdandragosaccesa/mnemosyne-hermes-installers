"""Static skill-text checks: unattended onboarding, no global board switch.

Task 1 of the brief (t_9f980dfe): the pm creates a repository's board with
`--default-workdir <team home>/repos/<slug>` at creation time. Onboarding no
longer has a `set-default-workdir` step — devops only clones, excludes
`.worktrees/`, and runs `forge detect`. Nothing in the team may change the
machine-wide current board, so no skill, soul or README may instruct an
agent to run `hermes kanban boards switch` or
`hermes kanban boards set-default-workdir`.

Round 2 (t_6d27c3af) fixes on top of that:

- `$HERMES_HOME` is per installed profile, but every profile's `dev-team` is
  a symlink into the same shared tree, so a raw `$HERMES_HOME/dev-team/...`
  string is not a stable cross-profile identifier. The board-creation
  command and the README must resolve a canonical team-home path (`pwd -P`)
  instead of embedding the profile-relative string literally.
- Every `hermes kanban ls`/`hermes kanban show` the pm or README documents
  for this pipeline must name its board explicitly with `--board`, and
  nothing may describe a now-nonexistent "switched per repository" step.
"""
import re
import unittest
from pathlib import Path

DEV_TEAM = Path(__file__).resolve().parents[3]

FORBIDDEN = re.compile(
    r"boards\s+switch|boards\s+use|set-default-workdir|"
    # D7 (PR #8 review): "boards create ... --switch" also mutates the
    # machine-wide current board. Bounded to 200 chars so an unrelated
    # later command elsewhere in the same file is never swept in.
    r"boards\s+create(?:.){0,200}?--switch",
    re.S,
)

# Lines that pair the forbidden phrase with an explicit prohibition are
# tolerated deliberately (acceptance criterion 1 allows it) because they
# tell an agent never to run the command, not to run it. Round 1 tolerated
# any line containing a negation marker anywhere in it, which a probe showed
# is too wide (a sentence that both forbids and then instructs still passed,
# e.g. "Never run that; instead run `hermes kanban boards switch x`.").
# Round 2 replaces that marker regex with an explicit allowlist of the exact
# tolerated lines, so only these two specific, hand-reviewed prohibition
# sentences are exempt — anything else containing the forbidden phrase is an
# offender, including a line that merely contains a negation word elsewhere.
ALLOWED_PROHIBITION_LINES = {
    "Nothing in the team may run `hermes kanban boards switch`: it mutates the machine-wide current",
    "`--board <repo-slug>`. Nothing in the team runs `hermes kanban boards switch` — it mutates the",
}

# Text files this test scans: every skill, every soul, and the README.
TEXT_FILES = sorted(DEV_TEAM.glob("skills/*/SKILL.md")) + sorted(
    DEV_TEAM.glob("souls/*.md")
) + [DEV_TEAM / "README.md"]

PIPELINE_SKILL = DEV_TEAM / "skills" / "running-the-pipeline" / "SKILL.md"
README = DEV_TEAM / "README.md"

# A raw, profile-relative team-home path. Every installed profile's
# `$HERMES_HOME` points at its own profile directory, so embedding this
# string literally in a `--default-workdir` value makes the board's stored
# path diverge across the pm's and devops' profiles even though both
# `dev-team` directories are symlinks to the same real tree.
RAW_PROFILE_RELATIVE_TEAM_HOME = re.compile(
    r"--default-workdir[^\n]*\$HERMES_HOME/dev-team(?!-prefix)"
)

# A `hermes kanban ls` / `hermes kanban show` invocation that does not name
# its board explicitly with `--board`.
LEDGER_COMMAND = re.compile(r"hermes kanban (ls|show)\b")


def find_forbidden_offenders(text):
    """D7/F4 (PR #8 review r2/r3): the one real scan, shared by the
    production check below and its split-line-probe test, so the probe
    exercises exactly this logic rather than a parallel reimplementation
    of it. Matches across line breaks (``flat``) and honours the explicit
    prohibition allowlist. Returns a list of (lineno, line, match).

    F4 (review r3 nit): a shell line continuation (``boards \\`` then a
    newline, then ``switch``) leaves a literal backslash between the two
    halves once newlines are flattened to spaces, which a plain
    ``\\s+`` gap does not match. Collapse a backslash-newline pair to two
    spaces FIRST (same length, so lineno offsets below stay aligned),
    before flattening any remaining newlines."""
    offenders = []
    flat = re.sub(r"\\\n", "  ", text)  # shell continuation -> two spaces, same length
    flat = flat.replace("\n", " ")  # remaining newlines -> one space, same length as text
    for m in FORBIDDEN.finditer(flat):
        lineno = text.count("\n", 0, m.start()) + 1
        line = text.splitlines()[lineno - 1].strip()
        if line in ALLOWED_PROHIBITION_LINES:
            continue
        offenders.append((lineno, line, m))
    return offenders


class NoGlobalBoardSwitchTest(unittest.TestCase):
    def test_text_files_exist(self):
        # Guard against a glob/path typo silently matching nothing.
        self.assertGreaterEqual(len(TEXT_FILES), 9, TEXT_FILES)
        for path in TEXT_FILES:
            self.assertTrue(path.is_file(), path)

    def test_no_file_instructs_boards_switch_or_set_default_workdir(self):
        """D7 (PR #8 review): matches across line breaks -- a forbidden
        command split across two lines (e.g. a long `hermes kanban boards
        create ... --switch` wrapped for readability) must not slip past
        this check the way a strictly per-line regex would."""
        offenders = []
        for path in TEXT_FILES:
            text = path.read_text(encoding="utf-8")
            for lineno, line, m in find_forbidden_offenders(text):
                offenders.append(f"{path.relative_to(DEV_TEAM)}:{lineno}: {line} [matched: {m.group(0)!r}]")
        self.assertEqual(
            offenders,
            [],
            "no skill, soul or README may instruct `boards switch`, `boards use`, "
            "`set-default-workdir`, or `boards create ... --switch`; found:\n" + "\n".join(offenders),
        )

    def test_forbidden_regex_matches_a_command_split_across_two_lines(self):
        """Red-before/green-after proof for D7's cross-line-break claim,
        exercised against find_forbidden_offenders -- the actual
        production scan above, not a reimplementation of it -- reading
        the match back from a real file on disk (F4 review r3 nit: an
        earlier version of this test wrote to a tempfile but matched
        against the in-memory `text` string instead, so the tempfile did
        nothing)."""
        import tempfile
        cases = [
            "Run `hermes kanban boards\nswitch team-a` first.\n",
            "Then `hermes kanban boards use team-a`.\n",
            "`hermes kanban boards create repo-x --default-workdir /x\n  --switch`\n",
            # F4 (review r3): a shell continuation, e.g. a wrapped command
            # in a code block: `hermes kanban boards \` then a newline,
            # then `switch team-a`.
            "```\nhermes kanban boards \\\nswitch team-a\n```\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "probe.md"
                    path.write_text(text, encoding="utf-8")
                    offenders = find_forbidden_offenders(path.read_text(encoding="utf-8"))
                    self.assertTrue(offenders, f"FORBIDDEN did not match: {text!r}")

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

    def test_default_workdir_is_not_raw_profile_relative_path(self):
        """--default-workdir must use a resolved canonical path, not the raw
        profile-relative `$HERMES_HOME/dev-team` string (round 2, finding 1):
        every installed profile's `$HERMES_HOME` is its own directory, so
        that raw string stores a different path per profile even though all
        profiles' `dev-team` resolves to the same real tree."""
        for path in (PIPELINE_SKILL, README):
            text = path.read_text(encoding="utf-8")
            offenders = [
                line.strip()
                for line in text.splitlines()
                if RAW_PROFILE_RELATIVE_TEAM_HOME.search(line)
            ]
            self.assertEqual(
                offenders,
                [],
                f"{path.relative_to(DEV_TEAM)}: --default-workdir must use a resolved "
                f"canonical path (e.g. $TEAM_HOME from `pwd -P`), not the raw "
                f"profile-relative $HERMES_HOME/dev-team; found: {offenders}",
            )
            self.assertIn(
                'pwd -P',
                text,
                f"{path.relative_to(DEV_TEAM)} must resolve the canonical team-home "
                "path with `pwd -P` before using it in --default-workdir",
            )

    def test_ledger_commands_name_board_explicitly(self):
        """Every `hermes kanban ls`/`hermes kanban show` the pm or README
        documents for this pipeline must carry `--board` on the same line
        (round 2, finding 2): without it the command reads the machine-wide
        current board instead of this repository's board."""
        offenders = []
        for path in (PIPELINE_SKILL, README):
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if LEDGER_COMMAND.search(line) and "--board" not in line:
                    offenders.append(f"{path.relative_to(DEV_TEAM)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "every `hermes kanban ls`/`hermes kanban show` must name its board "
            "explicitly with --board; found:\n" + "\n".join(offenders),
        )

    def test_no_switched_per_repository_wording(self):
        """The board is never switched (per-repository or otherwise) —
        every command names it explicitly. No text file may describe a
        now-removed 'switched per repository' step (round 2, finding 2)."""
        offenders = []
        for path in TEXT_FILES:
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if "switched per repository" in line.lower():
                    offenders.append(f"{path.relative_to(DEV_TEAM)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "stale 'switched per repository' wording found:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
