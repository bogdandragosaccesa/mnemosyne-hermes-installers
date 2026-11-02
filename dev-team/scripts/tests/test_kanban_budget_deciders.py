"""Tests for decision-model ledger spend in kanban-budget.py.

Builds a minimal, throwaway Hermes root per test: a board kanban.db (tasks +
task_runs), a profile state.db (sessions), and a deciders-ledger.jsonl. No
test touches a real Hermes install.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
