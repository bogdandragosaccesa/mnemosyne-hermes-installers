#!/usr/bin/env python3
"""Budget accounting for an outsourcing-team kanban board.

Hermes' kanban has no native cost tracking: `tasks` carries no spend column and
`tasks.session_id` is NULL for dispatcher-spawned workers. But every run records
which profile ran it and when (`task_runs.profile`, `started_at`, `ended_at`),
and each profile's own `state.db` records per-session `estimated_cost_usd`. So
spend is recoverable by correlating a run's time window against that profile's
sessions.

That correlation is the only thing here that is an inference rather than a fact,
and it is reported as such: a session is attributed to a run when it belongs to
the run's profile and starts inside the run's window (plus a small grace, since
the session row is written a moment after the worker is spawned). Sessions that
match no run are reported separately as unattributed rather than silently
dropped, so the total is never quietly wrong.

Usage:
    kanban-budget.py                          # report on the current board
    kanban-budget.py --board os-acme
    kanban-budget.py --budget 40 --json
    kanban-budget.py --set-budget 40          # record the ceiling on the board
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

GRACE_BEFORE = 10.0   # session row can predate the run's recorded start
GRACE_AFTER = 120.0   # ...or outlive its recorded end (final writes, teardown)


def hermes_root() -> Path:
    """The Hermes home, asked of Hermes rather than assumed to be ~/.hermes."""
    if env := os.environ.get("HERMES_HOME"):
        # HERMES_HOME is set per-profile by the gateway
        # (…/.hermes/profiles/<name>), but boards live at the Hermes home root.
        p = Path(env)
        if p.parent.name == "profiles":
            return p.parent.parent
        return p
    try:
        out = subprocess.run(
            ["hermes", "config", "path"], capture_output=True, text=True, timeout=60
        )
        if out.returncode == 0 and out.stdout.strip():
            # `hermes config path` returns the ACTIVE PROFILE's config.yaml
            # (…/.hermes/profiles/<name>/config.yaml) when a profile is active,
            # so .parent is the profile dir, not the Hermes home. Boards live at
            # the home root — climb out of profiles/<name> when we see it.
            cfg = Path(out.stdout.strip()).parent
            if cfg.parent.name == "profiles":
                return cfg.parent.parent
            return cfg
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / ".hermes"


def current_board(root: Path) -> str:
    try:
        out = subprocess.run(
            ["hermes", "kanban", "boards", "show"],
            capture_output=True, text=True, timeout=60,
        )
        for line in out.stdout.splitlines():
            if ":" in line and "board" in line.lower():
                return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "default"


def board_db(root: Path, board: str) -> Path:
    """default lives at the root; every other board under kanban/boards/<slug>/."""
    if board == "default":
        return root / "kanban.db"
    return root / "kanban" / "boards" / board / "kanban.db"


def ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def profile_state_db(root: Path, profile: str) -> Path:
    """The root profile's state.db sits at the top; named profiles get their own."""
    if profile in ("default", "", None):
        return root / "state.db"
    return root / "profiles" / profile / "state.db"


def load_sessions(root: Path, profile: str) -> list[sqlite3.Row]:
    con = ro(profile_state_db(root, profile))
    if con is None:
        return []
    try:
        return list(con.execute(
            "SELECT id, started_at, ended_at, model, "
            "       COALESCE(estimated_cost_usd, 0) AS est, "
            "       COALESCE(actual_cost_usd, 0) AS act, "
            "       cost_status, COALESCE(input_tokens,0) AS itok, "
            "       COALESCE(output_tokens,0) AS otok, "
            "       COALESCE(api_call_count,0) AS calls "
            "FROM sessions WHERE started_at IS NOT NULL"
        ))
    except sqlite3.Error:
        return []
    finally:
        con.close()


def collect(root: Path, board: str) -> dict:
    con = ro(board_db(root, board))
    if con is None:
        sys.exit(f"No kanban database for board '{board}' "
                 f"(looked in {board_db(root, board)})")
    try:
        tasks = {r["id"]: dict(r) for r in con.execute(
            "SELECT id, title, assignee, status, created_at, completed_at FROM tasks"
        )}
        runs = [dict(r) for r in con.execute(
            "SELECT id, task_id, profile, status, outcome, started_at, ended_at "
            "FROM task_runs WHERE started_at IS NOT NULL ORDER BY started_at"
        )]
    finally:
        con.close()

    # Cache each profile's sessions once; a board may have many runs per profile.
    by_profile: dict[str, list[sqlite3.Row]] = {}
    for run in runs:
        p = run["profile"] or "default"
        if p not in by_profile:
            by_profile[p] = load_sessions(root, p)

    claimed: set[tuple[str, str]] = set()   # (profile, session id) already attributed
    for run in runs:
        p = run["profile"] or "default"
        start = float(run["started_at"])
        end = float(run["ended_at"] or start)
        cost = tokens = calls = 0.0
        matched = []
        for s in by_profile.get(p, []):
            key = (p, s["id"])
            if key in claimed:
                continue
            st = float(s["started_at"])
            if start - GRACE_BEFORE <= st <= end + GRACE_AFTER:
                claimed.add(key)
                matched.append(s["id"])
                cost += float(s["act"] or 0) or float(s["est"] or 0)
                tokens += float(s["itok"]) + float(s["otok"])
                calls += float(s["calls"])
        run["cost"] = round(cost, 6)
        run["tokens"] = int(tokens)
        run["api_calls"] = int(calls)
        run["sessions"] = matched

    # Roll up per task and per role.
    per_task: dict[str, dict] = {}
    per_role: dict[str, dict] = {}
    for run in runs:
        t = per_task.setdefault(run["task_id"], {"cost": 0.0, "tokens": 0, "runs": 0})
        t["cost"] += run["cost"]
        t["tokens"] += run["tokens"]
        t["runs"] += 1
        r = per_role.setdefault(run["profile"] or "default",
                                {"cost": 0.0, "tokens": 0, "runs": 0, "tasks": set()})
        r["cost"] += run["cost"]
        r["tokens"] += run["tokens"]
        r["runs"] += 1
        r["tasks"].add(run["task_id"])
    for r in per_role.values():
        r["tasks"] = len(r["tasks"])

    unattributed = 0.0
    for p, sessions in by_profile.items():
        for s in sessions:
            if (p, s["id"]) not in claimed:
                unattributed += float(s["act"] or 0) or float(s["est"] or 0)

    return {
        "board": board,
        "tasks": tasks,
        "runs": runs,
        "per_task": per_task,
        "per_role": per_role,
        "total": round(sum(r["cost"] for r in runs), 6),
        "unattributed": round(unattributed, 6),
    }


def budget_file(root: Path, board: str) -> Path:
    return board_db(root, board).parent / "budget.json"


def read_budget(root: Path, board: str) -> dict:
    p = budget_file(root, board)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
    return {}


def report(data: dict, budget: float | None, reserve_pct: float) -> int:
    """Human-readable ledger. Returns a shell exit code: 2 = over, 1 = past reserve."""
    w = print
    w(f"Board: {data['board']}")
    w("")

    status_of = {t: v["status"] for t, v in data["tasks"].items()}
    done = sum(1 for s in status_of.values() if s == "done")
    w(f"Tasks: {len(data['tasks'])}  "
      f"({done} done, "
      f"{sum(1 for s in status_of.values() if s in ('ready', 'todo'))} queued, "
      f"{sum(1 for s in status_of.values() if s == 'running')} running, "
      f"{sum(1 for s in status_of.values() if s == 'blocked')} blocked, "
      f"{sum(1 for s in status_of.values() if s == 'review')} in review)")
    w("")

    if data["per_role"]:
        w("Spend by role")
        w("─" * 62)
        w(f"  {'ROLE':<22}{'COST':>10}{'RUNS':>7}{'TASKS':>7}{'TOKENS':>14}")
        for role, v in sorted(data["per_role"].items(),
                              key=lambda kv: -kv[1]["cost"]):
            w(f"  {role:<22}{'$%.4f' % v['cost']:>10}{v['runs']:>7}"
              f"{v['tasks']:>7}{v['tokens']:>14,}")
        w("")

    costly = sorted(data["per_task"].items(), key=lambda kv: -kv[1]["cost"])[:10]
    if costly:
        w("Most expensive cards")
        w("─" * 62)
        for tid, v in costly:
            if v["cost"] <= 0:
                continue
            meta = data["tasks"].get(tid, {})
            title = (meta.get("title") or "?")[:38]
            w(f"  {'$%.4f' % v['cost']:>9}  {tid}  {meta.get('assignee','?'):<20} {title}")
        w("")

    w("Total")
    w("─" * 62)
    w(f"  Attributed to cards:  ${data['total']:.4f}")
    if data["unattributed"]:
        w(f"  Unattributed:         ${data['unattributed']:.4f}  "
          f"(profile sessions outside any run window — interactive chats, "
          f"other boards)")

    rc = 0
    if budget:
        spent = data["total"]
        pct = (spent / budget * 100) if budget else 0
        reserve = budget * reserve_pct / 100
        usable = budget - reserve
        w(f"  Budget:               ${budget:.2f}")
        w(f"  Spent:                {pct:.1f}% of budget")
        w(f"  Reserve held back:    ${reserve:.2f} ({reserve_pct:.0f}%)")
        w(f"  Remaining (usable):   ${max(usable - spent, 0):.4f}")
        w("")
        if spent >= budget:
            w("  ** BUDGET EXHAUSTED — stop, report, do not start new cards. **")
            rc = 2
        elif spent >= usable:
            w("  ** Into the closeout reserve. Cut scope down the ladder; "
              "reserve is for QA, handover and the ledger only. **")
            rc = 1
        else:
            for mark in (75, 50, 25):
                if pct >= mark:
                    w(f"  Past the {mark}% checkpoint — compare spend against "
                      f"ACCEPTED cards and re-plan if delivery is lagging.")
                    break
    else:
        w("  No budget recorded. Set one: kanban-budget.py --set-budget 40")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Per-card, per-role and total spend for a kanban board.")
    ap.add_argument("--board", help="Board slug (default: the active board)")
    ap.add_argument("--budget", type=float,
                    help="Budget ceiling in USD for this report")
    ap.add_argument("--set-budget", type=float, metavar="USD",
                    help="Record the ceiling on the board and exit")
    ap.add_argument("--reserve-pct", type=float, default=15.0,
                    help="Closeout reserve held back (default: 15)")
    ap.add_argument("--json", action="store_true", help="Emit JSON")
    args = ap.parse_args()

    root = hermes_root()
    board = args.board or current_board(root)

    if args.set_budget is not None:
        p = budget_file(root, board)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = read_budget(root, board)
        rec.update({"budget_usd": args.set_budget,
                    "reserve_pct": args.reserve_pct})
        p.write_text(json.dumps(rec, indent=2) + "\n")
        print(f"Budget for board '{board}': ${args.set_budget:.2f} "
              f"(reserve {args.reserve_pct:.0f}%)")
        print(f"Recorded in {p}")
        return 0

    data = collect(root, board)
    rec = read_budget(root, board)
    budget = args.budget if args.budget is not None else rec.get("budget_usd")
    reserve = args.reserve_pct if args.reserve_pct != 15.0 else rec.get(
        "reserve_pct", args.reserve_pct)

    if args.json:
        data["budget_usd"] = budget
        data["reserve_pct"] = reserve
        if budget:
            data["pct_spent"] = round(data["total"] / budget * 100, 2)
            data["remaining_usable"] = round(
                max(budget * (1 - reserve / 100) - data["total"], 0), 6)
        print(json.dumps(data, indent=2, default=str))
        return 0

    return report(data, budget, reserve)


if __name__ == "__main__":
    sys.exit(main())
