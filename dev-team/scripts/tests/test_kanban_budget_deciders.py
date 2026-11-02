"""Tests for decision-model ledger spend in kanban-budget.py.

Builds a minimal, throwaway Hermes root per test: a board kanban.db (tasks +
task_runs), a profile state.db (sessions), and a deciders-ledger.jsonl. No
test touches a real Hermes install.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent.parent / "kanban-budget.py"
spec = importlib.util.spec_from_file_location("kanban_budget", MODULE_PATH)
kanban_budget = importlib.util.module_from_spec(spec)
sys.modules["kanban_budget"] = kanban_budget
spec.loader.exec_module(kanban_budget)


def _make_board_db(root: Path, board: str, runs: list[dict]) -> None:
    """One task per run, each run attributed the given cost via a matching
    session (started_at inside the run's window)."""
    db_path = kanban_budget.board_db(root, board)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, "
        "status TEXT, created_at REAL, completed_at REAL)"
    )
    con.execute(
        "CREATE TABLE task_runs (id TEXT PRIMARY KEY, task_id TEXT, "
        "profile TEXT, status TEXT, outcome TEXT, started_at REAL, "
        "ended_at REAL)"
    )
    sessions_by_profile: dict[str, list[tuple]] = {}
    for i, r in enumerate(runs):
        task_id = f"t_{i}"
        profile = r.get("profile", "worker")
        start = float(r["start"])
        end = float(r.get("end", start + 1))
        con.execute(
            "INSERT INTO tasks VALUES (?,?,?,?,?,?)",
            (task_id, f"task {i}", profile, "done", start, end),
        )
        con.execute(
            "INSERT INTO task_runs VALUES (?,?,?,?,?,?,?)",
            (f"run_{i}", task_id, profile, "done", "done", start, end),
        )
        sessions_by_profile.setdefault(profile, []).append(
            (f"s_{i}", start, end, "model-x", r["cost"], r["cost"], "actual", 0, 0, 0)
        )
    con.commit()
    con.close()

    for profile, sessions in sessions_by_profile.items():
        spath = kanban_budget.profile_state_db(root, profile)
        spath.parent.mkdir(parents=True, exist_ok=True)
        scon = sqlite3.connect(spath)
        scon.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, "
            "ended_at REAL, model TEXT, estimated_cost_usd REAL, "
            "actual_cost_usd REAL, cost_status TEXT, input_tokens INTEGER, "
            "output_tokens INTEGER, api_call_count INTEGER)"
        )
        scon.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)", sessions)
        scon.commit()
        scon.close()


def _write_ledger(root: Path, records: list[dict]) -> None:
    path = kanban_budget.ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _write_ledger_raw(root: Path, lines: list[str]) -> None:
    path = kanban_budget.ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for line in lines:
            f.write(line + "\n")


class DecidersSpendFiltersBoardTests(unittest.TestCase):
    def test_deciders_spend_filters_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_ledger(root, [
                {"board": "a", "cost": 0.10},
                {"board": "a", "cost": 0.05},
                {"board": "b", "cost": 100.0},
            ])
            self.assertAlmostEqual(
                kanban_budget.deciders_spend(root, "a"), 0.15)


class TotalIncludesDecidersTests(unittest.TestCase):
    def test_total_includes_deciders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_board_db(root, "a", [{"start": 100.0, "end": 105.0, "cost": 0.5}])
            _write_ledger(root, [{"board": "a", "cost": 0.25}])
            data = kanban_budget.collect(root, "a")
            self.assertAlmostEqual(data["total"], 0.75)
            self.assertAlmostEqual(data["deciders"], 0.25)


class LedgerDrivesExitCodeTests(unittest.TestCase):
    def _run(self, root: Path) -> int:
        data = kanban_budget.collect(root, "a")
        buf = io.StringIO()
        with redirect_stdout(buf):
            return kanban_budget.report(data, budget=1.0, reserve_pct=15.0)

    def test_ledger_drives_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_board_db(root, "a", [{"start": 100.0, "end": 105.0, "cost": 0.80}])

            # No ledger line: $0.80 total, usable line is $0.85 -> exit 0.
            self.assertEqual(self._run(root), 0)

            # +$0.10 ledger -> $0.90, into the $0.85 reserve line -> exit 1.
            _write_ledger(root, [{"board": "a", "cost": 0.10}])
            self.assertEqual(self._run(root), 1)

            # +$0.25 more ledger -> $1.05 total, >= $1.00 budget -> exit 2.
            _write_ledger(root, [{"board": "a", "cost": 0.10}, {"board": "a", "cost": 0.25}])
            self.assertEqual(self._run(root), 2)


class MissingOrCorruptLedgerTests(unittest.TestCase):
    def test_missing_ledger_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(kanban_budget.deciders_spend(root, "a"), 0.0)

    def test_corrupt_line_skipped_and_warned_never_crashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_ledger_raw(root, [
                json.dumps({"board": "a", "cost": 0.10}),
                "{not valid json",
                json.dumps({"board": "a", "cost": 0.05}),
            ])
            buf = io.StringIO()
            with redirect_stderr(buf):
                total = kanban_budget.deciders_spend(root, "a")
            self.assertAlmostEqual(total, 0.15)
            self.assertIn("1", buf.getvalue())


class I1NonFiniteOrNegativeCostTests(unittest.TestCase):
    """I1: NaN, infinite, negative, or non-numeric-string costs must never
    silently poison the total -- they are corrupt lines, excluded from the
    sum, and the report fails closed (exit 2) rather than looking healthy."""

    def _ledger_with(self, cost_line: str) -> Path:
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        _write_ledger_raw(root, [
            json.dumps({"board": "a", "cost": 0.10}),
            cost_line,
        ])
        return root

    def _spend_and_corrupt(self, cost_line: str):
        root = self._ledger_with(cost_line)
        buf = io.StringIO()
        with redirect_stderr(buf):
            total = kanban_budget.deciders_spend(root, "a")
        return total, buf.getvalue()

    def test_nan_cost_is_excluded_and_warned(self):
        total, warning = self._spend_and_corrupt('{"board": "a", "cost": NaN}')
        self.assertAlmostEqual(total, 0.10)
        self.assertIn("1", warning)

    def test_string_nan_cost_is_excluded_and_warned(self):
        total, warning = self._spend_and_corrupt('{"board": "a", "cost": "nan"}')
        self.assertAlmostEqual(total, 0.10)
        self.assertIn("1", warning)

    def test_negative_cost_is_excluded_and_warned(self):
        total, warning = self._spend_and_corrupt('{"board": "a", "cost": -50}')
        self.assertAlmostEqual(total, 0.10)
        self.assertIn("1", warning)

    def test_negative_exponent_string_cost_is_excluded_and_warned(self):
        total, warning = self._spend_and_corrupt('{"board": "a", "cost": "-1e9"}')
        self.assertAlmostEqual(total, 0.10)
        self.assertIn("1", warning)

    def test_bool_cost_is_excluded_and_warned(self):
        total, warning = self._spend_and_corrupt('{"board": "a", "cost": true}')
        self.assertAlmostEqual(total, 0.10)
        self.assertIn("1", warning)

    def test_corrupt_cost_line_fails_closed_in_report(self):
        root = self._ledger_with('{"board": "a", "cost": NaN}')
        _make_board_db(root, "a", [{"start": 100.0, "end": 105.0, "cost": 0.0}])
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            data = kanban_budget.collect(root, "a")
            rc = kanban_budget.report(data, budget=1000.0, reserve_pct=15.0)
        # Budget is enormous and nowhere near exceeded by the valid $0.10 --
        # only the corrupt line can explain a non-zero exit here.
        self.assertEqual(rc, 2)


class I2TeamHomeResolutionTests(unittest.TestCase):
    """I2: `deciders` resolves $HERMES_HOME/dev-team directly (never climbed
    out of a profile). kanban-budget.py must read the same ledger, even
    when the live team was installed rooted at a profile -- resolve_team_home()
    must do the same un-climbed lookup, and collect()/main() must use it for
    the ledger (while hermes_root()'s climbed path keeps locating board.db)."""

    def test_resolve_team_home_does_not_climb_a_profile_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"HERMES_HOME": f"{tmp}/profiles/dev-pm"}):
            self.assertEqual(kanban_budget.resolve_team_home(), Path(f"{tmp}/profiles/dev-pm"))

    def test_resolve_team_home_defaults_to_dot_hermes(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(Path, "home", return_value=Path("/fakehome")):
            self.assertEqual(kanban_budget.resolve_team_home(), Path("/fakehome/.hermes"))

    def test_budget_counts_what_deciders_wrote_from_a_profile_rooted_install(self):
        # Reproduces I2's live failure: the team was installed while
        # HERMES_HOME pointed at the devops profile, so `dev-team/` (and the
        # ledger inside it) lives under profiles/devops, not at the climbed
        # Hermes root. `deciders` itself, run later as profile dev-pm, writes
        # through dev-pm's own `dev-team` symlink -- which also points at
        # profiles/devops/dev-team. Both tools must land on that one file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)  # the climbed Hermes root, where board.db lives
            real_team_home = root / "profiles" / "devops" / "dev-team"
            real_team_home.mkdir(parents=True)
            (real_team_home / "deciders-ledger.jsonl").write_text(
                json.dumps({"board": "a", "cost": 0.30}) + "\n")

            pm_team_home = root / "profiles" / "dev-pm"
            pm_team_home.mkdir(parents=True)
            (pm_team_home / "dev-team").symlink_to(real_team_home, target_is_directory=True)

            _make_board_db(root, "a", [{"start": 100.0, "end": 105.0, "cost": 0.0}])

            data = kanban_budget.collect(root, "a", team_home=pm_team_home)
            self.assertAlmostEqual(data["deciders"], 0.30)
            self.assertAlmostEqual(data["total"], 0.30)

    def test_main_passes_resolve_team_home_to_collect(self):
        # The wiring itself: main() must not default collect() to the
        # climbed hermes_root() for the ledger.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real_team_home = root / "profiles" / "devops" / "dev-team"
            real_team_home.mkdir(parents=True)
            (real_team_home / "deciders-ledger.jsonl").write_text(
                json.dumps({"board": "a", "cost": 0.30}) + "\n")
            pm_home = root / "profiles" / "dev-pm"
            pm_home.mkdir(parents=True)
            (pm_home / "dev-team").symlink_to(real_team_home, target_is_directory=True)
            _make_board_db(root, "a", [{"start": 100.0, "end": 105.0, "cost": 0.0}])

            with mock.patch.dict(os.environ, {"HERMES_HOME": str(pm_home)}), \
                    mock.patch.object(kanban_budget, "hermes_root", return_value=root), \
                    mock.patch.object(kanban_budget, "current_board", return_value="a"), \
                    mock.patch.object(sys, "argv", ["kanban-budget.py", "--budget", "1000"]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = kanban_budget.main()
            self.assertEqual(rc, 0)
            self.assertIn("Decision models:      $0.3000", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
