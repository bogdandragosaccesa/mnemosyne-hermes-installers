"""dev-team stage rules: the design's stage table as code.

Pure functions, stdlib only, no I/O. The guard (guard.py) calls these and turns
any error list or exception into a refusal, so everything here leans towards
rejecting: an unknown stage, a malformed field or an ambiguous card is an
error, never a pass.

Cross-card checks (head_sha equal to the parent's, the forge re-read of a green
CI, the child card's existence, the round limit) need the board and belong to
the guard, not here.
"""
from __future__ import annotations

import math
import re

STAGES = ("intake", "plan", "implement", "validate", "review", "pr-ci")

SKILL_PREFIX = "dev-team-stage-"

# Git object names: abbreviated (>= 7) up to full SHA-256 (64).
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_URL_RE = re.compile(r"^https?://\S+$")


def stage_from_skills(skills: list[str] | None) -> str | None:
    """Return the card's stage from its `skills`, or None for an unguarded card.

    Raises ValueError when the card carries a dev-team stage skill that names
    no known stage, or skills for two different stages: such a card is
    malformed, and treating it as unguarded would fail open.
    """
    if not skills:
        return None
    found = set()
    for skill in skills:
        if not isinstance(skill, str) or not skill.startswith(SKILL_PREFIX):
            continue
        stage = skill[len(SKILL_PREFIX):]
        if stage not in STAGES:
            raise ValueError(f"unknown stage skill {skill!r}")
        found.add(stage)
    if len(found) > 1:
        raise ValueError(f"card carries several stage skills: {sorted(found)}")
    return found.pop() if found else None


# --- field checks: each returns an error string or None -------------------


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(field, v):
    if not isinstance(v, str) or not v.strip():
        return f"{field}: must be a non-empty string"
    return None


def _sha(field, v):
    if not isinstance(v, str) or not _SHA_RE.match(v):
        return f"{field}: must be a git commit sha (7-64 hex characters)"
    return None


def _list(field, v):
    if not isinstance(v, list):
        return f"{field}: must be a list"
    return None


def _nonempty_list(field, v):
    if not isinstance(v, list) or not v:
        return f"{field}: must be a non-empty list"
    return None


def _nonempty_str_list(field, v):
    if not isinstance(v, list) or not v:
        return f"{field}: must be a non-empty list of strings"
    if not all(isinstance(x, str) and x.strip() for x in v):
        return f"{field}: every entry must be a non-empty string"
    return None


def _budget(field, v):
    if not isinstance(v, dict):
        return f"{field}: must be an object {{amount, unit}}"
    amount = v.get("amount")
    is_number = _is_int(amount) or (
        isinstance(amount, float) and math.isfinite(amount)
    )
    if not is_number or amount < 0:
        return f"{field}: amount must be a finite non-negative number"
    if not isinstance(v.get("unit"), str) or not v["unit"].strip():
        return f"{field}: unit must be a non-empty string"
    return None


def _tests_run(field, v):
    if not isinstance(v, list) or not v:
        return f"{field}: must be a non-empty list of {{command, exit_code}}"
    for i, run in enumerate(v):
        if not isinstance(run, dict):
            return f"{field}[{i}]: must be an object {{command, exit_code}}"
        if not isinstance(run.get("command"), str) or not run["command"].strip():
            return f"{field}[{i}]: command must be a non-empty string"
        code = run.get("exit_code")
        if not _is_int(code):
            return f"{field}[{i}]: exit_code must be an integer"
        if code != 0:
            return f"{field}[{i}]: exit_code is {code}, every test run must exit 0"
    return None


def _one_of(*allowed):
    def check(field, v):
        if not isinstance(v, str) or v not in allowed:
            return f"{field}: must be one of {list(allowed)}"
        return None

    return check


def _url(field, v):
    if not isinstance(v, str) or not _URL_RE.match(v):
        return f"{field}: must be an http(s) URL"
    return None


# stage -> (required field, check) in table order.
_RULES = {
    "intake": (
        ("definition_of_done", _list),
        ("budget", _budget),
        ("repo", _nonempty_str),
    ),
    "plan": (("cards", _nonempty_str_list),),
    "implement": (
        ("branch", _nonempty_str),
        ("head_sha", _sha),
        ("changed_files", _nonempty_str_list),
        ("tests_run", _tests_run),
    ),
    "validate": (
        ("head_sha", _sha),
        ("verdict", _one_of("pass", "fail")),
        ("evidence", _list),
    ),
    "review": (
        ("head_sha", _sha),
        ("verdict", _one_of("approve", "changes")),
    ),
    "pr-ci": (
        ("pr_url", _url),
        ("head_sha", _sha),
        ("ci", _one_of("green", "red")),
    ),
}

# stage -> (field, value) that additionally requires non-empty `findings`.
_NEEDS_FINDINGS = {
    "validate": ("verdict", "fail"),
    "review": ("verdict", "changes"),
    "pr-ci": ("ci", "red"),
}


def validate_metadata(stage: str, md: dict) -> list[str]:
    """Check `md` against the stage table. Returns errors; empty means valid.

    Every error names the offending field. Extra fields are allowed.
    """
    if stage not in _RULES:
        return [f"stage: unknown stage {stage!r}"]
    if not isinstance(md, dict):
        return [f"metadata: must be an object, got {type(md).__name__}"]
    errors = []
    for field, check in _RULES[stage]:
        if field not in md:
            errors.append(f"{field}: required for stage {stage}")
            continue
        err = check(field, md[field])
        if err:
            errors.append(err)
    trigger = _NEEDS_FINDINGS.get(stage)
    if trigger and md.get(trigger[0]) == trigger[1]:
        err = _nonempty_list("findings", md.get("findings"))
        if err:
            errors.append(f"{err} when {trigger[0]} is {trigger[1]!r}")
    return errors


# stage -> (field, success value, child on success). Anything else -> implement.
_ROUTES = {
    "intake": (None, None, "plan"),
    "plan": (None, None, "implement"),
    "implement": (None, None, "validate"),
    "validate": ("verdict", "pass", "review"),
    "review": ("verdict", "approve", "pr-ci"),
    "pr-ci": ("ci", "green", None),
}


def expected_child(stage: str, md: dict) -> str | None:
    """The stage the required child card must carry; None if no child is required.

    Any outcome other than the stage's success value routes to a new `implement`
    card (rework), so a malformed verdict never advances or ends the chain.
    Raises ValueError for an unknown stage.
    """
    if stage not in _ROUTES:
        raise ValueError(f"unknown stage {stage!r}")
    field, success, child = _ROUTES[stage]
    if field is None:
        return child
    value = md.get(field) if isinstance(md, dict) else None
    return child if value == success else "implement"
