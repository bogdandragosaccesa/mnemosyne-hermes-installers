"""Core of the `deciders` CLI: decide, extract, summarize over OpenRouter.

Reads the restricted key from ``OPENROUTER_DECIDERS_KEY`` only. The key is
never written to a log, a fixture, stdout, or the project record. Every
stdout string is passed through ``scrub()`` before it is printed.

Credentials never leave this process except as the Authorization header of
the outbound request; everything printed is redacted first.
"""
from __future__ import annotations

import argparse
import json
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
}

TIMEOUT_S = 60.0


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
    return float(cost)


def _require_input(text: str) -> str:
    """Refuse to send an empty prompt to a paid model."""
    if not text.strip():
        raise DecidersError("deciders: bad-input", "input is empty (pass --input <file|->)")
    return text


def _cmd_decide(args, key: str, transport: Transport) -> dict:
    state = _read_json(args.state)
    questions = _read_json(args.questions)
    model = args.model or DEFAULT_MODELS["decide"]
    body = {"model": model, "state": state, "questions": questions}
    data = _require_dict(_call(transport, DECISIONS_URL, _headers(key), body, TIMEOUT_S))

    answers = data.get("answers")
    resp_model = data.get("model")
    if not isinstance(answers, dict) or not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    return {"answers": answers, "model": resp_model, "cost": _cost(data)}


def _cmd_extract(args, key: str, transport: Transport) -> dict:
    entities_spec = _read_json(args.entities)
    input_text = _require_input(_read_text(args.input) if args.input else "")
    model = args.model or DEFAULT_MODELS["extract"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": input_text}],
        "provider": {"options": {"scaledown": {"entities": entities_spec}}},
    }
    data = _require_dict(_call(transport, CHAT_URL, _headers(key), body, TIMEOUT_S))

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
    return {"entities": entities, "model": resp_model, "cost": _cost(data)}


def _cmd_summarize(args, key: str, transport: Transport) -> dict:
    input_text = _require_input(_read_text(args.input) if args.input else "")
    model = args.model or DEFAULT_MODELS["summarize"]
    body = {"model": model, "messages": [{"role": "user", "content": input_text}]}
    data = _require_dict(_call(transport, CHAT_URL, _headers(key), body, TIMEOUT_S))

    content = _chat_content(data)
    resp_model = data.get("model")
    if not isinstance(resp_model, str):
        raise DecidersError("deciders: bad-response")
    return {
        "summary": content,
        "evidence": False,
        "model": resp_model,
        "cost": _cost(data),
    }


_DISPATCH = {
    "decide": _cmd_decide,
    "extract": _cmd_extract,
    "summarize": _cmd_summarize,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deciders")
    sub = parser.add_subparsers(dest="command", required=True)

    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--state", required=True)
    p_decide.add_argument("--questions", required=True)
    p_decide.add_argument("--model")

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--entities", required=True)
    p_extract.add_argument("--input")
    p_extract.add_argument("--model")

    p_summarize = sub.add_parser("summarize")
    p_summarize.add_argument("--input")
    p_summarize.add_argument("--model")

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
    try:
        args = _build_parser().parse_args(argv)
        key = env.get("OPENROUTER_DECIDERS_KEY")
        if not key:
            raise DecidersError("deciders: no-key")
        handler = _DISPATCH[args.command]
        result = handler(args, key, transport)
    except Exception as exc:  # catch-all: never let an exception escape unredacted
        _emit(out, {"error": scrub(str(exc), key)})
        return EXIT_UNANSWERED
    _emit(out, result)
    return EXIT_OK
