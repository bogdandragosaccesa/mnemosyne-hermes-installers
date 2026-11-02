"""Core of the `deciders` CLI: decide, extract, summarize over OpenRouter.

Reads the restricted key from ``OPENROUTER_DECIDERS_KEY`` only. The key is
never written to a log, a fixture, stdout, or the project record. Every
stdout string is passed through ``scrub()`` before it is printed.

Credentials never leave this process except as the Authorization header of
the outbound request; everything printed is redacted first.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

# One owner for redact(): forge's copy, not a fork. Inserted once.
_FORGE_DIR = str(Path(__file__).resolve().parent.parent / "forge")
if _FORGE_DIR not in sys.path:
    sys.path.insert(0, _FORGE_DIR)
from forge_core import redact  # noqa: E402  (after sys.path setup, by design)


EXIT_OK = 0
EXIT_UNANSWERED = 2

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULT_MODELS = {
    "decide": "liquid/d1.1",
    "extract": "scaledown/extract",
    "summarize": "scaledown/summarize",
    "tier": "liquid/d1.1",
}

# Models that only answer DECISIONS_URL, never chat completions: an agent
# worker routed to one cannot run. The installers read this one line and
# refuse any of these (or a dated variant, e.g. "<id>-20261027", or an
# OpenRouter ":variant" suffix) as a model-tiers.json entry, so keep it a
# single-line tuple of string literals. An entry ending in "*" is a family
# prefix match (e.g. every "perplexity/pplx-decider-*" version), not a
# single model.
DECISION_MODELS = ("liquid/d1.1", "typesafe/jev-1.13", "upstage/solar-decide-flash", "perplexity/pplx-decider-*")

TIMEOUT_S = 60.0

# D2 (review): the only shape a task id is ever allowed to take. Anything
# else -- the key itself, a "Bearer <key>" header value, an over-long
# string, a newline -- is rejected as bad-input before any network call,
# so a typo in a skill or a card never spends money and never lands in
# the ledger.
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _task_id_is_valid(value: str, key: "str | None") -> bool:
    """True iff `value` matches TASK_ID_RE and does not embed `key`.

    The key-containment check is independent of the shape check: a key
    that happens to be 64-or-fewer alphanumeric/`-`/`_` characters would
    otherwise pass the regex outright.
    """
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        return False
    if key and key in value:
        return False
    return True

# Plan-stage tier rule (design, "Plan-stage tier proposal"). Consequence of
# missing the threshold either way: too low sends an uncertain card to a
# cheaper model and costs a rework round; too high escalates a routine card
# and costs money. 0.65 is a starting value, not a calibrated one -- the
# spike's evidence for it is weak (see PROBES.md).
TIER_CONFIDENT = 0.65
TIERS = ("fast", "standard", "deep")


class DecidersError(Exception):
    """Raised whenever deciders could not answer.

    ``label`` is one of the Global Constraints labels, e.g. ``"deciders:
    auth"``; it already carries the ``"deciders: "`` prefix so ``str(exc)``
    is directly usable as (the start of) the printed error.
    """

    def __init__(self, label: str, detail: str = ""):
        self.label = label
        self.detail = detail
        message = label if not detail else f"{label}: {detail}"
        super().__init__(message)


Transport = Callable[[str, dict, dict, float], dict]


def _status_label(code: int) -> str:
    if code == 401:
        return "deciders: auth"
    if code == 402:
        return "deciders: key-cap"
    if code == 403:
        return "deciders: forbidden"
    if code == 404:
        return "deciders: model-unavailable"
    if code == 429 or 500 <= code < 600:
        return "deciders: upstream"
    return f"deciders: http-{code}"


def urllib_transport(url: str, headers: dict, body: dict, timeout: float) -> dict:
    """The real transport: POST JSON, return parsed JSON, or raise DecidersError."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise DecidersError(_status_label(exc.code)) from exc
    except socket.timeout as exc:
        raise DecidersError("deciders: timeout") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            raise DecidersError("deciders: timeout") from exc
        raise DecidersError("deciders: upstream", str(exc.reason)) from exc
    except OSError as exc:
        # Connection reset/refused mid-request or mid-read: classified by
        # exception type only, never by message text.
        raise DecidersError("deciders: upstream", type(exc).__name__) from exc
    except UnicodeDecodeError as exc:
        raise DecidersError("deciders: bad-response", "body is not UTF-8") from exc

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DecidersError("deciders: bad-response", str(exc)) from exc


def ledger_path(home: Path) -> Path:
    """Shared spend ledger, under the team home (not per-profile)."""
    return home / "dev-team" / "deciders-ledger.jsonl"


def append_ledger(
    home: Path,
    env: Mapping[str, str],
    subcommand: str,
    model: str,
    cost: float,
    *,
    task_id: "str | None" = None,
    key: "str | None" = None,
) -> None:
    """Append one spend record as a single atomic write() of one JSON line.

    Opened with ``O_APPEND`` and written in one ``os.write`` call so
    concurrent appenders (multiple profiles, multiple processes) each land
    a whole, valid line -- never an interleaved partial write.

    ``task_id``, when given, is the explicit ``--task-id`` CLI value and
    always wins; by the time it reaches here it has already been
    validated by ``main()`` (an invalid explicit value raises before any
    handler runs, so this function never sees one). ``$HERMES_KANBAN_TASK``
    is read only as a fallback: a dispatched worker's own harness process
    knows its task id, but that variable is not reliably present in the
    environment a *subprocess* (such as this CLI, invoked via `secret exec`
    or a plain shell command) inherits -- proved by probing a real
    dispatched-worker shell, where sibling vars like HERMES_KANBAN_BOARD
    and HERMES_KANBAN_WORKSPACE were set but HERMES_KANBAN_TASK was not.
    Callers that know their task id (the skill instructs passing it
    explicitly) should always pass it.

    D2 (review): the env fallback is validated with the same
    ``_task_id_is_valid`` shape+key check as the explicit flag. Unlike the
    flag, an invalid env value never raises -- it silently becomes
    ``None`` -- because this runs after a call has already been billed
    and must still write a ledger line.
    """
    resolved_task_id = task_id
    if not resolved_task_id:
        env_task_id = env.get("HERMES_KANBAN_TASK") or None
        if env_task_id and _task_id_is_valid(env_task_id, key):
            resolved_task_id = env_task_id
        else:
            resolved_task_id = None
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "board": env.get("HERMES_KANBAN_BOARD") or None,
        "task_id": resolved_task_id,
        "subcommand": subcommand,
        "model": model,
        "cost": cost,
    }
    path = ledger_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def choose_tier(probabilities) -> str:
    """The plan's tier rule: top prob >= TIER_CONFIDENT wins outright;
    otherwise uncertainty resolves to the larger of the two leading tiers.

    Never guesses: an unknown option or probabilities that don't sum to
    about 1 is a `bad-response`, not a tier (Review Focus item 4).
    """
    if not isinstance(probabilities, dict) or set(probabilities.keys()) != set(TIERS):
        raise DecidersError("deciders: bad-response", "unknown tier option(s)")
    values = list(probabilities.values())
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        raise DecidersError("deciders: bad-response", "probability is not a number")
    # json.loads happily parses NaN/Infinity (transport, line ~109), and a
    # NaN total makes abs(total - 1.0) > 0.02 evaluate to False -- silently
    # returning a guessed tier. Reject non-finite and out-of-range values
    # explicitly rather than relying on the sum check to catch them.
    if any(not math.isfinite(v) or v < 0.0 or v > 1.0 for v in values):
        raise DecidersError(
            "deciders: bad-response", "probability is not finite and in [0, 1]"
        )
    total = sum(values)
    if not math.isfinite(total) or abs(total - 1.0) > 0.02:
        raise DecidersError(
            "deciders: bad-response", f"probabilities sum to {total}, not 1"
        )

    top_tier = max(TIERS, key=lambda t: probabilities[t])
    if probabilities[top_tier] >= TIER_CONFIDENT:
        return top_tier

    leading_two = sorted(TIERS, key=lambda t: probabilities[t], reverse=True)[:2]
    return max(leading_two, key=TIERS.index)


def _tier_override(home: Path, tier: str):
    """({provider, model}, None) from model-tiers.json, or (None, reason)."""
    path = home / "dev-team" / "model-tiers.json"
    try:
        text = path.read_text()
    except OSError:
        return None, "no tier table"
    try:
        table = json.loads(text)
    except json.JSONDecodeError:
        return None, "no tier table"
    entry = table.get(tier) if isinstance(table, dict) else None
    if entry is None:
        return None, f"no entry for {tier}"
    # M8: a string entry, or {"provider": "anthropic", "model": ""}, must
    # never be returned as a usable override -- require a dict with both
    # provider and model as non-empty strings.
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("provider"), str) or not entry.get("provider")
        or not isinstance(entry.get("model"), str) or not entry.get("model")
    ):
        return None, f"invalid entry for {tier} (must be {{provider, model}} with non-empty strings)"
    if _is_decision_model(entry):
        # The installers refuse these, but a hand-edited table is read as-is.
        return None, f"decisions-only model for {tier}: {entry.get('provider')}/{entry.get('model')}"
    return entry, None


def _strip_variant_suffix(name: str) -> str:
    """Drop an OpenRouter `:variant` suffix, e.g. `liquid/d1.1:nitro` -> `liquid/d1.1`."""
    return name.split(":", 1)[0]


def _matches_decision_model(name: str, pattern: str) -> bool:
    """`pattern` ending in "*" is a family prefix match; otherwise exact or
    a dated `<id>-...` variant."""
    if pattern.endswith("*"):
        return name.startswith(pattern[:-1])
    return name == pattern or name.startswith(pattern + "-")


def _is_decision_model(entry: dict) -> bool:
    """Same rule as the installers: bare or provider/model, each with any
    `:variant` suffix stripped first, exact or `<id>-*`, or a family prefix."""
    model = _strip_variant_suffix(str(entry.get("model", "")))
    provider = str(entry.get("provider", ""))
    names = (model, f"{provider}/{model}")
    return any(_matches_decision_model(n, m) for n in names for m in DECISION_MODELS)


def scrub(text: str, key: str | None) -> str:
    """forge's redact() plus literal replacement of the key value with `***`."""
    out = redact(text)
    if key:
        out = out.replace(key, "***")
    return out


def _call(transport: Transport, url: str, headers: dict, body: dict, timeout: float) -> dict:
    """Call `transport`, retrying exactly once on an `upstream` (429/5xx) error."""
    try:
        return transport(url, headers, body, timeout)
    except DecidersError as exc:
        if exc.label == "deciders: upstream":
            return transport(url, headers, body, timeout)
        raise


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _read_text(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise DecidersError("deciders: bad-input", str(exc)) from exc


def _read_json(path: str):
    text = _read_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DecidersError("deciders: bad-input", str(exc)) from exc


def _chat_content(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise DecidersError("deciders: bad-response", str(exc)) from exc


def _require_dict(data) -> dict:
    """The top level of every response must be a JSON object."""
    if not isinstance(data, dict):
        raise DecidersError("deciders: bad-response",
                            f"top level is {type(data).__name__}, not object")
    return data


def _cost(data: dict) -> float:
    """Cost as reported by the response: top-level ``cost``, else ``usage.cost``.

    Missing or non-numeric cost is a bad response, never a guessed 0.0 —
    the ledger would silently undercount.
    """
    cost = data.get("cost")
    if cost is None:
        usage = data.get("usage")
        if isinstance(usage, dict):
            cost = usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        raise DecidersError("deciders: bad-response", "cost missing or not a number")
    cost = float(cost)
    # I1: NaN/inf/negative must never reach the ledger -- a NaN total makes
    # the board budget gate's spent >= budget check silently False, and a
    # negative cost quietly refunds spend no call actually returned.
    if not math.isfinite(cost) or cost < 0:
        raise DecidersError("deciders: bad-response", "cost is not finite and non-negative")
    return cost


def _require_input(text: str) -> str:
    """Refuse to send an empty prompt to a paid model."""
    if not text.strip():
        raise DecidersError("deciders: bad-input", "input is empty (pass --input <file|->)")
    return text


def _require_questions_record(questions) -> dict:
    """I4: the live Decisions API rejects an array ("expected record,
    received array", http-400) -- `questions` must be a record keyed by
    question id, each value a dict carrying at least a `type`. Checked
    before any network call, as `bad-input`, not left for the API to
    reject (and bill) after the fact."""
    if not isinstance(questions, dict) or not questions:
        raise DecidersError(
            "deciders: bad-input",
            "--questions must be a non-empty object keyed by question id, "
            "e.g. {\"q1\": {\"type\": \"choice\", ...}}, not an array",
        )
    for qid, spec in questions.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str) or not spec.get("type"):
            raise DecidersError(
                "deciders: bad-input",
                f"--questions[{qid!r}] must be an object with a non-empty string 'type'",
            )
    return questions


def _require_entities_record(entities) -> dict:
    """I4: `entities` must be a flat record of type-name -> description
    strings (`{"<type-name>": "<description>"}`), never a nested or
    list shape -- PROBES.md step 2c shows every other shape either times
    out or silently returns zero entities live. Checked before any
    network call, as `bad-input`."""
    if not isinstance(entities, dict) or not entities:
        raise DecidersError(
            "deciders: bad-input",
            "--entities must be a non-empty flat object of "
            "{\"<type-name>\": \"<description>\"}, not a list or nested shape",
        )
    for name, description in entities.items():
        if not isinstance(name, str) or not isinstance(description, str) or not description:
            raise DecidersError(
                "deciders: bad-input",
                f"--entities[{name!r}] must map to a non-empty description string",
            )
    return entities


def _mark_received(ledger_info: dict, command: str, requested_model: str, raw) -> None:
    """M1: record that the transport returned, with a best-effort model
    and cost, before any further validation that might raise -- so
    `main()` can still write a ledger line (cost 0.0 if it doesn't parse)
    even when validation fails after a call that was already billed."""
    ledger_info["received"] = True
    ledger_info["command"] = command
    ledger_info["model"] = requested_model
    if not isinstance(raw, dict):
        return
    resp_model = raw.get("model")
    if isinstance(resp_model, str) and resp_model:
        ledger_info["model"] = resp_model
    cost = raw.get("cost")
    if cost is None:
        usage = raw.get("usage")
        if isinstance(usage, dict):
            cost = usage.get("cost")
    if (
        not isinstance(cost, bool) and isinstance(cost, (int, float))
        and math.isfinite(cost) and cost >= 0
    ):
        ledger_info["cost"] = float(cost)


def _cmd_decide(args, key: str, transport: Transport, ledger_info: dict) -> dict:
    state = _read_json(args.state)
    questions = _require_questions_record(_read_json(args.questions))
    model = args.model or DEFAULT_MODELS["decide"]
    body = {"model": model, "state": state, "questions": questions}
    raw = _call(transport, DECISIONS_URL, _headers(key), body, TIMEOUT_S)
    _mark_received(ledger_info, "decide", model, raw)
    data = _require_dict(raw)

    answers = data.get("answers")
    resp_model = data.get("model")
    if not isinstance(answers, dict) or not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    cost = _cost(data)
    ledger_info["model"] = resp_model
    ledger_info["cost"] = cost
    return {"answers": answers, "model": resp_model, "cost": cost}


def _cmd_extract(args, key: str, transport: Transport, ledger_info: dict) -> dict:
    entities_spec = _require_entities_record(_read_json(args.entities))
    input_text = _require_input(_read_text(args.input) if args.input else "")
    model = args.model or DEFAULT_MODELS["extract"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": input_text}],
        "provider": {"options": {"scaledown": {"entities": entities_spec}}},
    }
    raw = _call(transport, CHAT_URL, _headers(key), body, TIMEOUT_S)
    _mark_received(ledger_info, "extract", model, raw)
    data = _require_dict(raw)

    content = _chat_content(data)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DecidersError("deciders: bad-response", str(exc)) from exc
    entities = parsed.get("entities") if isinstance(parsed, dict) else None
    if not isinstance(entities, list):
        raise DecidersError("deciders: bad-response")
    resp_model = data.get("model")
    if not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    cost = _cost(data)
    ledger_info["model"] = resp_model
    ledger_info["cost"] = cost
    return {"entities": entities, "model": resp_model, "cost": cost}


def _cmd_summarize(args, key: str, transport: Transport, ledger_info: dict) -> dict:
    input_text = _require_input(_read_text(args.input) if args.input else "")
    model = args.model or DEFAULT_MODELS["summarize"]
    body = {"model": model, "messages": [{"role": "user", "content": input_text}]}
    raw = _call(transport, CHAT_URL, _headers(key), body, TIMEOUT_S)
    _mark_received(ledger_info, "summarize", model, raw)
    data = _require_dict(raw)

    content = _chat_content(data)
    resp_model = data.get("model")
    if not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    cost = _cost(data)
    ledger_info["model"] = resp_model
    ledger_info["cost"] = cost
    return {
        "summary": content,
        "evidence": False,
        "model": resp_model,
        "cost": cost,
    }


def _cmd_tier(args, key: str, transport: Transport, ledger_info: dict) -> dict:
    spec_text = _read_text(args.spec)[:20000]
    model = args.model or DEFAULT_MODELS["tier"]
    state = {"title": args.title, "spec": spec_text}
    # The Decisions API takes a record keyed by question id; an array gets
    # http-400 ("expected record, received array").
    questions = {
        "tier": {
            "type": "choice",
            "instructions": (
                "Which model tier does this software task need to be done "
                "right the first time?"
            ),
            "criteria": {
                "fast": "mechanical moves, renames, small doc edits",
                "standard": "routine implementation from a precise spec",
                "deep": (
                    "security-sensitive code, subtle failure handling, or "
                    "design judgment the spec leaves open"
                ),
            },
        }
    }
    body = {"model": model, "state": state, "questions": questions}
    raw = _call(transport, DECISIONS_URL, _headers(key), body, TIMEOUT_S)
    # M1: mark received (and capture model/cost) before choose_tier, which
    # can still raise on an unknown option or a bad probability sum -- a
    # billed call that fails only there must still write a ledger line.
    _mark_received(ledger_info, "tier", model, raw)
    data = _require_dict(raw)

    answers = data.get("answers")
    resp_model = data.get("model")
    if not isinstance(answers, dict) or not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    ledger_info["model"] = resp_model
    answer = answers.get("tier")
    if not isinstance(answer, dict):
        raise DecidersError("deciders: bad-response")
    probabilities = answer.get("probabilities")
    cost = _cost(data)
    ledger_info["cost"] = cost
    tier = choose_tier(probabilities)
    return {
        "tier": tier,
        "probabilities": probabilities,
        "model": resp_model,
        "cost": cost,
    }


_DISPATCH = {
    "decide": _cmd_decide,
    "extract": _cmd_extract,
    "summarize": _cmd_summarize,
    "tier": _cmd_tier,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deciders")
    sub = parser.add_subparsers(dest="command", required=True)

    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--state", required=True)
    p_decide.add_argument("--questions", required=True)
    p_decide.add_argument("--model")
    p_decide.add_argument("--task-id")

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--entities", required=True)
    p_extract.add_argument("--input")
    p_extract.add_argument("--model")
    p_extract.add_argument("--task-id")

    p_summarize = sub.add_parser("summarize")
    p_summarize.add_argument("--input")
    p_summarize.add_argument("--model")
    p_summarize.add_argument("--task-id")

    p_tier = sub.add_parser("tier")
    p_tier.add_argument("--title", required=True)
    p_tier.add_argument("--spec", required=True)
    p_tier.add_argument("--model")
    p_tier.add_argument("--task-id")

    return parser


def _emit(out, obj: dict) -> None:
    out.write(json.dumps(obj))
    out.write("\n")


def main(
    argv: list,
    *,
    home: Path,
    env: Mapping[str, str],
    transport: Transport = urllib_transport,
    out=sys.stdout,
) -> int:
    key = None
    ledger_info: dict = {}
    task_id: "str | None" = None
    try:
        try:
            args = _build_parser().parse_args(argv)
        except SystemExit as exc:
            # argparse also raises SystemExit(0) for --help/-h, which is
            # not an error and must exit cleanly with no stdout wrapper --
            # only its genuine error path (a truthy/nonzero code) becomes
            # the labelled bad-input contract below.
            if not exc.code:
                raise
            # M2: argparse's own error path calls sys.exit(2), which
            # `except Exception` below does not catch -- nothing would be
            # printed to stdout at all. Convert to the same labelled
            # contract as every other input problem.
            raise DecidersError(
                "deciders: bad-input", f"argument error (exit code {exc.code})"
            ) from exc
        key = (env.get("OPENROUTER_DECIDERS_KEY") or "").strip() or None
        if not key:
            raise DecidersError("deciders: no-key")
        # The explicit --task-id flag always wins over $HERMES_KANBAN_TASK:
        # see append_ledger's docstring for why the env var alone is not
        # trustworthy in a dispatched worker's subprocess environment.
        # D2 (review): validated here, before the handler runs, so an
        # invalid value -- the key itself, "Bearer <key>", an over-long
        # string, a newline -- never spends money and never reaches the
        # ledger. The error never echoes the rejected value.
        raw_task_id = (getattr(args, "task_id", None) or "").strip()
        if raw_task_id:
            if not _task_id_is_valid(raw_task_id, key):
                raise DecidersError("deciders: bad-input", "task-id")
            task_id = raw_task_id
        else:
            task_id = None
        handler = _DISPATCH[args.command]
        result = handler(args, key, transport, ledger_info)
        if args.command == "tier":
            override, override_reason = _tier_override(home, result["tier"])
            result = dict(result, override=override, override_reason=override_reason)
        ledger_info["success_write_attempted"] = True
        append_ledger(
            home, env, args.command, result["model"], result["cost"],
            task_id=task_id, key=key,
        )
    except Exception as exc:  # catch-all: never let an exception escape unredacted
        # M1: the transport may have returned (and been billed) even though
        # a later validation step raised -- write that spend too, cost 0.0
        # if it never parsed, rather than silently dropping it.
        # Skip the fallback when the success-path write is what failed: it
        # would fail again, inside this handler, and escape (review r2 D1).
        ledger_error = None
        if ledger_info.get("received") and not ledger_info.get("success_write_attempted"):
            try:
                append_ledger(
                    home, env, ledger_info.get("command", "?"),
                    ledger_info.get("model") or "unknown",
                    ledger_info.get("cost", 0.0),
                    task_id=task_id, key=key,
                )
            except OSError as le:
                ledger_error = le
        # M2: a bare ValueError (e.g. http.client's "Invalid header value"
        # for a malformed key) must not reach stdout unlabelled.
        if isinstance(exc, ValueError) and not isinstance(exc, DecidersError):
            message = f"deciders: bad-input: {exc}"
        else:
            message = str(exc)
        if ledger_error is not None:
            message = f"{message} (ledger write also failed: {ledger_error})"
        _emit(out, {"error": scrub(message, key)})
        return EXIT_UNANSWERED
    _emit(out, result)
    return EXIT_OK
