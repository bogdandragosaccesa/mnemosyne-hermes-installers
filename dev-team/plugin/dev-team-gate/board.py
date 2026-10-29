"""dev-team board view: the slice of the Hermes kanban board the guard reads.

`Board` is the protocol the guard depends on; tests satisfy it with a fake.
`KanbanBoard` is the real implementation and imports Hermes lazily, inside its
methods, so importing this module (and running the tests) never imports Hermes.

Read-only. Nothing here writes to the board.
"""
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

try:  # loaded as the plugin package
    from .stages import stage_from_skills
except ImportError:  # loaded flat, as the tests do
    from stages import stage_from_skills


@dataclass
class TaskView:
    id: str
    skills: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    # None means unknown; the guard treats unknown as "cannot verify".
    status: str | None = None
    block_kind: str | None = None


class Board(Protocol):
    def task(self, id: str) -> TaskView | None:
        """The card, or None when no card has this id."""

    def last_metadata(self, id: str) -> dict | None:
        """`metadata` of the card's last completed run, or None."""


class KanbanBoard:
    """Board over the real Hermes kanban DB.

    Names checked against the installed Hermes sources
    (`~/.hermes/hermes-agent/hermes_cli/`): `kanban_db_connect.connect_closing`
    (the closing wrapper around `connect`; a bare sqlite3 connection's context
    manager does not close the fd), `kanban_db.get_task`, `parent_ids`,
    `child_ids`, and `list_runs(..., state_type="outcome", state_name="completed")`
    for the last *completed* run. `latest_run` returns the newest run regardless
    of outcome, which is not what the design's "parent card's last completed run"
    asks for. `Run.metadata` is the JSON-decoded `task_runs.metadata` column.

    `board` defaults to `HERMES_KANBAN_BOARD`. Pass the `board` argument of the
    guarded tool call when it has one, so the guard reads the board the call
    writes to.
    """

    def __init__(self, board: str | None = None):
        self._board = board if board is not None else os.environ.get("HERMES_KANBAN_BOARD")

    def _connect(self):
        from hermes_cli.kanban_db_connect import connect_closing

        return connect_closing(board=self._board)

    def task(self, id: str) -> TaskView | None:
        from hermes_cli import kanban_db

        with self._connect() as conn:
            t = kanban_db.get_task(conn, id)
            if t is None:
                return None
            return TaskView(
                id=t.id,
                skills=list(t.skills or []),
                parents=list(kanban_db.parent_ids(conn, id)),
                children=list(kanban_db.child_ids(conn, id)),
                status=getattr(t, "status", None),
                block_kind=getattr(t, "block_kind", None),
            )

    def last_metadata(self, id: str) -> dict | None:
        from hermes_cli import kanban_db

        with self._connect() as conn:
            runs = kanban_db.list_runs(
                conn, id, include_active=False,
                state_type="outcome", state_name="completed",
            )
        if not runs:
            return None
        md = runs[-1].metadata
        return md if isinstance(md, dict) else None


def _require(board: Board, id: str) -> TaskView:
    view = board.task(id)
    if view is None:
        raise LookupError(f"card {id} not found on the board")
    return view


def ancestors(board: Board, task_id: str, include_self: bool = True) -> list[TaskView]:
    """Every card reachable through parent links, each once, breadth first.

    Raises LookupError when any card on the way is missing: an ancestry with a
    hole cannot be counted, and the guard fails closed on it.
    """
    seen = {task_id}
    queue = deque([task_id])
    out = []
    while queue:
        view = _require(board, queue.popleft())
        if include_self or view.id != task_id:
            out.append(view)
        for p in view.parents:
            if p not in seen:
                seen.add(p)
                queue.append(p)
    return out


def implement_rounds(board: Board, task_id: str) -> int:
    """Count `implement` cards in the card's ancestry, the card itself included."""
    return sum(
        1 for v in ancestors(board, task_id) if stage_from_skills(v.skills) == "implement"
    )


def project_slug(board: Board, task_id: str) -> str:
    """The forge project slug: the `repo` of the one intake card among the ancestors.

    Raises LookupError when there is no such intake card, more than one distinct
    `repo`, or a `repo` that is not a non-empty string.
    """
    repos = set()
    for v in ancestors(board, task_id):
        if stage_from_skills(v.skills) != "intake":
            continue
        md = board.last_metadata(v.id) or {}
        repo = md.get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise LookupError(f"intake card {v.id} has no completed `repo`")
        repos.add(repo.strip())
    if len(repos) != 1:
        raise LookupError(
            f"expected one intake `repo` among the ancestors of {task_id}, found {sorted(repos)}"
        )
    return repos.pop()
