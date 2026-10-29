"""dev-team guard: the `kanban_complete` check.

`check_complete` returns None to let the call through, or a refusal message the
worker sees as the tool error. It fails closed: any exception, missing card or
unverifiable state becomes a refusal naming what could not be verified. Only a
card with no dev-team stage skill is let through without checks.

Refusal messages start with a class label (`metadata:`, `head_sha:`, `plan:`,
`created_cards:`, `child:`, `review: rounds-exhausted`, `forge: unreachable`,
`ci:`, `guard:`), so a worker that must block can reuse the label.
"""
from __future__ import annotations

from typing import Callable

try:  # loaded as the plugin package
    from .board import Board, implement_rounds, project_slug
    from .stages import expected_child, stage_from_skills, validate_metadata
except ImportError:  # loaded flat, as the tests do
    from board import Board, implement_rounds, project_slug
    from stages import expected_child, stage_from_skills, validate_metadata

ROUND_LIMIT = 3

# stage -> the stage of the parent card whose head_sha it must repeat.
_PARENT_STAGE = {"validate": "implement", "review": "validate", "pr-ci": "review"}

_DETAIL_MAX = 300


class _Refuse(Exception):
    pass


def _describe(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 3] + "..."


def check_complete(
    board: Board, task_id: str, args: dict, ci_status: Callable[[str, str], str]
) -> str | None:
    """Rules, in order: unguarded card passes; metadata valid; head_sha equals
    the parent stage's; exactly one right child; round limit; CI re-verified."""
    try:
        return _check(board, task_id, args, ci_status)
    except _Refuse as refusal:
        return str(refusal)
    except Exception as exc:  # fail closed
        return f"guard: cannot verify card {task_id}, refusing ({_describe(exc)})"


def _view_and_stage(board: Board, id: str, label: str):
    view = board.task(id)
    if view is None:
        raise _Refuse(f"{label}: card {id} not found on the board")
    try:
        return view, stage_from_skills(view.skills)
    except ValueError as exc:
        raise _Refuse(f"{label}: card {id}: {exc}") from None


def _stage_of(board: Board, id: str, label: str) -> str | None:
    return _view_and_stage(board, id, label)[1]


def _check(board, task_id, args, ci_status):
    # 1. Unguarded card.
    view = board.task(task_id)
    if view is None:
        raise _Refuse(f"guard: card {task_id} not found on the board")
    stage = stage_from_skills(view.skills)
    if stage is None:
        return None

    if not isinstance(args, dict):
        raise _Refuse("metadata: tool arguments must be an object")
    md = args.get("metadata")

    # 2. Stage table.
    errors = validate_metadata(stage, md)
    if errors:
        raise _Refuse(f"metadata: stage {stage}: " + "; ".join(errors))
    assert isinstance(md, dict)  # validate_metadata rejects anything else

    # 3. head_sha carried over from the parent stage card's last completed run.
    want = _PARENT_STAGE.get(stage)
    if want:
        parents = [p for p in view.parents if _stage_of(board, p, "head_sha") == want]
        if len(parents) != 1:
            raise _Refuse(
                f"head_sha: card {task_id} must have exactly one {want} parent card, "
                f"found {len(parents)}"
            )
        pmd = board.last_metadata(parents[0])
        theirs = pmd.get("head_sha") if isinstance(pmd, dict) else None
        ours = md["head_sha"]
        if not isinstance(theirs, str) or theirs.lower() != ours.lower():
            raise _Refuse(
                f"head_sha: {ours} does not match the head_sha {theirs!r} of {want} "
                f"card {parents[0]}'s last completed run"
            )

    # Plan: every card it names exists and carries a stage skill.
    if stage == "plan":
        for cid in md["cards"]:
            if _stage_of(board, cid, "plan") is None:
                raise _Refuse(f"plan: card {cid} carries no dev-team-stage-* skill")

    # 4. Exactly one child of the expected stage, parented on this card.
    child = expected_child(stage, md)
    if child is not None:
        created = args.get("created_cards")
        if created is None:
            created = []
        if not isinstance(created, list) or not all(isinstance(c, str) for c in created):
            raise _Refuse("created_cards: must be a list of card ids")
        matches = []
        for cid in dict.fromkeys(created):
            cview, cstage = _view_and_stage(board, cid, "created_cards")
            if cstage == child and task_id in cview.parents:
                matches.append(cid)
        if len(matches) != 1:
            raise _Refuse(
                f"child: expected exactly one card in created_cards with skill "
                f"dev-team-stage-{child} and {task_id} as a parent, found "
                f"{len(matches)}{' ' + str(matches) if matches else ''}"
            )

        # 5. Round limit.
        if child == "implement":
            rounds = implement_rounds(board, matches[0])
            if rounds > ROUND_LIMIT:
                raise _Refuse(
                    f"review: rounds-exhausted: card {matches[0]} would be implement "
                    f"round {rounds}, the limit is {ROUND_LIMIT}. Do not complete; "
                    f'kanban_block(kind="needs_input") with a reason starting '
                    f"`review: rounds-exhausted`."
                )

    # 6. CI claimed green: the guard re-reads it from the forge.
    if stage == "pr-ci" and md["ci"] == "green":
        sha = md["head_sha"]
        slug = project_slug(board, task_id)
        try:
            state = ci_status(slug, sha)
        except Exception as exc:
            raise _Refuse(
                f"forge: unreachable: cannot verify CI for {sha} ({_describe(exc)})"
            ) from None
        if state != "green":
            raise _Refuse(
                f"ci: forge reports {state!r} for {sha}, not 'green'; "
                f"completion with ci=green refused"
            )

    return None
